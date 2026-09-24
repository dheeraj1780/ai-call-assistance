# Architecture

AI calling copilot for Indian MSMEs. The salesperson leads the call; the AI assists before,
during and after it. Modular monolith (ADR-001). See `DECISIONS.md` for the reasoning,
`../PROJECT_STATUS.md` for the current state and `../FINAL_MVP_AUDIT.md` for verification status.

## Shape

```
Browser (React SPA) ── REST /api/v1 ──────────────┐
        └── WebSocket /calls/{id}/live/ws ───────┐ │
                                                 ▼ ▼
                                        FastAPI (single process)
   routers → services → repositories → SQLAlchemy (tenant context per transaction)
        │            │                        │
        │            ├── AIGateway ──► AIProvider (Claude | mock)
        │            ├── EmbeddingProvider (hashing | Voyage)
        │            ├── CalendarProvider (Google | mock)
        │            └── jobs worker (in-process): knowledge embedding, post-call, retention
        │
        ├── /webhooks/telephony/{provider}  ◄── provider status callbacks (signed)
        └── /telephony/media/{provider}/{call} ◄── provider media fork (token)
                 │
                 ▼
        LiveSession ──► SpeechToTextProvider ──► final segments ──► DB (expires_at)
                 │                                              └──► LiveHub ──► browsers
                 └──► CopilotEngine (detectors, agenda, knowledge, budgeted LLM)
                                                 ▼
                          PostgreSQL 16 + pgvector (RLS FORCE'd on all tenant tables)
```

## Modules (`backend/app`)

| Module | Responsibility |
|---|---|
| `auth`, `users`, `tenants`, `audit` | Phase 1 foundation (sessions, rotating refresh tokens, RLS tenant context, audit log) |
| `contacts` | Contacts (customers/leads — a contact is the customer; `organization` holds their business, ADR-020) and free-form notes |
| `timeline` | Stored, tenant-scoped customer timeline events |
| `calls` | Call records and manual lifecycle; telephony fields |
| `agendas`, `call_prep` | Agenda items (status precedence), AI agenda suggestion, preparation context |
| `calendar` | `CalendarProvider`, OAuth, confirmed events |
| `telephony` | `TelephonyProvider`, webhooks, media ingest, lifecycle, simulator |
| `speech` | `SpeechToTextProvider` |
| `live` | `LiveSession` pipeline, transcript segments, `LiveHub`, live API/WebSocket |
| `copilot` | Deterministic detectors + `CopilotEngine` |
| `intel` | Copilot insights and structured call notes (+ human review) |
| `knowledge` | Upload/extraction/chunking, embedding job, retrieval, grounded answers |
| `postcall` | Post-call analysis job, summary, follow-up drafts |
| `dashboard` | Dashboard read model |
| `ai` | Provider interface, Claude/mock providers, gateway, embeddings, prompt safety |
| `jobs` | Postgres job queue + worker |
| `privacy` | Transcript retention sweep |
| `common` | Config, DB/tenant context, errors, logging, middleware, rate limiting, crypto |

## Real-time reliability rules

- The provider owns the phone call; our state follows its webhooks (browser never authoritative).
- Webhooks are verified, deduplicated and applied in lifecycle order only.
- STT failures are per track, with bounded re-open; AI failures mark the copilot "degraded".
- Browser WebSocket disconnects only end the subscription; reconnect resumes by `seq`/`epoch`.
- **Single process** (ADR-015): live sessions and the hub are in memory. Scaling out needs a
  shared pub/sub and sticky routing of the media stream — not built.

## Frontend (`frontend/src`)

Pages: Login, Register, Dashboard, Contacts, Contact detail (timeline/notes/calls/tasks), Add/Edit
contact, Plan call, Call preparation, Live call, Call detail (record + post-call summary + drafts +
calendar follow-up), Call history, Action items, Knowledge base, Calendar, Settings.
`lib/useLiveCall.ts` manages snapshot + WebSocket resume; `lib/live.ts` is a pure event reducer.

## Deployment

Not deployed. See `DEVELOPMENT.md` (Render staging) and `render.yaml`.
