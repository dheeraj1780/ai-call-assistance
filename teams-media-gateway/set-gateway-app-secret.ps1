<#
.SYNOPSIS
  E2: sets ONLY Gateway__AppSecret on the Teams media gateway VM, restarts the service and verifies
  media-platform readiness. Run it yourself in your own PowerShell window (never through a chat tool).

.DESCRIPTION
  * The secret is read with Read-Host -AsSecureString (hidden, not in shell history), converted to
    plaintext only in memory, and sent to Azure Resource Manager over HTTPS as a *protected parameter*
    of a managed Run Command (PUT .../virtualMachines/<vm>/runCommands/<name>). Protected parameters
    are encrypted for the VM and are never returned by Azure. It is NOT passed to az.exe or any other
    process command line on this PC.
  * On the VM the Run Command replaces only the "Gateway__AppSecret=" line of the service's registry
    Environment (HKLM\SYSTEM\CurrentControlSet\Services\CallCopilotTeamsMediaGateway), verifies every
    other line and the key's ACL are unchanged, restarts the service and runs read-only health checks
    (signed /health with the existing BackendSharedSecret, which never leaves the VM).
  * The Run Command resource is deleted afterwards (also on failure) and the deletion is verified.
  * Nothing secret is printed, logged or written to disk by this script.

  Nothing else is changed: no Azure networking, Azure Bot, Teams channel, app registration, other
  gateway setting, firewall rule or certificate.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\teams-media-gateway\set-gateway-app-secret.ps1
#>
[CmdletBinding()]
param(
    [string]$SubscriptionId = "499e7488-6781-488d-acc2-d14dd260e9a7",
    [string]$ResourceGroup = "callcopilot-poc",
    [string]$VmName = "callcopilot-teams-gateway",
    [string]$GatewayDnsName = "callcopilot-teams-gw.eastus.cloudapp.azure.com",
    [string]$ExpectedThumbprint = "A96DE7E958DB601C3F715829DA4F9178C190135C",
    # Proves the protected-parameter path with a random dummy value; changes nothing on the VM.
    [switch]$TestDelivery
)
Set-StrictMode -Version 1.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ApiVersion = "2025-04-01"
$RunCommandName = "e2-set-gateway-app-secret"

