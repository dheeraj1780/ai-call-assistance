# Testing the integrations

**No automated test contacts a real provider.**
- LIVE adapters run against `httpx.MockTransport`, which asserts the exact URLs, headers and bodies we would send.
- MOCK integrations use in-process providers.

## Automated tests (backend)

| File | What it proves |
|---|---|
| `tests/unit/test_integration_adapters.py` | Several checks, listed after this table |
| `tests/test_integrations.py` | Several checks, listed after this table |
| `tests/test_messaging.py` | Several checks, listed after this table |
| `tests/test_channel_calls.py` | Several checks, listed after this table |

`tests/unit/test_integration_adapters.py`:
- WhatsApp signature and challenge checks.
- Payload normalisation for text, media, status and malformed payloads.
- Phone and email normalisation.
- Plivo V3 signatures against **vectors produced with the official plivo-python SDK code**.
- Plivo answer XML (listen-only stream on both tracks), callback mapping and media parsing, including swapped tracks for inbound calls.
- Teams scopes and roles, HTML normalisation, `clientState` verification and gateway HMAC with a replay window.
- Requirement logic.

`tests/test_integrations.py`:
- Provider metadata and WhatsApp voice shown as NOT_AVAILABLE.
- Configuration is admin-only.
- **Secrets are write-only and encrypted, and absent from responses, logs and DB JSON.**
- Validation.
- Connection tests for WhatsApp, Plivo and Teams, with success, bad credentials, a number not on the account, and **missing calling permissions**.
- The public-URL requirement.
- Re-validation after a credential change.
- Mock mode, and mock mode refused in production.
- Disconnect.
- One account per workspace.
- RLS isolation.

`tests/test_messaging.py`:
- WhatsApp verification handshake and signature enforcement (missing, bad and tampered).
- **Idempotency.**
- Matched, unmatched and ambiguous senders; names alone never match; human linking and the identity that link remembers.
- **Cross-tenant webhooks rejected.**
- AI suggestion, notes and action items.
- **The AI never sends.**
- Human send, with the exact request asserted and idempotent client id.
- Forward-only delivery statuses.
- The 24-hour window.
- A send failure marks the integration ERROR and leaves the CRM intact.
- An AI outage keeps the message and deterministic notes.
- A disabled capability ignores webhooks.
- Retention purge.
- Tenant isolation.
- The mock simulator runs the real pipeline.
- Contact communication options.
- Teams: OAuth URL and callback, encrypted refresh token, missing-scope handling, notification validation handshake, `clientState` rejection, subscription body, and message fetch and ingest.

`tests/test_channel_calls.py`:
- Teams calls need a valid meeting link and the capability.
- **Transient Teams calls persist nothing derived from media** (no segments, notes or post-call job; live events only).
- Declared-recording calls persist only after confirmation.
- The call's conversation events.
- Simulation endpoint.
- Gateway failure leaves the call FAILED.
- Gateway event signatures and idempotency.
- Gateway client signing.
- Plivo outbound lifecycle, with the request body, answer XML, CONNECTED, COMPLETED with duration, and duplicates asserted.
- Hang-up through the REST API.
- **Cross-tenant callbacks are ignored.**
- Inbound calls, both matched and new-lead.
- Plivo media stream into the live transcript with correct speakers.
- The media WebSocket rejects a wrong provider or token.

Frontend (`vitest`):
- `IntegrationsPage.test.tsx`:
  - capabilities render from the backend
  - secrets are write-only, and only changed fields are sent
  - test and enable
  - members are read-only
- `ConversationPage.test.tsx`:
  - the suggestion is never sent without Send
  - edit then send carries an idempotency key
  - the blocked reason is shown
  - unmatched linking
  - the Communication panel shows only available actions
- `LiveCallPage.test.tsx`: the channel label and the transient banner.

Run:

```bash
cd backend && uv run pytest && uv run ruff check . && uv run mypy app tests
cd frontend && npm test && npm run typecheck && npm run lint && npm run build
```

## Manual mock-mode walkthrough (no credentials)

1. Settings → Integrations. For each provider: Configure → Mode **Mock** → Save → **Test Connection** → enable its capabilities.
2. On a contact with a phone number, the Communication panel shows WhatsApp Message, Teams Message, Teams Call and Phone Call, each marked "(mock)".
3. **WhatsApp:**
   1. In Integrations, click **Simulate WhatsApp Message** using the contact's phone.
   2. Open Conversations. You should see the message, then an AI suggestion within a few seconds (produced by the job worker).
   3. Click Edit → Send.
4. **Teams message:**
   1. On the contact, click Teams Message and link a mock chat.
   2. Click **Simulate Teams Message**, then open the conversation.
5. **Teams call:**
   1. On the contact, click Teams Call and paste any `https://teams.microsoft.com/l/meetup-join/...` link.
   2. Continue to the agenda, then open the live screen.
   3. Click Start, then **Simulate conversation (mock)**.
   4. Live-only (transient) mode shows everything live, keeps it in server memory during the call, and stores no transcript/notes afterwards. Audio is never stored in any mode.
6. **Phone call:** use the same flow with Phone Call.

## Controlled real-provider tests (next step, one provider at a time)

Each needs your credentials, a public HTTPS URL (deployment or tunnel) and your explicit go-ahead. Suggested order:

1. **WhatsApp Messages.** Test Connection, then register the webhook in Meta, then send a message from your own phone, then reply.
2. **Teams Messages.** Test Connection, then connect your account, then link a chat with a colleague, then exchange messages.
3. **Plivo phone call.** Test Connection, then **one paid call** between two of your own phones, checking the status transitions.
4. **Plivo audio, Teams calling:** after a real STT provider is chosen and added (and the gateway is built, for Teams).
