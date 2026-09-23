# Database

PostgreSQL 16 + pgvector. Migrations: Alembic (`backend/alembic/versions`). Conventions:
UUID primary keys (generated in the app), `timestamptz` everywhere, named constraints
(`pk_`, `fk_`, `uq_`, `ck_`, `ix_`), structured columns over JSON blobs.

## Tables (Phase 1 — migration `0001_foundation`)

| Table | Tenant-owned | RLS | Notes |
|---|---|---|---|
| `users` | no (global identity) | no | `email` lower-case enforced by CHECK; unique index on `lower(email)`; `is_active` |
| `companies` | yes (is the tenant) | FORCE | profile fields; `transcript_retention_days` default 30, CHECK 1–365 |
| `company_members` | yes | FORCE | `role IN (OWNER, ADMIN, MEMBER)`; `UNIQUE(user_id)` (one company per user, MVP); `UNIQUE(company_id, id)` for composite FKs |
| `auth_sessions` | no | no | one per login; `expires_at` (absolute max 30 d), `revoked_at`, `revoke_reason` |
| `refresh_tokens` | no | no | SHA-256 `token_hash` (unique), `expires_at`, `used_at` (rotation) |
| `audit_logs` | yes (nullable company) | FORCE | append-only for the app (no UPDATE/DELETE policies); `details` JSONB holds non-PII metadata only |

Extensions: `vector` (pgvector) is created by the first migration.

## Row-Level Security

Helper functions `app_current_company_id()` / `app_current_user_id()` read transaction-local
settings `app.company_id` / `app.user_id` (set by the app at every transaction start).

| Table | Policy |
|---|---|
| `companies` | `id = app_current_company_id()` (all commands) |
| `company_members` | SELECT: own company **or** own membership row (needed at login); INSERT/UPDATE/DELETE: own company only |
| `audit_logs` | SELECT: own company; INSERT: own company or NULL company; no UPDATE/DELETE |

`FORCE ROW LEVEL SECURITY` makes policies apply to the table owner too (on managed
Postgres the app role usually owns its tables). **Superusers and `BYPASSRLS` roles bypass
RLS**, so the app role must be neither; the API checks this at startup (fatal in
production) and a test asserts it.

## Pattern for future tenant-owned tables (Phase 2+)

```sql
company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
UNIQUE (company_id, id),                                 -- target for children
FOREIGN KEY (company_id, contact_id) REFERENCES contacts (company_id, id)  -- children
-- + ENABLE/FORCE RLS + policy company_id = app_current_company_id()
-- + indexes leading with company_id
```

## Operations

```
cd backend
uv run alembic upgrade head      # apply
uv run alembic downgrade base    # roll back everything (dev only)
uv run alembic check             # fail if models and migrations drift
```
