# PROJECT STATUS — AI Calling Copilot (MSME)

_Last updated: 2026-09-24 · Branch: `phase-2-crm` (local only, not pushed) · `main` = Phase 1 + staging deploy config_

This file is the hand-off point for any future session. Keep it current.

## Deployment

**NOT DEPLOYED.** `render.yaml` is prepared for a free-tier staging environment (see
`docs/DEVELOPMENT.md`). Deployment is a separate, later task.

## Environment notes

- No Docker. Local Postgres 16.2 + pgvector 0.6.2 from the `pgserver` wheel, port **5434**,
  role `callcopilot`, DBs `callcopilot_dev` / `callcopilot_test` (see memory / DEVELOPMENT.md).
  **The user starts it manually** — do not auto-start it.
- A different Postgres service on 5433 belongs to the machine; do not touch it.

## Phases

| # | Phase | Status | Verification |
|---|---|---|---|
| 1 | Foundation (auth, tenants, RLS, logging, React shell) | DONE (`d2815f9`) | 88 backend + 17 frontend tests passed 2026-09-23 |
| – | Staging deploy config (free plans, start.sh, client IP) | DONE on `main` (`c0903f6`) | unit tests + fail-closed start verified; NOT DEPLOYED |
| 2/3 | CRM: contacts, notes, timeline, calls (manual), action items | CODE COMPLETE | frontend 25/25 pass; **backend DB tests written, NOT YET RUN** (Postgres down) |
| 4 | Call preparation + AI agenda (AI provider layer, jobs queue) | CODE COMPLETE | frontend 26/26; backend tests `test_call_prep.py` written, NOT YET RUN |
| 5 | Calendar (Google OAuth, events with explicit confirmation) | CODE COMPLETE | frontend 29/29; backend unit 21/21 (Google adapter via MockTransport); DB tests NOT YET RUN; Google NOT LIVE VERIFIED |
| 6–7 | Telephony abstraction, call lifecycle, signed idempotent webhooks, media stream | CODE COMPLETE (MOCK provider) | tests written, NOT YET RUN; LIVE PROVIDER NOT VERIFIED |
| 8–9 | Streaming STT abstraction (mock), live transcript, browser WS with resume | CODE COMPLETE (MOCK STT) | frontend 34/34; backend tests NOT YET RUN |
| 10–13 | Live copilot (detectors + budgeted LLM pass), agenda tracking, requirements, objections | CODE COMPLETE | detector unit tests 36/36; DB tests NOT YET RUN |
| 14–15 | Knowledge base (PDF/DOCX/TXT, chunking, pgvector) + live retrieval | CODE COMPLETE | DB tests NOT YET RUN; Voyage embeddings NOT VERIFIED |
| 16 | Structured live notes with human review | CODE COMPLETE | DB tests NOT YET RUN |
| 17–21 | Post-call (grounded summary, AI action items, follow-up drafts, timeline), call history | CODE COMPLETE | frontend 37/37; DB tests NOT YET RUN |
| 22 | Dashboard | CODE COMPLETE | DB test NOT YET RUN |
| 23–27 | AI safety, retention, security, reliability, cost | NOT STARTED | |
| 28–32 | Tests, UI polish, E2E journey, docs, code quality | NOT STARTED | |

## Known limitations / open items

- Backend CRM tests (`tests/test_crm.py`, `tests/test_crm_isolation.py`) and migration
  `0002_crm` have not been executed against Postgres yet.
- Render `TRUSTED_PROXY_HOPS` must be measured on a real deployment.

## Next step

Continue with Phase 4 (call preparation). When Postgres is available: run
`uv run alembic upgrade head && uv run alembic check && uv run pytest` in `backend/`.
