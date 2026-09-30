# PROJECT STATUS — AI Calling Copilot (MSME)

_Last updated: 2026-09-30 (call-reliability + Plivo-first implementation pass)._ This file is the hand-off point for any future session._

- **Deployment:** **NOT DEPLOYED** (intentionally deferred; see `docs/DEVELOPMENT.md`)
- **Product direction:** Microsoft 365 / Teams is **optional**. Plivo phone calling is the primary
  real calling path; Teams, Google Meet and WhatsApp are optional providers. One provider-neutral
  call/AI pipeline. **Raw audio is never stored or recorded.**
- **Verification:** see `FINAL_MVP_AUDIT.md` (labels REAL-PROVIDER-VERIFIED / LOCALLY-VERIFIED /
  MOCK-VERIFIED / BLOCKED-BY-CREDENTIALS).
- **Next step:** controlled real Plivo call with public https + real Google STT, real LLM check
  (`backend/scripts/ai_smoke.py`), then Teams application-level E2E.

## Call reliability and data model — implemented 2026-09-30 (migration `0013_call_reliability`)

| Item | Status |
|---|---|
| Edit a planned call (meeting link re-validated per channel, language, objective, schedule, assignee); fixed after start | IMPLEMENTED · MOCK-VERIFIED |
| Re-open a call that never connected, correct it, start again | IMPLEMENTED · MOCK-VERIFIED |
| Idempotent, concurrency-safe Start (row lock) | IMPLEMENTED · MOCK-VERIFIED |
| Provider-neutral, idempotent, bounded End (`ENDING` state, forced local completion after `CALL_END_GRACE_SECONDS`) | IMPLEMENTED · MOCK-VERIFIED |
| Restart-safe stale-call recovery (`calls.recover`, every 60 s) | IMPLEMENTED · MOCK-VERIFIED |
| Reconnect/resume: `hello` carries call status; live-only state kept in server memory for the call | IMPLEMENTED · MOCK-VERIFIED |
| Audio never stored; text follows retention; Teams live-only by default (Microsoft Graph terms) | IMPLEMENTED · MOCK-VERIFIED |
| Editable transcript (keeps `original_text`) and summary (`EDITED`, AI text kept once), audited | IMPLEMENTED · MOCK-VERIFIED |
| Copilot on mixed/UNKNOWN speech incl. company-knowledge lookup | IMPLEMENTED · MOCK-VERIFIED |
| Plivo: stream via Audio Streams API only after the customer answered; no ACTIVE before CONNECTED; queued cancel; mu-law 8 kHz → PCM16 16 kHz for Chirp 3 | IMPLEMENTED · MOCK-VERIFIED · BLOCKED-BY-CREDENTIALS (real call) |
| Plan Call is channel-neutral (server-provided channel list; phone needs no Microsoft/Google account) | IMPLEMENTED · frontend tests |
| Live screen: prominent End, Ending state, audio state, customer context, fix-and-retry, transcript editing, STAKEHOLDER notes, correct retention notice | IMPLEMENTED · frontend tests |

**Verification (2026-09-30, PostgreSQL 16.x + pgvector, non-superuser role):** backend 515 passed
(28 new in `tests/test_call_reliability.py`), ruff + format + strict mypy clean, `alembic check`
clean, migrations down/up verified; frontend 92 passed, typecheck, lint and production build clean.
The .NET gateway was not rebuilt (unchanged; no .NET SDK in that environment).

**Real providers in this pass:** none executed - no Plivo, LLM, Google STT or Teams credentials and
no public https endpoint were available. Nothing is claimed as real-verified from this pass.

## Google Meet real-time meeting copilot — current focus (2026-09-25)

Google Meet is optional (history of its 2026-09-25 implementation below).
Details: `docs/integrations/google-meet.md`, `GOOGLE-MEET-CREDENTIALS-REQUIRED.md`, `REAL-GOOGLE-MEET-TEST.md`.

