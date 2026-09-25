<#
.SYNOPSIS
  Idempotent deployment of the CallCopilot Teams media gateway to an Azure Windows Server VM.
  POC tooling. It does not make Teams calling work by itself; see AZURE-VM-DEPLOYMENT.md.

.DESCRIPTION
  Modes
    -Package          (dev PC)  dotnet publish (win-x64, framework-dependent) + deploy-info.json
                                (commit) -> zip. Upload the zip somewhere the VM can download it.
    (default)         (VM)      deploy: OS check, ASP.NET Core runtime, directories, package,
                                service environment, Kestrel HTTPS, firewall, Windows service with
                                restart-on-failure, start, health check, summary.
    -HealthCheckOnly  (VM)      just the health check + summary.
    -DryRun           (VM/dev)  every read-only check and the planned changes; changes nothing.

  Everything the gateway reads comes from its existing configuration (GatewayOptions.cs,
  appsettings.json, standard ASP.NET Core Kestrel settings). Secrets are parameters (use Azure
  Run Command protected parameters), are stored only in the service's own registry environment
  (ACL: SYSTEM + Administrators) and are never printed. On a re-run, omitted settings keep their
  deployed values.

  Runs in Windows PowerShell 5.1 (Azure Run Command) as SYSTEM or an administrator.
#>
[CmdletBinding()]
param(
    # ---- modes ----
    [switch]$Package,
    [switch]$HealthCheckOnly,
    [switch]$DryRun,

    # ---- -Package ----
    [string]$OutputZip = "",

    # ---- deploy: gateway package ----
    # https URL (e.g. blob SAS) or local path of a zip: either -Package output (published app) or
    # the teams-media-gateway source folder (then built on the VM with the pinned SDK).
    [string]$PackageUrl = "",
    [string]$PackagePath = "",

    # ---- deploy: gateway settings (Gateway__* in GatewayOptions.cs) ----
    [string]$BackendSharedSecret = "",      # secret, >= 32 chars, = API TEAMS_MEDIA_GATEWAY_SECRET
    [string]$AppId = "",                    # Entra application (bot) id
    [string]$AppSecret = "",                # secret
    [string]$HomeTenantId = "",
    [string]$ServiceDnsName = "",           # public DNS name of this VM (matches the certificate)
    [string]$CertificateThumbprint = "",    # certificate in LocalMachine\My
    [string]$InstancePublicIPAddress = "",  # instance-level public IP (IMDS is tried if omitted)
    [string]$CallbackBaseUrl = "",          # default https://<ServiceDnsName>
    [int]$InstancePublicPort = 0,           # default 8445 (GatewayOptions)
    [int]$InstanceInternalPort = 0,         # default 8445 (GatewayOptions)
    [int]$MaxConcurrentCalls = 0,           # default 4 (GatewayOptions)

    # ---- deploy: host ----
    [string]$InstallRoot = "C:\CallCopilot\TeamsMediaGateway",
    [string]$AspNetCoreRuntimeVersion = "8.0.31",   # version the gateway was built/tested with
    [string]$SdkVersion = "8.0.425",                # only when the package is source
    [int]$LocalHttpPort = 9441                      # loopback-only diagnostics listener (README)
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ServiceName = "CallCopilotTeamsMediaGateway"    # = UseWindowsService name in GatewayApp.cs
$DisplayName = "CallCopilot Teams Media Gateway (POC)"
$ExeName = "TeamsMediaGateway.exe"
$DotnetDir = Join-Path $env:ProgramFiles "dotnet"
$FirewallGroup = "CallCopilot Teams Media Gateway"
$SecretNames = @("Gateway__BackendSharedSecret", "Gateway__AppSecret")
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

$script:Notes = New-Object System.Collections.Generic.List[string]
function Step([string]$m) { Write-Host ""; Write-Host "== $m" }
function Info([string]$m) { Write-Host "   $m" }
function Warn([string]$m) { Write-Host "   WARNING: $m"; $script:Notes.Add($m) }
function Plan([string]$m) { Write-Host "   [dry-run] would $m" }

# ======================================================================================
# -Package (dev PC)
# ======================================================================================
if ($Package) {
    $src = $PSScriptRoot
    $dotnet = (Get-Command dotnet -ErrorAction SilentlyContinue)
    $dotnetExe = if ($dotnet) { $dotnet.Source } else { Join-Path $env:USERPROFILE ".dotnet\dotnet.exe" }
    if (-not (Test-Path $dotnetExe)) { throw "dotnet SDK not found (PATH or %USERPROFILE%\.dotnet)" }
    if (-not $OutputZip) { $OutputZip = Join-Path $src "artifacts\teams-media-gateway-$Stamp.zip" }
    $work = Join-Path ([IO.Path]::GetTempPath()) "tmg-publish-$Stamp"
    Step "Publishing (Release, win-x64, framework-dependent)"
    & $dotnetExe publish (Join-Path $src "TeamsMediaGateway.csproj") -c Release -r win-x64 --self-contained false -o $work -nologo
    if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }
    $commit = ""; $dirty = $false
    try {
        $commit = (& git -C $src rev-parse HEAD 2>$null)
        $dirty = [bool](& git -C $src status --porcelain -- . 2>$null)
    } catch { }
    $info = [ordered]@{
        component = "teams-media-gateway"; commit = "$commit"; uncommitted_changes = $dirty
        built_at_utc = (Get-Date).ToUniversalTime().ToString("o"); target = "net8.0 win-x64 framework-dependent"
        aspnetcore_runtime_tested = $AspNetCoreRuntimeVersion
    }
    $info | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $work "deploy-info.json")
    New-Item -ItemType Directory -Force (Split-Path $OutputZip) | Out-Null
    if (Test-Path $OutputZip) { Remove-Item $OutputZip -Force }
    Compress-Archive -Path (Join-Path $work "*") -DestinationPath $OutputZip
    Remove-Item $work -Recurse -Force
    Step "Package ready"
    Info "zip:    $OutputZip ($([math]::Round((Get-Item $OutputZip).Length / 1MB, 1)) MB)"
    Info "commit: $commit$(if ($dirty) { ' (+ uncommitted changes)' })"
    Info "Upload it (e.g. Azure blob + read-only SAS) and pass the URL as -PackageUrl on the VM."
    return
}