# ---------------------------------------------------------------------------------------------
# Script executed on the VM (as SYSTEM). It receives the secret only as the protected parameter.
# ---------------------------------------------------------------------------------------------
$VmScript = @'
param([string]$AppSecret)
$ErrorActionPreference = "Stop"
$svcName = "CallCopilotTeamsMediaGateway"
$key = "HKLM:\SYSTEM\CurrentControlSet\Services\$svcName"
$dns = "__DNS__"
function Redact([string]$m) { if ($AppSecret -and $m) { return $m.Replace($AppSecret, "<redacted>") } return $m }
function Say([string]$m) { Write-Output (Redact $m) }
function Hash([string[]]$lines) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))) | ForEach-Object { $_.ToString("x2") }) } finally { $sha.Dispose() }
}
try {
    if ([string]::IsNullOrEmpty($AppSecret)) { Say "E2 ABORT: AppSecret parameter was not delivered to the VM. Nothing changed."; exit 3 }
    $before = @((Get-ItemProperty $key -Name Environment -ErrorAction Stop).Environment)
    if ($before.Count -lt 5) { Say "E2 ABORT: service Environment not found/incomplete ($($before.Count) lines). Nothing changed."; exit 4 }
    $othersBefore = @($before | Where-Object { -not $_.StartsWith("Gateway__AppSecret=") })
    $hadLine = [bool]($before | Where-Object { $_.StartsWith("Gateway__AppSecret=") })
    $wasSet = [bool]($before | Where-Object { $_.StartsWith("Gateway__AppSecret=") -and $_.Length -gt "Gateway__AppSecret=".Length })
    $aclBefore = (Get-Acl $key).Sddl
    $new = @(foreach ($l in $before) { if ($l.StartsWith("Gateway__AppSecret=")) { "Gateway__AppSecret=$AppSecret" } else { $l } })
    if (-not $hadLine) { $new += "Gateway__AppSecret=$AppSecret" }
    Say "BEFORE: lines=$($before.Count) AppSecret line present=$hadLine set=$wasSet"
    Set-ItemProperty -Path $key -Name Environment -Type MultiString -Value ([string[]]$new)
    $new = $null
    $after = @((Get-ItemProperty $key -Name Environment).Environment)
    $othersAfter = @($after | Where-Object { -not $_.StartsWith("Gateway__AppSecret=") })
    $secretOk = [bool]($after | Where-Object { $_ -ceq "Gateway__AppSecret=$AppSecret" })
    $othersSame = (Hash $othersBefore) -eq (Hash $othersAfter)
    $aclSame = (Get-Acl $key).Sddl -eq $aclBefore
    Say "AFTER: lines=$($after.Count) AppSecret stored=$secretOk other settings unchanged=$othersSame (sha256 compare) key ACL unchanged=$aclSame"
    if (-not ($secretOk -and $othersSame -and $aclSame)) {
        Set-ItemProperty -Path $key -Name Environment -Type MultiString -Value ([string[]]$before)
        Say "E2 ABORT: verification failed; previous Environment restored. Service NOT restarted."; exit 5
    }
    $before = $null; $after = $null
    $bss = ((Get-ItemProperty $key -Name Environment).Environment | Where-Object { $_.StartsWith("Gateway__BackendSharedSecret=") }) -replace "^Gateway__BackendSharedSecret=", ""

    Restart-Service $svcName -Force
    (Get-Service $svcName).WaitForStatus("Running", [TimeSpan]::FromSeconds(90))
    $restartedAt = Get-Date
    $svc = Get-CimInstance Win32_Service -Filter "Name='$svcName'"
    Say "SERVICE: $($svc.State) account=$($svc.StartName) start=$($svc.StartMode) delayed=$($svc.DelayedAutoStart) pid=$($svc.ProcessId)"

    $live = ""; $code = ""; $body = ""
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        $live = & curl.exe -s -o NUL -w "%{http_code}" --max-time 5 http://127.0.0.1:9441/healthz 2>$null
        if ($live -ne "200") { continue }
        $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
        $h = New-Object System.Security.Cryptography.HMACSHA256 (, [Text.Encoding]::UTF8.GetBytes($bss))
        try { $sig = -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("$ts.")) | ForEach-Object { $_.ToString("x2") }) } finally { $h.Dispose() }
        $tmp = [IO.Path]::GetTempFileName()
        try {
            $code = & curl.exe -s -o $tmp -w "%{http_code}" --max-time 20 -H "X-CC-Timestamp: $ts" -H "X-CC-Signature: v1=$sig" http://127.0.0.1:9441/health 2>$null
            $body = Get-Content $tmp -Raw -ErrorAction SilentlyContinue
        } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        if ($code -eq "200" -or ($code -eq "503" -and $i -ge 20)) { break }
    }
    $bss = $null
    Say "LOCAL /healthz: HTTP $live"
    Say "SIGNED /health: HTTP $code body=$body"
    $mp = $false; try { $mp = [bool](($body | ConvertFrom-Json).media_platform) } catch { }
    Say "E2 MEDIA_PLATFORM=$mp"
    Say "E2 SIGNED_HEALTH=$code"
    Say "APPSECRET REPORTED MISSING: $([bool]($body -match 'AppSecret'))"

    $svc = Get-CimInstance Win32_Service -Filter "Name='$svcName'"
    $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $svc.ProcessId -ErrorAction SilentlyContinue | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" })
    Say "LISTENING (service pid $($svc.ProcessId)): $($ports -join ', ')"
    Say "TCP 8445 listener: $([bool](Get-NetTCPConnection -State Listen -LocalPort 8445 -ErrorAction SilentlyContinue))"
    Say "TCP 80 listener: $([bool](Get-NetTCPConnection -State Listen -LocalPort 80 -ErrorAction SilentlyContinue))"
    try {
        $tcp = New-Object Net.Sockets.TcpClient("127.0.0.1", 443)
        $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true }))
        $ssl.AuthenticateAsClient($dns)
        $rc = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate
        Say "HTTPS 443 presents: $($rc.Thumbprint) $($rc.Subject) $($ssl.SslProtocol)"
        $ssl.Dispose(); $tcp.Close()
    } catch { Say "HTTPS 443 check failed: $($_.Exception.Message)" }
    $fw = @(Get-NetFirewallRule -Group "CallCopilot Teams Media Gateway" | ForEach-Object { $p = $_ | Get-NetFirewallPortFilter; "$($p.LocalPort)" })
    Say "FIREWALL gateway rules (ports): $($fw -join ',') ; TEMP port-80 rule present: $([bool](Get-NetFirewallRule -Name 'CallCopilot-TEMP-ACME-HTTP01-TCP80' -ErrorAction SilentlyContinue))"
    Say "EVENTS since restart (warnings/errors/media):"
    Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = $restartedAt.AddSeconds(-5) } -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -match "TeamsMediaGateway|\.NET Runtime|Application Error" -and ($_.LevelDisplayName -ne "Information" -or $_.Message -match "teams_media") } |
        Select-Object -First 6 | ForEach-Object {
            $m = ($_.Message -replace "\s+", " "); $i2 = $m.IndexOf("teams_media"); if ($i2 -lt 0) { $i2 = 0 }
            Say "  [$($_.LevelDisplayName)] $($_.ProviderName): $($m.Substring($i2, [Math]::Min(300, $m.Length - $i2)))" }
