# Google Cloud Speech-to-Text (streaming)

The first real streaming speech-to-text provider, behind the provider-neutral
`SpeechToTextProvider` interface (`app/speech/provider.py`). Implementation:
`app/speech/google.py`.

| Item | Status |
|---|---|
| Adapter: streaming, interim and final results, explicit language, rotation, reconnect, backpressure, shutdown | IMPLEMENTED · unit-tested (28 tests, real google-cloud-speech types, fake transport) |
| Synthetic end-to-end: PCM → adapter → live pipeline → copilot → live events | TESTED (`tests/test_teams_stt_e2e.py`, fake Google transport) |
| Real Google Cloud recognition | **NOT VERIFIED.** Needs your project and credentials. Run `scripts/google_stt_smoke.py` |

## How it fits

```
audio frames (Teams 16 kHz PCM / phone 8 kHz mu-law)
  → LiveSession: one STT stream per speaker track (salesperson / customer)
  → GoogleSTTStream: ~250 ms chunks → bounded queue → StreamingRecognize (v2, Chirp 3)
  → STTResult(text, is_final, start_ms, end_ms, confidence)   ← provider-neutral
  → transcript.partial / transcript.final → CopilotEngine → live screen
```

- **Nothing Google-specific leaves the adapter.** Another provider (Deepgram, Sarvam, Azure) would implement the same `open_stream` / `send` / `results` / `close` interface.
- **Speakers come from the audio track.** Chirp 3 does not diarise in streaming mode, so each speaker track gets its own stream.
- **Language is always explicit.**
  - Each call stores `language` (`en-IN`, `en-US`, `hi-IN` or `de-DE`), chosen when planning the call.
  - The default is `STT_LANGUAGE`.
  - Automatic language detection is not used.

## Behaviour

- **Request config (first message):**
  - `recognizer = projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_STT_LOCATION}/recognizers/_`
  - `model = chirp_3`
  - explicit decoding: `LINEAR16`/16 kHz (Teams) or `MULAW`/8 kHz (phone), mono
  - `language_codes = [call language]`
  - automatic punctuation
  - `interim_results = true`
- **Chunking and quota.**
  - Audio is aggregated into `STT_CHUNK_MS` chunks (default 250 ms). No request exceeds 24,000 bytes; Google's limit is 25 KB.
  - Google's quota is **3,000 requests/minute per project across all streams**. At 250 ms, one stream uses 240 requests/min, and one Teams call with two tracks uses about 480. That allows roughly **6 concurrent calls per project** before a quota increase.
  - Smaller chunks lower latency but use more quota.
- **Backpressure.** Each stream holds at most `STT_AUDIO_QUEUE_CHUNKS` chunks (default 120, about 30 s). When Google is slow or reconnecting, the **oldest** audio is dropped and counted (`chunks_dropped`). Callers never block, and memory stays bounded.
- **Stream limit.** Google closes streams after 5 minutes. Streams are rotated after `STT_STREAM_MAX_SECONDS` (default 280), and time offsets continue across rotations.
- **Errors.**
  - Retried with backoff, up to `STT_MAX_RECONNECTS` consecutive times (default 3):
    - UNAVAILABLE, INTERNAL, ABORTED, RESOURCE_EXHAUSTED and DEADLINE_EXCEEDED
    - a connect timeout (`STT_CONNECT_TIMEOUT_SECONDS`)
  - Not retried:
    - UNAUTHENTICATED, PERMISSION_DENIED and missing credentials → `stt_auth_failed`
    - INVALID_ARGUMENT, FAILED_PRECONDITION and NOT_FOUND → `stt_invalid_config`
  - A failed stream is contained by `LiveSession`:
    - the live screen shows "Live transcription is interrupted – your call continues"
    - it makes a bounded number of re-open attempts
    - the call is never ended because of STT.
