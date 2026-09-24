# Credentials and secret handling

## Where each credential lives

| Credential | Scope | Where you set it |
|---|---|---|
| WhatsApp phone number ID, business account ID, app ID | company | Settings → Integrations (plain config) |
| WhatsApp access token, app secret, verify token | company | Settings → Integrations (**encrypted, write-only**) |
| Plivo auth ID, phone number, application ID | company | Settings → Integrations |
| Plivo auth token | company | Settings → Integrations (**encrypted, write-only**) |
| Microsoft tenant ID | company | Settings → Integrations |
| Microsoft client ID / client secret | server (`MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET`) **or** company override | env / Integrations (secret encrypted) |
| Teams per-user refresh tokens | per salesperson | obtained by "Connect my Teams account", **encrypted** |
| `TEAMS_MEDIA_GATEWAY_URL`, `TEAMS_MEDIA_GATEWAY_SECRET` (≥ 32 chars) | server | env (API) + `Gateway__BackendSharedSecret` (gateway) |
| Gateway `AppId`, `AppSecret`, certificate thumbprint, IP/ports | gateway VM | env on the gateway |
| `TOKEN_ENCRYPTION_KEY` (≥ 32 chars) | server | env / secret manager; **required in production** |
| `PUBLIC_BASE_URL` | server | env; must be public **https** for any LIVE webhook |

**Why WhatsApp and Plivo credentials are not environment variables.**
- The product is multi-tenant, so every business has its own WhatsApp number and phone account.
- One server-wide WhatsApp token would route every tenant through one number.
- Each integration's webhook URL and signature secret are therefore per company.

**Credentials that were considered and are not required.**
- `WHATSAPP_ACCESS_TOKEN`/`APP_ID` and the other `WHATSAPP_*` values as server env: per company instead.
- `PLIVO_WEBHOOK_BASE_URL` and `PLIVO_MEDIA_WS_URL`: derived from `PUBLIC_BASE_URL`.
- `MICROSOFT_BOT_ID`/`MICROSOFT_BOT_SECRET` in the API: the bot identity is needed by the gateway only.
- `AZURE_RESOURCE_GROUP` and `AZURE_SUBSCRIPTION_ID`: nothing creates Azure resources automatically.

## How secrets are protected

- **Encryption at rest.** Secret values are serialised and encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256), using a key derived from `TOKEN_ENCRYPTION_KEY`. Only the ciphertext is stored, in `integrations.secrets_ciphertext`. Refresh tokens are encrypted the same way.
- **Write-only.**
  - No GET endpoint returns a secret. The API reports only `is_set` and a display value of `********`.
  - Saving with an empty secret field keeps the stored value, and `clear_secrets` removes one.
  - Tests check that responses, logs and database JSON never contain the plaintext.
- **Not logged.**
  - Adapters log provider, operation, status code and latency only.
  - The log formatter also redacts keys such as `auth_token`, `app_secret`, `client_secret`, `verify_token`, `access_token` and `refresh_token`.
  - Audit entries record field names, never values.
- **Isolation.** Integration rows are tenant-owned under forced RLS. One provider account (a WhatsApp phone number ID or Plivo auth ID) cannot be attached to two workspaces.
- **Rotation.** Changing a credential clears the last connection test and disables all capabilities until the connection is tested again.

## Production secret store (NOT IMPLEMENTED — required decision)

The application-level encryption key is the single master secret.

- **Keep it in the hosting platform's secret store**: a Render secret environment variable, or a cloud secret manager.
- **Never commit it.** Losing it makes the stored credentials unreadable, and every integration must then be configured again.
- **Not implemented:** envelope encryption with a cloud KMS (for example AWS KMS or Azure Key Vault) and automatic key rotation. Plan these before holding many tenants' credentials.
