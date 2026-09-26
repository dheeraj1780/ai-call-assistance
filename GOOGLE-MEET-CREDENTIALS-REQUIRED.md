# Google Meet: what you must set up before the first real test

The Google Meet integration is built and tested with a mocked Google. **Nothing has been tested
against real Google Meet yet.** The next step needs your Google account, your Google Cloud project,
and Google's Developer Preview approval. I cannot do these for you.

> **Possible blocker for a personal Gmail account: please check this first.**
> - Google's Meet Media API documentation explicitly supports *consumer (Gmail-owned)
>   meetings*: the person who started the meeting must be present to consent.
> - However, the Developer Preview Program page asks for a *"Google Workspace account"* and never
>   mentions personal Gmail accounts (checked 2026-09-25).
> - So it is **UNVERIFIED** whether `jdeeran2004@gmail.com` can be enrolled. Apply (step 3); Google's
>   answer settles it. If Gmail is refused, the options are a Google Workspace or Workspace for
>   Education account.

---

## 1. Google Cloud settings to enable

- **Project:** `mystical-vial-509619-b8` (the one already used for Chirp 3 STT). Nothing extra is
  needed in the project beyond items 2 to 7.
- **Project number:** the Developer Preview form needs the *number*, not the ID. Find it in Cloud
  console → **IAM & Admin → Settings → Project number**.

## 2. API to enable

- **Google Meet REST API** (`meet.googleapis.com`):
  https://console.cloud.google.com/flows/enableapi?apiid=meet.googleapis.com&project=mystical-vial-509619-b8
- The Media API's `connectActiveConference` is part of this API (v2beta). Speech-to-Text is already
  enabled.

## 3. Developer Preview enrollment: yes, required

- The Meet Media API is a Developer Preview. Google states: *"the Google Cloud project, OAuth
  principal, and all participants in the conference must be enrolled in the Developer Preview
  Program."*
- **Terms:** read the program terms first: https://developers.google.com/workspace/preview
- **Application form:**
  https://docs.google.com/forms/d/e/1FAIpQLSd7BiMXXHDlUDkF7G0TSY5zfJbQwFNH3m6K_ZYFi3vCHLFbng/viewform?resourcekey=0-1uHeVg8junj3PPTLNcn7WQ
- **What the form asks for:** the Cloud **project number** and the account email. The email must
  accept being added to Google Groups. Service accounts cannot be enrolled.
- **Timing:** Google says approval usually takes "a couple of days"; contact them if you hear
  nothing within a week.

## 4. Google account that must be enrolled

- The **OAuth principal**: the Google account you click "Connect Google Account" with in
  CallCopilot. Planned: `jdeeran2004@gmail.com`, subject to the blocker above.
- It must be the account that **is in the Meet** and, for a Gmail meeting, **started it**
  (consent).

## 5. Participants that must be enrolled

**Every participant** in the test conference, for example the colleague acting as the customer.
- Use only enrolled test accounts for the first test.
- Google rejects the connection if any account in the meeting is ineligible, e.g. an underage
  account or an old Meet mobile app.

## 6. OAuth consent screen

Google Cloud console → **Google Auth Platform** (APIs & Services → OAuth consent screen):
- **User type:** External (you have no Workspace organisation).
- **App name:** e.g. "CallCopilot POC". **Support email / developer contact:** your email.
- **Publishing status:** **Testing**. Add `jdeeran2004@gmail.com` (and any other test user) under
  **Test users**. Restricted scopes work for test users without Google's verification.
- **Refresh tokens expire after 7 days in Testing:** reconnect in CallCopilot if Google says the
  sign-in expired.
- **Scopes** (Data Access → Add scopes): the three listed in section 7. The media scope is
  *restricted*, so publishing the app to real users later requires Google's verification and a
  security assessment. It is not needed for testing.
- **"Google hasn't verified this app":** expect this warning when you connect; choose
  *Continue*.

## 7. Exact scopes (requested by the app; nothing else)

```
openid
email
https://www.googleapis.com/auth/meetings.space.readonly
https://www.googleapis.com/auth/meetings.conference.media.audio.readonly
```