- **Timestamps.** Chirp 3 streaming gives utterance **end** offsets only, with no word timestamps. `start_ms` is the previous final's end.
- **Confidence.** Chirp reports `0.0` in streaming; that is stored as unknown (`null`).
- **Shutdown.** `close()` flushes buffered audio, ends the request stream, waits up to 5 s for final results, then cancels.
- **Observability.** These log events carry counts, latencies and error codes only (no audio, no transcript text, no credentials):
  - `stt_session_started` / `stt_session_ended`: chunks sent and dropped, streams, reconnects, interim and final counts, malformed responses, average and max final latency, max interim latency
  - `stt_stream_error`
  - `live_session_closed`

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `STT_PROVIDER` | `mock` | `google` enables this adapter |
| `GOOGLE_CLOUD_PROJECT` | – | required when `STT_PROVIDER=google` |
| `GOOGLE_APPLICATION_CREDENTIALS` | – | path to a service-account JSON **outside the repository**, or use workload identity (Application Default Credentials). Never put the key in `.env` or git |
| `GOOGLE_STT_LOCATION` | `us` | Chirp 3 streaming is GA in `us` and `eu` multi-regions only (checked 2026-09-25). Audio from India is processed in that region: this is a **data-residency decision that REQUIRES LEGAL REVIEW** |
| `GOOGLE_STT_MODEL` | `chirp_3` | model |
| `STT_LANGUAGE` | `en-IN` | default call language (`en-IN`, `en-US`, `hi-IN`, `de-DE`) |
| `STT_CHUNK_MS` | 250 | audio per request |
| `STT_AUDIO_QUEUE_CHUNKS` | 120 | bounded buffer per stream |
| `STT_STREAM_MAX_SECONDS` | 280 | stream rotation |
| `STT_CONNECT_TIMEOUT_SECONDS` | 10 | connect timeout |
| `STT_MAX_RECONNECTS` | 3 | consecutive transient failures before giving up |

## Google Cloud setup (manual)

1. Create or choose a project, with billing enabled.
2. Enable the **Cloud Speech-to-Text API**.
3. Create a service account with the role **Cloud Speech Client** (`roles/speech.client`). That is the least privilege for recognition.
4. Create a JSON key, store it outside the repo, and set `GOOGLE_APPLICATION_CREDENTIALS` to its path. On a cloud host, prefer workload identity or the platform's secret store.
5. Set `STT_PROVIDER=google`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_STT_LOCATION=us` (or `eu`).
6. **Verify without Teams** (billable, uses your credentials):

   ```bash
   cd backend
   ffmpeg -i sample.m4a -ar 16000 -ac 1 sample.wav
   uv run python scripts/google_stt_smoke.py sample.wav --language en-IN
   ```

   You should see `interim` lines followed by `FINAL` lines.

## Tests

- `tests/unit/test_google_stt.py`:
  - the exact request configuration, for each language and encoding
  - chunking and the request size limit
  - interim and final normalisation and offsets
  - malformed or empty responses
  - error classification
  - auth errors not retried; transient reconnect; bounded give-up
  - connect timeout
  - rotation
  - flush, close, and a hung provider being cancelled
  - backpressure drops
  - stream isolation
  - log hygiene (no transcript text or audio)
- `tests/test_teams_stt_e2e.py`: the synthetic Teams meeting through the real adapter (see [teams-call-copilot.md](teams-call-copilot.md)).

## Known limitations

- No word-level timestamps or diarisation in Chirp 3 streaming (Google limitation).
- Latency and accuracy for Indian English and Hindi are **not measured**. Measure them with real calls.
- The quota for concurrent calls is shown above; request an increase before going beyond it.
- There is one shared client per process, and there is no multi-region failover.

## Privacy

- Raw audio exists only in memory until it is sent or dropped. It is never written to disk.
- Google processes the audio. Review Google Cloud's data-processing terms, and the choice of region, for DPDP compliance: **REQUIRES LEGAL REVIEW**.
- Transcripts follow the existing retention policy. For Teams they are stored **only** when recording was declared and confirmed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `stt_auth_failed` | `GOOGLE_APPLICATION_CREDENTIALS` missing/unreadable, API not enabled, or the service account lacks `roles/speech.client` |
| `stt_invalid_config` | the model is unavailable in `GOOGLE_STT_LOCATION` (use `us` or `eu` for `chirp_3`), or the language is unsupported |
| `chunks_dropped` > 0 | network to Google too slow, or quota throttling. Check `reconnects`, and consider a larger `STT_CHUNK_MS` |
| "Transcript delayed" in the UI | no interim/final results for 20+ s while the call is active. Check the gateway's `frames_forwarded` and the API's `stt_session_ended` log |