# ======================================================================================
# helpers (VM)
# ======================================================================================
$ServiceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"

function Get-ServiceEnvironment {
    $envMap = [ordered]@{}
    if (Test-Path $ServiceKey) {
        $values = (Get-ItemProperty $ServiceKey -Name Environment -ErrorAction SilentlyContinue)
        if ($values) {
            foreach ($line in $values.Environment) {
                $i = $line.IndexOf("=")
                if ($i -gt 0) { $envMap[$line.Substring(0, $i)] = $line.Substring($i + 1) }
            }
        }
    }
    return $envMap
}

function Get-DotnetRuntimes {
    $exe = Join-Path $DotnetDir "dotnet.exe"
    if (-not (Test-Path $exe)) { return @() }
    return @(& $exe --list-runtimes 2>$null)
}

function Invoke-Curl([string[]]$CurlArgs) {
    # curl.exe ships with Windows Server 2019+; returns @(status, body)
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $code = & curl.exe -s -o $tmp -w "%{http_code}" --max-time 10 @CurlArgs 2>$null
        $body = Get-Content $tmp -Raw -ErrorAction SilentlyContinue
        return @("$code", "$body")
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

function Get-SignedHeaders([string]$Secret) {
    # Signing.cs: X-CC-Signature = "v1=" + hex(HMAC-SHA256(secret, "{ts}." + body)); body empty
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 (, [Text.Encoding]::UTF8.GetBytes($Secret))
    try { $hash = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$ts.")) } finally { $hmac.Dispose() }
    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return @("-H", "X-CC-Timestamp: $ts", "-H", "X-CC-Signature: v1=$hex")
}

function Invoke-HealthCheck([hashtable]$Cfg) {
    $r = [ordered]@{ liveness = "not run"; readiness = "not run"; media_platform = $null; problems = @(); https = "not configured" }
    $base = "http://127.0.0.1:$($Cfg.LocalHttpPort)"
    $live = $null
    for ($i = 0; $i -lt 15; $i++) {
        $live = Invoke-Curl @("$base/healthz")
        if ($live[0] -eq "200") { break }
        Start-Sleep -Seconds 2
    }
    $r.liveness = if ($live[0] -eq "200") { "200 alive" } else { "FAILED (HTTP $($live[0]))" }
    if ($Cfg.Secret) {
        $ready = Invoke-Curl ((Get-SignedHeaders $Cfg.Secret) + @("$base/health"))
        $r.readiness = "HTTP $($ready[0])"
        try {
            $json = $ready[1] | ConvertFrom-Json
            $r.media_platform = $json.media_platform
            $r.problems = @($json.problems)
        } catch { }
    }
    if ($Cfg.HttpsEnabled -and $Cfg.HttpsPort -and $Cfg.Dns) {
        # Real TLS validation for the DNS name, without depending on public DNS/NSG yet.
        $h = Invoke-Curl @("--resolve", "$($Cfg.Dns):$($Cfg.HttpsPort):127.0.0.1", "https://$($Cfg.Dns):$($Cfg.HttpsPort)/healthz")
        $r.https = if ($h[0] -eq "200") { "OK (valid certificate for $($Cfg.Dns), port $($Cfg.HttpsPort))" } else { "FAILED (HTTP/curl status $($h[0]))" }
    }
    return $r
}

function Get-ListeningPorts {
    $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    if (-not $svc -or -not $svc.ProcessId) { return @() }
    $tcp = @(Get-NetTCPConnection -State Listen -OwningProcess $svc.ProcessId -ErrorAction SilentlyContinue |
        ForEach-Object { "TCP $($_.LocalAddress):$($_.LocalPort)" })
    $udp = @(Get-NetUDPEndpoint -OwningProcess $svc.ProcessId -ErrorAction SilentlyContinue |
        ForEach-Object { "UDP $($_.LocalAddress):$($_.LocalPort)" })
    return @($tcp + $udp | Sort-Object -Unique)
}

function Write-Summary([hashtable]$Cfg, $Health) {
    Step "DEPLOYMENT SUMMARY (POC - a real Teams call has NOT been tested by this script)"
    $runtimes = @(Get-DotnetRuntimes | Where-Object { $_ -match "Microsoft\.(AspNetCore|NETCore)\.App 8\." })
    Info ".NET runtimes:      $(if ($runtimes) { ($runtimes | ForEach-Object { ($_ -split ' \[')[0] }) -join '; ' } else { 'NOT INSTALLED' })"
    $infoFile = Join-Path $InstallRoot "app\deploy-info.json"
    if (Test-Path $infoFile) {
        $di = Get-Content $infoFile -Raw | ConvertFrom-Json
        Info "gateway commit:     $($di.commit)$(if ($di.uncommitted_changes) { ' (+ uncommitted changes)' }) built $($di.built_at_utc)"
    } else { Info "gateway commit:     unknown (no deploy-info.json)" }
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    Info "service:            $(if ($svc) { "$($svc.Status) ($ServiceName, startup $($svc.StartType))" } else { 'NOT INSTALLED' })"
    $ports = Get-ListeningPorts
    Info "listening:          $(if ($ports) { $ports -join ', ' } else { 'none' })"
    Info "HTTPS (Kestrel):    $($Health.https)"
    Info "health /healthz:    $($Health.liveness)"
    Info "health /health:     $($Health.readiness); media platform ready: $($Health.media_platform)"
    foreach ($p in $Health.problems) { Info "  - $p" }
    Info "firewall rules:     $((@(Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | ForEach-Object { $_.DisplayName }) -join '; '))"
    Step "STILL TO DO MANUALLY"
    $todo = New-Object System.Collections.Generic.List[string]
    foreach ($n in $script:Notes) { $todo.Add($n) }
    if ($Health.media_platform -ne $true) { $todo.Add("Media platform not ready: supply the settings listed under /health above and re-run.") }
    $httpsPortText = if ($Cfg.HttpsPort) { "$($Cfg.HttpsPort)" } else { "443 (the port of CallbackBaseUrl)" }
    $todo.Add("Azure NSG: allow inbound TCP $httpsPortText (HTTPS signalling) and TCP $($Cfg.MediaPort) (media) to this VM.")
    $todo.Add("DNS: $(if ($Cfg.Dns) { $Cfg.Dns } else { '<ServiceDnsName>' }) must resolve to the instance public IP $(if ($Cfg.Ip) { $Cfg.Ip } else { '<ip>' }).")
    $todo.Add("Azure Bot: Teams channel, calling enabled, webhook $(if ($Cfg.Callback) { $Cfg.Callback.TrimEnd('/') } else { 'https://<dns>' })/api/calling.")
    $todo.Add("Entra app: Calls.JoinGroupCall.All + Calls.AccessMedia.All (application) with admin consent.")
    $todo.Add("API: TEAMS_MEDIA_GATEWAY_URL=$(if ($Cfg.Callback) { $Cfg.Callback.TrimEnd('/') } else { 'https://<dns>' }) and TEAMS_MEDIA_GATEWAY_SECRET=<same as BackendSharedSecret>; API must be public https/wss.")
    foreach ($t in $todo) { Info "- $t" }
}

# ======================================================================================
# 1. OS / architecture
# ======================================================================================
Step "1. Operating system"
if (-not [Environment]::Is64BitProcess) {
    # Azure Run Command could start a 32-bit host: re-launch in 64-bit PowerShell.
    $ps64 = Join-Path $env:WINDIR "sysnative\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path $ps64) {
        Info "re-launching in 64-bit PowerShell"
        & $ps64 -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @PSBoundParameters
        exit $LASTEXITCODE
    }
    throw "64-bit PowerShell is required"
}
$os = Get-CimInstance Win32_OperatingSystem
$isServer = $os.ProductType -ne 1
$isX64 = $env:PROCESSOR_ARCHITECTURE -eq "AMD64"
Info "$($os.Caption) build $($os.BuildNumber), $($os.OSArchitecture), processor $env:PROCESSOR_ARCHITECTURE"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isX64) { if ($DryRun) { Warn "not x64 (the media library is win-x64 only)" } else { throw "x64 Windows is required (the media library is win-x64 only)" } }
if (-not $isServer) { if ($DryRun) { Warn "not Windows Server: application-hosted media needs Windows Server (dry-run continues)" } else { throw "Windows Server is required for application-hosted media" } }
if (-not $isAdmin -and -not $DryRun) { throw "run as SYSTEM (Azure Run Command) or an administrator" }

