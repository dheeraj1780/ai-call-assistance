# Call Copilot (MSME AI calling copilot) — MVP

An AI copilot that helps a salesperson before, during and after a phone call: preparation,
agenda, live assistance, structured notes, summary, action items and follow-up. The human
leads the call; the AI assists. Built for Indian MSMEs.

**Current phase: Phase 1 (foundation) complete** — authentication, companies, membership,
tenant isolation (app + PostgreSQL RLS), logging, error handling, React shell. Nothing
call-related is implemented yet. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Repository

```
backend/    FastAPI + SQLAlchemy + Alembic (Python 3.13, uv)
frontend/   React + TypeScript + Vite + Tailwind
docs/       architecture, decisions, API, database, security, AI, telephony, development
render.yaml Render Blueprint (API, static site, Postgres)
```

## Quick start (no Docker)

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). In short:

```bash
# PostgreSQL 16 + pgvector reachable, non-superuser role, dev + _test databases
cd backend  && cp .env.example .env && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev      # http://localhost:5173
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [Decisions (ADRs)](docs/DECISIONS.md)
- [API](docs/API.md) · [Database](docs/DATABASE.md) · [Security](docs/SECURITY.md)
- [AI](docs/AI.md) · [Telephony](docs/TELEPHONY.md) · [Development & deployment](docs/DEVELOPMENT.md)

Secrets never go in the repo: `.env` files are git-ignored; use `backend/.env.example`
and `frontend/.env.example` as templates.