| Item | Status |
|---|---|
| Google Meet provider/channel, per-user OAuth (encrypted refresh token), meeting lookup, Media API signalling proxy, session events, migration 0010 | IMPLEMENTED · PASS (41 backend tests, Google mocked) |
| Browser bridge: Google's Meet Media API reference client (vendored), 16 kHz PCM mixing → existing media WebSocket → Chirp 3 → copilot | IMPLEMENTED · PASS (unit/component tests with fakes) |
| Settings → Integrations card, plan Google Meet call, live-screen Meet panel | IMPLEMENTED · tests |
| Real Google OAuth / Meet API / Media API / real meeting audio | **NOT VERIFIED** — needs OAuth client + Developer Preview enrolment |
| Personal Gmail eligibility for the Developer Preview | **UNVERIFIED — possible blocker** |
| Speaker attribution for Meet | NOT IMPLEMENTED (mixed audio, speaker Unknown) |

## Teams real-time call copilot (optional) — history

The Teams media path was later REAL-PROVIDER-VERIFIED on a real E3 tenant (gateway join, media AVAILABLE, 16 kHz PCM delivered to the API). Rows below are the 2026-09-25 state. Details: `docs/integrations/teams-call-copilot.md`,
`docs/integrations/google-stt.md`, `teams-media-gateway/README.md`.

| Item | Status |
|---|---|
| Google Cloud STT v2 adapter (Chirp 3, streaming, interim/final, explicit language en-IN/en-US/hi-IN/de-DE, rotation, reconnect, bounded buffering, shutdown) | IMPLEMENTED · unit-tested · **real Google VERIFIED** for en-IN / en-US (TTS test audio); hi-IN / de-DE configured, NOT VERIFIED |
| Complete LOCAL call copilot with real Google STT (`backend/scripts/local_audio_call.py`, real-time-paced audio, not Teams) | **VERIFIED 2026-09-25**: 7-line test call → transcript, requirements, budget, timeline, price objection, agenda progress, missing agenda questions, notes, end of call, post-call summary. User-visible latency median 1.3 s, max 1.9 s |
| End of call: input finished → STT finals drained → copilot final pass → session closed → post-call (bounded, no fixed sleep) | IMPLEMENTED · tested · verified with real Google (last sentence arrived after input ended) |
| Silent per-speaker tracks (Teams unmixed audio) finalise their utterance after 800 ms | IMPLEMENTED · tested · verified with real Google |
| AI copilot LLM pass | `AI_PROVIDER=mock` (no Anthropic key configured): deterministic detectors + mock LLM. **Real Claude NOT exercised** |
| Per-call recognition language (migration `0009_call_language`) | IMPLEMENTED |
| Synthetic end-to-end (synthetic PCM → Google adapter → copilot → live events) | TESTED (development test, not a Teams call) |
| `teams-media-gateway` (.NET 8, media SDK 1.2.0.17950) | COMPILES (warnings as errors) · 33 tests pass · runs locally in degraded mode |
| Azure VM deployment script `teams-media-gateway/deploy-azure-vm.ps1` (+ `AZURE-VM-DEPLOYMENT.md`) | IMPLEMENTED · tested on the dev PC (package, dry-run, health check, Kestrel HTTPS with a store certificate) · **NOT RUN on the Azure VM yet** |
| Gateway media platform | starts only on the Azure Windows VM with real cert/IP; locally the SDK reports `Media platform failed to initialize` |
| Live screen: connecting / receiving / delayed / STT unavailable / Teams media unavailable / copilot analysing | IMPLEMENTED · frontend tests |
| Real Teams meeting | Media path later **REAL-PROVIDER-VERIFIED** (see top); application-level E2E not yet run |

