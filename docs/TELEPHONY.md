# Telephony

**Status (2026-09-30): provider-independent architecture IMPLEMENTED. Providers: mock
(development), **Plivo** (primary phone path; MOCK-VERIFIED, BLOCKED-BY-CREDENTIALS for a real
call - see [integrations/plivo.md](integrations/plivo.md)), Microsoft Teams (optional; media path
REAL-PROVIDER-VERIFIED through the gateway), Google Meet (optional; fakes only). No real phone
call has been placed from this repository.**

## Target flow (ADR-007)

```
Our application ──(create call)──► Telephony provider
                                      ├── leg 1: salesperson's normal phone (rings first)
                                      ├── leg 2: customer's phone (bridged)
                                      └── real-time media fork ──WebSocket──► our backend ──► STT ──► copilot
Provider ──(signed status webhooks)──► our backend
```

The phone call lives at the provider. Our backend, the browser, STT and the LLM can all fail
without dropping the call; they only lose live assistance.

## What is implemented

| Piece | Status | Where |
|---|---|---|
| `TelephonyProvider` interface (`create_call`, `end_call`, `verify_webhook`, `parse_webhook`, `parse_media_message`) | IMPLEMENTED | `app/telephony/provider.py` |
| Provider-neutral call lifecycle: PLANNED → INITIATED → RINGING → CONNECTED → ACTIVE → ENDING → COMPLETED / NO_ANSWER / FAILED / CANCELLED | IMPLEMENTED, tested | `app/calls/models.py`, `app/telephony/service.py` |
| Idempotent, concurrency-safe Start (`SELECT … FOR UPDATE`; a repeated Start returns the live call; one provider call) | IMPLEMENTED, tested | `telephony.service.start_call` |
| Idempotent, bounded End for every provider (ENDING → provider hang-up → wait ≤ `CALL_END_GRACE_SECONDS` → forced local completion; media/STT closed; post-call queued) | IMPLEMENTED, tested | `telephony.service.request_end` |
| Re-open a call that never connected (failed start, cancelled, no answer) → PLANNED, editable, startable again | IMPLEMENTED, tested | `POST /calls/{id}/reopen` |
| Restart-safe stale-call recovery (periodic `calls.recover`, DB-driven, idempotent) | IMPLEMENTED, tested | `app/telephony/recovery.py` |
| Media stream closes server-side once a call is ENDING/finished; early media before CONNECTED ignored (Plivo) | IMPLEMENTED, tested | `telephony.service.MediaIngest` |
| Webhook verification (HMAC-SHA256 over `timestamp.body`, 5-minute replay window) | IMPLEMENTED (mock scheme) | `MockTelephonyProvider.verify_webhook` |
| Idempotency: unique `(provider, event_id)`; duplicates acknowledged, not re-applied | IMPLEMENTED, tests written | `telephony_webhook_events` |
| Ordering: states only move forward; nothing changes after a terminal state | IMPLEMENTED, tests written | `apply_state` rank table |
| Tenant resolution for webhooks (`call_routes`, no personal data) | IMPLEMENTED | `app/telephony/models.py` |
| Media WebSocket with per-call HMAC token (expires) | IMPLEMENTED | `/api/v1/telephony/media/{provider}/{call_id}` |
| Separate speaker tracks → speaker from track (no diarization) | IMPLEMENTED for providers that send tracks | `app/live/session.py` |
| Mixed-audio providers → speaker `UNKNOWN` (diarization not implemented) | IMPLEMENTED (degraded) | same |
| Browser is never authoritative for call state | IMPLEMENTED | telephony service |
| Mock conversation simulator (same code paths: `process_event` + `MediaIngest` + STT) | MOCKED | `app/telephony/simulator.py` |

## Provider research (2026-09-24, from provider documentation)

Labels: **VERIFIED (docs)** = stated in the provider's own documentation (not tested by us);
**NOT VERIFIED** = not tested; **REQUIRES PROVIDER CONFIRMATION**; **REQUIRES LEGAL REVIEW**.

| Question | Exotel | Plivo | Twilio |
|---|---|---|---|
| Indian domestic calls with Indian caller ID | Indian provider — REQUIRES PROVIDER CONFIRMATION of plan/KYC | VERIFIED (docs): only India-registered businesses may use domestic routes; outbound caller ID must be a Plivo Indian number; KYC required (≈1 h–1 business day) | VERIFIED (docs): India guidelines list domestic calling as "N/A"; "Outbound calls to India can only be made from international (non-Indian) numbers" → **not suitable** |
| Real-time media over WebSocket | VERIFIED (docs): Stream applet (one-way) and Voicebot applet (two-way) | VERIFIED (docs): `<Stream>` XML, `audioTrack` inbound/outbound/both | VERIFIED (docs): Media Streams |
| Separate speaker tracks | VERIFIED (docs): stream is mono; with a Connect applet "audio from all ringing legs is sent (manual filtering required)" → **mixed audio** | `audioTrack="both"` exists; whether tracks arrive separately labelled is **not stated** → REQUIRES PROVIDER CONFIRMATION | VERIFIED (docs): media messages carry `track: inbound|outbound` |
| Audio format | VERIFIED (docs): 16-bit 8 kHz mono PCM (slin), base64 | VERIFIED (docs): L16 8/16/24 kHz or µ-law 8 kHz | VERIFIED (docs): µ-law 8 kHz |
| WebSocket auth | VERIFIED (docs): IP allow-list or basic auth in URL; ≤3 custom params | REQUIRES PROVIDER CONFIRMATION | REQUIRES PROVIDER CONFIRMATION |
| Call continues if our WebSocket drops | NOT STATED in docs → REQUIRES PROVIDER CONFIRMATION | NOT STATED → REQUIRES PROVIDER CONFIRMATION | NOT VERIFIED |
| Media anchoring | REQUIRES PROVIDER CONFIRMATION | VERIFIED (docs): both legs must originate and terminate in India (`violates_media_anchoring`). Whether streaming audio to servers **outside India** is allowed is **not stated** → REQUIRES PROVIDER CONFIRMATION + LEGAL REVIEW | n/a |
| Latency, pricing, concurrency | NOT VERIFIED | NOT VERIFIED | n/a |

