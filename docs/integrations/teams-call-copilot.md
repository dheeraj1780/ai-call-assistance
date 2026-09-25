# Teams real-time call copilot (primary POC capability)

## What works today, and what does not

| Part | Status (2026-09-25) |
|---|---|
| API: Teams calls, gateway contract, transcript persistence rules, live pipeline, copilot | IMPLEMENTED · tested (backend suite) |
| Google Speech-to-Text adapter | IMPLEMENTED · unit-tested. **Real Google VERIFIED** (en-IN, en-US; TTS test audio) |
| Complete local pipeline with real Google: real-time-paced audio → Google STT → transcript → copilot (agenda, missing questions, requirements, objections, notes) → live screen → end of call → post-call | **VERIFIED 2026-09-25** with the LOCAL DEVELOPMENT AUDIO SOURCE (`backend/scripts/local_audio_call.py`). **Not Teams media** |
| .NET media gateway (`teams-media-gateway/`) | **COMPILES** (.NET 8.0.425, warnings as errors). **33 tests pass**. **RUNS locally in degraded mode** |
| Gateway media platform on this dev PC | Microsoft's media SDK was started with placeholder settings and failed with `ServiceException: Media platform failed to initialize`. It needs the real certificate, public IP and Windows Server VM |
| Synthetic end-to-end (synthetic PCM → Google adapter → copilot → live events) | TESTED. **A development test, not a Teams call** |
| Live screen states: connecting/reconnecting, receiving/delayed transcript, STT unavailable, Teams media unavailable, copilot analysing, call ended | IMPLEMENTED · frontend tests |
| **A real Teams meeting** | **NOT TESTED.** Needs Azure/Teams setup and credentials (checklist below) |

## Architecture

```
Teams meeting ──Graph calling signalling──▶ gateway /api/calling (Microsoft JWT validated by the SDK)
      │  real-time media (application-hosted, Windows Server VM, instance public IP, TLS cert)
      ▼
teams-media-gateway (.NET 8, Microsoft.Graph.Communications.Calls.Media 1.2.0.17950)
  - joins listen-only (Recvonly), Pcm16K, unmixed per-speaker audio
  - CallSession: lifecycle + compliance (declare recording BEFORE any audio when storage is on)
  - AudioPipeline: bounded queue, oldest-dropped, reconnecting WebSocket to the API
      │ signed HTTPS events: ESTABLISHING/ESTABLISHED/TERMINATED/FAILED, RECORDING_*, media AVAILABLE/UNAVAILABLE
      │ WSS audio: /api/v1/telephony/media/teams/{call}?token=<per-call HMAC>
      ▼
CallCopilot API (FastAPI) - owns everything else
  MediaIngest → LiveSession (per track) → SpeechToTextProvider (Google Chirp 3) → transcript events
  → CopilotEngine (agenda, requirements, objections, notes, knowledge, LLM) → live hub → React
  persistence (retention, RLS tenant isolation, audit) only when the call is PERSISTED
```

The gateway contains **no** business, CRM, AI or prompting logic. The API does not touch Teams media.

## Compliance rules (enforced in code)

- **Transient (default).**
  - The gateway forwards audio and the API processes it in memory.
  - No transcript, notes, cards, agenda changes or post-call summary are stored.
  - The UI shows "Transient mode".
- **Store transcript & AI notes (per company).**
  1. The gateway calls `UpdateRecordingStatusAsync(Recording)` **before** it opens the media socket. All participants then see Teams' recording indicator.
  2. Only after that succeeds does the gateway send `RECORDING_CONFIRMED`. The call then becomes `PERSISTED`.
  3. If it fails, the gateway reports `RECORDING_FAILED` / media `UNAVAILABLE` and forwards **no audio**. The UI explains why.
- **Always:**
  - Raw audio is never written to disk, by either the gateway or the API.
  - The bot never speaks: `receive_only` is mandatory.

## Environment variables