# ---- effective configuration: parameters > deployed values > GatewayOptions defaults ----
$existing = Get-ServiceEnvironment
function Pick([string]$param, [string]$name, [string]$default = "") {
    if ($param) { return $param }
    if ($existing.Contains($name) -and $existing[$name]) { return $existing[$name] }
    return $default
}
$cfg = @{}
$cfg.Secret = Pick $BackendSharedSecret "Gateway__BackendSharedSecret"
$cfg.AppId = Pick $AppId "Gateway__AppId"
$cfg.AppSecret = Pick $AppSecret "Gateway__AppSecret"
$cfg.Tenant = Pick $HomeTenantId "Gateway__HomeTenantId"
$cfg.Dns = Pick $ServiceDnsName "Gateway__ServiceDnsName"
$cfg.Thumb = (Pick $CertificateThumbprint "Gateway__CertificateThumbprint") -replace "[^0-9A-Fa-f]", ""
$cfg.Ip = Pick $InstancePublicIPAddress "Gateway__InstancePublicIPAddress"
$cfg.Callback = Pick $CallbackBaseUrl "Gateway__CallbackBaseUrl" $(if ($cfg.Dns) { "https://$($cfg.Dns)" } else { "" })
$cfg.PublicPort = [int](Pick $(if ($InstancePublicPort) { "$InstancePublicPort" } else { "" }) "Gateway__InstancePublicPort" "8445")
$cfg.MediaPort = [int](Pick $(if ($InstanceInternalPort) { "$InstanceInternalPort" } else { "" }) "Gateway__InstanceInternalPort" "8445")
$cfg.MaxCalls = Pick $(if ($MaxConcurrentCalls) { "$MaxConcurrentCalls" } else { "" }) "Gateway__MaxConcurrentCalls" ""
$cfg.LocalHttpPort = $LocalHttpPort
$cfg.HttpsEnabled = $existing.Contains("Kestrel__Endpoints__Https__Url")
$cfg.HttpsPort = $null
if ($cfg.Callback) {
    $u = $null
    if ([Uri]::TryCreate($cfg.Callback, [UriKind]::Absolute, [ref]$u) -and $u.Scheme -eq "https") { $cfg.HttpsPort = $u.Port }
    else { Warn "CallbackBaseUrl '$($cfg.Callback)' is not an https URL" }
}

