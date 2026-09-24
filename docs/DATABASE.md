# Database

PostgreSQL 16 (15+ required: composite FKs use `ON DELETE SET NULL (column)`) + pgvector.
Migrations: Alembic `0001` → `0007` (`backend/alembic/versions`). UUID keys, `timestamptz`,
named constraints, structured columns (JSON only for small display extras / diagnostics).

**Verification status:** migrations 0001 was applied and round-tripped on 2026-09-23.
Migrations **0002–0007 have NOT yet been applied to a real database** in this session
(local Postgres was not running) — see `../PROJECT_STATUS.md`.

## Tables

| Migration | Table | Tenant | RLS | Notes |
|---|---|---|---|---|
| 0001 | users | global identity | – | lower-case email CHECK + unique `lower(email)` |
| 0001 | companies | tenant | FORCE | `transcript_retention_days` 1–365 (default 30) |
| 0001 | company_members | ✓ | FORCE | one company per user; `UNIQUE(company_id,user_id)` (0002) is the FK target for member references |
| 0001 | auth_sessions, refresh_tokens | global | – | server-side sessions, hashed rotating refresh tokens |
| 0001 | audit_logs | ✓ (nullable) | FORCE | append-only for the app |
| 0002 | contacts | ✓ | FORCE | status, source, tags (GIN), attributes (JSONB object ≤20 pairs), owner (composite FK to member) |
| 0002 | contact_notes | ✓ | FORCE | author (composite FK) |
| 0002 | calls | ✓ | FORCE | lifecycle status, objective, desired outcome, outcome; telephony fields (0005) |
| 0002 | action_items | ✓ | FORCE | kind TASK/FOLLOW_UP/APPOINTMENT, status, source MANUAL/AI, confirmation, assignee |
| 0002 | timeline_events | ✓ | FORCE | category + event_type (extended in 0004), refs to note/call/action item (cascade) |
| 0003 | agenda_items | ✓ | FORCE | status + `status_source` (DEFAULT/DETERMINISTIC/AI/MANUAL) + confidence |
| 0003 | ai_usage_records | ✓ | FORCE | tokens, latency, estimated cost, success/error per AI/embedding call |
| 0003 | jobs | system | – | ids only; partial unique `dedupe_key` |
| 0004 | calendar_connections | ✓ | FORCE | encrypted refresh token; status ACTIVE/REAUTH_REQUIRED |
| 0004 | calendar_events | ✓ | FORCE | events created through the app (confirmed) |
| 0005 | call_routes, telephony_webhook_events | system | – | tenant routing for webhooks; `UNIQUE(provider,event_id)` idempotency; no personal data |
| 0005 | transcript_segments | ✓ | FORCE + retention | final segments only; `UNIQUE(call_id,seq)`; `expires_at` |
| 0005 | copilot_insights | ✓ | FORCE + retention | `UNIQUE(call_id,dedupe_key)`; `expires_at` |
| 0005 | call_notes | ✓ | FORCE | structured notes; status SUGGESTED/CONFIRMED/EDITED/REJECTED; `UNIQUE(call_id,dedupe_key)`; segment link SET NULL on purge |
| 0006 | knowledge_documents | ✓ | FORCE | metadata, status UPLOADED/PROCESSING/READY/FAILED, version |
| 0006 | knowledge_chunks | ✓ | FORCE | `vector(1024)` + HNSW (cosine) |
| 0007 | call_summaries | ✓ | FORCE | 1:1 call; scalar fields each with CONFIRMED/INFERRED/NOT_DISCUSSED |
| 0007 | follow_up_drafts | ✓ | FORCE | channel EMAIL/WHATSAPP/GENERAL; status DRAFT/APPROVED/COPIED/DISCARDED; warnings |

## Row-Level Security

Every tenant table: `tenant_isolation` policy `company_id = app_current_company_id()` (USING and
WITH CHECK), `FORCE ROW LEVEL SECURITY`. The app sets `app.company_id` / `app.user_id` at the
start of every transaction. Composite FKs `(company_id, <ref>_id)` make cross-tenant references
impossible even inside a correct tenant context.

Retention policies (0005) on `transcript_segments` and `copilot_insights`:
`current_setting('app.system_task', true) = 'retention' AND expires_at < now()` for SELECT and
DELETE — the sweep can remove expired rows across tenants and nothing else.

System tables without RLS (`users`, `auth_sessions`, `refresh_tokens`, `jobs`, `call_routes`,
`telephony_webhook_events`) contain no tenant business content and are only accessed by id.

## Operations

```
cd backend
uv run alembic upgrade head
uv run alembic check          # models vs migrations drift
uv run alembic downgrade base # dev only
```