**API (`backend/.env`):**
- `TEAMS_MEDIA_GATEWAY_URL`: the gateway's HTTPS base URL.
- `TEAMS_MEDIA_GATEWAY_SECRET`: at least 32 characters, shared with the gateway.
- `PUBLIC_BASE_URL`: public HTTPS URL of the API. The gateway calls it back and opens WSS to it.
- `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET`, or per-company values: for the connection test and admin consent.
- `STT_PROVIDER=google`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_STT_LOCATION`, `STT_LANGUAGE`: see [google-stt.md](google-stt.md).

**Gateway** (environment variables on the Windows VM; see `teams-media-gateway/README.md`):
- `Gateway__AppId`, `Gateway__AppSecret`, `Gateway__HomeTenantId`
- `Gateway__ServiceDnsName`, `Gateway__CertificateThumbprint`
- `Gateway__InstancePublicIPAddress`, `Gateway__InstancePublicPort`, `Gateway__InstanceInternalPort`
- `Gateway__CallbackBaseUrl`
- `Gateway__BackendSharedSecret`: the same value as `TEAMS_MEDIA_GATEWAY_SECRET`
- `Gateway__MaxConcurrentCalls`, `Gateway__AudioQueueFrames`, `Gateway__MediaReconnectBackoffSeconds`, `Gateway__MaxMediaReconnects`
- `ASPNETCORE_URLS` and the Kestrel HTTPS certificate configuration

## Local development

1. **Backend and frontend only (no Teams, no Google).**
   - Integrations → Microsoft Teams → Mode **Mock** → Test → enable Real-time Call Copilot.
   - Plan a Teams call on a contact and choose the language.
   - On the live screen: Start → **Simulate conversation (mock)**.
   - This uses the mock gateway and mock STT; it shows the UI and the copilot.
2. **Synthetic media through the real Google adapter (automated).**
   - Run `uv run pytest tests/test_teams_stt_e2e.py`. It sends synthetic 16 kHz PCM through the real media ingest and the real `GoogleSpeechToTextProvider`, with a scripted fake Google transport, then through the live pipeline and copilot, and checks the live events.
   - Its tests cover:
     - declared-recording persistence
     - transient mode storing nothing
     - media unavailable
     - an STT outage during a call
   - **Label: development / synthetic media. Not a Teams call.**
3. **Real Google STT, no Teams:** `scripts/google_stt_smoke.py` with your credentials and a 16 kHz mono WAV.
   **The complete local call copilot with real Google STT (LOCAL DEVELOPMENT AUDIO SOURCE – not Teams):**
   - API with `STT_PROVIDER=google` on :8000, frontend on :5173.
   - `cd backend && uv run python scripts/local_audio_call.py --email <you> --password <pw> --setup --dialogue <dir>/dialogue.json --start-delay 20 --report report.json`
   - `--setup` puts Microsoft Teams in **Mock** mode with recording declared, creates a test contact and a Teams call with an 8-item agenda, and starts it. `--call <id>` attaches to a call you started in the UI instead.
   - `dialogue.json` lists 16 kHz mono 16-bit WAVs, each with an explicit speaker (`agent` / `customer`), like the gateway's unmixed tracks. `--mic --track customer --seconds 30` streams a microphone instead (`uv pip install sounddevice`).
   - Audio is sent as 20 ms frames on a real-time schedule through the same media WebSocket and `MediaIngest` the gateway uses (development endpoint `POST /api/v1/calls/{id}/dev/audio-source`: mock providers only, simulation enabled, never in production). Nothing is uploaded or stored.
   - Open the printed live-call URL. The script prints every event, then ends the call, waits for `session.phase = closed` and writes the report (per-final user-visible latency, copilot time, end-of-call phases, agenda, notes, post-call).
4. **The gateway locally (Windows):**
   1. `cd teams-media-gateway`
   2. `dotnet test tests/TeamsMediaGateway.Tests`
   3. `dotnet run`, with `Gateway__BackendSharedSecret` set.

   `/healthz` → 200. A signed `/health` → 503 with the list of missing settings. `/v1/calls` → 503 until the media platform starts. Without Azure and a certificate, the gateway **cannot** join a meeting.

## Real Teams test readiness checklist

| # | Item | Status | Who |
|---|---|---|---|
| 1 | **Entra app registration** (single-tenant is fine for the POC) with a client secret | not done | you |
| 2 | **Application permissions** `Calls.JoinGroupCall.All`, `Calls.AccessMedia.All`, plus **admin consent** (use "Grant admin consent (calling)" in Integrations) | not done | you (tenant admin) |
| 3 | **Azure Bot** resource using that app id; **Microsoft Teams channel** added; **calling enabled** with webhook `https://<gateway-dns>/api/calling` | not done | you |
| 4 | **Windows Server VM in Azure** (≥ 2 vCPU; Dv2 or 4 vCPU recommended) with an **instance-level public IP** and a DNS name | not done | you |
| 5 | **TLS certificate** for that DNS name, installed in `LocalMachine\My`; thumbprint set in `Gateway__CertificateThumbprint`; Kestrel HTTPS on the signalling port | not done | you |
| 6 | Inbound NSG/firewall rules: signalling port (HTTPS) and media port (default 8445 TCP, plus the media port range Microsoft documents) | not done | you |
| 7 | .NET 8 runtime on the VM, and the gateway published and running (`dotnet publish -c Release -r win-x64`) | not done | you, or me when asked |
| 8 | **Media library freshness:** pinned `1.2.0.17950` (2026-07-02). Microsoft requires ≤ ~3 months old, so **upgrade by ~2026-10**. Newer `1.2.0.18725` currently has dependencies that are missing on nuget.org | action | me/you |
| 9 | **API publicly reachable over HTTPS** (`PUBLIC_BASE_URL`), including WSS for `/api/v1/telephony/media/teams/...` | not done (not deployed) | deployment step |
| 10 | `TEAMS_MEDIA_GATEWAY_URL` / `TEAMS_MEDIA_GATEWAY_SECRET` on the API, and the same secret on the gateway | not done | you |
| 11 | **Google STT:** project, API enabled, service account `roles/speech.client`, key path via `GOOGLE_APPLICATION_CREDENTIALS`, `STT_PROVIDER=google` | **done on the dev PC** (verified 2026-09-25); repeat on the deployed API with a key or workload identity | you |
| 12 | CallCopilot → Integrations → Microsoft Teams (Live): tenant ID (plus the app id/secret, per company or platform) → Save → **Test Connection**. Expect ✓ credentials, ✓ calling permissions, ✓ gateway reachable. Then **Enable Real-time Call Copilot** | not done | you |
| 13 | A **Teams meeting** (scheduled with a classic `/l/meetup-join/...` link) that allows the bot to join (lobby policy) | not done | you |
| 14 | **Test users:** you as the salesperson, with "Connect my Teams account" done so your audio is labelled correctly, and a colleague acting as the customer | not done | you |
| 15 | Frontend served against the public API (`VITE_API_BASE_URL` / same-origin proxy, WebSocket reachable) | not done | deployment step |
| 16 | Consent and notice text for customers that an AI assistant listens: **REQUIRES LEGAL REVIEW** | open | you |

