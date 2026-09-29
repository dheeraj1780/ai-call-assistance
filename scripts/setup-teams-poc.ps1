<#
.SYNOPSIS
    Idempotent Azure Bot + Microsoft Teams channel (calling) setup for the CallCopilot Teams POC.

.DESCRIPTION
    Uses the EXISTING Entra app registration ("CallCopilot Teams"). It never creates an app
    registration or a tenant, never reads, writes or prints the client secret, and never changes the
    VM or the gateway. Re-running it is safe: every resource is checked before it is created, and
    nothing is overwritten unless it differs from the requested configuration.

    Derived from the repository (teams-media-gateway/):
      * Calling webhook = CallbackBaseUrl + "/api/calling" (src/Teams/BotService.cs, GatewayApp.cs).
        CallbackBaseUrl defaults to https://<ServiceDnsName>, so the webhook uses HTTPS on 443.
      * The gateway has NO Bot Framework messaging route (/api/messages), so no messaging endpoint
        is set unless you pass -MessagingEndpoint explicitly.
      * Media port = 8445 (InstancePublicPort), open health probe = GET /healthz.

    Steps: 1 az login/subscription  2 resource group  3 Microsoft.BotService provider
           4 Azure Bot (SingleTenant, existing App ID)  5 Teams channel + calling webhook
           6 gateway reachability (read-only)  7 summary checklist + manual items

.EXAMPLE
    .\scripts\setup-teams-poc.ps1 -AppId <full-client-id-guid> -DryRun
    .\scripts\setup-teams-poc.ps1 -AppId <full-client-id-guid>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppId,
    [string]$AppTenantId = "6b91daa7-ee14-4521-8abe-fddd0a581292",
    [string]$SubscriptionId = "",
    [string]$ResourceGroup = "callcopilot-poc",
    [ValidateLength(4, 42)][string]$BotName = "callcopilot-teams-bot",
    [string]$BotDisplayName = "CallCopilot Teams",
    [ValidateSet("F0", "S1")][string]$Sku = "F0",
    [string]$GatewayDnsName = "callcopilot-teams-gw.eastus.cloudapp.azure.com",
    [string]$CallbackBaseUrl = "",
    [string]$MessagingEndpoint = "",
    [string]$VmName = "callcopilot-teams-gateway",
    [string]$NsgName = "callcopilot-teams-gateway-nsg",
    [int]$MediaPort = 8445,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
foreach ($pair in @(@("AppId", $AppId), @("AppTenantId", $AppTenantId))) {
    if ($pair[1] -notmatch $GuidPattern) {
        throw "$($pair[0]) '$($pair[1])' is not a complete GUID (8-4-4-4-12). Copy it from Entra ID > App registrations > CallCopilot Teams > Overview."
    }
}
if (-not $CallbackBaseUrl) { $CallbackBaseUrl = "https://$GatewayDnsName" }
$CallbackBaseUrl = $CallbackBaseUrl.TrimEnd("/")
if ($CallbackBaseUrl -notmatch '^https://') { throw "CallbackBaseUrl must be https:// (Teams requires HTTPS for the calling webhook)." }
$CallingWebhook = "$CallbackBaseUrl/api/calling"
if ($MessagingEndpoint -and $MessagingEndpoint -notmatch '^https://') { throw "MessagingEndpoint must start with https://" }
$HttpsPort = ([Uri]$CallbackBaseUrl).Port

$Results = [ordered]@{}
$Manual = New-Object System.Collections.Generic.List[string]
function Set-Result([string]$Item, [string]$Status, [string]$Detail) {
    $Results[$Item] = [pscustomobject]@{ Item = $Item; Status = $Status; Detail = $Detail }
    $color = switch ($Status) { "OK" { "Green" } "PLANNED" { "Cyan" } "FAIL" { "Red" } default { "Yellow" } }
    Write-Host ("  [{0}] {1} - {2}" -f $Status, $Item, $Detail) -ForegroundColor $color
}
function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor White }

