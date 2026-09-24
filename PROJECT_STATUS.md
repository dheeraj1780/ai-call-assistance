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
| 4 | Call preparation + AI agenda | NOT STARTED | |
| 5 | Calendar (Google) | NOT STARTED | |
| 6–7 | Telephony abstraction + call sessions + webhooks | NOT STARTED | |
| 8–9 | Streaming STT abstraction + live transcript | NOT STARTED | |
| 10–13 | Live copilot, agenda intelligence, requirements, objections | NOT STARTED | |
| 14–15 | Knowledge base + retrieval | NOT STARTED | |
| 16 | Structured live notes | NOT STARTED | |
| 17–21 | Post-call, action items (AI), follow-up drafts, history, timeline integration | NOT STARTED | |
| 22 | Dashboard | NOT STARTED | |
| 23–27 | AI safety, retention, security, reliability, cost | NOT STARTED | |
| 28–32 | Tests, UI polish, E2E journey, docs, code quality | NOT STARTED | |

## Known limitations / open items

- Backend CRM tests (`tests/test_crm.py`, `tests/test_crm_isolation.py`) and migration
  `0002_crm` have not been executed against Postgres yet.
- Render `TRUSTED_PROXY_HOPS` must be measured on a real deployment.

## Next step

Continue with Phase 4 (call preparation). When Postgres is available: run
`uv run alembic upgrade head && uv run alembic check && uv run pytest` in `backend/`.
