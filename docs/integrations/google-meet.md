# Google Meet (real-time meeting copilot)

## Status (2026-09-25)

| Part | Status |
|---|---|
| Provider, channel, OAuth, meeting lookup, connect proxy, session events, UI | IMPLEMENTED |
| Backend tests (Google mocked with httpx.MockTransport) | PASS: 41 tests in `backend/tests/test_google_meet.py` |
| Browser bridge (Google's reference client, PCM framing, lifecycle) | PASS: unit/component tests with fakes (no WebRTC, no Google) |
| Real Google OAuth / Meet REST / Media API | **NOT VERIFIED.** Needs credentials and Developer Preview enrolment (`GOOGLE-MEET-CREDENTIALS-REQUIRED.md`) |
| Real Meet audio → Chirp 3 → copilot | **NOT VERIFIED** (`REAL-GOOGLE-MEET-TEST.md`) |
| Speaker attribution | NOT IMPLEMENTED: mixed audio, speaker "Unknown" |

## Architecture

```
Google Meet conference
   │  WebRTC (Opus, 3 virtual audio streams, data channels)            ← Google's servers
   ▼
Salesperson's browser: Google's Meet Media API reference client (vendored, src/vendor/meet-media-api)
   │ offer ──► POST /api/v1/calls/{id}/google-meet/connect ──► CallCopilot API
   │           (API: spaces.get + connectActiveConference with the user's OAuth token)
   │ answer ◄──────────────────────────────────────────────┘
   │ session-control / media-stats (uploaded as Google requests) / media-entries / participants
   ▼
Web Audio: 3 streams mixed at 16 kHz mono → PCM16 20 ms frames (src/lib/meet)
   │  existing media WebSocket /api/v1/telephony/media/google-meet/{call}?token=…
   ▼
MediaIngest → LiveSession → Chirp 3 (existing) → CopilotEngine (existing) → live hub → UI
```

- **Why the browser runs the WebRTC side.** Google ships the Media API client for the browser
  (TypeScript) and C++. The salesperson is in the meeting anyway. It needs no server media
  infrastructure, and Google's CORS limitation for localhost doesn't apply, because the REST call
  runs on our API.
- **No Google token in the browser.** The browser never holds a Google token. `connectActiveConference`
  is called by the API; its answer only contains SDP.
- **Provider-neutral core.**
  - `GoogleMeetCallingProvider` implements the existing `TelephonyProvider` interface.
  - Media uses the same JSON frame protocol as the Teams gateway.
  - Nothing outside `app/integrations/*google_meet*` and `src/lib/meet` knows about Google.

## Components

- **Backend:**
  - `app/integrations/providers/google_meet.py`: link parsing, OAuth client, Meet REST client,
    error mapping, mock, calling provider.
  - `app/integrations/google_meet_service.py`: per-user OAuth, token refresh, test, lookup,
    connect, events.
  - `app/integrations/google_meet_router.py`: HTTP endpoints.
  - Registry entry `GOOGLE_MEET`: fields, capabilities, requirements.
  - Migration `0010_google_meet`.
- **Frontend:**
  - `src/lib/meet/{bridge,pcm,meetingCode}.ts`
  - `src/components/MeetCopilotPanel.tsx`
  - the Google Meet account card on the Integrations page
  - the Google Meet option on the plan-call page

## Endpoints

| Method + path | Purpose |
|---|---|
| `GET /api/v1/integrations/google-meet/connection` | your Google connection (email, status) |
| `POST /api/v1/integrations/google-meet/connect` | start OAuth → `authorization_url` |
| `GET /api/v1/integrations/google-meet/oauth/callback` | OAuth redirect (signed state) |
| `DELETE /api/v1/integrations/google-meet/connection` | disconnect + revoke at Google |
| `GET /api/v1/integrations/google-meet/meetings?link=` | meeting lookup: exists? conference active? |
| `POST /api/v1/calls/{id}/google-meet/connect` | `{offer}` → `{answer, trace_id, space, media_ws_path, mock}` |
| `POST /api/v1/calls/{id}/google-meet/events` | `{event_id, state: waiting\|joined\|disconnected\|failed, reason}`; idempotent |

Plus the generic `/api/v1/integrations/google-meet` config, test and capability endpoints.

## Capabilities (advertised only as implemented)

- `REAL_TIME_CALL`: "Real-time Meeting Copilot".
- `MEETING_LOOKUP`.
- **Not offered:** meeting creation, transcript artifacts, participant metadata in the UI, video.

## Error codes (API and UI)

| Code | Meaning |
|---|---|
| `MEET_MEDIA_API_NOT_ELIGIBLE` | Developer Preview missing for the project, account or a participant |
| `MEET_CONSENT_REQUIRED` | No eligible consenter (Gmail: the initiator must be present) |
| `MEET_CONFERENCE_NOT_ACTIVE` | The conference hasn't started |
| `MEET_CONNECTIONS_EXHAUSTED` | Another Media API client is attached |
| `MEET_INCOMPATIBLE_PARTICIPANT` | An underage account or an old Meet app is present |
| `MEET_DISABLED` | Blocked by admin, host, watermarking or encryption |
| `MEET_MEETING_NOT_FOUND` | Meeting doesn't exist or isn't visible to the account |
| `MEET_SCOPE_MISSING` | Required Meet scopes not granted |
| `MEET_AUTH_EXPIRED` | Google sign-in expired or revoked; reconnect |
| `MEET_INVALID_OFFER` | Offer rejected (checked locally first, then by Google) |
| `MEET_UNAVAILABLE` | Google temporarily unavailable |
| `MEET_MOCK_MODE` | Mock mode can't receive real audio |

## Privacy and security

- **Audio:** transient only. It is held for one 20 ms frame in the browser, then in memory in the
  API. Nothing goes to disk, the database, object storage or logs. The database has no binary
  columns (asserted in tests).
- **Tokens:**
  - The refresh token is encrypted at rest.
  - Access tokens are minted per request, and are never stored, logged or sent to the browser.
  - Disconnecting revokes the grant at Google.
- **Isolation:**
  - `integration_user_connections` is per user and tenant (RLS).
  - The call endpoints check tenant and ownership.
  - The media WebSocket needs the per-call HMAC token.
- **Audit:** `google_meet.user_connected`, `user_disconnected`, `media_connected` and
  `media_connect_failed` are recorded; no tokens or content are stored in them.
- **Transcript storage:** off by default (Transient). Admins can choose "Store transcript & AI
  notes", but only with participants' consent.
- **Notice to participants:** Google shows participants the app-access notice, and the live screen
  states that meeting audio is processed.
- **Scopes:** the media scope is **restricted**. Publishing the OAuth app beyond test users requires
  Google's verification and security assessment. This is not a compliance certification.

## Known limitations

- **Developer Preview.** Everyone involved must be enrolled. Whether a personal Gmail account can
  enrol is unverified.
- **One Media API client per conference.** Google allows only one at a time.
- **Browser only.** Desktop Chrome or Edge; the tab must stay open while the copilot runs.
- **No speaker attribution.** Mixed audio makes the speaker Unknown. The copilot treats Unknown as
  possibly the customer, so notes are suggestions.
- **Media stats.** The media-stats upload follows Google's reference client unchanged.
