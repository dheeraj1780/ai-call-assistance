# Telephony

**Status: NOT IMPLEMENTED. No provider selected. No provider-specific code may be written
before the Phase 4 feasibility spike concludes (ADR-007).**

## Target flow (Option A — approved priority)

```
Our application ──► Telephony provider
                       ├── leg 1: salesperson's normal phone
                       ├── leg 2: customer's phone
                       └── real-time audio stream (WebSocket) ──► our backend
```

The salesperson keeps using a normal phone. The call must continue if our backend, the
WebSocket, STT or the LLM fail.

## Spike checklist

Every item gets a label: **VERIFIED** (tested by us, with evidence), **ASSUMED**,
**REQUIRES PROVIDER CONFIRMATION**, **REQUIRES LEGAL REVIEW**. Do not select a provider from
generic global documentation alone.

| # | Question | Status |
|---|---|---|
| 1 | Outbound calling from India to Indian mobiles/landlines | REQUIRES PROVIDER CONFIRMATION |
| 2 | Indian PSTN connectivity / bridging two PSTN legs | REQUIRES PROVIDER CONFIRMATION |
| 3 | Business KYC requirements and lead time | REQUIRES PROVIDER CONFIRMATION |
| 4 | Number provisioning (virtual numbers, series) | REQUIRES PROVIDER CONFIRMATION |
| 5 | Caller ID shown to the customer | REQUIRES PROVIDER CONFIRMATION |
| 6 | Real-time audio streaming to our WebSocket (format, sample rate) | REQUIRES PROVIDER CONFIRMATION |
| 7 | Separate inbound/outbound tracks (speaker attribution without diarization) | REQUIRES PROVIDER CONFIRMATION |
| 8 | End-to-end latency from Indian numbers (measure) | NOT TESTED |
| 9 | Pricing: per-minute (both legs), streaming, numbers | REQUIRES PROVIDER CONFIRMATION |
| 10 | Failure behaviour: call continues if our stream endpoint drops | NOT TESTED |
| 11 | Webhook signing, retries, idempotency keys | REQUIRES PROVIDER CONFIRMATION |
| 12 | Applicable Indian telecom rules (DoT/TRAI, TCCCPR/DLT, 140/160 number series) | REQUIRES LEGAL REVIEW |
| 13 | Consent / call-recording disclosure (DPDP Act 2023 & Rules) | REQUIRES LEGAL REVIEW |
| 14 | Data processing: where audio is processed/stored by the provider, retention | REQUIRES PROVIDER CONFIRMATION |

Candidate providers to evaluate (none verified): Exotel, Plivo, Ozonetel, Knowlarity,
Twilio (India domestic restrictions to be checked).

## Interface (to be designed in Phase 4/5)

`TelephonyProvider`: `create_call()`, `end_call()`, `get_call_status()`, `stream_audio()`,
`handle_webhook()` — domain code depends only on this interface.