__SCAN__
    Say "PLAINTEXT SCAN: files=$($fileHits.Count)$(if ($fileHits) { ' [' + ($fileHits -join '; ') + ']' }) events=$($eventHits.Count)$(if ($eventHits) { ' [' + ($eventHits -join '; ') + ']' }) own-command-line=$onCmdLine"
} catch {
    Say "E2 ERROR: $($_.Exception.Message)"; exit 1
} finally {
    $AppSecret = $null; $new = $null; $bss = $null; [GC]::Collect()
}
'@
# Plaintext scan run on the VM with the value still in memory; reports counts/paths only, never the value.
# Files: Run Command handler (logs, downloads, status), Azure guest-agent logs, gateway logs, temp folders.
# Events (last 2 h): Application, System, Windows PowerShell, PowerShell/Operational (script block logging).
# Command line: this process and its parent chain (how the handler passed the parameter).
$ScanBlock = @'
    $scanDirs = @("C:\WindowsAzure\Logs", "C:\Packages\Plugins", "C:\CallCopilot\TeamsMediaGateway\logs",
        "C:\Windows\Temp", "C:\Windows\SystemTemp", $env:TEMP) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    $fileHits = @(Get-ChildItem $scanDirs -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Length -lt 50MB } |
        Select-String -SimpleMatch -Pattern $AppSecret -List -ErrorAction SilentlyContinue | ForEach-Object { $_.Path })
    $eventHits = @()
    foreach ($log in @("Application", "System", "Windows PowerShell", "Microsoft-Windows-PowerShell/Operational")) {
        $n = @(Get-WinEvent -FilterHashtable @{ LogName = $log; StartTime = (Get-Date).AddHours(-2) } -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -and $_.Message.Contains($AppSecret) }).Count
        if ($n) { $eventHits += "${log}: $n" }
    }
    $onCmdLine = $false; $procId = $PID
    for ($depth = 0; $depth -lt 4 -and $procId; $depth++) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if (-not $p) { break }
        if ($p.CommandLine -and $p.CommandLine.Contains($AppSecret)) { $onCmdLine = $true }
        $procId = $p.ParentProcessId
    }
'@
$VmScript = $VmScript.Replace("__DNS__", $GatewayDnsName).Replace("__SCAN__", $ScanBlock)
if ($TestDelivery) {
    $RunCommandName = "e2-test-delivery"
    $VmScript = @'
param([string]$AppSecret)
if ([string]::IsNullOrEmpty($AppSecret)) { "E2 TEST: protected parameter NOT delivered"; exit 3 }
"E2 TEST: protected parameter delivered as a named script parameter (dummy value, not used)"
__SCAN__
"E2 TEST: plaintext in files=$($fileHits.Count)$(if ($fileHits) { ' [' + ($fileHits -join '; ') + ']' }) events=$($eventHits.Count)$(if ($eventHits) { ' [' + ($eventHits -join '; ') + ']' })"
"E2 TEST: value on process command line (self+parents)=$onCmdLine"
"E2 TEST: CLEAN=$(($fileHits.Count -eq 0) -and ($eventHits.Count -eq 0) -and (-not $onCmdLine))"
"E2 TEST: gateway service untouched: $((Get-Service CallCopilotTeamsMediaGateway).Status)"
'@
    $VmScript = $VmScript.Replace("__SCAN__", $ScanBlock)
}

function Say([string]$m, [string]$c = "Gray") { Write-Host $m -ForegroundColor $c }

# ---- az (the uv-tool install; az.bat on this PC resolves a wrong Python) ----
$AzExe = $null; $AzPre = @()
try { $uv = (& uv tool dir 2>$null); if ($uv -and (Test-Path (Join-Path $uv "azure-cli\Scripts\python.exe"))) { $AzExe = Join-Path $uv "azure-cli\Scripts\python.exe"; $AzPre = @("-m", "azure.cli") } } catch { }
if (-not $AzExe) { $cmd = Get-Command az -ErrorAction SilentlyContinue; if (-not $cmd) { throw "Azure CLI not found" }; $AzExe = $cmd.Source }
$env:AZURE_CORE_COLLECT_TELEMETRY = "false"
function Az { $p = $ErrorActionPreference; $ErrorActionPreference = "Continue"; $o = & $AzExe @AzPre @args --only-show-errors 2>$null; $ErrorActionPreference = $p; return $o }

