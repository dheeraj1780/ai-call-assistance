# PROJECT STATUS — AI Calling Copilot (MSME)

_Last updated: 2026-09-25. This file is the hand-off point for any future session._

- **Current branch:** `phase-2-crm` (local only, **not pushed**; all MVP phases live here)
- **`main`:** Phase 1 (`d2815f9`, pushed) + staging deploy config (`c0903f6`, **not pushed**)
- **Last commit:** see `git log -1` (docs + final audit on `phase-2-crm`)
- **Deployment:** **NOT DEPLOYED** (intentionally deferred; see `docs/DEVELOPMENT.md`)
- **Next step:** controlled real-provider tests, one provider at a time, with the user's
  credentials (see `docs/integrations/testing.md`); then a deployment/Render verification phase.

## Multi-channel integrations (2026-09-25)

Architecture and activation guides: `docs/integrations/` (overview, microsoft-teams, whatsapp,
plivo, credentials, testing). Migration `0008_integrations`.

| Item | Status |
|---|---|
| Provider/channel/capability model, Settings → Integrations (write-only encrypted secrets, test, enable, config check) | IMPLEMENTED · MOCK VERIFIED |
| Common conversation model + safe contact matching + shared AI assist for messages | IMPLEMENTED · MOCK VERIFIED |
| WhatsApp Cloud API messaging | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| WhatsApp voice calling | NOT SUPPORTED (needs a WebRTC/SIP media service; shown as NOT_AVAILABLE) |
| Teams messaging (delegated Graph, chat linking, notifications) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Teams real-time call copilot: API contract, mock gateway, transient/declared-recording persistence | IMPLEMENTED · MOCK VERIFIED |
| `teams-media-gateway/` (.NET) | SOURCE ONLY — NOT BUILT (no .NET SDK here), NOT VERIFIED |
| Plivo phone calls (outbound bridge, inbound, callbacks, V3 signatures) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Plivo real-time audio streaming | IMPLEMENTED · MOCK VERIFIED; blocked in LIVE mode until a real streaming STT provider is added |

**Blocking gap for real-time copilot on real calls:** only the mock STT exists; Plivo audio and
Teams calling need a real streaming speech-to-text provider (a product/cost decision).

Verification: backend **337 passed** (239 existing + 98 new), ruff + strict mypy clean,
`alembic check` clean, migration 0008 up/down/up verified; frontend 49 tests passed, typecheck,
lint and production build clean.

## Local verification (2026-09-24)

Against local PostgreSQL 18.2 (port 5433, pgvector 0.8.1) using the non-superuser role
`callcopilot` (NOSUPERUSER, NOBYPASSRLS): migrations 0001–0007 applied to `callcopilot_test` and
`callcopilot`; `alembic check` clean; **full backend suite 239 passed**; ruff + strict mypy clean;
RLS verified as the app role (0 rows without tenant context, cross-tenant reads/updates blocked);
API starts, `/health/ready` = ready, register/me/contacts smoke test + cross-tenant 404 passed.
Fixes from that run: MissingGreenlet in call/action-item PATCH, provider-specific knowledge
threshold, `tzdata` for Windows, figure-warning `%` regex, simulator turn-taking; conftest refuses
a TEST_DATABASE_URL that equals DATABASE_URL.

## Environment notes

- No Docker. Local PostgreSQL 18.2 service on port **5433** (the user manages it); app role
  `callcopilot` (non-superuser, owns the schema objects); DBs `callcopilot` (app) /
  `callcopilot_test` (tests). The `postgres` superuser must never be used by the app (bypasses RLS).

## Phases

| # | Phase | Status | Verification |
|---|---|---|---|
| 1 | Foundation | DONE `d2815f9` | 88 backend + 17 frontend tests passed (2026-09-23) |
| – | Staging deploy config | DONE `c0903f6` (main) | unit tests; fail-closed startup verified; NOT DEPLOYED |
| 2–3 | CRM + customer timeline | CODE COMPLETE `ef786b5` | frontend tests pass; DB tests PASS |
| 4 | Call prep, AI agenda, AI provider layer, jobs | CODE COMPLETE `5747663` | DB tests PASS |
| 5 | Google Calendar | CODE COMPLETE `372c611` | adapter unit-tested (MockTransport); NOT LIVE VERIFIED |
| 6–7 | Telephony abstraction, lifecycle, webhooks, sessions | CODE COMPLETE `dbb9514` | MOCK only; DB tests PASS; LIVE PROVIDER NOT VERIFIED |
| 8–9 | Streaming STT abstraction, live transcript | CODE COMPLETE `dbb9514` | MOCK STT only |
| 10–13 | Live copilot, agenda intelligence, requirements, objections | CODE COMPLETE `dbb9514` | detector unit tests pass |
| 14–15 | Knowledge base + live retrieval | CODE COMPLETE `dbb9514` | DB tests PASS; Voyage NOT VERIFIED |
| 16 | Structured live notes | CODE COMPLETE `dbb9514` | DB tests PASS |
| 17–21 | Post-call, action items, follow-ups, history, timeline | CODE COMPLETE `3282d79` | DB tests PASS |
| 22 | Dashboard | CODE COMPLETE `3282d79` | DB test PASS |
| 23–27 | AI safety, privacy/retention, security, reliability, cost | IMPLEMENTED across phases | see FINAL_MVP_AUDIT.md |
| 28 | Test suite | DONE | 239 backend tests pass |
| 29 | UI | 15 screens | NOT verified in a real browser |
| 30 | End-to-end journey | automated test (mock providers) | PASS |
| 31 | Documentation | DONE `f6dc33a` | – |
| 32 | Code quality pass | DONE | lint, types, TODO scan, log-safety fix |

## Known limitations

Mock telephony/STT only; single-process real-time pipeline; lexical hashing embeddings by
default; Claude/Voyage/Google not live verified; no consent capture; no browser verification;
Render proxy hop count unmeasured. Details: `FINAL_MVP_AUDIT.md`.
