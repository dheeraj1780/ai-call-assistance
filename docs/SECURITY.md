# Security

## Implemented in Phase 1

| Control | Implementation | Tested |
|---|---|---|
| Password hashing | Argon2id (m=19 MiB, t=2, p=1), rehash-on-login if params change | yes |
| Password policy | 10–128 chars, not blank | yes |
| User enumeration | identical 401 for unknown email / wrong password; dummy-hash timing equalisation | yes (response equality) |
| Access tokens | HS256 JWT, 15 min, alg pinned, `iss`/`aud`/`typ`/`exp` required | yes (expired, bad sig, tampered, alg=none, wrong aud/iss/typ, missing claims) |
| Session revocation | `sid` claim checked against `auth_sessions` on every request | yes (logout kills access token) |
| Refresh tokens | opaque, hashed at rest, rotated, httpOnly/Secure/SameSite=Strict, path `/api/v1/auth` | yes |
| Refresh theft detection | reuse after 10 s grace ⇒ session revoked + audit | yes |
| CSRF | cookie endpoints need `X-CSRF-Protection: 1` + allowed Origin; all other endpoints use bearer tokens (not ambient) | yes |
| Tenant isolation | tenant from token only, membership re-checked per request, `extra="forbid"` on updates, Postgres RLS (FORCE) | yes (API + DB level) |
| Authorization | role dependency (`OWNER`/`ADMIN` for company updates) | yes |
| Rate limiting | register/login/refresh per IP, login also per email | yes |
| Input validation | Pydantic schemas with length limits; validation errors never echo input | yes |
| Error hygiene | uniform envelope; unhandled errors → generic 500 with request id | yes |
| Security headers | nosniff, DENY framing, no-referrer, CSP `default-src 'none'`, `no-store`, HSTS in production | yes |
| CORS | explicit origin allow-list; wildcard rejected in production | yes |
| Secrets | env vars only; `.env` git-ignored; production refuses placeholder/short JWT secret or insecure cookies | yes (settings validation) |
| Logging | no bodies, no query strings, no headers; sensitive `extra` keys redacted | yes |
| Audit log | register, login, failed login, logout, refresh-token reuse, company update (field names only) | yes |
| DB role safety | startup check for superuser/BYPASSRLS | yes |

## Known limitations / follow-ups

- Rate limits are per process (ADR-011). Behind the Render static-site rewrite the client IP
  seen by the API may be the proxy's — **REQUIRES VERIFICATION on first deploy**; login is
  also limited per email regardless.
- Registration returns 409 for an existing email (standard UX trade-off; rate limited).
- No MFA, email verification, password reset or account lockout yet (not in Phase 1 scope).
- Failed logins for unknown emails are stored without a tenant and are visible only to
  operators with direct DB access.
- No CAPTCHA/bot protection on registration.

## Future phases (planned, NOT IMPLEMENTED)

- File upload validation (MIME sniffing, size limits) for the knowledge base.
- Prompt-injection defences for knowledge documents and transcripts (`AI.md`).
- Telephony webhook signature verification and idempotency (`TELEPHONY.md`).
- Encryption of Google OAuth refresh tokens at rest.
- Retention cleanup job for transcripts.
