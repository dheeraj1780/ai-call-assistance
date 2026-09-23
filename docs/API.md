# API

Base path: `/api/v1`. JSON only. Interactive docs at `/docs` in development (disabled in
production).

## Conventions

- **Auth:** `Authorization: Bearer <access_token>`. Tenant scope comes from the token —
  no endpoint accepts a company id.
- **Errors:** always
  ```json
  {"error": {"code": "invalid_token", "message": "…", "details": null, "request_id": "…"}}
  ```
  Codes so far: `unauthorized`, `invalid_token`, `invalid_credentials`,
  `invalid_refresh_token`, `forbidden`, `csrf_failed`, `not_found`, `method_not_allowed`,
  `email_taken`, `validation_error` (details = `[{loc, msg, type}]`), `rate_limited`
  (`Retry-After` header), `internal_error`, `not_ready`.
- **Request IDs:** send `X-Request-ID` (≤64 chars `[A-Za-z0-9._-]`) or one is generated;
  always returned in the response header and error body.
- **Pagination:** `?limit=` (1–200, default 50) & `?offset=`; response
  `{items, total, limit, offset}`.
- **Timestamps:** ISO-8601 with timezone. **IDs:** UUID.

## Endpoints (Phase 1)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register` | — | `{email, password, full_name, company_name}` → 201 `AuthResponse`; creates user + company + OWNER membership; sets refresh cookie |
| POST | `/auth/login` | — | `{email, password}` → `AuthResponse` |
| POST | `/auth/refresh` | refresh cookie + `X-CSRF-Protection: 1` | rotates the cookie → `AuthResponse` |
| POST | `/auth/logout` | refresh cookie + `X-CSRF-Protection: 1` | 204; revokes the session; idempotent |
| GET | `/me` | bearer | `{user, company, role}` |
| PATCH | `/me` | bearer | `{full_name?, phone?}` |
| GET | `/companies/current` | bearer | full company profile |
| PATCH | `/companies/current` | bearer, OWNER/ADMIN | `{name?, industry?, description?, website?, products_services?, target_customer?, ai_instructions?, transcript_retention_days? (1–365)}`; unknown fields → 422 |
| GET | `/companies/current/members` | bearer | paginated `{user_id, email, full_name, role, joined_at}` |
| GET | `/health` | — | liveness |
| GET | `/health/ready` | — | DB connectivity (503 `not_ready` if down) |

`AuthResponse`:
```json
{"access_token": "…", "token_type": "bearer", "expires_in": 900,
 "user": {"id": "…", "email": "…", "full_name": "…", "phone": null},
 "company": {"id": "…", "name": "…"}, "role": "OWNER"}
```

## Planned (not implemented)

`/contacts`, `/calls`, `/agendas`, `/copilot` (WebSocket), `/knowledge`, `/calendar`,
`/action-items`, `/webhooks/telephony/{provider}` — see the Phase 0 plan.
