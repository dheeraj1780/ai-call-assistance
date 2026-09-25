# Teams media gateway on an Azure Windows VM (POC)

`deploy-azure-vm.ps1` installs and runs the existing gateway on the VM through **Azure Run
Command**, with no RDP. It does not make Teams calling work by itself.

| | Status (2026-09-25) |
|---|---|
| Script tested on the dev PC (Windows 11) | `-Package` build; `-DryRun`; health check (`/healthz`, signed `/health`, wrong secret → 401) against the packaged build; Kestrel HTTPS from a certificate-store certificate through the same environment settings |
| Script run on the Azure VM | **NOT RUN YET** |
| Real Teams call | **NOT TESTED** |

## What decides the deployment (from the repository)

| Source | Determines |
|---|---|
| `TeamsMediaGateway.csproj` | `net8.0`, `win-x64`, x64 only. Media SDK `1.2.0.17950`, so the VM needs the **ASP.NET Core 8 runtime** (tested with 8.0.31; SDK 8.0.425 only if building on the VM). Package `Microsoft.Extensions.Hosting.WindowsServices` lets it run as a Windows service |
| `src/Core/GatewayOptions.cs` | All `Gateway__*` settings. Only `BackendSharedSecret` (≥ 32 chars) is required to start. Missing media settings leave the gateway running in **degraded** mode, and `/health` lists them. Media ports default to **8445** |
| `src/Teams/BotService.cs` | The media platform uses `CertificateThumbprint` (LocalMachine\My), `ServiceDnsName`, `InstancePublicIPAddress` and `InstancePublic/InternalPort`. Signalling URL = `CallbackBaseUrl` + `/api/calling`. Joins are **receive-only**, unmixed PCM 16 kHz |
| `src/GatewayApp.cs`, `src/Program.cs` | Listeners come from standard ASP.NET Core `Kestrel` configuration. Endpoints: `/healthz` (open), `/health`, `/v1/calls`, `DELETE /v1/calls/{id}` (HMAC-signed), `/api/calling` (Microsoft JWT). `UseWindowsService` (service name `CallCopilotTeamsMediaGateway`) |
| `src/Core/Signing.cs` | Signed health check: `X-CC-Signature: v1=hex(HMAC-SHA256(secret, "{ts}." + body))` |
| `appsettings.json` | Non-secret defaults only |

**The HTTPS port is the port of `CallbackBaseUrl`.** For `https://<dns>` that is **443**. Microsoft Graph and the Azure Bot webhook call `https://<dns>/api/calling`, and the API calls `https://<dns>/v1/calls`, so the gateway has no separate signalling-port setting.

## What the script does

It is idempotent: re-run it at any time. Settings you leave out keep their deployed values.

1. **Checks the machine.** It requires Windows Server and x64, re-launches in 64-bit PowerShell if needed, and must run as SYSTEM or an administrator.
2. **Installs prerequisites.** It installs the Windows feature `Server-Media-Foundation`, which Microsoft's Graph communications samples install. The feature may ask for one restart: restart, then re-run. It then installs the **ASP.NET Core runtime 8.0.31** to `C:\Program Files\dotnet` with Microsoft's `dotnet-install.ps1`. If the package is source, it also installs **SDK 8.0.425**.
3. **Creates the folders.** `C:\CallCopilot\TeamsMediaGateway\{app,releases,packages,logs}`, with an ACL that allows SYSTEM and Administrators only.
4. **Installs the build.** It downloads the package, which is either the `-Package` zip or a source zip (built on the VM), and keeps the previous build in `releases\`.
5. **Writes the configuration.** Settings go into the **service's own environment** (registry `...\Services\CallCopilotTeamsMediaGateway\Environment`, readable by SYSTEM and Administrators only):
   - all the `Gateway__*` settings
   - `ASPNETCORE_ENVIRONMENT=Production`
   - `DOTNET_ROOT`
   - Information-level logging to the Event Log

   It writes no files containing secrets, and secrets are never printed.
6. **Configures Kestrel.**
   - It always adds a **loopback-only** diagnostics listener on `http://127.0.0.1:9441`, used for health checks and never exposed.
   - It adds HTTPS on the `CallbackBaseUrl` port, with the certificate from `LocalMachine\My`, **only if** the certificate exists, has a private key, and its chain verifies. Otherwise Kestrel would refuse the certificate and the service would crash-loop.
