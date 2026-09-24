# Plivo (phone / PSTN)

## Purpose

Real phone calls with the passive live copilot. Plivo first rings the **salesperson**, then dials
the **customer** and bridges them. Audio from both sides can be streamed to the copilot. The
AI **never speaks**: the stream is unidirectional and nothing is played into the call.

## Capabilities

| Capability | Status |
|---|---|
| Phone calls (PHONE_CALL): outbound bridge, inbound calls, status callbacks, hang-up | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Real-time audio streaming (MEDIA_STREAM) | IMPLEMENTED · MOCK VERIFIED (message parsing + pipeline). **Cannot be enabled in LIVE mode yet:** needs a real streaming STT provider |

**Signature verification** is a port of the official `plivo-python` SDK's `signature_v3.py` (v4.62.0). It is unit-tested against vectors produced by that SDK code.

## Required credentials

Entered in Settings → Integrations → Plivo (per company, encrypted):

| Field | Secret | Notes |
|---|---|---|
| Auth ID | no | Plivo console → Overview |
| Auth token | **yes** | Plivo console → Overview. Also verifies Plivo's callback signatures |
| Plivo phone number (E.164) | no | A voice-enabled number rented on the account; used as caller ID |
| Application ID | no (optional) | Only for inbound calls: the Plivo application attached to the number |

- **No other Plivo credential is needed.**
- `PLIVO_WEBHOOK_BASE_URL` and `PLIVO_MEDIA_WS_URL` are not separate settings. Both are derived from `PUBLIC_BASE_URL`, which must be public **https**.
- Each call's answer, ring, hangup and fallback URLs are generated per call. They carry our call id and are covered by the V3 signature.

## Call flow (outbound)

```
POST /v1/Account/{auth_id}/Call/  from=<Plivo number> to=<salesperson phone>
  answer_url  → …/plivo/webhooks/<integration>/answer?cid=<call>   → XML:
      <Stream bidirectional="false" audioTrack="both" contentType="audio/x-mulaw;rate=8000"
              keepCallAlive="false">wss://…/api/v1/telephony/media/plivo/<call>?token=…</Stream>  (only if MEDIA_STREAM enabled)
      <Dial callerId="<Plivo number>" action=…dial_action callbackUrl=…dial><Number>customer</Number></Dial>
  ring_url    → RINGING        dial (DialAction=answer) → CONNECTED
  dial_action → NO_ANSWER/FAILED when the customer does not answer
  hangup_url  → COMPLETED / NO_ANSWER / FAILED / CANCELLED with Duration
  fallback    → FAILED (answer URL unreachable), hangup XML
```

- On the salesperson leg, the `inbound` track is the salesperson and the `outbound` track is the customer.
- For **inbound** calls, the customer is the answered leg, so the tracks are swapped (`&leg=customer` on the stream URL).

**Inbound calls.** Set the Plivo application's answer URL to `…/plivo/webhooks/<integration>/inbound` and its hangup URL to `…/hangup`. Both URLs are shown in the integration detail.
- The caller is matched to a contact by phone number. If there is no single match, a new contact is created with source `INBOUND_CALL`.
- The responsible salesperson is rung: the contact owner, or the first owner or admin with a phone number.
- The contact is never merged with existing contacts.

**Correlation and idempotency.**
- Callbacks are resolved through our signed `cid` for outbound calls, or Plivo's `CallUUID` for inbound calls.
- Each callback must belong to the integration's tenant.
- Events are deduplicated per `(provider, event id)`.
- Call states only move forward.
- Hang-up uses `DELETE /Call/{CallUUID}/`, falling back to `DELETE /Request/{request_uuid}/` if the call was never answered.

## Local testing

- **Mock mode:**
  1. Configure → Mock → Test → enable Phone Calls (and Real-time Audio).
  2. On a contact, click Phone Call, plan the call, and start it.
  3. Click **Simulate conversation (mock)**.
- **Without any integration:** the development fallback `TELEPHONY_PROVIDER=mock` behaves the same.
- **Live mode:** only with an HTTPS tunnel. **Every real call costs money.** No automated test places a real call.

## Production prerequisites

- A Plivo account (KYC for Indian numbers).
- A voice-enabled number.
- The salesperson's phone number in their profile, and the customer's phone number on the contact.
- A public https API.
- For live transcription, a real STT provider (not in this version).
- **TRAI/DoT rules for commercial calls in India, and call-recording and consent notices, are REQUIRES LEGAL REVIEW.**

## Known limitations

- There is no streaming STT provider, so the audio stream is useless without one. This is a blocker for live copilot on real calls.
- The Plivo WebSocket message format (`start`/`media`/`stop` with `media.track`/`payload`) and the Dial callback parameter names follow Plivo documentation and SDK. They are **EXTERNAL PROVIDER VERIFICATION REQUIRED**.

## Exact activation procedure

1. Rent a voice number in Plivo. Note the Auth ID and Auth Token.
2. Set `PUBLIC_BASE_URL` to public https.
3. Settings → Integrations → Plivo → Configure (Live) → enter the values → Save → **Test Connection**. Expect ✓ credentials and ✓ number on the account.
4. **Enable Phone Calls.**
5. Optional, for inbound calls: create a Plivo application with the inbound answer and hangup URLs, attach it to the number, and enter its ID.
6. Place one test call to your own second phone and check that the call's status moves through Ringing → Connected → Completed in CallCopilot.
7. Real-time audio: after an STT provider is added, Enable Real-time Audio Streaming.
