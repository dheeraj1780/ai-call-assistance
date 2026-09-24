# Communication integrations — overview

CallCopilot is an **AI customer-conversation copilot**. Phone, Microsoft Teams and WhatsApp are
*adapters* around one conversation model and one AI engine.

## Status labels used in these documents

| Label | Meaning |
|---|---|
| IMPLEMENTED | Code exists and is covered by automated tests |
| MOCK VERIFIED | Exercised end to end with mock providers / `httpx.MockTransport` (no real provider contacted) |
| CREDENTIAL REQUIRED | Needs your credentials before it can run for real |
| EXTERNAL PROVIDER VERIFICATION REQUIRED | Never run against the real provider; formats follow official docs/SDKs |
| NOT SUPPORTED | Deliberately not offered (reason given) |

## Architecture

```
Provider (Teams / WhatsApp / Plivo / mock)
   │  verified webhook · Graph notification · gateway event · media stream
   ▼
Provider adapter            app/integrations/providers/{whatsapp,teams,plivo}.py
   ▼
Conversation normaliser     app/integrations/normalizer.py  (ConversationEvent, MediaFrame)
   ▼
Common conversation model   app/conversations  (sessions, messages, participants, events,
                            contact identities, reply drafts) + calls/transcripts for real time
   ▼
AI copilot engine           messages: app/conversations/assist.py
                            live calls: app/copilot/engine.py (unchanged engine, channel = metadata)
   ▼
CRM                         timeline (with channel), structured notes, action items, knowledge
   ▼
Frontend                    Settings → Integrations, Conversations, contact Communication panel,
                            unified live call screen
```

- **Provider / channel / capability** (`app/integrations/domain.py`)
  - Providers: MICROSOFT_TEAMS, WHATSAPP, PLIVO.
  - Channels: PHONE, TEAMS, WHATSAPP.
  - Capabilities: MESSAGE, REAL_TIME_CALL, PHONE_CALL, MEDIA_STREAM, WHATSAPP_VOICE_CALL. CONTACT_SYNC is declared, but no provider implements it.
- **Registry** (`app/integrations/registry.py`): the fields, capabilities and requirements for each provider. The UI renders everything from `GET /api/v1/integrations/{slug}` and holds no provider logic.
- **Interfaces** (`app/integrations/providers/base.py`): `MessageProvider`, `WebhookProvider` and `ContactSyncProvider`. The existing `TelephonyProvider` serves as the calling and media-stream interface.
  - Implementations: `WhatsAppClient`, `TeamsMessageProvider`, `TeamsCallingProvider` (with the .NET media gateway), `PlivoTelephonyProvider`, and a mock for each.
  - Adapters are built only by `providers/factory.py` (dependency injection). Tests inject `httpx.MockTransport`.
- **One copilot.** There is no per-provider copilot. Every message runs the same detectors, knowledge retrieval and AI gateway. Every real-time call (phone, Teams, mock) feeds the same `LiveSession`/`CopilotEngine`.

## Status model

- **Integration:** `NOT_CONFIGURED → CONFIGURED → VALIDATED → ENABLED`, or `ERROR`.
- **Capability:** one of the states above, or `NOT_AVAILABLE`.
- A connection test is valid only for the configuration version it ran against. Changing any credential clears the test and disables every capability.
- A capability can be enabled only when **VALIDATED** and when no blocking requirement is missing. Blocking requirements include a public HTTPS URL and a real STT provider for real-time audio.
- Runtime authentication failures (for example, a send rejected with 401) put the integration into **ERROR** until it is re-tested.

## Modes