# ---- Azure CLI resolution: the uv-tool install (az.bat on this PC resolves a wrong Python) --------
$AzExe = $null; $AzPrefix = @()
try {
    $uvDir = (& uv tool dir 2>$null)
    if ($uvDir) {
        $candidate = Join-Path $uvDir "azure-cli\Scripts\python.exe"
        if (Test-Path $candidate) { $AzExe = $candidate; $AzPrefix = @("-m", "azure.cli") }
    }
} catch { }
if (-not $AzExe) {
    $cmd = Get-Command az -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Azure CLI not found (neither the uv tool 'azure-cli' nor 'az' on PATH)." }
    $AzExe = $cmd.Source
}
$env:AZURE_CORE_COLLECT_TELEMETRY = "false"
$env:AZURE_CORE_ONLY_SHOW_ERRORS = "true"

function Invoke-Az {
    # Runs az, returns @{ Ok; Json; Text; Err }. Output is parsed, never echoed wholesale.
    # Deliberately a simple function ($args) so az's -g/-n/-o are not bound as PowerShell parameters.
    $AzArgs = [string[]]$args
    $errFile = [IO.Path]::GetTempFileName()
    try {
        $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $out = & $AzExe @AzPrefix @AzArgs --only-show-errors 2>$errFile
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        $text = ($out | Out-String).Trim()
        $err = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
        if ($err) { $err = $err.Trim() }
        $json = $null
        if ($code -eq 0 -and $text) { try { $json = $text | ConvertFrom-Json } catch { } }
        return [pscustomobject]@{ Ok = ($code -eq 0); Json = $json; Text = $text; Err = $err }
    } finally { Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
}
function Test-NotFound($r) { return (-not $r.Ok) -and ($r.Err -match 'ResourceNotFound|NotFound|was not found|could not be found|does not exist') }
function Stop-OnAuth($r) {
    if ($r.Err -match 'AADSTS|az login|expired|Please run') {
        Write-Host $r.Err -ForegroundColor Red
        throw "Azure CLI authentication failed. Re-login interactively (MFA / security defaults) and rerun."
    }
}

Write-Host "CallCopilot Teams POC - Azure Bot setup $(if ($DryRun) { '(DRY RUN: no changes)' })" -ForegroundColor White
Write-Host "  App ID (existing registration): $AppId"
Write-Host "  App tenant:                     $AppTenantId"
Write-Host "  Calling webhook (from repo):    $CallingWebhook"
Write-Host "  Messaging endpoint:             $(if ($MessagingEndpoint) { $MessagingEndpoint } else { '(none - gateway has no /api/messages route)' })"

# ---- 1. Login + subscription -----------------------------------------------------------------------
Step "1. Azure CLI login and subscription"
if ($SubscriptionId) {
    $r = Invoke-Az account set --subscription $SubscriptionId
    if (-not $r.Ok) { Stop-OnAuth $r; throw "Cannot select subscription ${SubscriptionId}: $($r.Err)" }
}
$acct = Invoke-Az account show -o json
if (-not $acct.Ok) { Stop-OnAuth $acct; throw "Not logged in: run 'az login' first. $($acct.Err)" }
$SubTenant = $acct.Json.tenantId
Set-Result "Azure CLI login" "OK" "$($acct.Json.user.name) / subscription '$($acct.Json.name)' ($($acct.Json.id)) / tenant $SubTenant"
# A real ARM call: 'account show' reads the local cache only and succeeds even when tokens are blocked.
$rg = Invoke-Az group show -n $ResourceGroup -o json
if (-not $rg.Ok) { Stop-OnAuth $rg; throw "Resource group '$ResourceGroup' not found/readable: $($rg.Err)" }
if ($SubTenant -ne $AppTenantId) {
    Set-Result "Tenant alignment" "WARN" "subscription tenant $SubTenant != app tenant $AppTenantId (cross-tenant SingleTenant bot; Azure decides - see bot step)"
    $Manual.Add("Tenant mismatch: the Azure Bot resource lives in tenant $SubTenant while the app registration is in $AppTenantId. If bot creation is rejected or Teams calls fail auth, use a subscription in tenant $AppTenantId (or register the app there).")
} else { Set-Result "Tenant alignment" "OK" "subscription and app registration share tenant $AppTenantId" }

# ---- 2. Resource group --------------------------------------------------------------------------
Step "2. Resource group"
Set-Result "Resource group" "OK" "$ResourceGroup ($($rg.Json.location)) exists - not modified"

# ---- 3. Resource provider -------------------------------------------------------------------------
Step "3. Microsoft.BotService resource provider"
$prov = Invoke-Az provider show -n Microsoft.BotService --query registrationState -o tsv
if (-not $prov.Ok) { Stop-OnAuth $prov; throw "Cannot read provider state: $($prov.Err)" }
if ($prov.Text -eq "Registered") { Set-Result "BotService provider" "OK" "Registered" }
elseif ($DryRun) { Set-Result "BotService provider" "PLANNED" "would register (currently $($prov.Text))" }
else {
    $r = Invoke-Az provider register -n Microsoft.BotService --wait
    if (-not $r.Ok) { throw "Provider registration failed: $($r.Err)" }
    $prov = Invoke-Az provider show -n Microsoft.BotService --query registrationState -o tsv
    if ($prov.Text -ne "Registered") { throw "Provider state after register: $($prov.Text)" }
    Set-Result "BotService provider" "OK" "Registered (confirmed by ARM)"
}

# ---- 4. Azure Bot ----------------------------------------------------------------------------------
Step "4. Azure Bot '$BotName' with the existing App ID"
$BotReady = $false
$bot = Invoke-Az bot show -g $ResourceGroup -n $BotName -o json
if ($bot.Ok) {
    $p = $bot.Json.properties
    if ($p.msaAppId -ne $AppId) {
        throw "Bot '$BotName' exists with a DIFFERENT App ID ($($p.msaAppId)). Refusing to change it; pass another -BotName or fix it manually."
    }
    Set-Result "Azure Bot" "OK" "exists (msaAppType=$($p.msaAppType), msaAppTenantId=$($p.msaAppTenantId))"
    Set-Result "Existing App ID attached" "OK" "msaAppId = $($p.msaAppId) (read back from ARM)"
    $BotReady = $true
    $current = [string]$p.endpoint
    if ($MessagingEndpoint -and $current -ne $MessagingEndpoint) {
        if ($DryRun) { Set-Result "Messaging endpoint" "PLANNED" "would change '$current' -> '$MessagingEndpoint'" }
        else {
            $r = Invoke-Az bot update -g $ResourceGroup -n $BotName --endpoint $MessagingEndpoint -o json
            if (-not $r.Ok) { throw "bot update failed: $($r.Err)" }
            Set-Result "Messaging endpoint" "OK" "$($r.Json.properties.endpoint)"
        }
    } else { Set-Result "Messaging endpoint" $(if ($current) { "OK" } else { "N/A" }) $(if ($current) { $current } else { "not set (calling-only bot; the gateway has no messaging route)" }) }
} elseif (Test-NotFound $bot) {
    $args4 = @("bot", "create", "-g", $ResourceGroup, "-n", $BotName, "--app-type", "SingleTenant",
        "--appid", $AppId, "--tenant-id", $AppTenantId, "--sku", $Sku, "--location", "global",
        "--display-name", $BotDisplayName, "-o", "json")
    if ($MessagingEndpoint) { $args4 += @("--endpoint", $MessagingEndpoint) }
    if ($DryRun) {
        Set-Result "Azure Bot" "PLANNED" "would run: az $($args4 -join ' ')"
    } else {
        $r = Invoke-Az @args4
        if (-not $r.Ok) {
            Set-Result "Azure Bot" "FAIL" $r.Err
            $Manual.Add("Azure Bot creation was rejected (see error above). Create it in the portal: Create a resource > Azure Bot > 'Use existing app registration', type Single Tenant, App ID $AppId, tenant $AppTenantId.")
        } else {
            $bot = Invoke-Az bot show -g $ResourceGroup -n $BotName -o json
            if ($bot.Ok -and $bot.Json.properties.msaAppId -eq $AppId) {
                Set-Result "Azure Bot" "OK" "created (confirmed by 'az bot show')"
                Set-Result "Existing App ID attached" "OK" "msaAppId = $AppId (read back from ARM)"
                $BotReady = $true
            } else { Set-Result "Azure Bot" "FAIL" "create returned OK but 'bot show' could not confirm it" }
        }
    }
} else { Stop-OnAuth $bot; throw "Cannot read bot: $($bot.Err)" }

# ---- 5. Teams channel with calling -------------------------------------------------------------------
Step "5. Microsoft Teams channel (calling -> $CallingWebhook)"
function Get-TeamsProps($json) {
    $props = $json.properties
    if ($props.PSObject.Properties.Name -contains "properties") { $props = $props.properties }
    return $props
}
if (-not $BotReady) {
    Set-Result "Teams channel" $(if ($DryRun) { "PLANNED" } else { "SKIPPED" }) "needs the bot first"
    Set-Result "Calling enabled" $(if ($DryRun) { "PLANNED" } else { "SKIPPED" }) "would set enableCalling=true, webhook $CallingWebhook"
} else {
    $ch = Invoke-Az bot msteams show -g $ResourceGroup -n $BotName -o json
    $needsApply = $true
    if ($ch.Ok) {
        $tp = Get-TeamsProps $ch.Json
        $needsApply = -not ($tp.isEnabled -and $tp.enableCalling -and $tp.callingWebhook -eq $CallingWebhook)
    } elseif (-not (Test-NotFound $ch)) { Stop-OnAuth $ch; throw "Cannot read Teams channel: $($ch.Err)" }
    if ($needsApply -and $DryRun) {
        Set-Result "Teams channel" "PLANNED" "would $(if ($ch.Ok) { 're-apply' } else { 'create' }) via ARM PUT (isEnabled, enableCalling=true, callingWebhook=$CallingWebhook, CommercialDeployment)"
    } elseif ($needsApply) {
        # Direct ARM PUT instead of 'az bot msteams create' (preview), which (a) sends the swagger default
        # deploymentEnvironment=FallbackDeploymentEnvironment that the service rejects
        # (Azure/azure-rest-api-specs#28636) and (b) silently drops the webhook (passes calling_web_hook,
        # the SDK field is calling_webhook). CommercialDeployment = standard commercial Microsoft 365.
        $sub = $acct.Json.id
        $channelUrl = "https://management.azure.com/subscriptions/$sub/resourceGroups/$ResourceGroup/providers/Microsoft.BotService/botServices/$BotName/channels/MsTeamsChannel?api-version=2022-09-15"
        $bodyFile = [IO.Path]::GetTempFileName()
        try {
            @{ location = "global"; properties = @{ channelName = "MsTeamsChannel"; properties = @{
                        isEnabled = $true; enableCalling = $true; callingWebhook = $CallingWebhook
                        deploymentEnvironment = "CommercialDeployment" } } } |
                ConvertTo-Json -Depth 5 | Set-Content -Path $bodyFile -Encoding ascii
            $r = Invoke-Az rest --method put --url $channelUrl --body "@$bodyFile" -o json
        } finally { Remove-Item $bodyFile -Force -ErrorAction SilentlyContinue }
        if (-not $r.Ok) { Set-Result "Teams channel" "FAIL" $r.Err }
        $ch = Invoke-Az bot msteams show -g $ResourceGroup -n $BotName -o json
    }
    if ($ch.Ok) {
        $tp = Get-TeamsProps $ch.Json
        Set-Result "Teams channel" $(if ($tp.isEnabled) { "OK" } else { "FAIL" }) "isEnabled=$($tp.isEnabled) (read back from ARM)"
        $callOk = $tp.enableCalling -and $tp.callingWebhook -eq $CallingWebhook
        Set-Result "Calling enabled" $(if ($callOk) { "OK" } else { "FAIL" }) "enableCalling=$($tp.enableCalling), webhook=$($tp.callingWebhook)"
        Set-Result "Calling endpoint configured" $(if ($callOk) { "OK" } else { "FAIL" }) $CallingWebhook
    }
}

# ---- 6. Gateway reachability (read-only: the VM and gateway are NOT changed) --------------------------
Step "6. Gateway reachability (read-only)"
$vm = Invoke-Az vm get-instance-view -g $ResourceGroup -n $VmName --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv
if ($vm.Ok) {
    $state = $vm.Text -replace 'PowerState/', ''
    Set-Result "Gateway VM" $(if ($state -eq "running") { "OK" } else { "WARN" }) "$VmName is $state"
    if ($state -ne "running") { $Manual.Add("Start the gateway VM when ready to test: az vm start -g $ResourceGroup -n $VmName (billing resumes).") }
} else { Set-Result "Gateway VM" "WARN" "cannot read $VmName : $($vm.Err)" }

$nsg = Invoke-Az network nsg rule list -g $ResourceGroup --nsg-name $NsgName -o json
if ($nsg.Ok) {
    foreach ($port in @($HttpsPort, $MediaPort)) {
        $open = @($nsg.Json | Where-Object {
                $_.direction -eq "Inbound" -and $_.access -eq "Allow" -and
                (@($_.destinationPortRange) + @($_.destinationPortRanges) | Where-Object { $_ -eq "$port" -or $_ -eq "*" })
            })
        Set-Result "NSG inbound TCP $port" $(if ($open.Count) { "OK" } else { "WARN" }) $(if ($open.Count) { "allowed by $($open[0].name)" } else { "no allow rule" })
        if (-not $open.Count) { $Manual.Add("Open NSG '$NsgName' inbound TCP $port from the Internet (Graph signalling on $HttpsPort / Teams media on $MediaPort). Not done by this script: it changes the VM's exposure.") }
    }
} else { Set-Result "NSG rules" "WARN" "cannot read $NsgName : $($nsg.Err)" }

$healthUrl = "$CallbackBaseUrl/healthz"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 10 -Method Get
    Set-Result "Gateway reachable (HTTPS)" $(if ($resp.StatusCode -eq 200) { "OK" } else { "WARN" }) "GET $healthUrl -> $($resp.StatusCode)"
} catch {
    Set-Result "Gateway reachable (HTTPS)" "FAIL" "GET $healthUrl -> $($_.Exception.Message)"
    $Manual.Add("Gateway HTTPS: install a publicly trusted certificate for $GatewayDnsName in LocalMachine\My on the VM, set Gateway__CertificateThumbprint/ServiceDnsName/InstancePublicIPAddress/AppId/HomeTenantId, then rerun teams-media-gateway/deploy-azure-vm.ps1 -HealthCheckOnly.")
}

