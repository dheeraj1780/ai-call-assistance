# Call Copilot — AI calling copilot for Indian MSMEs

Helps a salesperson **before, during and after** a customer phone call: customer context and an
editable AI agenda before the call; a live transcript with agenda tracking, requirement and
objection detection, suggested questions and company-knowledge answers during the call; and a
grounded summary, action items and follow-up drafts after it. The salesperson stays in control —
the AI suggests, humans confirm, and nothing is sent to customers automatically.

**Status:** MVP implemented and runnable locally with offline mock providers. Real telephony and
STT providers are **not integrated** (architecture + mocks only), and the backend database test
suite for phases 2+ has **not yet been run** in this build session. **Not deployed.**
See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) and [`FINAL_MVP_AUDIT.md`](FINAL_MVP_AUDIT.md).

## Repository

```
backend/    FastAPI + SQLAlchemy + Alembic + PostgreSQL/pgvector (Python 3.13, uv)
frontend/   React + TypeScript + Vite + Tailwind + TanStack Query + React Router
docs/       architecture, decisions (ADRs), API, database, security, privacy, AI, telephony, development
render.yaml Render Blueprint for a free-tier staging environment (not deployed)
```

## Quick start (no Docker)

```bash
# PostgreSQL 16 + pgvector reachable; non-superuser role; dev + *_test databases
cd backend  && cp .env.example .env && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Then follow the end-to-end demo in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#running-the-whole-product-locally-offline-mock-providers).

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [Decisions (ADRs)](docs/DECISIONS.md) · [API](docs/API.md)
- [Database](docs/DATABASE.md) · [Security](docs/SECURITY.md) · [Privacy](docs/PRIVACY.md)
- [AI](docs/AI.md) · [Telephony](docs/TELEPHONY.md) · [Development & deployment](docs/DEVELOPMENT.md)

Secrets never go in the repo: `.env` files are git-ignored; use the `.env.example` templates.
