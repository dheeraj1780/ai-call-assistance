# Telephony

**Status: provider-independent architecture IMPLEMENTED; only a MOCK provider exists.
LIVE PROVIDER INTEGRATION NOT VERIFIED — no real phone call has been placed.**

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
| Provider-neutral call lifecycle: PLANNED → INITIATED → RINGING → CONNECTED → ACTIVE → COMPLETED / NO_ANSWER / FAILED / CANCELLED | IMPLEMENTED | `app/calls/models.py`, `app/telephony/service.py` |
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
