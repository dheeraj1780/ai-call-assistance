# Teams media gateway (C# / .NET 8)

| | Status (2026-09-25) |
|---|---|
| Build | **Compiles** with .NET SDK 8.0.425, `TreatWarningsAsErrors=true` |
| Tests | **33 passed**: `dotnet test tests/TeamsMediaGateway.Tests` |
| Local run | **Runs** on Windows 11. `/healthz` 200; a signed `/health` reports the missing settings; the API's own signed client authenticates |
| Media platform | With placeholder settings, Microsoft's SDK fails at start-up (`ServiceException: Media platform failed to initialize`). It needs a real certificate, an instance public IP and a Windows Server VM in Azure |
| Real Teams meeting | **NOT TESTED** |

## Why a separate .NET service

- Real-time Teams meeting audio is only available to **application-hosted media bots** using `Microsoft.Graph.Communications.Calls.Media`.
- That library is C#/.NET only and runs on Windows (Windows Server in Azure for production: Cloud Service, VM Scale Set, IaaS VM or AKS Windows nodes; not App Service).
- The Python API stays the business service.
- This gateway only:
  - joins meetings
  - receives audio
  - enforces the recording-status rule
  - forwards audio to the API

## Layout

```
TeamsMediaGateway.csproj        net8.0, win-x64
src/Program.cs, GatewayApp.cs   HTTP host + endpoints; degraded mode when media can't start
src/Core/                       SDK-free, unit-tested:
  GatewayOptions.cs             settings + validation (fatal vs media problems)
  Signing.cs                    HMAC contract with the API (same algorithm as the Python side)
  Contracts.cs                  JoinRequest / GatewayEvent / IMediaBot / UnavailableMediaBot
  CallSession.cs                lifecycle + compliance (declare recording before audio)
  AudioPipeline.cs              bounded queue (drop-oldest), reconnecting WebSocket sink
  BackendClient.cs              signed events with bounded retries
src/Teams/                      Microsoft SDK adapters:
  BotService.cs                 CommunicationsClient + media platform, join (Recvonly, Pcm16K,
                                unmixed), CallHandler (ICall/audio socket → CallSession)
  AuthenticationProvider.cs     MSAL token provider; SetAuthentication(...) makes the SDK's own
                                DefaultAuthenticationProvider validate inbound Microsoft JWTs
  JoinInfo.cs                   classic /l/meetup-join/ link → ChatInfo + OrganizerMeetingInfo
tests/TeamsMediaGateway.Tests   xUnit + ASP.NET Core TestServer + real local WebSocket server
```

**Package note.**
- `Microsoft.Graph.Communications.Calls.Media` is pinned to **1.2.0.17950** (published 2026-07-02).
- The newer **1.2.0.18725** (2026-09-03) cannot be restored from nuget.org. It depends on `Bond.Core.NET`, `Microsoft.Identity.ServerAuthorization` and `Microsoft.Skype.Bots.Media.Library`, which are not published there (checked 2026-09-25).
- Microsoft requires a media library version that is not older than about three months, so **upgrade by ~2026-10**.

## Contract with the CallCopilot API

**Signing.** Every API↔gateway request is signed with the shared secret (≥ 32 chars):
`X-CC-Timestamp` and `X-CC-Signature: v1=hex(HMAC-SHA256(secret, "{ts}." + body))`. The window is 5 minutes.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | liveness only (no details) |
| `GET /health` | signed | 200 ready / 503 with `problems`; used by the API connection test |
| `POST /v1/calls` | signed | join: `{call_id, tenant_id, join_url, media_ws_url, events_url, persistence, salesperson_aad_id, receive_only:true}` → `{gateway_call_id}` |
| `DELETE /v1/calls/{id}` | signed | leave |
| `POST /api/calling` | Microsoft JWT (validated by the SDK) | Graph call-signalling notifications |

**What the gateway sends back.**
- Events go to `events_url`, signed: `state`, `recording_status` and `media_status`, each with an `error_code`.
- Audio goes over the WSS `media_ws_url`, which carries a per-call HMAC token from the API (never logged):
  - `{"event":"start","format":{"encoding":"linear16","sample_rate":16000}}`
  - `{"event":"media","track":"agent|customer|mixed","seq":n,"payload":base64}`
  - `{"event":"stop"}`

## Reliability

- **Audio queue.**
  - At most `AudioQueueFrames` 20 ms frames are queued per call (default 250, about 5 s).
  - When the API socket is slow, the **oldest** frames are dropped and counted.
  - The media thread never blocks.
  - Malformed frames (empty, odd length, > 64 KB) are rejected and counted.
- **Socket reconnects.** A socket failure reconnects with linear backoff. After `MaxMediaReconnects` consecutive failures the pipeline stops trying and drops frames, keeping memory bounded. The Teams call is unaffected.
- **Events.** Events retry up to 3 times on network errors or 5xx. A 4xx is not retried. Event sending never throws into the call.
- **Logs.** `teams_media_session_started` and `teams_media_session_ended` report seconds and frames received, forwarded, dropped and malformed, plus reconnects. Audio, tokens and secrets are never logged.

## Configuration

**Gateway settings.** Use environment variables (`Gateway__*`) or a secret store. Never put secrets in `appsettings.json`.

| Setting | Required | Notes |
|---|---|---|
| `Gateway__BackendSharedSecret` | **yes (fatal)** | = API `TEAMS_MEDIA_GATEWAY_SECRET` |
| `Gateway__AppId`, `Gateway__AppSecret`, `Gateway__HomeTenantId` | media | Entra app / Azure Bot |
| `Gateway__ServiceDnsName`, `Gateway__CertificateThumbprint` | media | the cert lives in `LocalMachine\My` and matches the DNS name |
| `Gateway__InstancePublicIPAddress`, `Gateway__InstancePublicPort`, `Gateway__InstanceInternalPort` | media | instance-level public IP |
| `Gateway__CallbackBaseUrl` | media | `https://<dns>`; Graph calls `/api/calling` |
| `Gateway__MaxConcurrentCalls`, `Gateway__AudioQueueFrames`, `Gateway__MediaReconnectBackoffSeconds`, `Gateway__MaxMediaReconnects` | optional | limits |

**HTTP listener.** `ASPNETCORE_URLS` plus Kestrel HTTPS certificate settings (standard ASP.NET Core configuration).

## Build / test / run

```powershell
dotnet --version                       # 8.0.x
dotnet test tests/TeamsMediaGateway.Tests
$env:Gateway__BackendSharedSecret = "<same as API TEAMS_MEDIA_GATEWAY_SECRET>"
$env:ASPNETCORE_URLS = "http://127.0.0.1:9441"   # local only; HTTPS in Azure
dotnet run -c Release
```

**Deploying to the VM.** Use `deploy-azure-vm.ps1` (Azure Run Command, no RDP); see [AZURE-VM-DEPLOYMENT.md](AZURE-VM-DEPLOYMENT.md). The readiness checklist is in `docs/integrations/teams-call-copilot.md`.
