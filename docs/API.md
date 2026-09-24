# API

Base path: `/api/v1`. JSON unless noted. Interactive docs at `/docs` in development (disabled in
production). Tenant scope always comes from the access token — no endpoint accepts a company id.

## Conventions

- **Auth:** `Authorization: Bearer <access_token>`; refresh via httpOnly cookie + `X-CSRF-Protection: 1`.
- **Errors:** `{"error": {"code", "message", "details", "request_id"}}`. Cross-tenant ids return 404.
  Notable codes: `validation_error`, `invalid_reference` (422, referenced record not in your
  company), `invalid_state` / `invalid_status_transition` (409), `missing_phone` (422),
  `ai_unavailable` / `ai_budget_exceeded` (503), `calendar_not_connected` (409),
  `calendar_reauth_required` (409), `unsupported_document` family (415), `rate_limited` (429).
- **Pagination:** `limit`/`offset` → `{items, total, limit, offset}`; the timeline uses a keyset
  cursor (`before` → `next_cursor`).
- **Timestamps:** ISO-8601 with timezone; client-supplied datetimes must include an offset.

## Endpoints

| Method | Path | Summary |
|---|---|---|
| GET | `/action-items` | List Action Items |
| POST | `/action-items` | Create Action Item |
| GET | `/action-items/{item_id}` | Get Action Item |
| PATCH | `/action-items/{item_id}` | Update Action Item |
| DELETE | `/action-items/{item_id}` | Delete Action Item |
| POST | `/action-items/{item_id}/confirm` | Confirm Action Item |
| POST | `/auth/login` | Login |
| POST | `/auth/logout` | Logout |
| POST | `/auth/refresh` | Refresh |
| POST | `/auth/register` | Register |
| POST | `/calendar/connect` | Connect |
| GET | `/calendar/connection` | Get Connection |
| DELETE | `/calendar/connection` | Disconnect |
| GET | `/calendar/events` | List Events |
| POST | `/calendar/events` | Create Event |
| PUT | `/calendar/events/{event_id}` | Update Event |
| GET | `/calendar/upcoming` | Upcoming |
| GET | `/calls` | List Calls |
| POST | `/calls` | Create Call |
| GET | `/calls/{call_id}` | Get Call |
| PATCH | `/calls/{call_id}` | Update Call |
| GET | `/calls/{call_id}/agenda` | Get Agenda |
| PUT | `/calls/{call_id}/agenda` | Replace Agenda |
| POST | `/calls/{call_id}/agenda/suggest` | Suggest Agenda |
| PATCH | `/calls/{call_id}/agenda/{item_id}` | Override Agenda Item |
| POST | `/calls/{call_id}/end` | End Call |
| PATCH | `/calls/{call_id}/follow-ups/{draft_id}` | Review Draft |
| POST | `/calls/{call_id}/insights/{insight_id}/dismiss` | Dismiss Insight |
| GET | `/calls/{call_id}/live` | Live Snapshot |
| GET | `/calls/{call_id}/notes` | List Notes |
| POST | `/calls/{call_id}/notes` | Add Note |
| PATCH | `/calls/{call_id}/notes/{note_id}` | Review Note |
| DELETE | `/calls/{call_id}/notes/{note_id}` | Delete Note |
| GET | `/calls/{call_id}/post-call` | Get Post Call |
| POST | `/calls/{call_id}/post-call/retry` | Retry Post Call |
| GET | `/calls/{call_id}/prep` | Get Prep |
| POST | `/calls/{call_id}/simulate` | Simulate |
| POST | `/calls/{call_id}/start` | Start Call |
| GET | `/calls/{call_id}/transcript` | Transcript |
| GET | `/companies/current` | Get Current Company |
| PATCH | `/companies/current` | Update Current Company |
| GET | `/companies/current/members` | List Members |
| GET | `/contacts` | List Contacts |
| POST | `/contacts` | Create Contact |
| GET | `/contacts/{contact_id}` | Get Contact |
| PATCH | `/contacts/{contact_id}` | Update Contact |
| DELETE | `/contacts/{contact_id}` | Delete Contact |
| GET | `/contacts/{contact_id}/notes` | List Notes |
| POST | `/contacts/{contact_id}/notes` | Add Note |
| PATCH | `/contacts/{contact_id}/notes/{note_id}` | Update Note |
| DELETE | `/contacts/{contact_id}/notes/{note_id}` | Delete Note |
| GET | `/contacts/{contact_id}/timeline` | Get Timeline |
| GET | `/dashboard` | Dashboard |
| GET | `/knowledge/documents` | List Documents |
| POST | `/knowledge/documents` | Upload Document |
| GET | `/knowledge/documents/{document_id}` | Get Document |
| DELETE | `/knowledge/documents/{document_id}` | Delete Document |
| POST | `/knowledge/query` | Query |
| GET | `/me` | Get Me |
| PATCH | `/me` | Update Me |

Plus (not in OpenAPI):

| Kind | Path | Notes |
|---|---|---|
| POST | `/webhooks/telephony/{provider}` | Provider status callbacks; HMAC signature + timestamp required; idempotent |
| WebSocket | `/telephony/media/{provider}/{call_id}?token=` | Provider media fork; per-call HMAC token |
| WebSocket | `/calls/{call_id}/live/ws` | Browser live events; first message `{"type":"auth","token",...,"last_seq","epoch"}` |
| GET | `/health`, `/health/ready` | Liveness / DB readiness |
| GET | `/health/client-ip` | Deployment diagnostic, 404 unless `DIAGNOSTICS_ENABLED=true` |

## Live WebSocket events

`call.status`, `transcript.partial` (not replayed), `transcript.final`, `agenda.updated`,
`insight.created`, `insight.dismissed`, `note.upserted`, `note.deleted`, `stt.status`,
`copilot.status`, plus control messages `hello`, `resync`, `pong`. Each event carries `seq` and
`epoch`; clients reconnect with the last values to receive missed events.

## Deliberately absent

There is no endpoint that sends email/WhatsApp/SMS to customers, and none that creates calendar
events without `confirm: true`.
