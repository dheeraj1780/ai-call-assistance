<#
.SYNOPSIS
  E2 (RDP method): set ONLY Gateway__AppSecret of the CallCopilot Teams media gateway.
  Copy this file to the VM and run it LOCALLY from an elevated Windows PowerShell 5.1 session:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\azureuser\Desktop\e2-set-appsecret-on-vm.ps1

  This file contains no secret and takes no parameters.

.DESCRIPTION
  Source of truth: teams-media-gateway/deploy-azure-vm.ps1 and src/:
    * service "CallCopilotTeamsMediaGateway" (GatewayApp.cs UseWindowsService / deploy script $ServiceName)
    * settings = MultiString value "Environment" of HKLM\SYSTEM\CurrentControlSet\Services\<service>
      (deploy script step 8; key ACL SYSTEM + Administrators); secret key name "Gateway__AppSecret"
    * loopback diagnostics http://127.0.0.1:9441 (deploy script $LocalHttpPort): GET /healthz (open) and
      GET /health signed with Gateway__BackendSharedSecret:
      X-CC-Timestamp / X-CC-Signature = "v1=" + hex(HMAC-SHA256(secret, "{ts}." + body)) (Signing.cs)
    * HTTPS 443 (port of CallbackBaseUrl), media 8445 (InstanceInternalPort)

  Secret handling:
    * read twice with Read-Host -AsSecureString; mismatch rejected;
    * plaintext only in this process's memory; never printed (nor its length), never a command-line
      argument, never written to a file, never sent over the network, never bound to a cmdlet parameter
      (registry read/write and all searches use .NET APIs);
    * BSTRs zeroed and variables cleared at the end.

  Nothing but the Gateway__AppSecret line changes. On any integrity difference the previous value is
  restored and the script stops WITHOUT restarting the service. Event logs are not cleared, the
  BackendSharedSecret is not rotated, and no firewall/certificate/network/Teams setting is touched.
  The script deletes itself after a successful run.
#>
Set-StrictMode -Off
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ---- constants from the repository (deploy-azure-vm.ps1 / GatewayOptions.cs) ----
$ServiceName = "CallCopilotTeamsMediaGateway"
$SubKey = "SYSTEM\CurrentControlSet\Services\$ServiceName"
$SecretKey = "Gateway__AppSecret"
$BackendKey = "Gateway__BackendSharedSecret"
$LocalBase = "http://127.0.0.1:9441"
$Dns = "callcopilot-teams-gw.eastus.cloudapp.azure.com"
$ExpectedThumb = "A96DE7E958DB601C3F715829DA4F9178C190135C"
$ExpectedVm = "callcopilot-teams-gateway"
$ExpectedRg = "callcopilot-poc"
$ExpectedSub = "499e7488-6781-488d-acc2-d14dd260e9a7"
$Started = Get-Date