if ($HealthCheckOnly) {
    $health = Invoke-HealthCheck $cfg
    Write-Summary $cfg $health
    return
}

# ======================================================================================
# 2. Windows features + .NET
# ======================================================================================
Step "2. Windows Media Foundation + ASP.NET Core runtime $AspNetCoreRuntimeVersion"
if ($isServer) {
    # The Teams media library needs Media Foundation, which is an optional feature on Windows Server
    # (Microsoft's Graph communications samples install it).
    $mf = Get-WindowsFeature -Name Server-Media-Foundation -ErrorAction SilentlyContinue
    if ($mf -and -not $mf.Installed) {
        if ($DryRun) { Plan "install Windows feature Server-Media-Foundation" }
        else {
            $res = Install-WindowsFeature -Name Server-Media-Foundation
            Info "Server-Media-Foundation installed (restart needed: $($res.RestartNeeded))"
            if ("$($res.RestartNeeded)" -eq "Yes") { Warn "Restart the VM once (Media Foundation), then re-run this script." }
        }
    } elseif ($mf) { Info "Server-Media-Foundation: installed" }
    else { Warn "Could not query Server-Media-Foundation" }
}
$installScript = Join-Path $InstallRoot "dotnet-install.ps1"
function Install-Dotnet([string[]]$DotnetArgs) {
    if (-not (Test-Path $installScript)) {
        Invoke-WebRequest -UseBasicParsing "https://dot.net/v1/dotnet-install.ps1" -OutFile $installScript
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installScript -InstallDir $DotnetDir -Architecture x64 -NoPath @DotnetArgs
    if ($LASTEXITCODE -ne 0) { throw "dotnet-install failed: $DotnetArgs" }
}
$haveRuntime = [bool](Get-DotnetRuntimes | Where-Object { $_ -like "Microsoft.AspNetCore.App $AspNetCoreRuntimeVersion *" })
if ($haveRuntime) { Info "ASP.NET Core ${AspNetCoreRuntimeVersion}: present in $DotnetDir" }
elseif ($DryRun) { Plan "install ASP.NET Core runtime $AspNetCoreRuntimeVersion (includes .NET runtime) to $DotnetDir" }
else {
    New-Item -ItemType Directory -Force $InstallRoot | Out-Null
    Install-Dotnet @("-Runtime", "aspnetcore", "-Version", $AspNetCoreRuntimeVersion)
    Info "installed ASP.NET Core runtime $AspNetCoreRuntimeVersion"
}

# ======================================================================================
# 3. Directories
# ======================================================================================
Step "3. Directories under $InstallRoot"
$dirs = @("app", "releases", "packages", "logs") | ForEach-Object { Join-Path $InstallRoot $_ }
foreach ($d in $dirs) { if ($DryRun) { if (-not (Test-Path $d)) { Plan "create $d" } } else { New-Item -ItemType Directory -Force $d | Out-Null } }
if (-not $DryRun) {
    # Only SYSTEM and Administrators (the service runs as LocalSystem).
    & icacls.exe $InstallRoot /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
}

# ======================================================================================
# 4. Package: download, (build if source), stage
# ======================================================================================
Step "4. Gateway package"
$appDir = Join-Path $InstallRoot "app"
$staged = $null
if (-not $PackageUrl -and -not $PackagePath) {
    if (Test-Path (Join-Path $appDir $ExeName)) { Info "no package given: keeping the deployed build" }
    else { throw "no gateway deployed yet: pass -PackageUrl or -PackagePath" }
} else {
    $zip = $PackagePath
    if ($PackageUrl) {
        if ($PackageUrl -notmatch "^https://") { throw "-PackageUrl must be https" }
        $zip = Join-Path $(if ($DryRun) { [IO.Path]::GetTempPath() } else { Join-Path $InstallRoot "packages" }) "gateway-$Stamp.zip"
        Info "downloading package (URL not printed: it may contain a SAS token)"
        Invoke-WebRequest -UseBasicParsing $PackageUrl -OutFile $zip
    }
    if (-not (Test-Path $zip)) { throw "package not found: $zip" }
    $extract = Join-Path $(if ($DryRun) { [IO.Path]::GetTempPath() } else { Join-Path $InstallRoot "releases" }) "extract-$Stamp"
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $exe = Get-ChildItem $extract -Recurse -Filter $ExeName | Select-Object -First 1
    $proj = Get-ChildItem $extract -Recurse -Filter "TeamsMediaGateway.csproj" | Select-Object -First 1
    if ($exe) {
        $staged = $exe.DirectoryName
        Info "published build found"
    } elseif ($proj) {
        Info "source found: building with .NET SDK $SdkVersion"
        if ($DryRun) { Plan "install SDK $SdkVersion and dotnet publish $($proj.Name)" }
        else {
            $dotnetExe = Join-Path $DotnetDir "dotnet.exe"
            $haveSdk = [bool](& $dotnetExe --list-sdks 2>$null | Where-Object { $_ -like "$SdkVersion *" })
            if (-not $haveSdk) { Install-Dotnet @("-Version", $SdkVersion) }
            $out = Join-Path $InstallRoot "releases\$Stamp"
            & $dotnetExe publish $proj.FullName -c Release -r win-x64 --self-contained false -o $out -nologo
            if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }
            @{ component = "teams-media-gateway"; commit = "unknown (built on VM from source zip)"; built_at_utc = (Get-Date).ToUniversalTime().ToString("o") } |
                ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $out "deploy-info.json")
            $staged = $out
        }
    } else { throw "package contains neither $ExeName nor TeamsMediaGateway.csproj" }
    if ($staged) {
        $di = Join-Path $staged "deploy-info.json"
        if (Test-Path $di) { Info "package commit: $((Get-Content $di -Raw | ConvertFrom-Json).commit)" }
        if (-not (Test-Path (Join-Path $staged "Microsoft.Skype.Bots.Media.dll"))) { Warn "Microsoft.Skype.Bots.Media.dll not in package: is it the win-x64 publish output?" }
    }
}