7. **Opens Windows Firewall.** Inbound TCP on the HTTPS port and TCP on `InstanceInternalPort` (8445), and nothing else.
8. **Registers the service.** Windows service `CallCopilotTeamsMediaGateway`, running as LocalSystem (it needs the certificate's private key and the media platform), with automatic delayed start. Stopping the service is graceful: the bot leaves active calls.
9. **Sets recovery.** Restart after 5 s, 10 s and 30 s; the counter resets daily; restart also on a non-zero exit.
10. **Starts the gateway.**
11. **Runs the health check.**
    - `http://127.0.0.1:9441/healthz`
    - signed `/health` (media platform ready, plus the list of problems)
    - HTTPS `https://<dns>:<port>/healthz`, resolved to 127.0.0.1 with **real certificate validation**
12. **Prints a summary.** It covers:
    - .NET runtimes
    - gateway commit (`deploy-info.json`)
    - service status
    - listening ports
    - HTTPS
    - health results
    - what is still manual

   It also copies itself to `C:\CallCopilot\TeamsMediaGateway\deploy-azure-vm.ps1` for later health checks.

**Unchanged by the script:**
- Receive-only joins
- Declared recording before any audio
- Audio forwarded in memory only (bounded queue); raw audio is never written to disk
- The API, Plivo and the main application

## Prerequisites

- **The VM.** Windows Server 2025 Datacenter: Azure Edition, Standard_D4als_v7 (4 vCPU / 8 GB), running.
- **A public IP on the VM's NIC.** This is the instance-level public IP Microsoft requires; use a static IP. Standard-SKU IPs are not reported by the Azure metadata service, so pass `-InstancePublicIPAddress`.
- **A DNS name for that IP.** For example, the public IP's DNS label `<label>.<region>.cloudapp.azure.com`, or your own domain with an A record.
- **A publicly trusted TLS certificate for that DNS name**, with a private key, in `LocalMachine\My` (see the next section).
- **The Azure CLI**, logged in with rights to run commands on the VM and to write to a storage account for the package.
- **The Entra app and Azure Bot** (the Microsoft steps below). These can be done before or after the first deployment.

## The certificate

- It must be issued by a **public CA**. Teams will not accept self-signed certificates, and the script leaves HTTPS off for any chain that does not verify.
- Its subject or SAN must be the `ServiceDnsName`. A wildcard is accepted.
- It must include the private key.
- It must be installed in **`Cert:\LocalMachine\My`**.
- The same certificate serves:
  - Kestrel HTTPS (signalling)
  - the media platform (`CertificateThumbprint`)

**Getting it onto the VM without RDP.** Choose one; this is not automated here.
- **Recommended:** store it in Azure Key Vault and install the **Key Vault VM extension** with `certificateStoreLocation = LocalMachine` and `certificateStoreName = My`. The extension also handles renewal.
- **Or** upload a PFX to a private blob and run:

  ```powershell
  az vm run-command invoke -g <rg> -n <vm> --command-id RunPowerShellScript --scripts '
    param($u,$p) Invoke-WebRequest -UseBasicParsing $u -OutFile C:\Windows\Temp\gw.pfx
    Import-PfxCertificate C:\Windows\Temp\gw.pfx -CertStoreLocation Cert:\LocalMachine\My -Password (ConvertTo-SecureString $p -AsPlainText -Force) | Select Thumbprint, Subject
    Remove-Item C:\Windows\Temp\gw.pfx' --parameters "u=<pfx SAS URL>" "p=<pfx password>"
  ```

  `invoke` parameters appear in the run-command history. Rotate the PFX password afterwards, or use the Key Vault extension.

## Required settings

| Script parameter | Gateway setting | Secret | Where it comes from |
|---|---|---|---|
| `-BackendSharedSecret` | `Gateway__BackendSharedSecret` | **yes** | Generate ≥ 32 random characters. The same value goes into the API's `TEAMS_MEDIA_GATEWAY_SECRET`. **Required** |
| `-AppId` | `Gateway__AppId` | no | Entra application (client) ID of the bot |
| `-AppSecret` | `Gateway__AppSecret` | **yes** | Client secret of that app |
| `-HomeTenantId` | `Gateway__HomeTenantId` | no | Your Entra tenant ID |
| `-ServiceDnsName` | `Gateway__ServiceDnsName` | no | DNS name of the VM (it must match the certificate) |
| `-CertificateThumbprint` | `Gateway__CertificateThumbprint` | no | Thumbprint of the certificate in LocalMachine\My |
| `-InstancePublicIPAddress` | `Gateway__InstancePublicIPAddress` | no | Static public IP of the VM's NIC |
| `-CallbackBaseUrl` | `Gateway__CallbackBaseUrl` | no | Default `https://<ServiceDnsName>`. Its port is the HTTPS port |
| `-InstancePublicPort` / `-InstanceInternalPort` | same names | no | Default 8445 / 8445 |
| `-PackageUrl` | – | the SAS URL grants read access | The package zip (next section) |

## Deploy

**1. Build the package on the dev PC.** The branch is not on GitHub, so the VM cannot clone it.

```powershell
cd teams-media-gateway
powershell -ExecutionPolicy Bypass -File .\deploy-azure-vm.ps1 -Package -OutputZip .\artifacts\gateway.zip
```

The output (framework-dependent win-x64, about 18 MB) includes `deploy-info.json` with the git commit.

**2. Upload the package and create a short-lived read-only link.**

```bash
az storage blob upload --account-name <sa> -c deploy -n gateway.zip -f teams-media-gateway/artifacts/gateway.zip --auth-mode login
az storage blob generate-sas --account-name <sa> -c deploy -n gateway.zip --permissions r --expiry <UTC+2h> --https-only --as-user --auth-mode login -o tsv
```

**3. Run the script on the VM with Azure Run Command.** Use a managed Run Command. Secrets go in `--protected-parameters`, which are encrypted and not returned by `show`.

```bash
az vm run-command create -g <rg> --vm-name <vm> --name deploy-teams-gateway \
  --script @teams-media-gateway/deploy-azure-vm.ps1 \
  --parameters PackageUrl="https://<sa>.blob.core.windows.net/deploy/gateway.zip?<sas>" \
               ServiceDnsName=<dns> InstancePublicIPAddress=<ip> CertificateThumbprint=<thumbprint> \
               AppId=<app-guid> HomeTenantId=<tenant-guid> \
  --protected-parameters BackendSharedSecret=<secret> AppSecret=<client-secret> \
  --timeout-in-seconds 3600

az vm run-command show -g <rg> --vm-name <vm> --name deploy-teams-gateway --instance-view \
  --query "instanceView.{state:executionState, exit:exitCode, output:output, error:error}"
```

- **A first deployment can run before the Microsoft side is ready.** With only `PackageUrl` and `BackendSharedSecret`, the gateway runs degraded, and the summary lists what is missing.
- **Re-running.** Use `az vm run-command update` (or delete, then create) with only the settings that change; the others are kept. Without `PackageUrl`, the deployed build is kept.

## Verify after deployment

**Health check on the VM:**

```bash
az vm run-command invoke -g <rg> -n <vm> --command-id RunPowerShellScript \
  --scripts "& 'C:\CallCopilot\TeamsMediaGateway\deploy-azure-vm.ps1' -HealthCheckOnly"
```

What to expect:
- `service: Running`
- `health /healthz: 200 alive`
- `health /health: HTTP 200; media platform ready: True`. HTTP 503 lists the problems instead.
- `HTTPS (Kestrel): OK (valid certificate for <dns>, port 443)`
- listening ports: TCP 443, 127.0.0.1:9441 and the media port

**From outside, after the NSG and DNS are set up:**
- `curl https://<dns>/healthz` should return `{"status":"alive"}`.
- From the CallCopilot API, **Integrations → Microsoft Teams → Test Connection** should show "gateway reachable". This check uses the signed `/health`.

**Logs.** Event Viewer → Application. Read them without RDP:

```powershell
Get-WinEvent -LogName Application -MaxEvents 50 | Where-Object ProviderName -match 'TeamsMediaGateway|.NET Runtime' | Format-List TimeCreated, Message
```

Run this through `run-command invoke`. It shows `teams_media_platform_started` or `teams_media_unavailable reasons=...`, and `teams_media_session_*` counters. Logs never contain audio, tokens or secrets.

## Azure NSG: inbound ports

| Port | Protocol | Why | Source |
|---|---|---|---|
| **443** (the port of `CallbackBaseUrl`) | TCP | HTTPS signalling: Microsoft Graph → `/api/calling`, CallCopilot API → `/v1/calls`, `/health` | Internet (Microsoft Graph has no small fixed range) |
| **8445** (`InstancePublicPort`) | TCP | Teams media to the media platform (`InstanceInternalPort` on the VM) | Internet |

- Nothing else is opened by this repository's configuration: port 9441 is loopback-only.
- **Not verified here:** Microsoft's current requirements for application-hosted media bots may list additional media ports for the SDK version in use. Check Microsoft's "application-hosted media bot" requirements before the first call, and add only what they require.
- **RDP (3389) is not needed** for this procedure.

## Remaining Microsoft Entra / Azure Bot steps (manual)

1. **Entra app registration.** Single-tenant is fine. Create a client secret: this is `AppId` / `AppSecret`.
2. **API permissions (Application):** `Calls.JoinGroupCall.All` and `Calls.AccessMedia.All`. Then **grant admin consent**, either in the Entra portal or through CallCopilot → Integrations → "Grant admin consent (calling)".
3. **Azure Bot resource.**
   - Microsoft App ID = that app.
   - **Channels → Microsoft Teams:** enable, with **Calling** turned on and the webhook `https://<dns>/api/calling`.
4. **Teams policy.** The meeting's lobby settings must let the bot join. For a tenant with application access restrictions, an application access policy may also be required.
5. **The CallCopilot API must be publicly reachable over HTTPS/WSS.**
   - The gateway posts events to it and streams audio to `wss://<api>/api/v1/telephony/media/teams/...`.
   - The main application is **not deployed**. This is a blocker for the first real call, and the decision is yours (for example, deploy the API, or expose a dev API through a tunnel).
   - Set on the API: `TEAMS_MEDIA_GATEWAY_URL=https://<dns>`, `TEAMS_MEDIA_GATEWAY_SECRET=<BackendSharedSecret>`, `PUBLIC_BASE_URL=https://<api>`, `STT_PROVIDER=google` and the Google settings.
6. **In CallCopilot:** Integrations → Microsoft Teams → mode **Live**, with the tenant ID, app ID and secret. Then **Test Connection** should show ✓ credentials, ✓ calling permissions and ✓ gateway reachable. Then **Enable Real-time Call Copilot**.

## First real Teams test

The copilot is listen-only, and all participants see the Teams recording indicator.

1. **Deploy the gateway,** then confirm the `-HealthCheckOnly` results listed above: media platform ready, and HTTPS OK.
2. **Confirm Test Connection** in CallCopilot shows all three ✓.
3. **Schedule a Teams meeting.** Use a classic `https://teams.microsoft.com/l/meetup-join/...` link, and allow the bot through the lobby.
4. **In CallCopilot:** plan a **Teams call** for a test contact, paste the meeting link, and choose language `en-IN`. You are the salesperson; do "Connect my Teams account" so your audio is labelled correctly.
5. **Join the meeting** yourself, with a colleague acting as the customer. Click **Start** on the live screen. Expect:
   - "● Live", then "Connected", then "● Receiving transcript"
   - transcript lines with the correct speakers
   - copilot cards and agenda progress
6. **Speak the 7-line test conversation** (see `docs/integrations/teams-call-copilot.md`). Then end the meeting, or click End. Expect "Finalising transcript…", then Completed, then the post-call summary.
7. **Collect the evidence:**
   - gateway Event Log: `teams_meeting_joined` and `teams_media_session_started/ended` (frames received / forwarded / dropped)
   - API logs: `stt_session_ended`, `transcript_final` (latencies) and `live_session_closed`
8. **Record the result** in `PROJECT_STATUS.md`. Only then may Teams calling be described as tested.

## Known limitations (POC)

- **One VM, LocalSystem service account, no scale-out and no load balancer.** It handles `MaxConcurrentCalls` calls (default 4).
- **The media SDK `1.2.0.17950` must be upgraded by about 2026-10.** Microsoft's freshness rule applies; see the README.
- **Certificate renewal is not handled by the script.** Use the Key Vault VM extension, and re-run the script if the thumbprint changes.
- **Secrets sit in the service registry environment**, readable by SYSTEM and Administrators. A secret store such as Key Vault is the production path.