$plain = $null
function Say([string]$m) { if ($plain -and $m) { $m = $m.Replace($plain, "<redacted>") }; Write-Host $m }
function Sha([string]$s) {
    $h = [Security.Cryptography.SHA256]::Create()
    try { -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString("x2") }) } finally { $h.Dispose() }
}
function Read-Env {
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey)
    try {
        return [pscustomobject]@{
            Kind  = $k.GetValueKind("Environment")
            Lines = [string[]]$k.GetValue("Environment", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        }
    } finally { $k.Close() }
}
function Write-Env([string[]]$lines) {
    # Same value the deploy script writes (New-ItemProperty ... -PropertyType MultiString), via .NET.
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey, $true)
    try { $k.SetValue("Environment", $lines, [Microsoft.Win32.RegistryValueKind]::MultiString) } finally { $k.Close() }
}
function Get-KeySddl { (Get-Acl "HKLM:\$SubKey").Sddl }
function Get-Structure([string[]]$lines) {
    # Key names + order, and SHA-256 over every line except Gateway__AppSecret. Values are never printed.
    $keys = @($lines | ForEach-Object { $_.Split([char[]]"=", 2)[0] })
    $others = @($lines | Where-Object { -not $_.StartsWith("$SecretKey=") })
    [pscustomobject]@{
        Keys        = ($keys -join ",")
        OthersHash  = (Sha ($others -join "`n"))
        Count       = $lines.Count
        SecretLines = @($lines | Where-Object { $_.StartsWith("$SecretKey=") }).Count
        SecretSet   = [bool]($lines | Where-Object { $_.StartsWith("$SecretKey=") -and $_.Length -gt ($SecretKey.Length + 1) })
    }
}
function Get-ServiceBaseline {
    $s = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey)
    try { $other = @($k.GetValueNames() | Where-Object { $_ -ne "Environment" } | Sort-Object | ForEach-Object { "$_=" + (($k.GetValue($_) | ForEach-Object { "$_" }) -join "|") }) -join ";" } finally { $k.Close() }
    Sha "$($s.PathName)|$($s.StartMode)|$($s.DelayedAutoStart)|$($s.StartName)|$other"
}
function Get-FirewallBaseline {
    Sha ((@(Get-NetFirewallRule | Where-Object { $_.Group -like "CallCopilot*" -or $_.Name -like "CallCopilot*" } | ForEach-Object {
        $p = $_ | Get-NetFirewallPortFilter; $a = $_ | Get-NetFirewallAddressFilter
        "$($_.Name)|$($_.Enabled)|$($_.Direction)|$($_.Action)|$($_.Profile)|$($p.Protocol)|$($p.LocalPort)|$($a.RemoteAddress)" }) | Sort-Object) -join ";")
}
function Get-FirewallSummary {
    (@(Get-NetFirewallRule | Where-Object { $_.Group -like "CallCopilot*" -or $_.Name -like "CallCopilot*" } | ForEach-Object {
        $p = $_ | Get-NetFirewallPortFilter; "$($p.Protocol) $($p.LocalPort)" }) | Sort-Object) -join ", "
}
function Get-CertBaseline { (@(Get-ChildItem Cert:\LocalMachine\My | ForEach-Object { $_.Thumbprint }) | Sort-Object) -join ";" }
function Test-Listen([int]$port) { [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) }
function Get-ListenSummary { "443=$(if (Test-Listen 443) { 'listening' } else { 'not listening' }) 8445=$(if (Test-Listen 8445) { 'listening' } else { 'not listening' }) 80=$(if (Test-Listen 80) { 'LISTENING' } else { 'closed' })" }
function Get-Healthz { try { [int](Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "$LocalBase/healthz").StatusCode } catch { 0 } }
function Get-SignedHealth {
    $bss = ""
    foreach ($l in (Read-Env).Lines) { if ($l.StartsWith("$BackendKey=")) { $bss = $l.Substring($BackendKey.Length + 1) } }
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $h = New-Object Security.Cryptography.HMACSHA256 (, [Text.Encoding]::UTF8.GetBytes($bss))
    try { $sig = -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("$ts.")) | ForEach-Object { $_.ToString("x2") }) } finally { $h.Dispose(); $bss = $null }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 -Uri "$LocalBase/health" -Headers @{ "X-CC-Timestamp" = $ts; "X-CC-Signature" = "v1=$sig" }
        return @([int]$r.StatusCode, [string]$r.Content)
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { $sr = New-Object IO.StreamReader($resp.GetResponseStream()); return @([int]$resp.StatusCode, $sr.ReadToEnd()) }
        return @(0, "")
    }
}
function Get-MediaPlatform([string]$body) { try { return [bool](($body | ConvertFrom-Json).media_platform) } catch { return $false } }
function Get-Problems([string]$body) { try { return (@(($body | ConvertFrom-Json).problems) -join "; ") } catch { return "" } }
function Get-PresentedThumb {
    try {
        $tcp = New-Object Net.Sockets.TcpClient("127.0.0.1", 443)
        $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true }))
        $ssl.AuthenticateAsClient($Dns)
        $t = (New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate).Thumbprint
        $ssl.Dispose(); $tcp.Close(); return $t
    } catch { return "none ($($_.Exception.GetType().Name))" }
}

