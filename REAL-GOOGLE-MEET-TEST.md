# First real Google Meet test (live audio → Chirp 3 → copilot)

**Status:** not run yet. Only this test can show **VERIFIED** for real Meet media. The mocked
tests (PASS) prove our code, not Google's side.

## Preconditions

- Everything in `GOOGLE-MEET-CREDENTIALS-REQUIRED.md` is done: API enabled, consent screen in
  Testing, Web OAuth client, env vars set, and the Developer Preview **approved** for the project,
  your account and every participant.
- The API runs on :8000 with `STT_PROVIDER=google` (Chirp 3, already verified) and the frontend on
  :5173.
- You use **desktop Chrome or Edge** for CallCopilot. You can join the Meet itself from any device;
  a current Meet app is required.
- In CallCopilot, the Google Meet integration is Live, your Google account is connected, and both
  capabilities are enabled.

## Procedure (record the time of each step)

1. **Create the meeting.** In Google Meet, signed in as the enrolled account, create a meeting
   (*New meeting → Start an instant meeting*) and **stay in it**. For a Gmail meeting, the
   initiator must be present to consent. Copy the link `https://meet.google.com/xxx-xxxx-xxx`.
2. **Admit the customer.** Let a second **enrolled** participant join as the "customer".
3. **Plan the call.** In CallCopilot, open a test contact → **Google Meet**, paste the link,
   language **English (India)**, then **Plan**. Open the call → **Start**. The status becomes
   *Initiated*.
4. **Connect the copilot.** On the live screen: **Connect copilot to meeting**. Expect:
   - **"Connecting…" → "Waiting for Google to admit the copilot…".** Google Meet shows the
     host/participants that an app wants access; **accept it in Meet**.
   - **"● Receiving meeting audio · N audio stream(s)".** The call status becomes *Active*.
5. **Speak the test conversation.** Use the 7-line script from
   `docs/integrations/teams-call-copilot.md`; the "customer" says the customer lines. Expect
   within about 1–2 s:
   - **Transcript:** lines appear. The speaker shows as *Unknown*, because Meet's mixed audio isn't
     attributed yet.
   - **Copilot:** cards, notes (Excel, 5 branches, budget, timeline, price objection) and agenda
     progress.
6. **Disconnect.** Click **Disconnect copilot**. The meeting itself continues. The call ends:
   "Finalising transcript…", then Completed.
7. **Test recovery.** Optionally, start another call on the same meeting and reconnect. Google
   allows one Media API client per conference, and releases the previous one after about 30 s.

## Evidence to collect (no secrets involved)

- **API log:**
  - `google_meet_connected` (with `trace_id`)
  - `stt_session_started` / `stt_session_ended` (`chunks_sent`, `final_latency_ms_*`)
  - `transcript_final` (`stt_latency_ms`, `copilot_ms`)
  - `live_session_closed`
- **Browser:** `chrome://webrtc-internals` shows 3 audio receivers with increasing `packetsReceived`.
- **CallCopilot:** the transcript, notes and agenda screenshots.

## Pass / fail

| Result | Meaning |
|---|---|
| **VERIFIED** | Step 4 reaches "Receiving meeting audio" **and** step 5 produces real transcript lines from speech in the Meet |
| `MEET_MEDIA_API_NOT_ELIGIBLE` | Developer Preview not active for the project, account or a participant |
| `MEET_CONSENT_REQUIRED` | The Gmail meeting's initiator isn't in the meeting, or didn't accept |
| `MEET_CONFERENCE_NOT_ACTIVE` | Nobody has joined the meeting yet |
| `MEET_CONNECTIONS_EXHAUSTED` | Another Media API client is still attached; wait 30 s |
| `MEET_INCOMPATIBLE_PARTICIPANT` / `MEET_DISABLED` | A participant, host or meeting setting blocks apps |
| `MEET_INVALID_OFFER` | Browser WebRTC not accepted; send me Google's trace id |
| Connected, but no transcript | Send me the `stt_session_ended` and `live_session_closed` lines |

"Meeting joined" alone is **not** success. Success means real speech appears as transcript.
