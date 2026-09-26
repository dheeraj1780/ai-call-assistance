# First real WhatsApp test (Meta Cloud API)

**Status:** not run. Only this test can show **VERIFIED** for real WhatsApp. The automated tests
(mocked Meta) prove CallCopilot's handling, not Meta's side.

**Preconditions:** `REAL-WHATSAPP-SETUP.md` is complete:
- the integration is Live, Test Connection passes and Messages is enabled
- Meta's webhook is verified, with `messages` subscribed
- the public URL is running
- your test phone (the "customer") is in the test number's recipient list

Record the time of each step.

## A. Inbound: customer → CallCopilot

1. **Customer writes first.** From your personal WhatsApp (the "customer"), send a message to the
   business test number, e.g. *"Hi, do you have inventory software for 5 branches? Budget around
   2 lakh."*
2. **Check the conversation.** Within a few seconds, **Conversations** (refreshes every 10 s) shows
   a WhatsApp conversation with the message text and the WhatsApp profile name.
   - **Number matches a contact's phone:** the conversation is linked automatically, and the
     contact's timeline shows "WhatsApp message received from customer".
   - **Unknown number:** click **Create contact from this sender** (or link an existing contact).
     A contact with that phone number is created, the conversation is linked, and the next message
     goes to the timeline directly.
3. **AI assist.** Within a few seconds the conversation shows an **AI suggestion** (a draft), plus
   suggested notes (e.g. requirement "5 branches", budget) when a contact is linked. With
   `AI_PROVIDER=mock` the suggestion comes from the mock; real Claude needs `ANTHROPIC_API_KEY`.
4. **Duplicate check.** Meta may deliver the same webhook twice. The message must appear exactly
   once.

## B. Outbound: human-approved reply → customer

5. **Review the draft.** Edit the suggestion if needed, then click **Send**. Nothing is sent without
   this click.
6. **Customer receives it.** The reply arrives on the customer's WhatsApp.
7. **Status updates.** The message status in CallCopilot moves **Sent → Delivered → Read** (open
   the chat on the phone to trigger "Read"). The page refreshes every 5 seconds.
8. **Timeline.** The contact's timeline shows "WhatsApp message sent".

## C. Error handling (optional, safe)

9. **Unlisted recipient.** Send to a number that is *not* in the test recipient list, e.g. by
   linking a conversation to a contact with another number. Expect "not in the allowed recipient
   list"; the message is marked Failed.
10. **Closed window.** More than 24 h after the customer's last message, CallCopilot blocks sending
   and explains the 24-hour window.

## Evidence to collect (no secrets)

- **Screenshots:** the conversation (inbound, AI draft, sent message with Delivered/Read) and the
  contact timeline.
- **API log lines:**
  - `provider_request` with `op=send_message` and its status
  - `message_send_failed` (error code only), if any
  - `webhook_rejected` (reason only), if any
- **Meta side:** App Dashboard → WhatsApp → API Setup / Insights, if something doesn't arrive.

## Pass / fail

| Result | Meaning |
|---|---|
| **VERIFIED inbound** | Steps 1–2 produce the real message in CallCopilot |
| **VERIFIED outbound** | Steps 5–7: the customer receives the reply and at least "Delivered" comes back |
| Nothing arrives | Check: Meta webhook verified, `messages` subscribed, app subscribed to the WABA (Test Connection), public URL reachable, `webhook_rejected` in the log (wrong app secret) |
| "access token rejected" | The token expired (temporary tokens last about 24 h). Paste a new one in Settings |
| Error 131030 | Recipient not verified for the test number (setup step 6) |
