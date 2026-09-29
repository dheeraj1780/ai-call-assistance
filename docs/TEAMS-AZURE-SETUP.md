# Teams POC: Azure Bot setup

The script is `scripts/setup-teams-poc.ps1`. It is idempotent: it checks every resource before creating it and refuses to change a bot that is attached to a different App ID.

It **uses** the existing Entra app "CallCopilot Teams". It never creates an app registration or a tenant. It never touches the client secret, the VM, the gateway, Plivo, WhatsApp or Google Meet.

## Prerequisites

1. **Azure CLI**
   - The script uses the uv-tool install (`uv tool dir`\azure-cli), and falls back to `az` on PATH.
   - This tenant uses security defaults, so re-login interactively with MFA:
     ```powershell
     $py = Join-Path (uv tool dir) "azure-cli\Scripts\python.exe"
     & $py -m azure.cli logout
     & $py -m azure.cli login --tenant 748ec784-319e-4b0c-be1a-7403649ca01a --scope "https://management.core.windows.net//.default"
     ```
2. **The full Client ID** of "CallCopilot Teams". The script rejects anything that is not a complete 8-4-4-4-12 GUID.
3. **Owner/Contributor** on resource group `callcopilot-poc`.

## Run

```powershell
# Preview only (read-only Azure calls, no changes):
.\scripts\setup-teams-poc.ps1 -AppId <CLIENT-ID-GUID> -DryRun
# Apply:
.\scripts\setup-teams-poc.ps1 -AppId <CLIENT-ID-GUID>
```

Defaults and overrides (see `param()` in the script):

| Parameter | Default |
|---|---|
| `-AppTenantId` | `6b91daa7-…` |
| `-ResourceGroup` | `callcopilot-poc` |
| `-BotName` | `callcopilot-teams-bot` |
| `-Sku` | `F0` |
| `-GatewayDnsName` | `callcopilot-teams-gw.eastus.cloudapp.azure.com` |
| `-CallbackBaseUrl` | `https://<dns>` |
| `-MessagingEndpoint` | none |

## Values derived from the repository

- **Calling webhook:** `CallbackBaseUrl + /api/calling`.
  - Source: `teams-media-gateway/src/Teams/BotService.cs` and `GatewayApp.cs`.
  - Resolves to `https://callcopilot-teams-gw.eastus.cloudapp.azure.com/api/calling`, on port 443.
- **Messaging endpoint:** not set. The gateway has no `/api/messages` route, and the bot is calling-only.
- **Media port:** 8445. **Health probe:** `GET /healthz`.

## What the script does

1. Verifies the login with a real ARM call.
2. Checks the resource group.
3. Registers `Microsoft.BotService`.
4. Creates the Azure Bot: `SingleTenant`, existing App ID, location `global`.
5. Configures the Teams channel with a direct ARM `PUT …/channels/MsTeamsChannel?api-version=2022-09-15`:
   - It sets `enableCalling=true`, `callingWebhook=<webhook>` and `deploymentEnvironment=CommercialDeployment`.
   - It doesn't use `az bot msteams create` (a preview command), which has two bugs:
     - It sends `FallbackDeploymentEnvironment`, which the service rejects (Azure/azure-rest-api-specs#28636).
     - It silently drops the webhook.
6. Runs read-only checks: VM power state, NSG rules for 443/8445, `GET https://<dns>/healthz`.
7. Prints a checklist.

Every OK is read back from ARM. The script exits with 1 if any item is FAIL.

## Checklist (fill from the script output)

- [ ] Azure Bot created
- [ ] Existing App ID attached (`msaAppId` read back)
- [ ] Teams channel configured
- [ ] Calling enabled
- [ ] Calling endpoint = `…/api/calling`
- [ ] Gateway reachable over HTTPS

## Manual items (not automated, by design)

- **Client secret:**
  - Set `Gateway__AppSecret` on the VM yourself.
  - Also set `Gateway__AppId`, `Gateway__HomeTenantId`, `Gateway__ServiceDnsName`, `Gateway__InstancePublicIPAddress` and `Gateway__CertificateThumbprint`.
  - Then run `teams-media-gateway/deploy-azure-vm.ps1 -HealthCheckOnly`.
- **TLS certificate:** install a publicly trusted certificate for the gateway DNS name in `LocalMachine\My` on the VM.
- **NSG:** open inbound TCP 443 and 8445. The script only reports them, because opening them changes the VM's exposure.
- **VM:** it is currently deallocated. Start it only when testing.
- **Tenant mismatch:**
  - The subscription is in tenant `748ec784…`, but the app registration is in tenant `6b91daa7…`.
  - If `az bot create` rejects the cross-tenant SingleTenant app, or Teams auth fails, use a subscription in the app's tenant.
- **Teams tenant:** the tenant that hosts the meetings needs Teams licenses, and the bot must be allowed there.
- **Graph permissions and admin consent:** already granted, per the owner. The script does not change them.
- **Backend:** set `TEAMS_MEDIA_GATEWAY_URL` and `TEAMS_MEDIA_GATEWAY_SECRET` in `backend/.env` when testing.