**Deadline:** the media SDK must be upgraded by ~2026-10 (Microsoft's 3-month freshness rule).

## Multi-channel integrations (2026-09-25)

Architecture and activation guides: `docs/integrations/` (overview, microsoft-teams, whatsapp,
plivo, credentials, testing). Migration `0008_integrations`.

| Item | Status |
|---|---|
| Provider/channel/capability model, Settings → Integrations (write-only encrypted secrets, test, enable, config check) | IMPLEMENTED · MOCK VERIFIED |
| Common conversation model + safe contact matching + shared AI assist for messages | IMPLEMENTED · MOCK VERIFIED |
| WhatsApp Cloud API messaging | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| WhatsApp voice calling | NOT SUPPORTED (needs a WebRTC/SIP media service; shown as NOT_AVAILABLE) |
| Teams messaging (delegated Graph, chat linking, notifications) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Teams real-time call copilot: API contract, mock gateway, transient/declared-recording persistence | IMPLEMENTED · MOCK VERIFIED |
| `teams-media-gateway/` (.NET) | SOURCE ONLY — NOT BUILT (no .NET SDK here), NOT VERIFIED |
| Plivo phone calls (outbound bridge, inbound, callbacks, V3 signatures) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Plivo real-time audio streaming | IMPLEMENTED · MOCK VERIFIED; superseded 2026-09-30 (Audio Streams API after answer, Google STT available) |

**(Resolved since:** Google Chirp 3 STT is implemented and locally verified; see top.)

Verification: backend **337 passed** (239 existing + 98 new), ruff + strict mypy clean,
`alembic check` clean, migration 0008 up/down/up verified; frontend 49 tests passed, typecheck,
lint and production build clean.

## Local verification (2026-09-24)

Against local PostgreSQL 18.2 (port 5433, pgvector 0.8.1) using the non-superuser role
`callcopilot` (NOSUPERUSER, NOBYPASSRLS): migrations 0001–0007 applied to `callcopilot_test` and
`callcopilot`; `alembic check` clean; **full backend suite 239 passed**; ruff + strict mypy clean;
RLS verified as the app role (0 rows without tenant context, cross-tenant reads/updates blocked);
API starts, `/health/ready` = ready, register/me/contacts smoke test + cross-tenant 404 passed.
Fixes from that run: MissingGreenlet in call/action-item PATCH, provider-specific knowledge
threshold, `tzdata` for Windows, figure-warning `%` regex, simulator turn-taking; conftest refuses
a TEST_DATABASE_URL that equals DATABASE_URL.

## Environment notes

- No Docker. Local PostgreSQL 18.2 service on port **5433** (the user manages it); app role
  `callcopilot` (non-superuser, owns the schema objects); DBs `callcopilot` (app) /
  `callcopilot_test` (tests). The `postgres` superuser must never be used by the app (bypasses RLS).

## Phases

| # | Phase | Status | Verification |
|---|---|---|---|
| 1 | Foundation | DONE `d2815f9` | 88 backend + 17 frontend tests passed (2026-09-23) |
| – | Staging deploy config | DONE `c0903f6` (main) | unit tests; fail-closed startup verified; NOT DEPLOYED |
| 2–3 | CRM + customer timeline | CODE COMPLETE `ef786b5` | frontend tests pass; DB tests PASS |
| 4 | Call prep, AI agenda, AI provider layer, jobs | CODE COMPLETE `5747663` | DB tests PASS |
| 5 | Google Calendar | CODE COMPLETE `372c611` | adapter unit-tested (MockTransport); NOT LIVE VERIFIED |
| 6–7 | Telephony abstraction, lifecycle, webhooks, sessions | CODE COMPLETE `dbb9514` | MOCK only; DB tests PASS; LIVE PROVIDER NOT VERIFIED |
| 8–9 | Streaming STT abstraction, live transcript | CODE COMPLETE `dbb9514` | MOCK STT only |
| 10–13 | Live copilot, agenda intelligence, requirements, objections | CODE COMPLETE `dbb9514` | detector unit tests pass |
| 14–15 | Knowledge base + live retrieval | CODE COMPLETE `dbb9514` | DB tests PASS; Voyage NOT VERIFIED |
| 16 | Structured live notes | CODE COMPLETE `dbb9514` | DB tests PASS |
| 17–21 | Post-call, action items, follow-ups, history, timeline | CODE COMPLETE `3282d79` | DB tests PASS |
| 22 | Dashboard | CODE COMPLETE `3282d79` | DB test PASS |
| 23–27 | AI safety, privacy/retention, security, reliability, cost | IMPLEMENTED across phases | see FINAL_MVP_AUDIT.md |
| 28 | Test suite | DONE | 239 backend tests pass |
| 29 | UI | 15 screens | NOT verified in a real browser |
| 30 | End-to-end journey | automated test (mock providers) | PASS |
| 31 | Documentation | DONE `f6dc33a` | – |
| 32 | Code quality pass | DONE | lint, types, TODO scan, log-safety fix |

## Known limitations

Mock telephony/STT only; single-process real-time pipeline; lexical hashing embeddings by
default; Claude/Voyage/Google not live verified; no consent capture; no browser verification;
Render proxy hop count unmeasured. Details: `FINAL_MVP_AUDIT.md`.
