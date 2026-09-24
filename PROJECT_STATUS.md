# PROJECT STATUS — AI Calling Copilot (MSME)

_Last updated: 2026-09-24. This file is the hand-off point for any future session._

- **Current branch:** `phase-2-crm` (local only, **not pushed**; all MVP phases live here)
- **`main`:** Phase 1 (`d2815f9`, pushed) + staging deploy config (`c0903f6`, **not pushed**)
- **Last commit:** see `git log -1` (docs + final audit on `phase-2-crm`)
- **Deployment:** **NOT DEPLOYED** (intentionally deferred; see `docs/DEVELOPMENT.md`)
- **Next step:** start local Postgres, run the full backend suite, fix failures; then a separate
  deployment/Render verification phase.

## ⚠️ Most important open item

The backend tests that need PostgreSQL (~190 tests: CRM, call prep, calendar, real-time,
knowledge, post-call, tenant isolation, end-to-end journey) and migrations **0002–0007 have never
been executed**. The local test Postgres was stopped by the system for low memory, and the user
asked to start it manually. Verified so far: ruff lint, strict mypy (136 files), app import and
route registration (41 REST paths), test collection, 40 DB-free backend unit tests, 37 frontend
tests, frontend typecheck/lint/production build.

```bash
# start Postgres (port 5434, see docs/DEVELOPMENT.md), then:
cd backend && uv run alembic upgrade head && uv run alembic check && uv run pytest
```

Expect some failures on the first run: this code has not met a real database since Phase 1.

## Environment notes

- No Docker. Local Postgres 16.2 + pgvector 0.6.2 from the `pgserver` wheel, port **5434**, role
  `callcopilot`, DBs `callcopilot_dev` / `callcopilot_test`. **The user starts it manually.**
- Another Postgres service on 5433 belongs to the machine; do not touch it.

## Phases

| # | Phase | Status | Verification |
|---|---|---|---|
| 1 | Foundation | DONE `d2815f9` | 88 backend + 17 frontend tests passed (2026-09-23) |
| – | Staging deploy config | DONE `c0903f6` (main) | unit tests; fail-closed startup verified; NOT DEPLOYED |
| 2–3 | CRM + customer timeline | CODE COMPLETE `ef786b5` | frontend tests pass; DB tests NOT RUN |
| 4 | Call prep, AI agenda, AI provider layer, jobs | CODE COMPLETE `5747663` | DB tests NOT RUN |
| 5 | Google Calendar | CODE COMPLETE `372c611` | adapter unit-tested (MockTransport); NOT LIVE VERIFIED |
| 6–7 | Telephony abstraction, lifecycle, webhooks, sessions | CODE COMPLETE `dbb9514` | MOCK only; DB tests NOT RUN; LIVE PROVIDER NOT VERIFIED |
| 8–9 | Streaming STT abstraction, live transcript | CODE COMPLETE `dbb9514` | MOCK STT only |
| 10–13 | Live copilot, agenda intelligence, requirements, objections | CODE COMPLETE `dbb9514` | detector unit tests pass |
| 14–15 | Knowledge base + live retrieval | CODE COMPLETE `dbb9514` | DB tests NOT RUN; Voyage NOT VERIFIED |
| 16 | Structured live notes | CODE COMPLETE `dbb9514` | DB tests NOT RUN |
| 17–21 | Post-call, action items, follow-ups, history, timeline | CODE COMPLETE `3282d79` | DB tests NOT RUN |
| 22 | Dashboard | CODE COMPLETE `3282d79` | DB test NOT RUN |
| 23–27 | AI safety, privacy/retention, security, reliability, cost | IMPLEMENTED across phases | see FINAL_MVP_AUDIT.md |
| 28 | Test suite | WRITTEN | backend DB suite NOT RUN |
| 29 | UI | 15 screens | NOT verified in a real browser |
| 30 | End-to-end journey | automated test written (mock providers) | NOT RUN |
| 31 | Documentation | DONE `f6dc33a` | – |
| 32 | Code quality pass | DONE | lint, types, TODO scan, log-safety fix |

## Known limitations

Mock telephony/STT only; single-process real-time pipeline; lexical hashing embeddings by
default; Claude/Voyage/Google not live verified; no consent capture; no browser verification;
Render proxy hop count unmeasured. Details: `FINAL_MVP_AUDIT.md`.
