<#
.SYNOPSIS
  Phase 1 (runs ON THE VM over RDP, elevated Windows PowerShell 5.1): replace ONLY
  Gateway__BackendSharedSecret of the CallCopilot Teams media gateway with a new value that is entered
  locally (hidden, twice), restart the service, and prove the new secret works and the old one no longer does.

    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File C:\Users\azureuser\Desktop\rotate-backend-secret-on-vm.ps1

  The new value comes from new-gateway-shared-secret.ps1 on the dev PC (clipboard over RDP). This file
  contains no secret and takes no parameters.

.DESCRIPTION
  Same mechanism and safety pattern as e2-set-appsecret-on-vm.ps1 (deploy-azure-vm.ps1 conventions):
  MultiString "Environment" of HKLM\SYSTEM\CurrentControlSet\Services\CallCopilotTeamsMediaGateway,
  read/written with the .NET registry API. Old and new secrets live only in this process's memory:
  never printed (nor length/hash), never a command-line or cmdlet argument (helpers select them by
  name), never written to any file. Only the Gateway__BackendSharedSecret line changes; every other
  line (incl. Gateway__AppSecret), the key order, the value type, the key ACL and the service
  configuration are verified unchanged.

  Rollback:
    * before the restart: any integrity difference -> previous value written back, NO restart, stop;
    * after the restart: service not Running, /healthz not 200, signed /health with the NEW secret not
      200, or the OLD secret not rejected with 401 -> previous value written back, service restarted,
      stop (the gateway returns to the known-working state with the previous secret).
  Event logs are never cleared. Deletes itself after a successful run.
#>
Set-StrictMode -Off
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ServiceName = "CallCopilotTeamsMediaGateway"
$SubKey = "SYSTEM\CurrentControlSet\Services\$ServiceName"
$SecretKey = "Gateway__BackendSharedSecret"
$LocalBase = "http://127.0.0.1:9441"
$Dns = "callcopilot-teams-gw.eastus.cloudapp.azure.com"
$ExpectedThumb = "A96DE7E958DB601C3F715829DA4F9178C190135C"
$ExpectedVm = "callcopilot-teams-gateway"; $ExpectedRg = "callcopilot-poc"; $ExpectedSub = "499e7488-6781-488d-acc2-d14dd260e9a7"

# ---- pure helpers (unit-tested offline with dummy values) --------------------------------------
function Sha([string]$s) {
    $h = [Security.Cryptography.SHA256]::Create()
    try { -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString("x2") }) } finally { $h.Dispose() }
}
function Get-Structure([string[]]$Lines, [string]$Key) {
    # Key names + order and a SHA-256 over every line except <Key>= (values never printed).
    [pscustomobject]@{
        Keys       = (@($Lines | ForEach-Object { $_.Split([char[]]"=", 2)[0] }) -join ",")
        OthersHash = Sha ((@($Lines | Where-Object { -not $_.StartsWith("$Key=") })) -join "`n")
        Count      = $Lines.Count
        KeyLines   = @($Lines | Where-Object { $_.StartsWith("$Key=") }).Count
    }
}
function New-RotatedLines([string[]]$Lines, [string]$Key, [scriptblock]$ValueProvider) {
    # Replaces the single existing <Key>= line in place; everything else is copied unchanged.
    if (@($Lines | Where-Object { $_.StartsWith("$Key=") }).Count -ne 1) { throw "Expected exactly one '$Key=' line. Nothing changed." }
    $out = New-Object string[] $Lines.Count
    for ($i = 0; $i -lt $Lines.Count; $i++) { $out[$i] = if ($Lines[$i].StartsWith("$Key=")) { "$Key=" + (& $ValueProvider) } else { $Lines[$i] } }
    return , $out
}
function Test-Rotation($Before, $After, [string]$Key, [scriptblock]$ExpectedLine) {
    # Before/After: string[] of the Environment value. Returns named booleans (all must be true).
    $b = Get-Structure $Before $Key; $a = Get-Structure $After $Key
    $exp = & $ExpectedLine
    [ordered]@{
        "new secret stored in the $Key line"      = [bool]($After | Where-Object { [string]::Equals($_, $exp, [StringComparison]::Ordinal) })
        "exactly one $Key line"                   = ($a.KeyLines -eq 1)
        "line count unchanged"                    = ($a.Count -eq $b.Count)
        "key names and order unchanged"           = ($a.Keys -ceq $b.Keys)
        "every other line unchanged (sha256)"     = ($a.OthersHash -eq $b.OthersHash)
    }
}
function Test-NewSecret([scriptblock]$CandidateProvider, [scriptblock]$CurrentProvider) {
    # Returns $null when acceptable, otherwise a reason (never containing the value).
    # (Parameter names deliberately differ from the callers' $new/$old: PowerShell names are case-insensitive.)
    $n = & $CandidateProvider; $o = & $CurrentProvider
    if (-not $n) { return "empty entry" }
    if ($n.Length -lt 32) { return "shorter than 32 characters" }
    if ($n.Length -gt 256) { return "longer than 256 characters" }
    if ($n -notmatch '^[\x21-\x7E]+$') { return "contains whitespace, control or non-ASCII characters" }
    if ([string]::Equals($n, $o, [StringComparison]::Ordinal)) { return "identical to the current secret" }
    return $null
}
function Get-PostRestartDecision([string]$ServiceState, [int]$Healthz, [int]$SignedNew, [int]$SignedOld) {
    if ($ServiceState -ne "Running") { return "rollback: service $ServiceState" }
    if ($Healthz -ne 200) { return "rollback: /healthz $Healthz" }
    if ($SignedNew -ne 200 -and $SignedNew -ne 503) { return "rollback: signed /health with the new secret returned $SignedNew" }
    if ($SignedOld -ne 401) { return "rollback: the old secret was not rejected (returned $SignedOld)" }
    return "ok"
}

