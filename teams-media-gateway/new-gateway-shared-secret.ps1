<#
.SYNOPSIS
  Phase 1 (runs on the DEV PC): generate the new gateway shared secret, store it as
  TEAMS_MEDIA_GATEWAY_SECRET in the git-ignored backend/.env, and hand it to the VM once through the
  clipboard (for rotate-backend-secret-on-vm.ps1, which reads it with hidden input over RDP).

    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\teams-media-gateway\new-gateway-shared-secret.ps1

  Never prints the secret, its length or a hash. Never passes it to a child process or command line.
  The only places it is written: backend/.env (git-ignored) and, temporarily, the clipboard (marked
  "not for clipboard history / cloud clipboard"; cleared after the paste or the timeout).

.PARAMETER Replace
  Required when backend/.env already has a TEAMS_MEDIA_GATEWAY_SECRET line (a later rotation).
.PARAMETER ClipboardSeconds
  How long the value stays on the clipboard at most (default 180 s).
#>
[CmdletBinding()]
param(
    [switch]$Replace,
    [ValidateRange(30, 600)][int]$ClipboardSeconds = 180
)
Set-StrictMode -Off
$ErrorActionPreference = "Stop"

$Key = "TEAMS_MEDIA_GATEWAY_SECRET"

# ---- pure helpers (unit-tested offline with dummy values) --------------------------------------
function New-UrlSafeSecret {
    # 48 cryptographically random bytes -> base64url without padding = exactly 64 characters.
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    try { return [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=') }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}
function Update-EnvText {
    # Returns the new .env text with exactly one "<Key>=<Value>" line: replaces an existing line in
    # place, or appends one. Every other line (comments, blanks, order, line endings) is preserved.
    # Text and value come from provider scriptblocks, so no secret-bearing string is ever bound to a
    # PowerShell parameter (the .env holds other secrets too).
    param([scriptblock]$TextProvider, [string]$Name, [scriptblock]$ValueProvider, [switch]$AllowReplace)
    $Text = [string](& $TextProvider)
    $nl = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = [System.Collections.Generic.List[string]]::new()
    if ($Text.Length) { $lines.AddRange([string[]]($Text -split "\r?\n", 0)) }
    $trailing = $lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq ""
    if ($trailing) { $lines.RemoveAt($lines.Count - 1) }
    $idx = @(for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^\s*$([regex]::Escape($Name))\s*=") { $i } })
    if ($idx.Count -gt 1) { throw "$Name appears $($idx.Count) times in the file; fix it manually first. Nothing changed." }
    if ($idx.Count -eq 1 -and -not $AllowReplace) { throw "$Name already exists; rerun with -Replace to rotate it. Nothing changed." }
    $line = "$Name=" + (& $ValueProvider)
    if ($idx.Count -eq 1) { $lines[$idx[0]] = $line } else { $lines.Add($line) }
    return ($lines -join $nl) + $nl
}
function Get-EnvLinesExcept([scriptblock]$TextProvider, [string]$Name) {
    # Every line except <Name>=..., in order, blank lines and comments included (a final newline ignored).
    $Text = [string](& $TextProvider)
    $all = [System.Collections.Generic.List[string]]::new()
    if ($Text.Length) { $all.AddRange([string[]]($Text -split "\r?\n", 0)) }
    if ($all.Count -gt 0 -and $all[$all.Count - 1] -eq "") { $all.RemoveAt($all.Count - 1) }
    @($all | Where-Object { $_ -notmatch "^\s*$([regex]::Escape($Name))\s*=" })
}

# ---- main -------------------------------------------------------------------------------------------
if ($MyInvocation.InvocationName -eq ".") { return }   # dot-sourced by the offline tests: helpers only

$script:secret = $null
$envPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\backend\.env"))
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$exit = 1
try {
    if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne "STA") { throw "Run with powershell.exe -STA (needed for the clipboard). Nothing changed." }
    foreach ($p in @("Transcription|EnableTranscripting", "ModuleLogging|EnableModuleLogging", "ScriptBlockLogging|EnableScriptBlockLogging")) {
        $n = $p.Split("|")
        foreach ($root in @("HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell", "HKCU:\SOFTWARE\Policies\Microsoft\Windows\PowerShell")) {
            if ((Get-ItemProperty "$root\$($n[0])" -Name $n[1] -ErrorAction SilentlyContinue).($n[1]) -eq 1) { throw "PowerShell $($n[0]) policy is enabled. Nothing changed." }
        }
    }
    if (-not (Test-Path -LiteralPath $envPath)) { throw "backend/.env not found at $envPath. Nothing changed." }
    # git check-ignore gets only the file path (never the secret).
    & git -C $repo check-ignore -q -- "backend/.env" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "backend/.env is NOT git-ignored; refusing to write a secret into it. Nothing changed." }

    $raw = [IO.File]::ReadAllBytes($envPath)
    $bom = $raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF
    $enc = New-Object Text.UTF8Encoding($bom)
    $before = $enc.GetString($raw, $(if ($bom) { 3 } else { 0 }), $raw.Length - $(if ($bom) { 3 } else { 0 }))

    $script:secret = New-UrlSafeSecret
    $script:before = $before; $before = $null
    $after = Update-EnvText -TextProvider { $script:before } -Name $Key -ValueProvider { $script:secret } -AllowReplace:$Replace

    # Write next to the original, then atomically replace it (keeps the file's ACL; no backup copy).
    $tmp = "$envPath.tmp-" + [guid]::NewGuid().ToString("N")
    try {
        [IO.File]::WriteAllText($tmp, $after, $enc)
        [IO.File]::Replace($tmp, $envPath, [NullString]::Value)
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
    $after = $null

    $allBytes = [IO.File]::ReadAllBytes($envPath)
    $bomNow = $allBytes.Length -ge 3 -and $allBytes[0] -eq 0xEF -and $allBytes[1] -eq 0xBB -and $allBytes[2] -eq 0xBF
    $script:check = $enc.GetString($allBytes, $(if ($bomNow) { 3 } else { 0 }), $allBytes.Length - $(if ($bomNow) { 3 } else { 0 }))
    [Array]::Clear($allBytes, 0, $allBytes.Length); $allBytes = $null
    $keyLines = @(($script:check -split "\r?\n") | Where-Object { $_ -match "^\s*$Key\s*=" })
    $stored = $keyLines.Count -eq 1 -and [string]::Equals($keyLines[0], "$Key=" + $script:secret, [StringComparison]::Ordinal)
    $othersSame = ((Get-EnvLinesExcept { $script:before } $Key) -join "`n") -ceq ((Get-EnvLinesExcept { $script:check } $Key) -join "`n")
    $script:check = $null; $keyLines = $null; $script:before = $null
    Write-Host "backend/.env: $Key stored=$stored (exactly one line); all other lines unchanged=$othersSame; BOM preserved=$($bomNow -eq $bom)"
    if (-not ($stored -and $othersSame -and $bomNow -eq $bom)) { throw ".env verification failed - inspect backend/.env manually (the value is not shown)." }

    # ---- clipboard hand-off (history / cloud sync excluded; cleared afterwards) ----
    Add-Type -AssemblyName System.Windows.Forms
    $data = New-Object Windows.Forms.DataObject
    $data.SetData([Windows.Forms.DataFormats]::UnicodeText, $script:secret)
    # Windows clipboard formats: DWORD 0 = do not keep in clipboard history / do not sync to cloud clipboard.
    $data.SetData("CanIncludeInClipboardHistory", (New-Object IO.MemoryStream(, [BitConverter]::GetBytes([int]0))))
    $data.SetData("CanUploadToCloudClipboard", (New-Object IO.MemoryStream(, [BitConverter]::GetBytes([int]0))))
    [Windows.Forms.Clipboard]::SetDataObject($data, $true)
    $data = $null
    Write-Host ""
    Write-Host "The new secret is on the clipboard (excluded from clipboard history and cloud sync)."
    Write-Host "In the RDP session: paste it into BOTH hidden prompts of rotate-backend-secret-on-vm.ps1."
    Write-Host "Then press Enter here. The clipboard is cleared automatically after $ClipboardSeconds s."
    $deadline = (Get-Date).AddSeconds($ClipboardSeconds)
    while ((Get-Date) -lt $deadline) {
        if ([Console]::KeyAvailable) { if ([Console]::ReadKey($true).Key -eq "Enter") { break } }
        Start-Sleep -Milliseconds 250
    }
    $exit = 0
} catch {
    $m = $_.Exception.Message
    if ($script:secret) { $m = $m.Replace($script:secret, "<redacted>") }
    Write-Host "STOPPED: $m"
} finally {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        if ($script:secret -and [Windows.Forms.Clipboard]::ContainsText() -and [Windows.Forms.Clipboard]::GetText().Contains($script:secret)) {
            [Windows.Forms.Clipboard]::Clear()
        }
        Write-Host "clipboard holds the secret now: $([bool]($script:secret -and [Windows.Forms.Clipboard]::ContainsText() -and [Windows.Forms.Clipboard]::GetText().Contains($script:secret)))"
    } catch { Write-Host "clipboard check failed ($($_.Exception.GetType().Name)); clear it manually (copy any other text)." }
    $script:secret = $null; $script:before = $null; $script:check = $null; $after = $null; [GC]::Collect()
}
exit $exit
