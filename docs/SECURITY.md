# Security

Status labels: **TESTED** (automated test exists and passed), **WRITTEN** (test exists, not yet
run against Postgres in this session), **UNIT** (DB-free test passed), **DESIGN** (implemented, no
dedicated test).

## Controls

| Area | Control | Status |
|---|---|---|
| Passwords | Argon2id (OWASP params), rehash on login, 10–128 chars | TESTED (Phase 1) |
| Sessions | 15-min JWT (alg pinned, iss/aud/typ/exp), server-side sessions, rotating hashed refresh tokens, reuse detection (10 s grace), logout revokes | TESTED (Phase 1) |
| CSRF | Cookie endpoints need `X-CSRF-Protection` + allowed Origin; everything else uses bearer tokens | TESTED (Phase 1) |
| Tenant isolation | Tenant from token only; membership re-checked per request; `extra="forbid"` bodies; FORCE RLS on every tenant table; composite tenant FKs | Phase 1 TESTED; CRM/calls/transcripts/notes/knowledge/post-call/calendar isolation tests WRITTEN |
| Authorization | Owner/admin vs member rules (contacts, notes, calls, action items, agenda, notes review, knowledge management, company settings) | WRITTEN |
| Webhooks | HMAC-SHA256 over `timestamp.body`, 5-min replay window, size cap, unique event id, ordered application | WRITTEN |
| Media stream | Per-call expiring HMAC token; token only in URL query (never logged: access log records paths only) | WRITTEN |
| Browser WebSocket | Access token in the first message (not URL), tenant check, 10 s auth timeout; no cookies → no cross-site WebSocket hijacking | WRITTEN |
| Rate limiting | Auth endpoints per IP/email; user-triggered AI per company; client IP from configured trusted hop count (ADR-014) | TESTED (auth), UNIT (IP resolution) |
| Input validation | Pydantic schemas with lengths, enums, aware datetimes, E.164-ish phones, tag/attribute caps | WRITTEN |
| SQL injection | SQLAlchemy parameters everywhere; raw SQL only with bound parameters; LIKE wildcards escaped | WRITTEN (search test) |
| XSS | React escapes output; no `dangerouslySetInnerHTML`; API CSP `default-src 'none'` | DESIGN |
| SSRF | No user-controlled outbound URLs (provider endpoints are fixed constants) | DESIGN |
| File uploads | Admin only; size cap; extension + magic-byte checks; DOCX zip-bomb limits; PDF page/text limits; originals not stored | WRITTEN |
| Prompt injection | Data blocks, no model tools, schema-only outputs, server-side grounding checks | UNIT (delimiter test) + WRITTEN |
| AI action boundary | AI cannot send messages, create calendar events or confirm tasks; drafts need humans; no send endpoint | WRITTEN (OpenAPI assertion) |
| Secrets | Env vars only; production refuses placeholder/short secrets, missing webhook/encryption secrets, or mock providers unless explicitly allowed | UNIT |
| Sensitive logging | No bodies/headers/query strings; sensitive `extra` keys redacted; SQL errors hide bound parameters; no transcript text in logs | TESTED (Phase 1) + DESIGN |
| OAuth tokens | Refresh tokens Fernet-encrypted; signed short-lived OAuth state | WRITTEN |
| DB role | Startup refuses superuser/BYPASSRLS role in production | TESTED (Phase 1) |

## Known limitations

- Rate limits and live sessions are per process (single instance).
- Registration reveals whether an email exists (409).
- No MFA, email verification, password reset, or account lockout.
- WebSocket and webhook security depend on a real provider's signing scheme once integrated.
- The Render hop count for client IPs is NOT VERIFIED until measured on a deployment.
- No automated dependency/vulnerability scanning in CI (no CI configured).
