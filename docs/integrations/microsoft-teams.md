# Microsoft Teams

There are two independent capabilities:

| Capability | Status |
|---|---|
| Messages (Teams chats via Microsoft Graph) | IMPLEMENTED · MOCK VERIFIED · CREDENTIAL REQUIRED · EXTERNAL PROVIDER VERIFICATION REQUIRED |
| Real-time Call Copilot (Teams meetings) | Backend contract, mock gateway and UI: IMPLEMENTED · MOCK VERIFIED. The .NET media gateway is **source only: NOT BUILT, NOT VERIFIED**. It cannot be enabled in LIVE mode until a real-time STT provider exists (see below) |

## 1. Messaging

**How it works.**
- Each salesperson signs in with their own Microsoft account using delegated OAuth. Microsoft allows sending chat messages with application permissions only for data migration, so per-user sign-in is required.
- The salesperson **links one of their existing Teams chats to a contact**.
- CallCopilot subscribes to that chat (`/chats/{id}/messages`, `changeType=created`, without resource data, so no encryption certificate is needed). It renews the subscription about every 55 minutes from a periodic job.
- On each notification (authenticated by a per-integration `clientState` HMAC), a job fetches the message with the user's token and ingests it into the common conversation model.
- AI suggestions follow. The salesperson reviews and clicks Send.

**Delegated permissions (least privilege).**
- `offline_access`, for the refresh token.
- `User.Read`, to identify the signed-in user.
- `Chat.Read`, to list the user's chats, read messages, and subscribe to chat messages.
- `ChatMessage.Send`, to send the reply.

**Not requested:**
- `Chat.ReadWrite`
- `Chat.ReadWrite.All`
- `Chat.Read.All`
- any application messaging permission

**Limitations.**
- Customers usually sit outside your Microsoft 365 tenant. Chats with them exist only through guest access or external federation, and they must already exist in Teams, because creating chats is not implemented.
- Chat history from before linking is not imported.
- Channel (team) messages are not supported.
- Subscriptions last at most 1 hour without lifecycle notifications, so renewal depends on the job worker running.

## 2. Real-time call copilot

