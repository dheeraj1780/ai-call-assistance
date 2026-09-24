# WhatsApp Business Platform (Cloud API)

## Purpose

Receive customer WhatsApp messages in the CRM, get AI reply suggestions and extracted notes,
and reply after human review. **Official Cloud API only; WhatsApp Web is never automated.**

## Capabilities

| Capability | Status |
|---|---|
| Messages (MESSAGE) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Voice calls (WHATSAPP_VOICE_CALL) | **NOT SUPPORTED in this version** (shown as NOT_AVAILABLE with the reason) |

**Why voice is not available.**
- Meta's WhatsApp Business Calling API is a real, official API, generally available in India according to public sources checked on 2026-09-24 (**REQUIRES PROVIDER CONFIRMATION** for your number).
- Call audio is delivered over **WebRTC**: SDP offers and answers arrive through Graph webhooks and the media flows as SRTP. It can also be delivered over **SIP**.
- Using it needs a media service that terminates WebRTC or SIP, much like the Teams media gateway. That service does not exist here.
- The number must also be eligible, for example a minimum business-initiated messaging tier and country rules.
- Nothing fake was built. Messaging is unaffected.

## Required credentials

These are entered in **Settings → Integrations → WhatsApp Business → Configure** and stored per company, encrypted.

| Field | Secret | Where to find it |
|---|---|---|
| Phone number ID | no | Meta App Dashboard → WhatsApp → API Setup |
| WhatsApp Business Account ID | no | same page |
| Access token | **yes** | Business Settings → System users → generate a token with `whatsapp_business_messaging` and `whatsapp_business_management` |
| App secret | **yes** | App settings → Basic |
| Webhook verify token | **yes** | Any random string of 16 or more characters that you choose. Enter the same value in Meta |
| Meta App ID | no (optional) | App settings → Basic |

Server settings: `PUBLIC_BASE_URL` must be public **https**. `WHATSAPP_GRAPH_API_VERSION` defaults to `v23.0`; confirm it is still supported when you activate.

## Required permissions

- `whatsapp_business_messaging`, to send messages and read the phone number.
- `whatsapp_business_management`, used by the connection test to confirm the number belongs to the business account.

## Webhook setup

1. Save the configuration. The detail page then shows the **callback URL**: `https://<PUBLIC_BASE_URL>/api/v1/integrations/whatsapp/webhooks/<integration-id>`.
2. In Meta App Dashboard → WhatsApp → Configuration → Webhook, paste the callback URL and your verify token, then click **Verify and save**. Meta calls GET with `hub.mode=subscribe`, and we echo `hub.challenge`. The checklist item "Webhook registered and verified" turns ✓.
3. Subscribe to the **`messages`** webhook field.

Each POST is verified with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret). Changes for any other `phone_number_id` are ignored.

## Behaviour

- **Incoming message**
  1. Signature is checked.
  2. Idempotency is checked (`integration_webhook_receipts` plus a unique `(session, wamid)`).
  3. The message is normalised.
  4. The conversation is matched to a contact, using a confirmed identity first, then the normalised phone number.
  5. The message and a timeline event are stored. The timeline event does not contain the message text.
  6. A `conversation.assist` job starts. It adds deterministic notes, then runs an AI pass, then creates a SUGGESTED draft, notes and unconfirmed action items.
- **Ambiguous or unknown senders** are left AMBIGUOUS or UNMATCHED until a human links the conversation (Conversations → "Needs linking"). They are never merged, and names are never used for matching.
- **Sending** is always a click on **Send** in the conversation. The UI sends an idempotency key, so a double click sends only once. Free-form text can be sent only within the **24-hour customer-service window**; outside it the API returns `whatsapp_window_closed`. Template messages are **not implemented**.
- **Delivery statuses** (sent → delivered → read, failed) only move forward.
- **Supported message types:** text is fully supported. Images, documents and video are shown as labels with their caption. Voice notes are **not transcribed**. Other types are marked unsupported.

## Local testing

- **Mock mode** (no credentials):
  1. Configure → Mode "Mock" → Save → Test Connection → Enable Messages.
  2. Use **Simulate WhatsApp Message**, then open Conversations.
- **Live mode on a laptop:** expose the API through an HTTPS tunnel and set `PUBLIC_BASE_URL` to the tunnel URL.

## Production prerequisites

- Meta Business verification.
- A WhatsApp Business Account with a registered phone number.
- A system-user token.
- An HTTPS public URL.
- `TOKEN_ENCRYPTION_KEY` held in a secret store.

## Known limitations

- No template messages.
- No media download.
- Voice notes are not transcribed.
- No WhatsApp calling (see above).

## Privacy

- Message text is personal data.
- It is retained for the company's retention period, then purged along with AI drafts.
- It is not written to logs or the timeline.
- Consent to process customer messages with AI is **REQUIRES LEGAL REVIEW**.

## Exact activation procedure

1. Complete the Meta setup above and collect the five values.
2. Set `PUBLIC_BASE_URL` to an https URL that Meta can reach.
3. Settings → Integrations → WhatsApp Business → Configure → Mode **Live** → enter the values → **Save**.
4. **Test Connection.** Expect ✓ "Access token can read the phone number" and ✓ "Phone number belongs to the WhatsApp Business Account".
5. In Meta, register the callback URL and verify token, then subscribe to `messages`.
6. **Enable Messages.**
7. Send a WhatsApp message from a test phone to your business number, and confirm it appears in Conversations.
8. Reply from CallCopilot within 24 hours.