# ---- main -------------------------------------------------------------------------------------------
if ($MyInvocation.InvocationName -eq ".") { return }   # dot-sourced by the offline tests: helpers only

$script:old = $null; $script:new = $null
function Say([string]$m) {
    foreach ($s in @($script:old, $script:new)) { if ($s -and $m) { $m = $m.Replace($s, "<redacted>") } }
    Write-Host $m
}
function Read-Env {
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey)
    try { return [pscustomobject]@{ Kind = $k.GetValueKind("Environment"); Lines = [string[]]$k.GetValue("Environment", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } }
    finally { $k.Close() }
}
function Write-Env([string[]]$Lines) {
    # The ONLY registry write: the service's MultiString "Environment" value (deploy-azure-vm.ps1 step 8).
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey, $true)
    try { $k.SetValue("Environment", $Lines, [Microsoft.Win32.RegistryValueKind]::MultiString) } finally { $k.Close() }
}
function Get-Fingerprint {
    $s = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SubKey)
    try { $vals = @($k.GetValueNames() | Where-Object { $_ -ne "Environment" } | Sort-Object | ForEach-Object { "$_=" + ((@($k.GetValue($_)) | ForEach-Object { "$_" }) -join "|") }) -join ";" } finally { $k.Close() }
    [pscustomobject]@{
        Service = Sha "$($s.PathName)|$($s.StartMode)|$($s.DelayedAutoStart)|$($s.StartName)|$vals"
        KeyAcl  = (Get-Acl "HKLM:\$SubKey").Sddl
        Fw      = Sha ((@(Get-NetFirewallRule | Where-Object { $_.Group -like "CallCopilot*" -or $_.Name -like "CallCopilot*" } | ForEach-Object {
                        $p = $_ | Get-NetFirewallPortFilter; $a = $_ | Get-NetFirewallAddressFilter
                        "$($_.Name)|$($_.Enabled)|$($_.Direction)|$($_.Action)|$($_.Profile)|$($p.Protocol)|$($p.LocalPort)|$($a.RemoteAddress)" }) | Sort-Object) -join ";")
        Certs   = (@(Get-ChildItem Cert:\LocalMachine\My | ForEach-Object { $_.Thumbprint }) | Sort-Object) -join ";"
    }
}
function Get-SignedHealth([ValidateSet("new", "old")][string]$Which) {
    # Selects the secret by NAME (the value is never a parameter). HMAC per Signing.cs, empty body.
    $secret = if ($Which -eq "new") { $script:new } else { $script:old }
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $h = New-Object Security.Cryptography.HMACSHA256 (, [Text.Encoding]::UTF8.GetBytes($secret))
    try { $sig = -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("$ts.")) | ForEach-Object { $_.ToString("x2") }) } finally { $h.Dispose(); $secret = $null }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 -Uri "$LocalBase/health" -Headers @{ "X-CC-Timestamp" = $ts; "X-CC-Signature" = "v1=$sig" }
        return [pscustomobject]@{ Code = [int]$r.StatusCode; Body = [string]$r.Content }
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { $sr = New-Object IO.StreamReader($resp.GetResponseStream()); return [pscustomobject]@{ Code = [int]$resp.StatusCode; Body = $sr.ReadToEnd() } }
        return [pscustomobject]@{ Code = 0; Body = "" }
    }
}
function Get-Healthz { try { [int](Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "$LocalBase/healthz").StatusCode } catch { 0 } }
function Test-Listen([int]$Port) { [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) }
function Get-PresentedThumb {
    try {
        $tcp = New-Object Net.Sockets.TcpClient("127.0.0.1", 443)
        $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true }))
        $ssl.AuthenticateAsClient($Dns)
        $t = (New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate).Thumbprint
        $ssl.Dispose(); $tcp.Close(); return $t
    } catch { return "none ($($_.Exception.GetType().Name))" }
}
function Wait-Healthy {
    $hz = 0; $n = $null
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        $hz = Get-Healthz
        if ($hz -ne 200) { continue }
        $n = Get-SignedHealth -Which new
        if ($n.Code -eq 200 -or $i -ge 25) { break }
    }
    return [pscustomobject]@{ Healthz = $hz; New = $n }
}
function Clear-ClipboardIfSecret {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $t = [Windows.Forms.Clipboard]::GetText()
        $had = [bool]($t -and (($script:new -and $t.Contains($script:new)) -or ($script:old -and $t.Contains($script:old))))
        $t = $null
        if ($had) { [Windows.Forms.Clipboard]::Clear() }
        return $had
    } catch { return $null }
}