**Test procedure (once the checklist is done).**
1. Plan a Teams call for a test contact: paste the meeting link and pick the language.
2. Join the meeting yourself, then click **Start**. The bot joins listen-only.
3. The live screen should show "● Live", then "Connected", then "● Receiving transcript" as you speak.
4. Transcript lines should appear with the correct speaker, and copilot cards should follow.
5. Leave the meeting, or click End. The status becomes Completed. In transient mode nothing is stored.
6. Collect these logs:
   - gateway: `teams_media_session_started` and `teams_media_session_ended`, with the frame counters
   - API: `stt_session_ended` (latencies, drops) and `copilot_llm_pass`

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Gateway `/health` 503 "Media platform failed to start" | certificate thumbprint/location, instance public IP, ports, OS (Windows Server) |
| Join returns `unsupported_join_url` | the link is the new short `/meet/...` format. Use a classic meeting link |
| UI "Teams meeting audio is not reaching the copilot" | `recording_status_failed`: the tenant/policy refused the recording status. `media_socket_connect_failed`: the gateway cannot reach the API's WSS URL |
| UI "Live transcription is interrupted" | Google errors. See the API log `stt_stream_error` (`stt_auth_failed` / `stt_invalid_config` / `stt_unavailable`) |
| "Transcript delayed" | no STT results for 20 s while the call is active. Compare the gateway's `frames_forwarded` with the API's `chunks_sent`/`chunks_dropped` |
| Speakers all "Speaker" | the salesperson has not connected Teams messaging (no Entra id to label tracks), so audio is `mixed` |

## Known limitations

- Never run against a real Teams meeting.
- Only classic meeting links are supported.
- One VM, with calls pinned to it. No scale-out or draining.
- Speaker attribution uses the salesperson's Entra id and the unmixed audio source mapping. **NOT VERIFIED** with real meetings.
- Google Chirp 3 streaming is available only in the `us`/`eu` regions.
- The gateway's HTTPS termination and certificate binding must be configured on the VM.