$vmUrl = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Compute/virtualMachines/$VmName"
$rcUrl = "$vmUrl/runCommands/${RunCommandName}?api-version=$ApiVersion"

Say "== E2: set Gateway__AppSecret on $VmName (only this setting)" "White"
$token = Az account get-access-token --subscription $SubscriptionId --resource "https://management.azure.com/" --query accessToken -o tsv
if (-not $token) { throw "No Azure token. Sign in first (az login --tenant 748ec784-319e-4b0c-be1a-7403649ca01a) and rerun." }
$headers = @{ Authorization = "Bearer $token" }
function Arm([string]$Method, [string]$Url, [byte[]]$Body = $null) {
    $args2 = @{ Method = $Method; Uri = $Url; Headers = $headers; UseBasicParsing = $true; ContentType = "application/json; charset=utf-8" }
    if ($Body) { $args2.Body = $Body }
    try { return Invoke-WebRequest @args2 } catch {
        $r = $_.Exception.Response
        $status = if ($r) { [int]$r.StatusCode } else { 0 }
        if ($status -eq 404) { return $null }
        $detail = ""; try { $sr = New-Object IO.StreamReader($r.GetResponseStream()); $e = ($sr.ReadToEnd() | ConvertFrom-Json).error; $detail = "$($e.code): $($e.message)" } catch { }
        throw "ARM $Method failed (HTTP $status) $detail"
    }
}

# ---- pre-flight (read-only) ----
$vm = (Arm GET "${vmUrl}?`$expand=instanceView&api-version=$ApiVersion").Content | ConvertFrom-Json
$power = @($vm.properties.instanceView.statuses | Where-Object { $_.code -like "PowerState/*" })[0].code
Say "VM: $($vm.name) location=$($vm.location) $power"
if ($power -ne "PowerState/running") { throw "VM is not running. Nothing changed." }
if (Arm GET $rcUrl) { Say "Leftover run command '$RunCommandName' found: deleting it first." "Yellow"; [void](Arm DELETE $rcUrl) }

# ---- read the secret (hidden) ----
if ($TestDelivery) {
    Say "TEST DELIVERY: a random dummy value is sent instead of the secret; the VM is not changed." "Cyan"
    # The dummy is not a secret: it is kept to check that it never appears in the returned output.
    $dummy = "e2dummy" + [guid]::NewGuid().ToString("N")
    $s1 = New-Object Security.SecureString; foreach ($ch in $dummy.ToCharArray()) { $s1.AppendChar($ch) }
    $s2 = $s1.Copy()
} else {
    # BLOCKED (2026-09-29 -TestDelivery): the managed Run Command handler passes protected parameters on
    # the powershell.exe command line, and Windows PowerShell logs that command line (HostApplication)
    # in the "Windows PowerShell" event log. A real secret must not be sent this way.
    throw "Real-secret mode is disabled: -TestDelivery showed protected parameters reach the VM's process command line and the 'Windows PowerShell' event log. Nothing changed."
    $s1 = Read-Host -AsSecureString -Prompt "Paste the CallCopilot Teams client secret VALUE (input hidden)"
    $s2 = Read-Host -AsSecureString -Prompt "Paste it again to confirm (input hidden)"
}
$b1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s1); $b2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s2)
$plain = $null; $bodyBytes = $null; $json = $null
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b1)
    $same = $plain -ceq [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b2)
    if (-not $plain) { throw "Empty secret. Nothing changed." }
    if (-not $same) { throw "The two entries differ. Nothing changed." }
    if ($plain -ne $plain.Trim()) { throw "The secret has leading/trailing whitespace (copy/paste issue?). Nothing changed." }
    Say "secret received (value not shown)"

    $json = @{ location = $vm.location; properties = @{
            source = @{ script = $VmScript }
            protectedParameters = @(@{ name = "AppSecret"; value = $plain })
            asyncExecution = $false; timeoutInSeconds = 600; treatFailureAsDeploymentFailure = $false } } | ConvertTo-Json -Depth 6 -Compress
    $bodyBytes = [Text.Encoding]::UTF8.GetBytes($json)
    $json = $null
    Say "sending managed Run Command (secret as protected parameter)..."
    [void](Arm PUT $rcUrl $bodyBytes)
} finally {
    if ($bodyBytes) { [Array]::Clear($bodyBytes, 0, $bodyBytes.Length) }
    $plain = $null; $json = $null; $bodyBytes = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b1); [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2)
    $s1.Dispose(); $s2.Dispose(); [GC]::Collect()
}

