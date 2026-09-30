# Plivo (phone / PSTN): the primary calling path

## Purpose

Real phone calls with the passive live copilot. **No Microsoft 365 or Google account is needed.**
Plivo first rings the **salesperson's own phone**, then dials the **customer** and bridges them.
Once the customer answers, both sides of the call are streamed (listen-only) to the copilot. The
AI **never speaks**, and **no call recording is used**: audio goes to speech-to-text in memory and
is discarded.

## Status (2026-09-30)

| Capability | Status |
|---|---|
| Outbound bridge, inbound calls, status callbacks, V3 signatures, hang-up / cancel | IMPLEMENTED · MOCK-VERIFIED (httpx `MockTransport`, signed callbacks) · **BLOCKED-BY-CREDENTIALS** for a real call |
| Real-time audio (MEDIA_STREAM): Audio Streams API started after the customer answers, mu-law 8 kHz normalised to PCM16 16 kHz for Google Chirp 3 | IMPLEMENTED · MOCK-VERIFIED · decoder unit-tested against the G.711 table · **real 8 kHz telephony recognition quality NOT VERIFIED** |
| Lifecycle: no ACTIVE before the customer answered; End idempotent and bounded; no orphan call/stream after cancel | IMPLEMENTED · tested (`backend/tests/test_call_reliability.py`) |
| Plivo semantics checked against Plivo's public docs (Stream XML, Audio Streams API, Dial callbacks, stream WebSocket protocol) | DONE 2026-09-30 (docs only; REAL-PROVIDER VERIFICATION still required) |

No real Plivo call has been placed from this repository. The exact remaining prerequisites are
listed under **Exact activation procedure**.

## Required credentials

Entered in Settings → Integrations → Plivo (per company, stored encrypted with `TOKEN_ENCRYPTION_KEY`):

| Field | Secret | Notes |
|---|---|---|
| Auth ID | no | Plivo console → Overview |
| Auth token | **yes** | Plivo console → Overview. Also verifies Plivo's callback signatures |
| Plivo phone number (E.164) | no | A voice-enabled number rented on the account; used as caller ID |
| Application ID | no (optional) | Only for inbound calls: the Plivo application attached to the number |

- No other Plivo credential is needed. All callback and media URLs are derived from
  `PUBLIC_BASE_URL`, which must be **public https**.
- Server side, live transcription needs `STT_PROVIDER=google` (+ `GOOGLE_CLOUD_PROJECT` and
  credentials; see [google-stt.md](google-stt.md)). The Real-time Audio capability cannot be
  enabled while the mock STT is configured.

## Call flow (outbound)

```
POST /v1/Account/{auth_id}/Call/  from=<Plivo number> to=<salesperson phone>     -> INITIATED
  ring_url    -> RINGING
  answer_url  -> (salesperson answered) XML:
                 <Dial callerId=<Plivo number> action=…/dial_action callbackUrl=…/dial>
                   <Number>customer</Number></Dial>                                (no <Stream>)
  dial callback DialAction=answer (DialALegUUID = the salesperson leg)             -> CONNECTED
      -> POST /v1/Account/{auth_id}/Call/{call_uuid}/Stream/   (only if MEDIA_STREAM enabled)
           audio_track=both, bidirectional=false, content_type=audio/x-mulaw;rate=8000,
           service_url=wss://…/api/v1/telephony/media/plivo/<call>?token=…&leg=agent
  first audio frame after CONNECTED                                                -> ACTIVE
  dial_action -> NO_ANSWER / FAILED when the customer does not answer
  hangup_url  -> COMPLETED / NO_ANSWER / FAILED / CANCELLED with Duration
  fallback    -> FAILED (answer URL unreachable), hangup XML
```

**Why the stream is not in the answer XML.** Plivo documents `keepCallAlive` on `<Stream>` as:
`true` - the stream element runs exclusively and the following XML (our `<Dial>`) runs only after
the stream disconnects; `false` (the default) - the call ends when streaming stops. Neither allows
"bridge the customer while streaming". An XML stream would also start while the customer's phone
is still ringing (ringback tone would reach STT). The stream is therefore started with the Audio
Streams REST API **after** the dial callback reports the customer's answer. As a second guard, the
media WebSocket ignores frames until the call is CONNECTED.