- **`meetings.space.readonly`** (sensitive): reads the meeting space and whether a conference is
  active.
- **`meetings.conference.media.audio.readonly`** (restricted): live audio via the Media API.
- **Not requested:** video, `meetings.conference.media.readonly`, or Drive.

## 8. OAuth client type

**Web application**: Google Auth Platform → Clients → **Create client** → *Web application*.
- Use a **new client just for Meet**, e.g. named "CallCopilot Meet (local)".
- Don't reuse the service-account key used for STT; that is a different mechanism.

## 9. Redirect URI to register on that client (exactly)

```
http://localhost:8000/api/v1/integrations/google-meet/oauth/callback
```

- **Authorized JavaScript origins:** none needed; the browser never calls Google directly.
- **Different API URL:** if you later run the API elsewhere, register
  `https://<api-host>/api/v1/integrations/google-meet/oauth/callback` and set
  `GOOGLE_MEET_REDIRECT_URI`.
- **Where to check it:** CallCopilot shows the exact value under Settings → Integrations → Google
  Meet → *Provider setup values*.

## 10. Where to put the credentials locally

- **File:** `backend/.env`. It is git-ignored and never committed. Edit it yourself in a text editor.
- **Don't paste it anywhere else:** not in chat, and not in any other file.
- **JSON download:** if Google offers a client-secret JSON download, you don't need it. Only the
  two values below are used.

## 11. Environment variables

```
GOOGLE_MEET_CLIENT_ID=<...>.apps.googleusercontent.com
GOOGLE_MEET_CLIENT_SECRET=<client secret>
GOOGLE_MEET_REDIRECT_URI=http://localhost:8000/api/v1/integrations/google-meet/oauth/callback
```

These are already set and unchanged: `STT_PROVIDER=google`, `GOOGLE_CLOUD_PROJECT_ID`,
`GOOGLE_APPLICATION_CREDENTIALS`, `STT_LANGUAGE_CODE`, `TOKEN_ENCRYPTION_KEY` (or the dev
fallback).

## 12. Exact commands and UI steps after setting the variables

```
cd backend && uv run alembic upgrade head          # adds the Google Meet provider/channel (0010)
cd backend && uv run uvicorn app.main:app --port 8000
cd frontend && npm run dev                         # http://localhost:5173 (Chrome or Edge)
```

Then, in CallCopilot as an admin:
1. **Configure.** Settings → Integrations → **Google Meet** → Mode **Live**. Set *Meeting transcript
   storage* to **Transient** for the first test, then **Save**.
2. **Connect.** **Connect Google Account** → sign in as the enrolled account → allow both Meet
   permissions. You should see "Your Google account is connected for Google Meet."
3. **Test.** **Test Connection** should show ✓ OAuth client, ✓ account connected, ✓ sign-in valid,
   ✓ Meet permissions granted. "Developer Preview access" stays unconfirmed until the first live
   connection; that is expected.
4. **Enable.** **Enable** "Real-time Meeting Copilot" and "Meeting lookup".

## 13. Real test procedure

See **REAL-GOOGLE-MEET-TEST.md**.

## 14. What to tell me after setup (safe to share)

- "Meet API enabled" and "consent screen in Testing with test users added": yes or no.
- Developer Preview: **approved / refused / pending**, and for which account(s).
- Whether the Gmail account was accepted, or which account type Google required.
- The result of **Test Connection** (the check list; it contains no secrets).
- During the live test: the error **code** shown in CallCopilot (e.g. `MEET_CONSENT_REQUIRED`) and
  Google's **trace id** if one is shown. The trace id is not a secret.
- The **client ID** is not secret, but I don't need it.

## 15. What you must NOT give me

- **Never** paste into chat:
  - the **OAuth client secret** (`GOCSPX-...`)
  - **refresh tokens**, **access tokens** (`ya29...`) or **authorization codes**
  - **service-account private keys** or JSON key files
  - **passwords** or 2FA codes
- **Not needed:** the contents of `backend/.env`. Just say "done".
- **Pasted by mistake:** tell me, and rotate the secret in Google Cloud (Clients → reset secret).
