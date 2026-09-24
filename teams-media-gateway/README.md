# Teams media gateway (C# / .NET)

**Status: SOURCE ONLY. NOT BUILT, NOT RUN, NOT VERIFIED.** There was no .NET SDK on the
development machine and there is no Azure/Windows infrastructure yet. Treat the code as a
complete first draft to compile, fix and verify on the real target.

## Why this separate service exists

Real-time audio from Teams meetings is only available to **application-hosted media bots** built
with Microsoft's `Microsoft.Graph.Communications.Calls.Media` library. That library is C#/.NET only
and runs only on **Windows Server in Azure** (Cloud Service, VM Scale Set, IaaS VM or AKS Windows
nodes; not Azure App Service/Web Apps). A normal FastAPI WebSocket cannot receive Teams media.
That is why the gateway is separate. The Python API stays the business service, and the AI copilot is not moved into .NET.

```
Microsoft Teams meeting
      │  (Graph Communications signalling + real-time media, Windows VM with public IP)
      ▼
teams-media-gateway (.NET)  ── joins meeting listen-only, unmixed 16 kHz PCM in memory
      │  signed HTTPS events  POST /api/v1/integrations/teams/gateway/events
      │  audio WebSocket      wss://…/api/v1/telephony/media/teams/{call_id}?token=…
      ▼
CallCopilot API (FastAPI) ── same live pipeline as phone calls: STT → copilot → live UI
```

## Contract with the CallCopilot API

All API↔gateway HTTP requests are signed with `TEAMS_MEDIA_GATEWAY_SECRET` (≥ 32 chars):
`X-CC-Timestamp` (unix seconds) and `X-CC-Signature: v1=hex(HMAC-SHA256(secret, "{ts}." + body))`.
Requests more than 5 minutes old are rejected.

| Direction | Request | Purpose |
|---|---|---|
| API → gateway | `GET /health` | connection test (signed, empty body) |
| API → gateway | `POST /v1/calls` `{call_id, tenant_id, join_url, media_ws_url, events_url, persistence, salesperson_aad_id, receive_only: true}` → `{gateway_call_id}` | join a meeting |
| API → gateway | `DELETE /v1/calls/{gateway_call_id}` | leave |
| gateway → API | `POST events_url` `{event_id, call_id, gateway_call_id, state?, recording_status?, error_code?}` | `state`: ESTABLISHING / ESTABLISHED / TERMINATED / FAILED. `recording_status`: RECORDING_CONFIRMED / RECORDING_FAILED |
| gateway → API | WebSocket `media_ws_url` (per-call HMAC token in the URL) | `{"event":"start","format":{"encoding":"linear16","sample_rate":16000}}`, `{"event":"media","track":"agent|customer|mixed","seq":n,"payload":base64}`, `{"event":"stop"}` |
| Microsoft → gateway | `POST /api/calling` | Graph call-signalling notifications (Microsoft-signed JWT validated) |

## Compliance: recording and persistence

Microsoft's policy says an app may not record or persist media, or data derived from it, until
it has called `updateRecordingStatus` and received a success response. The gateway and API do this as follows:

- **TRANSIENT (default):** audio is forwarded for in-memory processing only. The API stores no transcript, no AI notes, no agenda changes and no post-call summary for the call (`calls.transcript_persistence = TRANSIENT`).
- **RECORDING_DECLARED (opt-in per company):** after the call is established, the gateway calls `UpdateRecordingStatusAsync(Recording)`. All participants then see the Teams recording indicator. Only after it succeeds does the gateway report `RECORDING_CONFIRMED` and start forwarding audio. The API then stores the transcript (retention-controlled). If the call fails, the gateway reports `RECORDING_FAILED` and forwards no audio.
- **Always:** raw audio is never written to disk, and the bot is receive-only, so the AI never speaks.

Whether this configuration satisfies your legal obligations (consent, notices, DPDP Act) is
**REQUIRES LEGAL REVIEW**.

## Prerequisites (you create these; nothing is created automatically)

1. **Entra app registration** (can be the same app as Teams messaging), with a client secret.
2. **Azure Bot resource** using that app id, with the **Microsoft Teams channel** enabled and **Calling** enabled. Set the webhook to `https://<ServiceDnsName>/api/calling`.
3. **Application permissions with admin consent:** `Calls.JoinGroupCall.All` and `Calls.AccessMedia.All`. The CallCopilot "Grant admin consent" button opens the consent page.
4. **A Windows Server VM** (≥ 2 cores; Microsoft recommends a Dv2-series or ≥ 4 vCPU) with these:
   - an **instance-level public IP**
   - a public DNS name
   - a TLS certificate for that name, installed in `LocalMachine\My`
   - inbound TCP open for the signalling port (default 9441) and the media port (default 8445)
5. **.NET 8 SDK** to build. Use a **current** version of `Microsoft.Graph.Communications.Calls.Media`: Microsoft deprecates versions older than about three months.
6. **In the CallCopilot API environment:** `TEAMS_MEDIA_GATEWAY_URL=https://<ServiceDnsName>:9441` and `TEAMS_MEDIA_GATEWAY_SECRET`, the same value as `Gateway__BackendSharedSecret`.
7. **A real streaming speech-to-text provider** in the API. The mock STT cannot transcribe real audio. This is not included yet.

## Configuration (environment variables)

`Gateway__AppId`, `Gateway__AppSecret`, `Gateway__ServiceDnsName`, `Gateway__CertificateThumbprint`,
`Gateway__InstancePublicIPAddress`, `Gateway__InstancePublicPort`, `Gateway__InstanceInternalPort`,
`Gateway__CallSignalingPort`, `Gateway__CallbackBaseUrl`, `Gateway__BackendSharedSecret`,
`Gateway__MaxConcurrentCalls`.

## Build / run (on the Windows VM)

```powershell
dotnet restore
dotnet build -c Release
dotnet run -c Release
```

## Known gaps before first real use

- Never compiled. Expect API adjustments against the SDK version you install. Also pin package versions.
- `JoinInfo.Parse` supports the classic `meetup-join` link format only. Newer short links (`teams.microsoft.com/meet/<id>?p=`) need resolving through Graph `onlineMeetings` (`OnlineMeetings.Read.All`), which is not implemented.
- Speaker attribution needs the salesperson's Entra user id. The API passes it when that user has connected Teams messaging; otherwise audio is labelled `mixed`.
- Single VM, and calls are pinned to it. There is no scale-out or draining logic yet.
- Guest/anonymous meeting join and lobby admission policies depend on the tenant's Teams settings.