$Started = Get-Date
$b1 = [IntPtr]::Zero; $b2 = [IntPtr]::Zero; $s1 = $null; $s2 = $null
$restore = $null; $success = $false; $restarted = $false
try {
    Say "== Phase 1 (local on VM): rotate ONLY $SecretKey"
    # ---- preconditions (same as the successful E2 script), all before any secret is requested ----
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "Not elevated: start Windows PowerShell with 'Run as administrator'. Nothing changed." }
    if (-not [Environment]::Is64BitProcess) { throw "Run 64-bit Windows PowerShell. Nothing changed." }
    if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne "STA") { throw "Run with powershell.exe -STA (needed to clear the clipboard). Nothing changed." }
    try { $imds = Invoke-RestMethod -Headers @{ Metadata = "true" } -TimeoutSec 5 -Uri "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01" }
    catch { throw "Cannot read Azure instance metadata: is this the gateway VM? Nothing changed." }
    if ($imds.name -ne $ExpectedVm -or $imds.resourceGroupName -ne $ExpectedRg -or $imds.subscriptionId -ne $ExpectedSub) { throw "Wrong host ($($imds.name)/$($imds.resourceGroupName)). Nothing changed." }
    if (-not (Get-Service $ServiceName -ErrorAction SilentlyContinue)) { throw "Service $ServiceName not found. Nothing changed." }
    $unsafe = @()
    foreach ($p in @("Transcription|EnableTranscripting", "ModuleLogging|EnableModuleLogging", "ScriptBlockLogging|EnableScriptBlockLogging")) {
        $n = $p.Split("|")
        foreach ($root in @("HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell", "HKCU:\SOFTWARE\Policies\Microsoft\Windows\PowerShell")) {
            if ((Get-ItemProperty "$root\$($n[0])" -Name $n[1] -ErrorAction SilentlyContinue).($n[1]) -eq 1) { $unsafe += "$($n[0]) ($root)" }
        }
    }
    if ($unsafe) { throw "PowerShell logging policy enabled: $($unsafe -join ', '). Nothing changed." }
    if (-not $PSCommandPath) { throw "Run this script with -File (see the header). Nothing changed." }

    # ---- baseline (no values printed) ----
    $env0 = Read-Env
    if ($env0.Kind -ne [Microsoft.Win32.RegistryValueKind]::MultiString -or $env0.Lines.Count -lt 5) { throw "Unexpected Environment value ($($env0.Kind), $($env0.Lines.Count) lines). Nothing changed." }
    $st0 = Get-Structure $env0.Lines $SecretKey
    if ($st0.KeyLines -ne 1) { throw "Expected exactly one '$SecretKey=' line, found $($st0.KeyLines). Nothing changed." }
    foreach ($l in $env0.Lines) { if ($l.StartsWith("$SecretKey=")) { $script:old = $l.Substring($SecretKey.Length + 1) } }
    $fp0 = Get-Fingerprint
    $h0 = Get-SignedHealth -Which old
    Say "BASELINE: $($st0.Count) lines, MultiString; keys (order): $($st0.Keys)"
    Say "BEFORE: signed /health (current secret)=$($h0.Code) | 8445=$(Test-Listen 8445) 443=$(Test-Listen 443) 80=$(Test-Listen 80) | 443 presents $(Get-PresentedThumb)"
    if ($h0.Code -ne 200) { throw "The gateway is not healthy before the rotation (signed /health $($h0.Code)). Nothing changed." }

    # ---- new secret, locally, hidden, twice ----
    $s1 = Read-Host -AsSecureString -Prompt "Paste the NEW gateway shared secret (hidden)"
    $s2 = Read-Host -AsSecureString -Prompt "Paste it again to confirm (hidden)"
    $b1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s1); $b2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s2)
    $script:new = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b1)
    $same = [string]::Equals($script:new, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b2), [StringComparison]::Ordinal)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2); $b2 = [IntPtr]::Zero; $s2.Dispose(); $s2 = $null
    Say "VM clipboard held a secret and was cleared: $(Clear-ClipboardIfSecret)"
    if (-not $same) { throw "The two entries differ. Nothing changed." }
    $why = Test-NewSecret { $script:new } { $script:old }
    if ($why) { throw "New secret rejected: $why. Nothing changed." }
    Say "new secret entered twice, matched, >= 32 characters, differs from the current one (value not shown)"

    # ---- write ONLY the one line, verify, restore on any difference (no restart) ----
    $restore = [string[]]$env0.Lines.Clone()
    Write-Env (New-RotatedLines $env0.Lines $SecretKey { $script:new })
    $env1 = Read-Env
    $checks = Test-Rotation $env0.Lines $env1.Lines $SecretKey { "$SecretKey=" + $script:new }
    $fp1 = Get-Fingerprint
    $checks["value kind still MultiString"] = ($env1.Kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString)
    $checks["registry key ACL unchanged"] = ($fp1.KeyAcl -eq $fp0.KeyAcl)
    $checks["service configuration unchanged"] = ($fp1.Service -eq $fp0.Service)
    $env1 = $null
    foreach ($k in $checks.Keys) { Say "INTEGRITY: $k = $($checks[$k])" }
    if ($checks.Values -contains $false) {
        Write-Env $restore
        $back = Get-Structure (Read-Env).Lines $SecretKey
        $restore = $null
        throw "Integrity check failed: previous Environment restored (matches baseline: $($back.OthersHash -eq $st0.OthersHash -and $back.Keys -ceq $st0.Keys)). Service NOT restarted."
    }

    # ---- restart and verify; roll back to the previous value if the gateway does not come back ----
    Say "restarting $ServiceName ..."
    $restartAt = Get-Date
    $restarted = $true
    Restart-Service $ServiceName -Force
    try { (Get-Service $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(90)) } catch { }
    $w = Wait-Healthy
    $o = Get-SignedHealth -Which old
    $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $decision = Get-PostRestartDecision $svc.State $w.Healthz $(if ($w.New) { $w.New.Code } else { 0 }) $o.Code
    if ($decision -ne "ok") {
        Say "POST-RESTART CHECK FAILED ($decision): restoring the previous Environment and restarting"
        Write-Env $restore
        Restart-Service $ServiceName -Force
        try { (Get-Service $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(90)) } catch { }
        Start-Sleep -Seconds 5
        $back = Get-SignedHealth -Which old
        $restore = $null
        throw "Rolled back to the previous secret (signed /health with it now: $($back.Code))."
    }
    $restore = $null

    $mp = $false; $problems = ""
    try { $j = $w.New.Body | ConvertFrom-Json; $mp = [bool]$j.media_platform; $problems = (@($j.problems) -join "; ") } catch { }
    $fp2 = Get-Fingerprint; $st2 = Get-Structure (Read-Env).Lines $SecretKey
    $thumb = Get-PresentedThumb
    $ev = @(Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = $restartAt } -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match "TeamsMediaGateway|\.NET Runtime|Application Error" })
    Say ""
    Say "AFTER: service=$($svc.State) ($($svc.StartName)) | /healthz=$($w.Healthz)"
    Say "AFTER: signed /health NEW secret=$($w.New.Code) media_platform=$mp problems=[$problems] | OLD secret=$($o.Code) (401 = rejected)"
    Say "AFTER: 8445=$(Test-Listen 8445) 443=$(Test-Listen 443) 80=$(Test-Listen 80) | 443 presents $thumb (expected: $($thumb -eq $ExpectedThumb))"
    Say "UNCHANGED: other lines incl. AppSecret=$($st2.OthersHash -eq $st0.OthersHash) keys/order=$($st2.Keys -ceq $st0.Keys) key ACL=$($fp2.KeyAcl -eq $fp0.KeyAcl) service config=$($fp2.Service -eq $fp0.Service) firewall=$($fp2.Fw -eq $fp0.Fw) certificates=$($fp2.Certs -eq $fp0.Certs)"
    Say "EVENTS since restart: errors=$(@($ev | Where-Object { $_.Level -le 2 }).Count) media_started=$(@($ev | Where-Object { $_.Message -match 'teams_media_platform_started' }).Count) init_failed=$(@($ev | Where-Object { $_.Message -match 'teams_media_platform_init_failed' }).Count)"

    # ---- exposure scan for the NEW value (counts only; .NET string search, never a cmdlet argument) ----
    $dirs = @("C:\CallCopilot\TeamsMediaGateway\logs", "C:\Windows\Temp", $env:TEMP, "C:\WindowsAzure\Logs", "C:\Packages\Plugins",
        (Join-Path $env:APPDATA "Microsoft\Windows\PowerShell\PSReadLine")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    $fileHits = @(Get-ChildItem $dirs -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Length -lt 60MB -and $_.LastWriteTime -ge $Started.AddMinutes(-5) } |
        Where-Object { $b = $null; try { $b = [IO.File]::ReadAllBytes($_.FullName) } catch { }
            $hit = [bool]($b -and ([Text.Encoding]::UTF8.GetString($b).Contains($script:new) -or [Text.Encoding]::Unicode.GetString($b).Contains($script:new))); $b = $null; $hit } |
        ForEach-Object { $_.FullName })
    $evHits = 0
    foreach ($log in @("Application", "System", "Windows PowerShell", "Microsoft-Windows-PowerShell/Operational")) {
        $evHits += @(Get-WinEvent -FilterHashtable @{ LogName = $log; StartTime = $Started } -ErrorAction SilentlyContinue | Where-Object { $_.Message -and $_.Message.Contains($script:new) }).Count
    }
    $cmdHits = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script:new) }).Count
    Say "SECRET SCAN (new value): files=$($fileHits.Count)$(if ($fileHits) { ' [' + ($fileHits -join '; ') + ']' }) events=$evHits process-command-lines=$cmdHits clipboard-cleared-now=$(Clear-ClipboardIfSecret)"

    $success = ($svc.State -eq "Running" -and $w.New.Code -eq 200 -and $mp -and $o.Code -eq 401 -and (Test-Listen 8445) -and (Test-Listen 443) -and -not (Test-Listen 80) -and $thumb -eq $ExpectedThumb -and
        $st2.OthersHash -eq $st0.OthersHash -and $fp2.Fw -eq $fp0.Fw -and $fp2.Certs -eq $fp0.Certs -and $fp2.Service -eq $fp0.Service -and $fileHits.Count -eq 0 -and $evHits -eq 0 -and $cmdHits -eq 0)
    Say $(if ($success) { "PHASE 1 VM RESULT: SUCCESS (new secret active, old secret rejected, media_platform=true)" } else { "PHASE 1 VM RESULT: NOT CLEAN - see the lines above (script kept for re-inspection)" })
} catch {
    Say "PHASE 1 STOPPED: $($_.Exception.Message)"
    if ($restore) {
        try {
            Write-Env $restore
            if ($restarted) {
                # The running process already read the new value: restart so it matches the registry again.
                Restart-Service $ServiceName -Force
                Say "previous Environment restored and service restarted (gateway back on the previous secret)"
            } else { Say "previous Environment restored (service not restarted)" }
        } catch { Say "RESTORE FAILED - restore manually before restarting the service" }
    }
} finally {
    [void](Clear-ClipboardIfSecret)
    if ($b1 -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b1); $b1 = [IntPtr]::Zero }
    if ($b2 -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2); $b2 = [IntPtr]::Zero }
    if ($s1) { $s1.Dispose() }; if ($s2) { $s2.Dispose() }
    $script:old = $null; $script:new = $null; $restore = $null; $env0 = $null; $same = $null
    [GC]::Collect(); [GC]::WaitForPendingFinalizers(); [GC]::Collect()
    if ($success -and $PSCommandPath -and (Test-Path $PSCommandPath)) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
        Write-Host "temporary script removed: $(-not (Test-Path $PSCommandPath))"
    }
}
if ($success) { exit 0 } else { exit 1 }
