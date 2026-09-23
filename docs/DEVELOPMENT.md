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

## Environments

`APP_ENV` = `development` | `test` | `production`. Production enforces secure cookies, a
non-placeholder JWT secret, no wildcard CORS, disables `/docs`, enables HSTS, and refuses
to start if the DB role bypasses RLS.

## Deploying to Render

`render.yaml` (Blueprint) defines: Postgres `callcopilot-db`, API web service
`callcopilot-api` (`rootDir: backend`), static site `callcopilot-web` (`rootDir: frontend`).

1. Create the Blueprint from the repo in the Render dashboard.
2. Set `CORS_ORIGINS` on the API to the static site's URL.
3. Update the `/api/*` rewrite destination in `render.yaml` to the API's real URL.
4. First-deploy checks (all currently **ASSUMED**, record results here):
   - `CREATE EXTENSION vector` succeeds for the Render DB user (docs say pgvector is
     supported — VERIFIED in docs only).
   - The Render DB user is not a superuser / has no BYPASSRLS (the API refuses to start in
     production otherwise).
   - `preDeployCommand` runs migrations on the chosen instance type (not available on free
     instances; run `uv run alembic upgrade head` from a shell instead).
   - The static-site rewrite forwards `Set-Cookie` and `Origin`, and the API sees the real
     client IP (rate limiting).
   - `.python-version` / `PYTHON_VERSION=3.13.11` is honoured.
