# FINAL MVP AUDIT

_2026-09-24 · branch `phase-2-crm` @ post-docs commit · NOT DEPLOYED_

Ratings: **PASS** · **PASS WITH LIMITATIONS** · **NOT VERIFIED** · **BLOCKED** · **FUTURE**.

**Overall: the MVP is implemented end to end, but it is NOT VERIFIED.** The backend
database-backed test suite (phases 2–30) and migrations 0002–0007 have not been run, because the
local PostgreSQL instance was down and the user asked to start it manually. Nothing below is
production-ready.

| Subsystem | Rating | Evidence / gaps |
|---|---|---|
| Architecture | PASS WITH LIMITATIONS | Modular monolith, provider interfaces for AI/STT/embeddings/telephony/calendar; single-process real-time (ADR-015) |
| Authentication | PASS | Phase 1: 88 backend tests passed (sessions, rotation, reuse detection, CSRF, invalid tokens) |
| Multi-tenancy | NOT VERIFIED | Phase 1 isolation PASS; new tables use FORCE RLS + composite FKs; cross-tenant tests written for every new resource but not run |
| Database | NOT VERIFIED | Migrations 0002–0007 written, never applied; `alembic check` not run since 0001 |
| RLS | NOT VERIFIED | Policies in migrations; retention-only cross-tenant policy; tests written |
| CRM | NOT VERIFIED | Backend + UI complete; frontend tests pass |
| Call preparation | NOT VERIFIED | Context, editable agenda, grounded AI suggestion (mock); frontend test passes |
| Telephony | PASS WITH LIMITATIONS (architecture) / BLOCKED (live) | Interface, lifecycle, signed idempotent webhooks, media tokens, mock provider; no real provider; provider research in docs/TELEPHONY.md (Twilio excluded; Plivo/Exotel need confirmation; data residency + consent need legal review) |
| STT | PASS WITH LIMITATIONS | Streaming interface + mock; no real STT; no diarization |
| Live transcript | NOT VERIFIED | Pipeline, hub resume, WS auth; reducer unit tests pass; backend tests not run; no browser test |
| Copilot | PASS WITH LIMITATIONS | Detector unit tests pass (40 DB-free tests); LLM pass budgeted; real LLM not live verified |
| Agenda intelligence | NOT VERIFIED | Status precedence unit-tested; tracking tested only in unrun DB tests |
| Knowledge base | NOT VERIFIED | Upload validation, chunking, pgvector retrieval, grounded answers; hashing embeddings are lexical, not semantic |
| Notes | NOT VERIFIED | Structured notes, human review overrides AI |
| Post-call processing | NOT VERIFIED | Grounding enforced in code; figure warnings unit-tested |
| Action items | NOT VERIFIED | AI items unconfirmed until a human confirms |
| Follow-up | NOT VERIFIED | Drafts only; no send endpoint (asserted in a written test) |
| Calendar | PASS WITH LIMITATIONS | Google adapter error mapping unit-tested (MockTransport); OAuth flow NOT LIVE VERIFIED; explicit confirmation required |
| Customer timeline | NOT VERIFIED | Calls, notes, tasks, status changes, summaries, follow-ups, calendar |
| Security | PASS WITH LIMITATIONS | See docs/SECURITY.md; log-safety fix (hidden SQL parameters); many controls only covered by unrun tests |
| Privacy | PASS WITH LIMITATIONS | No audio persistence (schema has no binary columns), partials never stored, configurable retention; no consent capture; legal review required |
| Retention | NOT VERIFIED | Hourly job + narrow RLS policy; test written |
| Testing | NOT VERIFIED | Backend: 40 DB-free unit tests PASS, ~190 DB tests not run; frontend: 37 PASS |
| Frontend | PASS WITH LIMITATIONS | 15 screens; typecheck/lint/build PASS; never exercised in a real browser |
| Cost controls | PASS WITH LIMITATIONS | Budgets, caps, timeouts, usage records; real costs unmeasured |
| Provider integrations | BLOCKED / NOT VERIFIED | Claude, Voyage, Google implemented but untested live; telephony/STT not implemented |
| Deployment | FUTURE | Render staging blueprint prepared; not deployed by design |

## Must do before any real use

1. Run migrations and the full backend suite on Postgres; fix failures.
2. Browser-test the full journey (register → prepare → simulated call → post-call → timeline).
3. Integrate and verify a real telephony + STT provider; confirm media streaming legality.
4. Legal review: consent for commercial calls, call-processing disclosure, data residency, DPDP.
5. Live-verify Claude, Voyage and Google Calendar with real credentials.
