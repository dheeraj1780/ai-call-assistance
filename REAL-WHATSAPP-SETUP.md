# Real WhatsApp (Meta Cloud API) setup for the CallCopilot POC

**Status:** the WhatsApp integration is implemented and tested against a *mocked* Meta API. **No
real Meta request or real webhook has been processed yet.** These steps need your Meta account and
cannot be done by Claude.

**Never paste into chat:** the access token, the app secret or the verify token. You type them
into CallCopilot's own form (Settings → Integrations → WhatsApp), which stores them encrypted.

---

## 0. Before you start (local machine)

1. **Set a token-encryption key** in `backend/.env`, *before* saving real Meta secrets. Generate it
   yourself (for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`) and never
   change it afterwards; stored secrets can't be decrypted with another key.
   ```
   TOKEN_ENCRYPTION_KEY=<your random 48+ char value>
   ```
2. **Get a public HTTPS URL for the API.** Meta only sends webhooks to a public `https://` URL, not
   `localhost`. The main app is not deployed, so for this POC you need either:
   - a tunnel to your PC (e.g. `cloudflared tunnel --url http://localhost:8000`, or `ngrok http
     8000`), **or**
   - a deployed API.

   This is your decision; both expose the API publicly while running. Then set:
   ```
   PUBLIC_BASE_URL=https://<your-public-host>
   ```
   Restart the API afterwards. A tunnel URL changes each time a quick tunnel restarts, so you would
   then update it in Meta (step 5).

## 1. Meta developer account and app

1. Go to https://developers.facebook.com → **My Apps → Create app**. Choose the WhatsApp use case
   ("Connect with customers through WhatsApp"; app type Business) and link or create a Meta
   Business portfolio.
2. The app gets the **WhatsApp** product. Meta creates a **test WhatsApp Business Account** and a
   **test phone number** (free, for development).

## 2. Values CallCopilot needs, and where to find them

| CallCopilot field | Where in Meta | Secret? |
|---|---|---|
| Phone number ID | App Dashboard → WhatsApp → **API Setup** → "Phone number ID" (not the phone number) | no |
| WhatsApp Business Account ID | same page → "WhatsApp Business Account ID" | no |
| Meta App ID (optional) | App Dashboard → App settings → Basic | no |
| Access token | see step 3 | **yes** |
| App secret | App Dashboard → **App settings → Basic → App secret → Show**. Used to verify webhook signatures (`X-Hub-Signature-256`) | **yes** |
| Webhook verify token | **you invent it**: 16–128 characters of `A-Z a-z 0-9 _ - . ~`. Enter the same value in Meta (step 5) | treat as secret |

## 3. Access token

- **Quick test:** API Setup → "Generate access token" gives a temporary token that **expires in about
  24 hours**. When it expires, sends fail with "access token rejected"; paste a new one.
- **Durable (recommended):**
  1. Open **Meta Business Suite → Settings → Users → System users**.
  2. Create a system user and assign it your app and the WhatsApp Business Account (full control).
  3. Generate a token for the app with permissions **`whatsapp_business_messaging`** and
     **`whatsapp_business_management`**.

## 4. Configure CallCopilot (as an admin)

1. Start the API (with `PUBLIC_BASE_URL` set) and the frontend.
2. Open **Settings → Integrations → WhatsApp Business**, choose Mode **Live**, and fill in the
   fields from step 2. Then **Save**.
3. **Test Connection.** Expect:
   - ✓ "Access token can read the phone number"
   - ✓ "Phone number belongs to the WhatsApp Business Account"
   - "A Meta app is subscribed to this WhatsApp Business Account's webhooks" (informational):
     - If it shows ✗, subscribe your app to the WABA, running this **yourself** in a terminal (the
       token stays on your machine):
       ```
       curl -X POST "https://graph.facebook.com/v23.0/<WABA_ID>/subscribed_apps" -H "Authorization: Bearer <ACCESS_TOKEN>"
       ```
     - Then run Test Connection again. Without a subscription, **no inbound messages arrive**.
4. **Enable** the **Messages** capability. It requires `PUBLIC_BASE_URL` to be `https://`.
5. Copy the **Webhook URL** shown under *Provider setup values*:
   `https://<your-public-host>/api/v1/integrations/whatsapp/webhooks/<integration-id>`

## 5. Configure the webhook in Meta

1. In App Dashboard → **WhatsApp → Configuration → Webhook → Edit**:
   - **Callback URL:** the Webhook URL from step 4.5
   - **Verify token:** exactly the value you saved in CallCopilot
   - Click **Verify and save**. CallCopilot answers Meta's challenge; its requirement list then
     shows the webhook as verified.
2. Under **Webhook fields**, **subscribe to `messages`**. That field carries inbound messages *and*
   delivery/read statuses.

## 6. Test recipient (test number only)

In API Setup → **To** → "Manage phone number list", add the WhatsApp number you will test with and
confirm the code Meta sends. With the test number you can only send to at most 5 verified numbers;
otherwise Meta returns error 131030, which CallCopilot shows as "not in the allowed recipient list".

## 7. Graph API version

- **Default:** CallCopilot calls `https://graph.facebook.com/v23.0`.
- **Change it:** set `WHATSAPP_GRAPH_API_VERSION=vNN.N` in `backend/.env` (then restart).
- **Check before testing:** that v23.0 is still supported for your app (App settings → Advanced →
  API version).

## What this POC deliberately does not do

- **No template messages.** You can only reply within **24 hours of the customer's last message**,
  so the customer must message first.
- **No bulk sending, campaigns or automatic replies.** Every message is sent by a person clicking
  **Send**; AI replies are suggestions only.
- **Inbound media** (images, voice notes, documents) is shown as a label only. It is not downloaded
  or stored.

## What to tell Claude afterwards (safe)

- "Configured in Live mode" plus the **Test Connection** result list. It contains no secrets.
- Whether Meta's **Verify and save** succeeded.
- The API log lines `webhook_rejected` (reason only) or `message_send_failed` (error code), if any.

Do **not** share the access token, app secret, verify token, `.env` contents or Meta
login codes.