# ======================================================================================
# 5. Configuration (service environment; nothing secret in files)
# ======================================================================================
Step "5. Gateway configuration"
if (-not $cfg.Ip) {
    try {
        $cfg.Ip = Invoke-RestMethod -Headers @{ Metadata = "true" } -TimeoutSec 3 -Uri `
            "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text"
        if ($cfg.Ip) { Info "instance public IP from Azure IMDS: $($cfg.Ip) (verify it is the VM's instance-level public IP)" }
    } catch { }
    if (-not $cfg.Ip) { Warn "InstancePublicIPAddress not given and not reported by Azure IMDS (Standard SKU IPs are not): pass -InstancePublicIPAddress" }
}
if (-not $cfg.Secret -or $cfg.Secret.Length -lt 32) {
    throw "BackendSharedSecret is required (>= 32 characters; = API TEAMS_MEDIA_GATEWAY_SECRET). The gateway refuses to start without it."
}

# ---- 6. certificate / Kestrel (standard ASP.NET Core settings) ----
Step "6. HTTPS (Kestrel)"
$cert = $null
$script:CertTrusted = $false
if ($cfg.Thumb) {
    $cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Thumbprint -eq $cfg.Thumb.ToUpperInvariant() } | Select-Object -First 1
    if (-not $cert) { Warn "certificate $($cfg.Thumb) not found in LocalMachine\My: import it (with private key) and re-run" }
    else {
        $names = @($cert.DnsNameList | ForEach-Object { $_.Unicode })
        Info "certificate: $($cert.Subject), expires $($cert.NotAfter.ToString('yyyy-MM-dd')), private key: $($cert.HasPrivateKey)"
        if (-not $cert.HasPrivateKey) { Warn "certificate has no private key" }
        if ($cert.NotAfter -lt (Get-Date)) { Warn "certificate expired" }
        # Kestrel (AllowInvalid=false) refuses an untrusted certificate and the whole host would
        # then crash-loop, so HTTPS is only switched on for a certificate whose chain verifies.
        $script:CertTrusted = $cert.Verify()
        if (-not $script:CertTrusted) { Warn "certificate chain does not verify (self-signed / missing intermediate?): HTTPS left off. Teams needs a publicly trusted certificate." }
        if ($cfg.Dns -and -not ($names | Where-Object { $_ -eq $cfg.Dns -or ($_.StartsWith("*.") -and $cfg.Dns.EndsWith($_.Substring(1))) })) {
            Warn "certificate names ($($names -join ', ')) do not cover ServiceDnsName $($cfg.Dns)"
        }
    }
} else { Warn "no CertificateThumbprint: HTTPS and the media platform stay off (loopback diagnostics only)" }
$httpsOn = [bool]($cert -and $cert.HasPrivateKey -and $script:CertTrusted -and $cfg.Dns -and $cfg.HttpsPort)

$envMap = [ordered]@{
    "ASPNETCORE_ENVIRONMENT"                            = "Production"
    "DOTNET_ROOT"                                       = $DotnetDir
    "Logging__EventLog__LogLevel__Default"              = "Information"
    # loopback-only listener for health checks on the VM (never exposed; no firewall rule)
    "Kestrel__Endpoints__Loopback__Url"                 = "http://127.0.0.1:$LocalHttpPort"
    "Gateway__BackendSharedSecret"                      = $cfg.Secret
    "Gateway__AppId"                                    = $cfg.AppId
    "Gateway__AppSecret"                                = $cfg.AppSecret
    "Gateway__HomeTenantId"                             = $cfg.Tenant
    "Gateway__ServiceDnsName"                           = $cfg.Dns
    "Gateway__CertificateThumbprint"                    = $cfg.Thumb
    "Gateway__InstancePublicIPAddress"                  = $cfg.Ip
    "Gateway__InstancePublicPort"                       = "$($cfg.PublicPort)"
    "Gateway__InstanceInternalPort"                     = "$($cfg.MediaPort)"
    "Gateway__CallbackBaseUrl"                          = $cfg.Callback
}
if ($cfg.MaxCalls) { $envMap["Gateway__MaxConcurrentCalls"] = $cfg.MaxCalls }
if ($httpsOn) {
    # Kestrel selects the store certificate by subject; the thumbprint check above pins which one.
    $envMap["Kestrel__Endpoints__Https__Url"] = "https://*:$($cfg.HttpsPort)"
    $envMap["Kestrel__Endpoints__Https__Certificate__Subject"] = $cfg.Dns
    $envMap["Kestrel__Endpoints__Https__Certificate__Store"] = "My"
    $envMap["Kestrel__Endpoints__Https__Certificate__Location"] = "LocalMachine"
    $envMap["Kestrel__Endpoints__Https__Certificate__AllowInvalid"] = "false"
    Info "HTTPS on port $($cfg.HttpsPort) (from CallbackBaseUrl) for $($cfg.Dns)"
} else { Info "HTTPS not configured yet" }
$cfg.HttpsEnabled = $httpsOn
foreach ($k in $envMap.Keys) {
    $shown = if ($SecretNames -contains $k) { $(if ($envMap[$k]) { "<set>" } else { "<missing>" }) } elseif ($envMap[$k]) { $envMap[$k] } else { "<missing>" }
    Info ("{0,-52} {1}" -f $k, $shown)
}

# ======================================================================================
# 7. Firewall (only the gateway's own ports)
# ======================================================================================
Step "7. Windows Firewall"
$rules = @()
if ($httpsOn) { $rules += @{ Name = "$ServiceName-HTTPS"; Port = $cfg.HttpsPort; Label = "HTTPS signalling (Graph /api/calling, API /v1/calls)" } }
$rules += @{ Name = "$ServiceName-Media"; Port = $cfg.MediaPort; Label = "Teams media (InstanceInternalPort)" }
$keep = $rules | ForEach-Object { $_.Name }
foreach ($old in @(Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue)) {
    if ($keep -notcontains $old.Name) { if ($DryRun) { Plan "remove firewall rule $($old.DisplayName)" } else { Remove-NetFirewallRule -Name $old.Name } }
}
foreach ($r in $rules) {
    $display = "$FirewallGroup - $($r.Label) TCP $($r.Port)"
    if ($DryRun) { Plan "allow inbound TCP $($r.Port) ($($r.Label))"; continue }
    $existingRule = Get-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue
    if ($existingRule) { Remove-NetFirewallRule -Name $r.Name }
    New-NetFirewallRule -Name $r.Name -DisplayName $display -Group $FirewallGroup -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $r.Port -Profile Any | Out-Null
    Info "allowed inbound TCP $($r.Port) ($($r.Label))"
}

if ($DryRun) {
    Step "8-10. Service"
    Plan "stop $ServiceName, copy the new build to $appDir, write its environment (registry, ACL SYSTEM+Administrators)"
    Plan "register $ServiceName (LocalSystem, automatic delayed start, restart on failure 5s/10s/30s) and start it"
    Step "DRY RUN finished: nothing was changed."
    foreach ($n in $script:Notes) { Info "- $n" }
    return
}

# ======================================================================================
# 8. Install build + service registration
# ======================================================================================
Step "8. Windows service"
$binPath = "`"$(Join-Path $appDir $ExeName)`""
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne "Stopped") {
    Info "stopping service (active calls are left gracefully)"
    Stop-Service $ServiceName -Force
    (Get-Service $ServiceName).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
}
if ($staged) {
    if (Test-Path $appDir) {
        $backup = Join-Path $InstallRoot "releases\previous-$Stamp"
        Move-Item $appDir $backup
        Info "previous build kept in $backup"
    }
    Copy-Item $staged $appDir -Recurse
    Get-ChildItem (Join-Path $InstallRoot "releases") -Directory -Filter "extract-*" | Where-Object { $_.FullName -ne $staged } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Info "installed build into $appDir"
}
if (-not $svc) {
    New-Service -Name $ServiceName -BinaryPathName $binPath -DisplayName $DisplayName -StartupType Automatic `
        -Description "Listen-only Teams meeting audio -> CallCopilot API (POC). Audio is not stored." | Out-Null
    Info "service registered"
} else {
    & sc.exe config $ServiceName binPath= $binPath start= delayed-auto | Out-Null
}
& sc.exe config $ServiceName start= delayed-auto obj= LocalSystem | Out-Null
# Service-only environment (not machine-wide): readable by SYSTEM and Administrators only.
$lines = [string[]]@($envMap.Keys | Where-Object { $envMap[$_] } | ForEach-Object { "$_=$($envMap[$_])" })
New-ItemProperty -Path $ServiceKey -Name Environment -PropertyType MultiString -Value $lines -Force | Out-Null
$acl = Get-Acl $ServiceKey
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
    $id = New-Object Security.Principal.SecurityIdentifier $sid
    $acl.AddAccessRule((New-Object Security.AccessControl.RegistryAccessRule($id, "FullControl", "ContainerInherit", "None", "Allow")))
}
Set-Acl $ServiceKey $acl
Info "service environment written ($($lines.Count) settings)"

# Keep this script on the VM for later health checks / re-runs:
#   & "C:\CallCopilot\TeamsMediaGateway\deploy-azure-vm.ps1" -HealthCheckOnly
if ($PSCommandPath -and (Test-Path $PSCommandPath)) {
    Copy-Item $PSCommandPath (Join-Path $InstallRoot "deploy-azure-vm.ps1") -Force -ErrorAction SilentlyContinue
}

# ======================================================================================
# 9. Recovery
# ======================================================================================
Step "9. Restart on failure"
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
& sc.exe failureflag $ServiceName 1 | Out-Null
Info "restart after 5 s / 10 s / 30 s; counter resets daily; also on non-zero exit"

# ======================================================================================
# 10. Start
# ======================================================================================
Step "10. Start"
Start-Service $ServiceName
(Get-Service $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(60))
Info "running"

# ======================================================================================
# 11-12. Health + summary
# ======================================================================================
Step "11. Health check"
$health = Invoke-HealthCheck $cfg
if ($health.liveness -notlike "200*") {
    Warn "gateway did not answer /healthz; see Event Viewer > Windows Logs > Application (source TeamsMediaGateway / .NET Runtime)"
}
Write-Summary $cfg $health
$log = Join-Path $InstallRoot "logs\deploy-$Stamp.log"
"deployed $Stamp; liveness=$($health.liveness); readiness=$($health.readiness); https=$($health.https)" | Set-Content -Encoding UTF8 $log
if ($health.liveness -notlike "200*") { exit 2 }