- **LIVE:** real provider credentials.
- **MOCK:** no credentials needed. Nothing is sent externally, and the integration is clearly labelled "Mock mode". Mock mode is refused in production unless `ALLOW_MOCK_PROVIDERS_IN_PRODUCTION=true`.
  - Developer simulators (`/api/v1/integrations/dev/simulate/*` and `POST /calls/{id}/simulate`) build real provider-shaped payloads.
  - Those payloads go through the same parsing, normalisation, conversation, AI and timeline code as live traffic.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/integrations` | Safe metadata for each provider: status, capabilities, `last_tested_at`, error state |
| `GET /api/v1/integrations/{slug}` | Fields (secrets write-only), requirements, last test, setup URLs |
| `GET /api/v1/integrations/{slug}/config-check` | "What do I still need to configure?" |
| `PUT /api/v1/integrations/{slug}/config` | Save the configuration. Admins only; secrets are encrypted |
| `POST /api/v1/integrations/{slug}/test` | Test the connection |
| `POST /api/v1/integrations/{slug}/capabilities/{cap}/enable\|disable` | Turn a capability on or off |
| `DELETE /api/v1/integrations/{slug}` | Disconnect |
| `GET/POST/DELETE /api/v1/integrations/microsoft-teams/connection\|connect`, `/oauth/callback`, `/chats`, `/chats/link`, `/admin-consent-url` | Teams per-user sign-in and chat linking |
| `GET /api/v1/conversations`, `GET /api/v1/conversations/{id}`, `POST …/messages`, `PATCH …/drafts/{id}`, `POST …/link` | Conversations |
| `GET /api/v1/contacts/{id}/communication`, `POST /api/v1/contacts/{id}/conversations` | Channel actions for a contact |
| `/api/v1/integrations/whatsapp/webhooks/{integration_id}` (GET, POST) | WhatsApp webhooks |
| `/api/v1/integrations/teams/webhooks/{integration_id}` | Teams change notifications |
| `/api/v1/integrations/teams/gateway/events` | Teams media gateway events |
| `/api/v1/integrations/plivo/webhooks/{integration_id}/{answer\|ring\|dial\|dial_action\|hangup\|fallback\|stream\|inbound}` | Plivo callbacks |
| `wss://…/api/v1/telephony/media/{mock\|plivo\|teams}/{call_id}?token=` | Media streams |

Webhook URLs carry the **integration id** so that the tenant is resolved from our own routing table (`integration_routes`), never from an identifier inside the payload. The provider signature is then verified with **that** integration's secret.

## Security, privacy and reliability reviews

See [credentials.md](credentials.md) for secret handling and [testing.md](testing.md) for evidence. The summary below records what was reviewed during implementation.

**Security**
- Every new table is tenant-owned with forced RLS, except three system tables that hold no personal data:
  - `integration_routes`
  - `integration_webhook_receipts`
  - `call_routes`
- Composite tenant foreign keys are used throughout.
- Webhooks are verified per integration: WhatsApp HMAC-SHA256, Plivo V3 signature, Teams `clientState` HMAC, gateway HMAC with a replay window.
- Idempotency is enforced by receipts plus unique message and event ids.
- Cross-tenant replay is rejected, and this is tested.
- Secrets are Fernet-encrypted, write-only, never logged, and redacted by key name in logs.
- Admin-only configuration is audited without values.
- One provider account cannot be attached to two workspaces.
- Media WebSockets need a per-call HMAC token and the matching provider route.

**Privacy**
- Raw audio is never stored.
- Messages and AI reply drafts follow the company's transcript retention (default 30 days) through the same RLS-scoped retention sweep.
- Timeline events for messages contain no message text.
- Teams calls are **transient by default**: nothing derived from media is stored unless recording is declared and confirmed.
- Message contents and transcripts are still personal data: consent, notices and DPDP obligations are **REQUIRES LEGAL REVIEW**.

**Reliability**
- Every provider call has a timeout. Only idempotent reads are retried.
- Sends are never retried automatically, to avoid duplicates. A failed send is stored as FAILED and the user retries explicitly, with an idempotency key.
- Webhook processing is fast; AI runs in bounded-retry jobs.
- A provider outage never corrupts CRM data: messages are stored before AI runs, and an AI failure only means no suggestion.
- A media-gateway or WebSocket failure never ends the call.
- Structured logs record provider, operation, status and latency, never payloads.
