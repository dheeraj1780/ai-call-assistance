# Architecture Decision Records

Status labels used across the docs: **IMPLEMENTED**, **VERIFIED**, **ASSUMED**, **MOCKED**,
**NOT IMPLEMENTED**, **REQUIRES PROVIDER CONFIRMATION**, **REQUIRES LEGAL REVIEW**.

---

## ADR-001 — Modular monolith (Accepted, 2026-09-23)

One FastAPI application with internal modules (`auth`, `tenants`, `users`, `audit`, later
`contacts`, `calls`, `copilot`, …). Each module owns `models / schemas / repository /
service / router`. No microservices, Kubernetes, Redis, Celery or event bus unless a
demonstrated requirement appears.

## ADR-002 — Stack (Accepted)

- Frontend: React + TypeScript + Vite + Tailwind CSS, React Router, TanStack Query.
- Backend: Python 3.13, FastAPI, Pydantic, SQLAlchemy 2 (async, asyncpg), Alembic.
- Database: PostgreSQL 16 + pgvector.
- Tooling: uv, ruff, mypy (strict), pytest; npm, ESLint, tsc, Vitest.

## ADR-003 — Provider abstractions (Accepted; NOT IMPLEMENTED yet)

`AIProvider`, `SpeechToTextProvider`, `EmbeddingProvider`, `TelephonyProvider` interfaces.
Domain code depends only on these interfaces and our own Pydantic types — never on vendor
SDK objects. Claude (Anthropic) is the initial `AIProvider`. Interfaces carry a language
parameter so Hinglish / Indian regional languages can be added later (MVP: English /
Indian English only; no multilingual UI).

## ADR-004 — Tenancy: one company per user, isolation at two layers (Accepted; IMPLEMENTED)

- Every business is a company (tenant). In the MVP a user belongs to exactly one company
  (`UNIQUE (company_members.user_id)`); the schema is otherwise multi-membership ready.
- The tenant is derived only from the verified access token and re-checked against
  `company_members` on every request. Client-supplied tenant IDs are never trusted
  (update schemas forbid unknown fields).
- **Database layer:** PostgreSQL Row-Level Security, `FORCE`d so it also applies to the
  table-owning role. Policies read transaction-local settings (`app.company_id`,
  `app.user_id`) that the app applies at the start of every transaction. A session with
  no tenant context sees no tenant rows (fail closed).
- The app refuses to start in production if its DB role is a superuser or has
  `BYPASSRLS` (these silently bypass RLS).
- Tenant-owned child tables will use composite FKs `(company_id, parent_id)` so a row can
  never reference another tenant's parent (`company_members` already exposes
  `UNIQUE (company_id, id)` as a target).
- `users`, `auth_sessions`, `refresh_tokens` are global identity tables (login must find a
  user before a tenant is known); they are only accessed by id / email / token hash.

## ADR-005 — Authentication (Accepted; IMPLEMENTED)

- Argon2id password hashing (OWASP minimum parameters, m=19 MiB, t=2, p=1).
- Access token: HS256 JWT, 15 min, claims `sub`, `cid` (company), `sid` (session), `typ`,
  `iss`, `aud`; algorithm pinned on decode. Kept in browser memory only.
- Refresh token: opaque random value, stored as SHA-256 hash, rotated on every refresh,
  httpOnly + Secure + SameSite=Strict cookie scoped to `/api/v1/auth`.
- Server-side sessions (`auth_sessions`): logout or refresh-token theft revokes the
  session and every access token referencing it is rejected immediately.
- Reuse of a rotated refresh token outside a 10 s grace window ⇒ treated as theft ⇒
  session revoked. The grace window (and cross-tab Web Locks in the frontend) prevents
  concurrent tabs from logging the user out.
- CSRF for cookie endpoints (`/auth/refresh`, `/auth/logout`): mandatory custom header
  (forces a CORS preflight) + Origin allow-list check.

## ADR-006 — Transcript retention & privacy (Accepted; schema field IMPLEMENTED, cleanup NOT IMPLEMENTED)

- Raw call audio is never persisted by our application.
- Transcripts are personal data. Default retention 30 days, configurable per company
  (`companies.transcript_retention_days`, 1–365, DB-enforced).
- Every transcript segment will carry `expires_at`; a jobs-system task deletes expired rows
  (Phase 5/8).
- Structured notes and business records are kept until the user deletes them, subject to
  the eventual privacy policy.
- Raw transcript content, passwords, tokens and secrets are never logged.

## ADR-007 — Telephony: feasibility spike first (Accepted; NOT IMPLEMENTED)

No production telephony code until the Phase 4 spike completes. Target flow (Option A):
the provider bridges the salesperson's normal phone and the customer's phone and forks a
real-time audio stream (ideally separate inbound/outbound tracks) to our backend. The call
must never depend on our backend/WebSocket staying connected. See `TELEPHONY.md`.

## ADR-008 — Real-time reliability (Accepted; NOT IMPLEMENTED)

Sequence-numbered events for reconnect/resume, idempotent provider webhooks, AI failure
never terminates the call.

## ADR-009 — AI architecture (Accepted; NOT IMPLEMENTED)

Incremental structured conversation state (never resend the whole transcript),
deterministic detectors for low-latency events, LLM for richer extraction, confidence
thresholds, de-duplication, a cap on visible live cards, schema-validated output. See `AI.md`.

## ADR-010 — No Docker requirement; Render for cloud (Accepted, 2026-09-23; IMPLEMENTED)

- Local development runs natively on Windows (Python 3.13, Node 22). Developers point
  `DATABASE_URL` at any PostgreSQL 16+ with pgvector (local install, portable binaries, or a
  cloud dev database). Docker is not required.
- Cloud: Render Web Service (API), Render Static Site (frontend), Render PostgreSQL
  (pgvector supported — VERIFIED in Render docs). A background worker only when needed.
- All environment-specific configuration comes from environment variables; `render.yaml`
  describes the deployment.
- The frontend calls the API on its own origin (`/api/*`), proxied by Vite in development
  and by a Render static-site rewrite in production, so the refresh cookie stays
  first-party with `SameSite=Strict`.

## ADR-011 — In-process rate limiting (Accepted; IMPLEMENTED)

Fixed-window limiter in memory for auth endpoints (per IP, and per email for login).
Correct for a single API instance; with multiple instances limits become per-instance.
Replace with a shared store only when we scale out.

## ADR-012 — Emails normalised to lower case (Accepted; IMPLEMENTED)

Emails are stored lower-cased (`CHECK (email = lower(email))`) with a unique index on
`lower(email)`, instead of the `citext` extension (portable across Postgres builds).

## ADR-013 — Migrations run at API startup on staging (Accepted, 2026-09-24; IMPLEMENTED)

Render's `preDeployCommand` is paid-only. `backend/scripts/start.sh` runs
`alembic upgrade head` before uvicorn with `set -euo pipefail`; a failed migration stops the
start (fail closed) and Alembic applies pending migrations in one transaction. This is only
safe with a single API instance starting at a time. **Revisit before scaling out** (use a
pre-deploy step or a migration job with an advisory lock).

## ADR-014 — Client IP from a configured number of trusted proxy hops (Accepted; IMPLEMENTED)

uvicorn runs with `--no-proxy-headers`. The app takes the client address from
`X-Forwarded-For` only at position `-TRUSTED_PROXY_HOPS` (entries left of it are
client-controlled and ignored). Default `0` ignores the header entirely. Any missing, short
or malformed header falls back to the TCP peer (stricter shared bucket, never bypassable).
The hop count must be measured per deployment, never guessed.
