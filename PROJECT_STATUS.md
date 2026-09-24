# PROJECT STATUS — AI Calling Copilot (MSME)

_Last updated: 2026-09-24. This file is the hand-off point for any future session._

- **Current branch:** `phase-2-crm` (local only, **not pushed**; all MVP phases live here)
- **`main`:** Phase 1 (`d2815f9`, pushed) + staging deploy config (`c0903f6`, **not pushed**)
- **Last commit:** see `git log -1` (docs + final audit on `phase-2-crm`)
- **Deployment:** **NOT DEPLOYED** (intentionally deferred; see `docs/DEVELOPMENT.md`)
- **Next step:** separate deployment/Render verification phase (not started).

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