**How it works.**
1. The salesperson plans a **Teams call** for a contact and pastes the Teams meeting link.
2. On **Start**, the API asks the **teams-media-gateway** (C#/.NET, Windows Server in Azure) to join the meeting **listen-only**.
3. The gateway reports ESTABLISHING, ESTABLISHED and TERMINATED, and streams unmixed 16 kHz PCM audio to the API.
4. The audio goes through the same `LiveSession`, STT, `CopilotEngine` and live screen as phone calls. The header shows "Channel: Microsoft Teams".

**Why .NET.** Microsoft's application-hosted media library exists only for C#/.NET on Windows Server. See [teams-media-gateway/README.md](../../teams-media-gateway/README.md).

**Persistence and compliance.**
- **Transient (default):**
  - The transcript, AI notes, cards and agenda changes are shown live only.
  - Nothing derived from call media is stored.
  - No post-call summary is generated.
  - The UI says so.
- **Store transcript & AI notes:**
  - The gateway calls `updateRecordingStatus(Recording)` and waits for success before any audio leaves it. All participants see the Teams recording indicator.
  - The call becomes `PERSISTED` only after `RECORDING_CONFIRMED`.
  - On failure the call stays transient and no audio is forwarded.
  - This is set per company with **"Teams call transcript storage"**.
- Recording is **never** enabled silently. Raw audio is never stored.

**Application permissions (admin consent).**
- `Calls.JoinGroupCall.All`, to join meetings.
- `Calls.AccessMedia.All`, to receive real-time audio.
- The connection test reads the roles granted in an app-only token and lists any that are missing.

**Media requirements:**
- An Azure Bot resource with the Teams channel and calling enabled.
- A Windows Server VM with an instance-level public IP, a DNS name and a TLS certificate.
- The gateway deployed on it.
- `TEAMS_MEDIA_GATEWAY_URL` and `TEAMS_MEDIA_GATEWAY_SECRET` set on the API.
- **A real streaming speech-to-text provider.** The mock STT cannot transcribe audio. Until one is added, Real-time Call Copilot shows NOT_CONFIGURED in LIVE mode with that reason.

## Required credentials

**Company level**, entered in Settings → Integrations → Microsoft Teams:
- **Tenant (directory) ID** (required).
- Optionally your own app's client ID and secret, instead of the platform app.
- Call transcript storage mode.

**Server level** (environment):
- `MICROSOFT_CLIENT_ID` and `MICROSOFT_CLIENT_SECRET`: the platform's multi-tenant Entra app. Needed unless the company enters its own.
- `TEAMS_MEDIA_GATEWAY_URL` and `TEAMS_MEDIA_GATEWAY_SECRET`: calling only.

**Gateway level:** see the gateway README (`Gateway__AppId`, `Gateway__AppSecret`, certificate, IP and ports).

## Entra app registration

**Redirect URIs (Web):**
- `https://<PUBLIC_BASE_URL>/api/v1/integrations/microsoft-teams/oauth/callback`
- `https://<PUBLIC_BASE_URL>/api/v1/integrations/microsoft-teams/admin-consent/callback`

**Permissions:** the delegated and application permissions listed above.

## Webhook / notification setup

Nothing to paste manually. CallCopilot creates the subscription when a chat is linked. Its notification URL is `https://<PUBLIC_BASE_URL>/api/v1/integrations/teams/webhooks/<integration-id>`, and it must be publicly reachable over HTTPS. Microsoft validates it at creation time, and we echo `validationToken`.

## Local testing

- **Mock mode:** Configure → Mock → Save → Test → enable Messages and Real-time Call Copilot.
- **Messages:**
  1. On a contact, click "Teams Message" and link a mock chat.
  2. In Integrations, click **Simulate Teams Message**.
  3. Open the conversation.
- **Calls:**
  1. On a contact, click "Teams Call" and enter any `https://teams.microsoft.com/...` link.
  2. Start the call.
  3. Click **Simulate conversation (mock)**. This plays gateway events and audio frames through the real event and media paths.

## Production prerequisites

- A Microsoft 365 tenant with Teams licences.
- Admin consent for the permissions above.
- For calling, all of the following:
  - Azure resources
  - the gateway, built, deployed and verified
  - a real STT provider

## Privacy

- Chat messages and transcripts are personal data under the company's retention.
- Teams meeting participants are notified through the recording indicator only in the storage mode.
- Informing customers that an AI assistant listens is **REQUIRES LEGAL REVIEW**.

## Exact activation procedure

**Messaging**
1. Register or choose the Entra app, add the redirect URIs and the delegated permissions, and create a client secret. Set `MICROSOFT_CLIENT_ID`/`_SECRET`, or enter them per company.
2. Set `PUBLIC_BASE_URL` to public https.
3. Settings → Integrations → Microsoft Teams → Configure (Live): enter the tenant ID → Save → **Test Connection**. Expect ✓ credentials; the calling checks are optional.
4. **Enable Messages**. Each salesperson clicks **Connect my Teams account** and consents.
5. On a contact, click Teams Message, pick the chat with that customer, and confirm that new messages arrive.

**Calling (after the gateway is built and deployed)**
1. Create the Azure Bot with calling enabled, pointed at the gateway.
2. Add the application permissions and click **Grant admin consent (calling)**.
3. Deploy the gateway, and set `TEAMS_MEDIA_GATEWAY_URL`/`_SECRET` on the API.
4. Add a real STT provider (not in this version).
5. Test Connection, expecting ✓ calling permissions and ✓ gateway reachable, then Enable Real-time Call Copilot.
6. Run a test meeting with an internal colleague acting as the customer.