$b1 = [IntPtr]::Zero; $b2 = [IntPtr]::Zero; $s1 = $null; $s2 = $null
$restoreLines = $null; $newLines = $null; $env0 = $null; $env1 = $null
$success = $false
try {
    Say "== E2 (local on VM): set ONLY $SecretKey"

    # ---- 1-4. preconditions (all before the secret is requested) ----
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "Not elevated: start Windows PowerShell with 'Run as administrator'. Nothing changed." }
    if (-not [Environment]::Is64BitProcess) { throw "Run 64-bit Windows PowerShell. Nothing changed." }
    try {
        $imds = Invoke-RestMethod -Headers @{ Metadata = "true" } -TimeoutSec 5 -Uri "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01"
    } catch { throw "Cannot read Azure instance metadata: is this the Azure gateway VM? Nothing changed." }
    if ($imds.name -ne $ExpectedVm -or $imds.resourceGroupName -ne $ExpectedRg -or $imds.subscriptionId -ne $ExpectedSub) {
        throw "Wrong host: $($imds.name) / $($imds.resourceGroupName) (expected $ExpectedVm / $ExpectedRg). Nothing changed."
    }
    if (-not (Get-Service $ServiceName -ErrorAction SilentlyContinue)) { throw "Service $ServiceName not found. Nothing changed." }
    $policyRoot = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell"
    $unsafe = @()
    foreach ($p in @("Transcription|EnableTranscripting", "ModuleLogging|EnableModuleLogging", "ScriptBlockLogging|EnableScriptBlockLogging")) {
        $n = $p.Split("|")
        foreach ($root in @($policyRoot, "HKCU:\SOFTWARE\Policies\Microsoft\Windows\PowerShell")) {
            $v = (Get-ItemProperty "$root\$($n[0])" -Name $n[1] -ErrorAction SilentlyContinue).($n[1])
            if ($v -eq 1) { $unsafe += "$($n[0]) ($root)" }
        }
    }
    if ($unsafe) { throw "PowerShell logging policy enabled: $($unsafe -join ', '). Stopped before requesting the secret. Nothing changed." }
    if (-not $PSCommandPath) { throw "Run this script with -File (see the header). Nothing changed." }
    Say "preconditions OK: elevated, 64-bit, host $($imds.name)/$($imds.resourceGroupName), service present, no transcription/module/script-block logging policy"

    # ---- 5. baseline (no secret values printed) ----
    $env0 = Read-Env
    if ($env0.Kind -ne [Microsoft.Win32.RegistryValueKind]::MultiString -or $env0.Lines.Count -lt 5) { throw "Unexpected Environment value ($($env0.Kind), $($env0.Lines.Count) lines). Nothing changed." }
    $st0 = Get-Structure $env0.Lines
    if ($st0.SecretLines -ne 1) { throw "Expected exactly one existing '$SecretKey=' entry, found $($st0.SecretLines). Nothing changed." }
    $acl0 = Get-KeySddl; $svc0 = Get-ServiceBaseline; $fw0 = Get-FirewallBaseline; $cert0 = Get-CertBaseline
    $h0 = Get-SignedHealth
    Say "BASELINE: $($st0.Count) lines, kind MultiString; keys (order): $($st0.Keys)"
    Say "BASELINE: other-lines sha256 $($st0.OthersHash.Substring(0,16))..; key ACL sha256 $((Sha $acl0).Substring(0,16))..; service config sha256 $($svc0.Substring(0,16)).."
    Say "BASELINE: firewall [$(Get-FirewallSummary)]; LocalMachine\My: $($cert0 -replace ';', ', ')"
    Say "BEFORE: AppSecret=$(if ($st0.SecretSet) { 'present' } else { 'missing' }) | media_platform=$(Get-MediaPlatform $h0[1]) | signed /health=$($h0[0]) | /healthz=$(Get-Healthz) | $(Get-ListenSummary)"

    # ---- 6-8. read the secret locally (hidden, twice) ----
    $s1 = Read-Host -AsSecureString -Prompt "Enter the CallCopilot Teams client secret VALUE (hidden)"
    $s2 = Read-Host -AsSecureString -Prompt "Enter it again to confirm (hidden)"
    $b1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s1)
    $b2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s2)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b1)
    $same = [string]::Equals($plain, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b2), [StringComparison]::Ordinal)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2); $b2 = [IntPtr]::Zero; $s2.Dispose(); $s2 = $null
    if (-not $plain) { throw "Empty entry. Nothing changed." }
    if (-not $same) { throw "The two entries differ. Nothing changed." }
    if ($plain -ne $plain.Trim() -or $plain.IndexOfAny([char[]]@("`r", "`n", [char]0)) -ge 0) { throw "Entry has leading/trailing whitespace or control characters. Nothing changed." }
    Say "secret entered twice and matched (value not shown)"

    # ---- 9. replace ONLY the existing Gateway__AppSecret= entry ----
    $restoreLines = [string[]]$env0.Lines.Clone()
    $newLines = New-Object System.Collections.Generic.List[string]
    foreach ($l in $env0.Lines) { if ($l.StartsWith("$SecretKey=")) { $newLines.Add("$SecretKey=$plain") } else { $newLines.Add($l) } }
    Write-Env ([string[]]$newLines.ToArray())
    $newLines = $null

    # ---- 15-16. integrity ----
    $env1 = Read-Env; $st1 = Get-Structure $env1.Lines
    $checks = [ordered]@{
        "Gateway__AppSecret present and equal to the entered value" = [bool]($env1.Lines | Where-Object { [string]::Equals($_, "$SecretKey=$plain", [StringComparison]::Ordinal) })
        "value kind still MultiString" = ($env1.Kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString)
        "line count unchanged" = ($st1.Count -eq $st0.Count)
        "key names and order unchanged" = ($st1.Keys -eq $st0.Keys)
        "every other line unchanged (sha256)" = ($st1.OthersHash -eq $st0.OthersHash)
        "registry key ACL unchanged" = ((Get-KeySddl) -eq $acl0)
        "service configuration unchanged" = ((Get-ServiceBaseline) -eq $svc0)
    }
    $env1 = $null
    foreach ($k in $checks.Keys) { Say "INTEGRITY: $k = $($checks[$k])" }
    if ($checks.Values -contains $false) {
        Write-Env $restoreLines
        $back = Get-Structure (Read-Env).Lines
        throw "Integrity check failed: previous Environment restored (restored hash matches baseline: $($back.OthersHash -eq $st0.OthersHash -and $back.Keys -eq $st0.Keys)). Service NOT restarted."
    }
    $restoreLines = $null

    # ---- 17. restart ----
    Say "restarting $ServiceName ..."
    $restartAt = Get-Date
    Restart-Service $ServiceName -Force
    (Get-Service $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(90))

    # ---- 18. verification ----
    $hz = 0; $h1 = @(0, ""); $mp1 = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        $hz = Get-Healthz
        if ($hz -ne 200) { continue }
        $h1 = Get-SignedHealth; $mp1 = Get-MediaPlatform $h1[1]
        if (($h1[0] -eq 200 -and $mp1) -or $i -ge 25) { break }
    }
    $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $problems = Get-Problems $h1[1]
    $thumb = Get-PresentedThumb
    $ev = @(Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = $restartAt } -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -match "TeamsMediaGateway|\.NET Runtime|Application Error" })
    $missingEv = @($ev | Where-Object { $_.Message -match "AppSecret is missing" }).Count
    $errEv = @($ev | Where-Object { $_.LevelDisplayName -in @("Error", "Critical") })
    $mediaEv = @($ev | Where-Object { $_.Message -match "teams_media_(platform_started|unavailable)" } | ForEach-Object {
        $m = $_.Message -replace "\s+", " "; $x = $m.IndexOf("teams_media"); $m.Substring($x, [Math]::Min(240, $m.Length - $x)) })
    $fwSame = (Get-FirewallBaseline) -eq $fw0; $certSame = (Get-CertBaseline) -eq $cert0
    $svcSame = (Get-ServiceBaseline) -eq $svc0; $st2 = Get-Structure (Read-Env).Lines; $aclSame = (Get-KeySddl) -eq $acl0

    Say ""
    Say "AFTER: service=$($svc.State) (pid $($svc.ProcessId), $($svc.StartName))"
    Say "AFTER: /healthz=$hz | signed /health=$($h1[0]) | media_platform=$mp1 | problems=[$problems]"
    Say "AFTER: AppSecret reported missing: $([bool]($problems -match 'AppSecret'))"
    Say "AFTER: $(Get-ListenSummary)"
    Say "AFTER: 443 presents $thumb (expected $ExpectedThumb`: $($thumb -eq $ExpectedThumb))"
    Say "EVENTS since restart: 'AppSecret is missing'=$missingEv errors=$($errEv.Count) media=[$($mediaEv -join ' | ')]"
    foreach ($e in ($errEv | Select-Object -First 3)) { $m = $e.Message -replace '\s+', ' '; Say "  error: $($m.Substring(0, [Math]::Min(300, $m.Length)))" }
    Say "UNCHANGED: other settings=$($st2.OthersHash -eq $st0.OthersHash) keys/order=$($st2.Keys -eq $st0.Keys) key ACL=$aclSame service config=$svcSame firewall=$fwSame certificates=$certSame"

    # ---- 19. secret-exposure scan (value still in memory; counts only; .NET string search) ----
    $dirs = @("C:\CallCopilot\TeamsMediaGateway\logs", "C:\Windows\Temp", $env:TEMP, "C:\WindowsAzure\Logs", "C:\Packages\Plugins",
        (Join-Path $env:APPDATA "Microsoft\Windows\PowerShell\PSReadLine")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    $fileHits = @(Get-ChildItem $dirs -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 50MB -and $_.LastWriteTime -ge $Started.AddMinutes(-5) } |
        Where-Object { $t = $null; try { $t = [IO.File]::ReadAllText($_.FullName) } catch { }; $hit = [bool]($t -and $t.Contains($plain)); $t = $null; $hit } |
        ForEach-Object { $_.FullName })
    $evHits = @()
    foreach ($log in @("Application", "System", "Windows PowerShell", "Microsoft-Windows-PowerShell/Operational")) {
        $n = @(Get-WinEvent -FilterHashtable @{ LogName = $log; StartTime = $Started } -ErrorAction SilentlyContinue | Where-Object { $_.Message -and $_.Message.Contains($plain) }).Count
        if ($n) { $evHits += "${log}: $n" }
    }
    $cmdHits = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($plain) }).Count
    $clip = $false
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $t = [Windows.Forms.Clipboard]::GetText(); $clip = [bool]($t -and $t.Contains($plain)); $t = $null
        if ($clip) { [Windows.Forms.Clipboard]::Clear() }
    } catch { }
    Say "SECRET SCAN: files=$($fileHits.Count)$(if ($fileHits) { ' [' + ($fileHits -join '; ') + ']' }) events=$(if ($evHits) { $evHits -join ', ' } else { 0 }) process-command-lines=$cmdHits clipboard=$(if ($clip) { 'contained it -> cleared' } else { 'clean' })"

    $success = ($svc.State -eq "Running" -and $h1[0] -eq 200 -and $mp1 -and $missingEv -eq 0 -and (Test-Listen 8445) -and (Test-Listen 443) -and -not (Test-Listen 80) -and $thumb -eq $ExpectedThumb)
    if ($success) { Say "E2 VM RESULT: SUCCESS (media_platform=true, signed /health=200)" }
    else { Say "E2 VM RESULT: NOT READY - AppSecret is configured, see the lines above (script kept for re-inspection)" }
} catch {
    Say "E2 STOPPED: $($_.Exception.Message)"
    if ($restoreLines) { try { Write-Env $restoreLines; Say "previous Environment restored (service not restarted)" } catch { Say "RESTORE FAILED - do not restart the service; restore manually" } }
} finally {
    if ($b1 -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b1); $b1 = [IntPtr]::Zero }
    if ($b2 -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2); $b2 = [IntPtr]::Zero }
    if ($s1) { $s1.Dispose() }; if ($s2) { $s2.Dispose() }
    $plain = $null; $newLines = $null; $restoreLines = $null; $env0 = $null; $env1 = $null; $same = $null
    [GC]::Collect(); [GC]::WaitForPendingFinalizers(); [GC]::Collect()
    if ($success -and $PSCommandPath -and (Test-Path $PSCommandPath)) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
        Write-Host "temporary script removed: $(-not (Test-Path $PSCommandPath))"
    }
}
if ($success) { exit 0 } else { exit 1 }
