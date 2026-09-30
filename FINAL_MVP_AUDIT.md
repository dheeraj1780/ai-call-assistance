# FINAL MVP AUDIT

_Updated 2026-09-30 after the call-reliability implementation pass · NOT DEPLOYED_

Labels: **REAL-PROVIDER-VERIFIED** · **LOCALLY-VERIFIED** (real component on a dev machine) ·
**MOCK-VERIFIED** (automated tests with fakes) · **BLOCKED-BY-CREDENTIALS** · **FUTURE**.

**Overall:** the product works end to end with mock providers and is covered by the full
automated suite (backend 515 tests on PostgreSQL 16 + pgvector, frontend 92 tests; lint, strict
typing, build and `alembic check` clean). Microsoft 365 is **not** required. No real phone call,
no real LLM call and no real Teams application-level call were made in the implementation pass:
the environment had no Plivo / LLM / Google / Teams credentials and no public https endpoint.

| Subsystem | Rating | Evidence / gaps |
|---|---|---|
| Call planning + editing before start | MOCK-VERIFIED | `PATCH /calls/{id}` edits link/language/objective/schedule while PLANNED; link re-validated per channel (Teams rule = gateway parser); fixed after start |
| Failed-start recovery | MOCK-VERIFIED | `POST /calls/{id}/reopen` → PLANNED → correct → start again |
| Start idempotency / concurrency | MOCK-VERIFIED | row lock; 5 concurrent starts → 1 provider call |
| End Call (all providers) | MOCK-VERIFIED | ENDING → hang-up once → bounded wait → forced completion; media/STT closed; post-call queued |
| Stale-call recovery | MOCK-VERIFIED | periodic `calls.recover`, DB-driven, restart-safe, idempotent, tenant-safe |
| Browser reconnect | MOCK-VERIFIED | `hello` carries status; snapshot reload; live-only state resumes from server memory |
| Audio privacy | MOCK-VERIFIED | no binary column anywhere (test); no recording APIs; audio only in bounded memory queues |
| Text retention | MOCK-VERIFIED | transcript/notes/summary retained with `expires_at` policy; Teams live-only by default (Microsoft terms) with in-memory state for the call |
| Transcript / summary editing | MOCK-VERIFIED | provenance kept (`original_text`, `original_summary`, `EDITED`), audited, tenant-isolated |
| Copilot on mixed audio | MOCK-VERIFIED | UNKNOWN speaker → extraction + knowledge lookup (labelled "possible customer question") |
| Plivo phone path | MOCK-VERIFIED · BLOCKED-BY-CREDENTIALS | semantics checked against Plivo docs; stream via Audio Streams API after answer; mu-law→PCM16 16 kHz; no real call placed |
| Teams | media REAL-PROVIDER-VERIFIED (earlier); app path MOCK-VERIFIED | optional; no credentials in this pass |
| Google Meet | MOCK-VERIFIED | optional; Developer Preview eligibility unverified |
| WhatsApp messaging | MOCK-VERIFIED · BLOCKED-BY-CREDENTIALS | optional |
| Google STT (Chirp 3) | LOCALLY-VERIFIED (2026-09-25, en-IN/en-US 16 kHz) | 8 kHz telephony audio after normalisation NOT VERIFIED |
| LLM (Claude / Gemini) | MOCK-VERIFIED · BLOCKED-BY-CREDENTIALS | configured default `AI_MODEL=claude-opus-5` is a valid Anthropic model id; `scripts/ai_smoke.py` for a real check |
| Knowledge / RAG | MOCK-VERIFIED | concurrent duplicate uploads serialised (advisory lock); Gemini embeddings not live-verified |
| Multi-tenancy / RLS | MOCK-VERIFIED | new endpoints tested cross-tenant (404); recovery reads via a SELECT-only system-task policy |
| Teams media gateway (.NET) | not re-run in this pass | unchanged; no .NET SDK in the implementation environment; media SDK freshness upgrade due ~2026-10 |
| Deployment | FUTURE | Render blueprint prepared; not deployed |

## Must do before real use

1. Controlled real Plivo call (outbound, then inbound) with public https and real Google STT
   (`docs/integrations/plivo.md` → Exact activation procedure); measure 8 kHz recognition quality.
2. Real LLM check (`backend/scripts/ai_smoke.py`), then a real call with `AI_PROVIDER` set.
3. Teams application-level E2E on the existing real tenant (optional provider).
4. Legal review: consent/notice for commercial calls, DPDP, data residency, Plivo media anchoring.