Sources: [Exotel AgentStream — Stream and Voicebot applet](https://developer.exotel.com/docs/agentstream/stream-voicebot-applet),
[Exotel getting started](https://developer.exotel.com/docs/agentstream/getting-started),
[Plivo Audio Streaming](https://www.plivo.com/docs/voice/xml/audio-streaming),
[Plivo India calling regulations](https://www.plivo.com/docs/voice/concepts/india-calling),
[Plivo: Domestic calling in India](https://support.plivo.com/hc/en-us/articles/16859884362265-Domestic-Calling-in-India),
[Twilio Media Streams messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages),
[Twilio India voice guidelines](https://www.twilio.com/en-us/guidelines/in/voice).
Ozonetel and Knowlarity were not evaluated in this pass.

### Conclusions

1. **Twilio is not a fit** for Indian MSMEs calling Indian customers (no domestic Indian caller ID).
2. **Plivo** is the most promising first integration *if* it confirms that `audioTrack="both"`
   delivers separately labelled tracks and that forking media to our backend is compatible
   with its India media-anchoring rules.
3. **Exotel** is India-native but its stream mixes legs; speaker attribution would need
   diarization (our STT abstraction supports provider speaker labels; with mixed audio the
   transcript shows `UNKNOWN` speakers until a diarizing STT provider is added).
4. **Data residency is a first-order constraint**: the free Render regions are outside India, and
   STT/LLM providers may process audio/text outside India. REQUIRES LEGAL REVIEW before any real
   customer call is streamed.

## Compliance (REQUIRES LEGAL REVIEW)

- Plivo's documentation states cold calling is prohibited in India and explicit digital consent
  is needed before commercial calls; calls without consent are treated as Unsolicited Commercial
  Communication. The product does not yet record customer calling consent (FUTURE: consent fields
  on contacts, and blocking calls without consent).
- Number series: 140-series promotional, landline series for service/transactional, 160-series
  for BFSI (per Plivo docs). Which series an MSME sales call needs → REQUIRES LEGAL REVIEW.
- Call-processing disclosure/consent to the customer (DPDP Act) → REQUIRES LEGAL REVIEW; a
  configurable announcement is not implemented yet.

## Adding a real provider

1. Implement `TelephonyProvider` in `app/telephony/<provider>.py`: signature verification per the
   provider's scheme, webhook → `TelephonyEvent` mapping (stable event ids!), media message →
   `MediaFrame` (track mapping) and `create_call` (bridge agent → customer with media fork URL
   from `_callback_urls`).
2. Register it in `get_telephony_provider()` and allow it in `TELEPHONY_PROVIDER`.
3. Point a real STT provider at the provider's audio format (`SpeechToTextProvider`).
4. Verify with real calls: connectivity, tracks, latency, call survival when our WebSocket drops,
   duplicate/out-of-order webhooks, and media residency.

## Lifecycle guarantees (2026-09-30)

- **Start** locks the call row, so double clicks / retries / concurrent requests place exactly one
  provider call; a Start on a call that is already live returns it (200). Any provider failure -
  including an unexpected exception - marks the call FAILED with a `telephony_error` code instead of
  leaving it INITIATED.
- **Failed start recovery:** `POST /calls/{id}/reopen` turns a call that never connected back into
  PLANNED (provider link and route cleared, history kept on the timeline). Its details (meeting link,
  language, objective, schedule) can then be corrected with `PATCH /calls/{id}` and it can be started
  again. A call that connected cannot be re-opened (plan a new one).
- **End** is provider-neutral: ENDING is committed first (a restart cannot lose the intent), the
  provider is asked to hang up once, and the provider's confirmation is awaited for at most
  `CALL_END_GRACE_SECONDS` (default 8). Without it the call is completed locally: COMPLETED if it
  ever connected, otherwise CANCELLED. Late provider events can never move a call backwards.
- **Recovery sweep** (`calls.recover`, every 60 s, from the job worker): reads in-flight calls through
  a read-only RLS policy that exists only inside `app.system_task = 'call_recovery'`, then acts per
  call under its own tenant context:

  | Condition | Action |
  |---|---|
  | INITIATED without a provider call id for > `CALL_START_STALE_SECONDS` (120) | FAILED `start_interrupted` |
  | INITIATED/RINGING without activity for > `CALL_CONNECTING_TIMEOUT_SECONDS` (600) | hang up, FAILED `connect_timeout` |
  | CONNECTED/ACTIVE without media or events for > `CALL_INACTIVITY_TIMEOUT_MINUTES` (30) | hang up, COMPLETED `inactivity_timeout` |
  | any live call older than `CALL_MAX_DURATION_HOURS` (8) | hang up, COMPLETED `max_duration` |
  | ENDING longer than the grace period + 5 s | COMPLETED (forced) |
  | finished but end-of-call processing never recorded (`finalized_at` NULL > 60 s) | re-run drain + post-call enqueue |

  `last_activity_at` is refreshed (throttled, every 15 s) by the media stream and by every provider
  event, so a quiet but healthy call is never ended.
- **Browser disconnects** never affect a call. A reconnecting live screen gets a `hello` with the
  call's current status and reloads the snapshot (the server is authoritative); it never creates a
  new call.