# ---- 7. Summary --------------------------------------------------------------------------------------
$Manual.Add("Client secret: set Gateway__AppSecret on the VM yourself (the same secret that already exists for app $AppId). This script never reads or prints it.")
$Manual.Add("Graph application permissions Calls.JoinGroupCall.All / Calls.AccessMedia.All + admin consent: already granted per the owner. Verify in Entra ID > App registrations > API permissions (not changed here).")
$Manual.Add("Teams policy: the tenant that hosts the Teams meetings must have Teams licenses; allow the bot in that tenant (Teams admin center / application access policy) before a real call.")
$Manual.Add("Backend: set TEAMS_MEDIA_GATEWAY_URL=$CallbackBaseUrl and TEAMS_MEDIA_GATEWAY_SECRET (= Gateway__BackendSharedSecret) in backend/.env when testing.")

Write-Host "`n== Checklist $(if ($DryRun) { '(DRY RUN)' })" -ForegroundColor White
$Results.Values | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host
Write-Host "Manual items remaining:" -ForegroundColor Yellow
$i = 1; foreach ($m in $Manual) { Write-Host ("  {0}. {1}" -f $i, $m); $i++ }

$failed = @($Results.Values | Where-Object { $_.Status -eq "FAIL" })
if ($failed.Count) { exit 1 } else { exit 0 }
