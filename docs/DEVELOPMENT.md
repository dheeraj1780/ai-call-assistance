# Development

Docker is **not** required. Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/),
Node.js 22 + npm, Git, and a PostgreSQL 16+ database with the pgvector extension.

## 1. Database

Any PostgreSQL 16+ with pgvector works. You need two databases (dev and `*_test`) owned by
a **non-superuser** role — superusers bypass Row-Level Security and the test suite asserts
that the role does not.

```sql
-- as an admin user
CREATE ROLE callcopilot LOGIN PASSWORD '<password>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE callcopilot_dev  OWNER callcopilot;
CREATE DATABASE callcopilot_test OWNER callcopilot;
-- in each database (creating the extension needs admin rights on self-managed Postgres):
CREATE EXTENSION IF NOT EXISTS vector;
```

Options:

- **Local PostgreSQL install** — pgvector must be available for that install (on Windows
  it usually has to be built; see the pgvector README).
- **Portable binaries without admin rights (used to verify Phase 1).** The `pgserver` PyPI
  package ships PostgreSQL 16.2 + pgvector 0.6.2 binaries for Windows (wheels up to
  Python 3.12; the binaries themselves don't depend on Python):
  ```bash
  uv venv %USERPROFILE%\.pgtools --python 3.12
  uv pip install --python %USERPROFILE%\.pgtools\Scripts\python.exe pgserver
  set PGBIN=%USERPROFILE%\.pgtools\Lib\site-packages\pgserver\pginstall\bin
  %PGBIN%\initdb -D %USERPROFILE%\.pgdata -U postgres -W -A scram-sha-256 -E UTF8
  %PGBIN%\pg_ctl -D %USERPROFILE%\.pgdata -o "-p 5434" -l %USERPROFILE%\.pgdata\pg.log start
  ```
  then run the SQL above with `%PGBIN%\psql -h 127.0.0.1 -p 5434 -U postgres`.
  Note: this build does not include `citext` (we don't use it).
- **A cloud development database** (e.g. a separate Render Postgres). Never point the test
  URL at a shared database: tests truncate every table.

## 2. Backend

```bash
cd backend
cp .env.example .env          # then edit DATABASE_URL, TEST_DATABASE_URL, JWT_SECRET
uv sync                       # creates .venv with runtime + dev dependencies
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

Checks:

```bash
uv run pytest                 # real Postgres at TEST_DATABASE_URL (name must end in _test)
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic/env.py
uv run alembic check          # models vs migrations drift
```

## 3. Frontend

```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173, proxies /api to 127.0.0.1:8000
npm test && npm run lint && npm run typecheck && npm run build
```

## Running the whole product locally (offline, mock providers)

1. Start Postgres and the API (`uv run uvicorn app.main:app --reload --port 8000`) and the
   frontend (`npm run dev`). The in-process job worker starts with the API.
2. Register, then in **Settings** add your phone number (needed to start calls).
3. **Knowledge**: upload a TXT/PDF/DOCX product guide (admin).
4. **Contacts → Add contact** (with a phone number) → **Prepare call** → objective →
   **Suggest with AI** → edit → **Save agenda** → **Start call**.
5. On the live screen press **Simulate conversation (mock)**: the mock telephony provider plays a
   scripted conversation through webhooks, the media stream and mock STT. Watch the transcript,
   agenda tracking, copilot cards and notes; review/edit notes.
6. When the call completes, open **View summary**: grounded summary, AI action items to confirm,
   follow-up drafts to edit/approve/copy (never sent), and schedule a calendar follow-up (mock
   calendar unless Google is configured).
7. The contact timeline and dashboard show the whole relationship history.

Real providers: set `AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`, `EMBEDDING_PROVIDER=voyage` +
`VOYAGE_API_KEY`, `CALENDAR_PROVIDER=google` + Google OAuth client (redirect URI
`{PUBLIC_BASE_URL}/api/v1/calendar/oauth/callback`). Telephony/STT have no real adapter yet.

Tests: `uv run pytest` (needs `TEST_DATABASE_URL`), DB-free subset:
`uv run pytest --noconftest tests/unit`.

## Environments

`APP_ENV` = `development` | `test` | `production`. Production enforces secure cookies, a
non-placeholder JWT secret, no wildcard CORS, disables `/docs`, enables HSTS, and refuses
to start if the DB role bypasses RLS.

## Deploying to Render (development/staging only)

**Status: NOT DEPLOYED.** `render.yaml` (Blueprint) defines three resources, all pinned to
free plans (omitting `plan` makes Render use PAID defaults):

| Resource | Type | Plan |
|---|---|---|
| `callcopilot-db` | PostgreSQL 16 | `free` |
| `callcopilot-api` | Python web service (`rootDir: backend`) | `free` |
| `callcopilot-web` | Static site (`rootDir: frontend`) | none (static sites are free) |

Free-tier limitations (from Render docs, 2026-09-23): the free Postgres **expires 30 days
after creation** (14-day grace, then deleted), 1 GB, **no backups** — it is a staging
database, not a production one. Free web services sleep after 15 idle minutes (~1 min cold
start), share 750 instance-hours/month, and have no shell/one-off jobs. Bandwidth beyond the
free allowance is billed if a payment method is on file (otherwise services are suspended).

Startup (`backend/scripts/start.sh`): `alembic upgrade head` then uvicorn. Fails closed —
a failed migration exits before the API starts. Single-instance only (ADR-013).

Rate-limit client IP (ADR-014): `TRUSTED_PROXY_HOPS=0` ignores `X-Forwarded-For` (all
clients share one bucket — safe but coarse). After deploying, measure the proxy chain with
`GET /health/client-ip` (enabled by `DIAGNOSTICS_ENABLED=true`), set the measured hop
count, re-test, then disable diagnostics.

Dashboard steps: push `main` → New → Blueprint → select the repo → enter `CORS_ORIGINS`
(the static site URL) → create. Then confirm the API URL matches the `/api/*` rewrite in
`render.yaml` (update it if Render assigned a different host).
