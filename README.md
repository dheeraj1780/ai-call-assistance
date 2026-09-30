# Call Copilot — AI calling copilot for Indian MSMEs

Helps a salesperson **before, during and after** a customer phone call: customer context and an
editable AI agenda before the call; a live transcript with agenda tracking, requirement and
objection detection, suggested questions and company-knowledge answers during the call; and a
grounded summary, action items and follow-up drafts after it. The salesperson stays in control —
the AI suggests, humans confirm, and nothing is sent to customers automatically.

**Status (2026-09-30):** MVP implemented end to end and covered by automated tests (mock
providers). **Microsoft 365 is not required**: phone calls go through **Plivo** (primary path);
Microsoft Teams, Google Meet and WhatsApp are optional integrations. Google Chirp 3 streaming STT is
implemented and locally verified; the Teams media path is real-provider-verified; a real Plivo call
and a real LLM call have not been run yet (credentials required). **Raw call audio is never
recorded or stored.** **Not deployed.** See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) and
[`FINAL_MVP_AUDIT.md`](FINAL_MVP_AUDIT.md).

## Repository

```
backend/    FastAPI + SQLAlchemy + Alembic + PostgreSQL/pgvector (Python 3.13, uv)
frontend/   React + TypeScript + Vite + Tailwind + TanStack Query + React Router
teams-media-gateway/  optional .NET 8 Teams media bot (only for the Teams provider)
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
- Integrations: [overview](docs/integrations/overview.md) · [Plivo (phone)](docs/integrations/plivo.md) ·
  [Teams (optional)](docs/integrations/microsoft-teams.md) · [Google Meet (optional)](docs/integrations/google-meet.md) ·
  [WhatsApp (optional)](docs/integrations/whatsapp.md) · [Google STT](docs/integrations/google-stt.md)

Secrets never go in the repo: `.env` files are git-ignored; use the `.env.example` templates.
