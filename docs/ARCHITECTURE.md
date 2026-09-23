# Architecture

AI calling copilot for Indian MSMEs. The human salesperson leads the call; the AI assists
before, during and after it. See `DECISIONS.md` for the reasoning behind each choice.

## Current status (end of Phase 1)

| Area | Status |
|---|---|
| Repository, tooling, configuration | IMPLEMENTED |
| Auth (register/login/refresh/logout, `/me`) | IMPLEMENTED, tested |
| Companies, membership, roles | IMPLEMENTED, tested |
| Tenant isolation (app layer + Postgres RLS) | IMPLEMENTED, tested |
| Structured logging, request IDs, error schema, audit log | IMPLEMENTED, tested |
| React shell: login, register, dashboard | IMPLEMENTED, tested |
| Render deployment (`render.yaml`) | WRITTEN, NOT YET DEPLOYED |
| Contacts, calls, agenda, copilot, knowledge, calendar | NOT IMPLEMENTED (Phases 2–7) |
| Telephony | NOT IMPLEMENTED — feasibility spike first (Phase 4) |

## Shape

```
Browser (React SPA)
   │  same-origin /api/*  (Vite proxy in dev, Render static-site rewrite in prod)
   ▼
FastAPI (modular monolith)
   ├── middleware: request ID → access log → security headers → error envelope
   ├── routers  (/api/v1/…)
   ├── services (use cases, transactions, audit)
   ├── repositories (tenant-scoped queries)
   └── SQLAlchemy async session ── sets app.company_id / app.user_id per transaction
   ▼
PostgreSQL 16 + pgvector  (Row-Level Security FORCE'd on tenant tables)
```

Future external integrations (behind interfaces, none implemented yet): telephony provider,
speech-to-text, LLM (Claude), embeddings, Google Calendar.

## Backend layout

```
backend/
  app/
    main.py              app factory, middleware, routers, health endpoints
    common/              config, db (engine/session/tenant context), logging, middleware,
                         errors, rate_limit, models base, shared schemas
    auth/                passwords, tokens, sessions/refresh tokens, service, router, deps
    users/               user model, /me
    tenants/             companies, memberships, repository, router
    audit/               audit log model + helpers
  alembic/               migrations (0001_foundation)
  tests/                 real-Postgres tests
```

Module rule: routers are thin; services own transactions and call `session.commit()`
explicitly; repositories take the tenant id explicitly. Domain code must not import vendor
SDKs.

## Request lifecycle (authenticated)

1. `RequestContextMiddleware` assigns/validates `X-Request-ID`, adds security headers,
   catches unhandled errors (generic 500 envelope) and writes one access-log line
   (method, path without query string, status, latency, request/user/company ids).
2. `get_principal` verifies the JWT, binds the tenant context to the DB session, and in one
   query checks: membership for (user, company), user active, session not revoked/expired.
3. The handler/service queries through repositories; RLS filters anything that slips
   through.
4. Services commit explicitly; the session dependency rolls back anything uncommitted.

## Frontend layout

```
frontend/src/
  lib/api.ts          fetch wrapper: in-memory access token, refresh-on-401, error parsing,
                      refresh coalescing (in-tab) + Web Locks (cross-tab)
  auth/               AuthProvider (session restore via refresh cookie), useAuth, guards
  pages/              LoginPage, RegisterPage, DashboardPage
  components/         AppLayout, small UI primitives
```

## Deployment (Render)

See `render.yaml` and `DEVELOPMENT.md → Deploying to Render`. API = Render Web Service
(migrations via `preDeployCommand`), frontend = Render Static Site with `/api/*` rewrite to
the API, database = Render PostgreSQL. No background worker yet.