$out = ""
try {
    $deadline = (Get-Date).AddMinutes(15)
    do {
        Start-Sleep -Seconds 10
        $rc = (Arm GET "$($rcUrl)&`$expand=instanceView").Content | ConvertFrom-Json
        $state = $rc.properties.provisioningState
        Say "  run command: provisioning=$state execution=$($rc.properties.instanceView.executionState)"
    } while ($state -notin @("Succeeded", "Failed", "Canceled") -and (Get-Date) -lt $deadline)
    $iv = $rc.properties.instanceView
    $out = "$($iv.output)"
    Say "`n== VM output (exitCode $($iv.exitCode))" "White"
    Say $out
    if ($iv.error) { Say "stderr: $($iv.error)" "Yellow" }
} finally {
    Say "`n== Removing the Run Command resource" "White"
    try { [void](Arm DELETE $rcUrl) } catch { Say "delete request: $($_.Exception.Message)" "Yellow" }
    $gone = $false
    for ($i = 0; $i -lt 30 -and -not $gone; $i++) { Start-Sleep -Seconds 5; $gone = -not (Arm GET $rcUrl) }
    Say "run command '$RunCommandName' removed: $gone" $(if ($gone) { "Green" } else { "Red" })
    $remaining = @(((Arm GET "$vmUrl/runCommands?api-version=$ApiVersion").Content | ConvertFrom-Json).value | ForEach-Object { $_.name })
    Say "managed run commands now on VM: $($remaining -join ', ')"
}

if ($TestDelivery) {
    $inOutput = ($out + "$($iv.error)").Contains($dummy)
    Say "dummy value present in returned Run Command output/stderr: $inOutput"
    if ($gone -and -not $inOutput -and $out -match "E2 TEST: protected parameter delivered" -and $out -match "E2 TEST: CLEAN=True") {
        Say "`nTEST DELIVERY: PASSED (delivered; not on any command line, file, event log or output; run command removed). Now run without -TestDelivery." "Green"; exit 0
    }
    Say "`nTEST DELIVERY: FAILED - do not send the real secret." "Red"; exit 1
}

# ---- external read-only checks (from this PC) ----
Say "`n== External checks" "White"
$mediaOk = $out -match "E2 MEDIA_PLATFORM=True"
$healthOk = $out -match "E2 SIGNED_HEALTH=200"
$h = & curl.exe -s -o NUL -w "%{http_code} verify=%{ssl_verify_result}" --max-time 15 "https://$GatewayDnsName/healthz"
Say "GET /healthz: $h (verify=0 means trusted)"
Say "GET /health unsigned: $(& curl.exe -s -o NUL -w '%{http_code}' --max-time 15 "https://$GatewayDnsName/health") (expect 401)"
try {
    $tcp = New-Object Net.Sockets.TcpClient($GatewayDnsName, 443)
    $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false)
    $ssl.AuthenticateAsClient($GatewayDnsName)
    $rcCert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate
    Say "certificate presented externally: $($rcCert.Thumbprint) (expected ${ExpectedThumbprint}: $($rcCert.Thumbprint -eq $ExpectedThumbprint))"
    $ssl.Dispose(); $tcp.Close()
} catch { Say "external TLS check failed: $($_.Exception.Message)" "Yellow" }
foreach ($port in 80, 8445) {
    $c = New-Object Net.Sockets.TcpClient; $ar = $c.BeginConnect($GatewayDnsName, $port, $null, $null); $ok = $ar.AsyncWaitHandle.WaitOne(5000)
    Say "TCP $port from internet: $(if ($ok -and $c.Connected) { 'OPEN' } else { 'no connection' })"; $c.Close()
}
if ($mediaOk) {
    Say "POST /api/calling (no token): $(& curl.exe -s -o NUL -w '%{http_code}' --max-time 15 -X POST -H 'Content-Type: application/json' -d '{}' "https://$GatewayDnsName/api/calling") (expect an auth rejection, not 503)"
} else { Say "POST /api/calling skipped: media platform not ready" "Yellow" }

if ($mediaOk -and $healthOk) { Say "`nE2 RESULT: SUCCESS - media_platform=true and signed /health=200" "Green"; exit 0 }
Say "`nE2 RESULT: NOT READY - see VM output above (do not treat Teams media as working)" "Red"; exit 1