**Tracks.** On the salesperson (A) leg the `inbound` track is the salesperson and the `outbound`
track is what they hear - the customer. For **inbound** calls the answered leg is the customer, so
the tracks are swapped (`&leg=customer`).

**Audio format.** Plivo streams G.711 mu-law at 8 kHz. `app/speech/g711.py` decodes it to PCM16 and
upsamples to 16 kHz (linear interpolation, stateful across chunks) inside the Google STT stream, so
every provider reaches the recogniser in the same format as Teams/Meet audio. Nothing is written
anywhere.

**Inbound calls.** Set the Plivo application's answer URL to `…/plivo/webhooks/<integration>/inbound`
and its hangup URL to `…/hangup` (both shown in the integration detail). The caller is matched to a
contact by phone number (else a new `INBOUND_CALL` contact, never merged); the contact owner (or the
first owner/admin with a phone number) is rung.

## Ending and cancelling

- **End Call** (`POST /calls/{id}/end`) is idempotent and bounded: the call becomes `ENDING`, the app
  sends `DELETE /Call/{uuid}/` (or `DELETE /Request/{request_uuid}/` while still queued/ringing),
  waits up to `CALL_END_GRACE_SECONDS` for Plivo's hangup callback, and otherwise completes the call
  locally (media and STT closed, transcript finalised, post-call queued). Repeated clicks send one
  hang-up.
- The media WebSocket is closed by the server as soon as the call is `ENDING` or finished.
- A late answer for a cancelled call receives `<Hangup/>`; a late connect starts no stream.
- A periodic recovery sweep (`calls.recover`, every 60 s) ends calls whose provider callbacks never
  arrived (see [../TELEPHONY.md](../TELEPHONY.md)).

## Correlation and idempotency

- Callbacks are resolved through our signed `cid` (outbound) or Plivo's `CallUUID` (inbound), and
  must belong to the integration's tenant (V3 signature with that tenant's auth token).
- Events are deduplicated per `(provider, event id)`; call states only move forward.
- The stream is requested exactly once per call (only when the CONNECTED event is newly applied).

## Local testing

- **Mock mode:** Configure → Mock → Test → enable Phone Calls (and Real-time Audio). Plan a phone
  call on a contact, start it, click **Simulate conversation (mock)**.
- **Without any integration:** the development fallback `TELEPHONY_PROVIDER=mock` behaves the same.
- **Credential check (read-only, no call placed):**
  `PLIVO_AUTH_ID=… PLIVO_AUTH_TOKEN=… PLIVO_NUMBER=+91… uv run python scripts/plivo_smoke.py`

## Exact activation procedure

1. Rent a voice number in Plivo (India: KYC for domestic routes). Note the Auth ID and Auth Token.
2. Deploy the API with a public https `PUBLIC_BASE_URL` (or an https tunnel for a test).
3. Server: `STT_PROVIDER=google` with Google credentials; optionally `AI_PROVIDER` + key.
4. Settings → Integrations → Plivo → Configure (Live) → Save → **Test Connection** (✓ credentials,
   ✓ number) → **Enable Phone Calls** → **Enable Real-time Audio Streaming**.
5. Profile: your own phone number. Contact: the customer's phone number.
6. Place one test call to your own second phone. Expect Ringing → Connected → Active (after the
   second phone answers) → live transcript → End call → Completed → post-call summary.
7. Optional: inbound (application answer/hangup URLs, application ID).

**Every real call costs money.** No automated test places a real call.

## Production prerequisites / open questions

- TRAI/DoT rules for commercial calls in India, call-processing notices and consent:
  **REQUIRES LEGAL REVIEW.** Whether Plivo permits streaming India call audio to servers outside
  India (media anchoring): **REQUIRES PROVIDER CONFIRMATION.**
- The media WebSocket is authenticated with a per-call HMAC token in the URL (Plivo connects to the
  URL we provide; there is no header mechanism for the WebSocket upgrade in this flow). The token
  expires after 4 hours, is bound to the call and provider, and is never logged by the app.
