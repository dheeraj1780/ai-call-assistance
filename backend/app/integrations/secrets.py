"""Secret storage abstraction for integration credentials.

``EncryptedDatabaseSecretStore`` (default): the secret values of one integration are serialised
to JSON and encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key derived from
TOKEN_ENCRYPTION_KEY (required in production, see config). Only the ciphertext is stored in
``integrations.secrets_ciphertext``; plaintext exists only in memory while a provider adapter
runs. Secrets are never returned by any GET API and never logged.

Production note: the encryption key itself must come from a secret manager (e.g. Render
secret env var, AWS Secrets Manager/KMS, Azure Key Vault). Key rotation and envelope
encryption with a cloud KMS are NOT implemented; see docs/integrations/credentials.md.
"""

import json
from typing import Protocol

from app.common.crypto import decrypt, encrypt

_PREFIX = "integration-secrets:v1:"


class SecretStore(Protocol):
    def seal(self, values: dict[str, str]) -> str: ...
    def open(self, ciphertext: str | None) -> dict[str, str]: ...


class EncryptedDatabaseSecretStore:
    def seal(self, values: dict[str, str]) -> str:
        return encrypt(_PREFIX + json.dumps(values, sort_keys=True))

    def open(self, ciphertext: str | None) -> dict[str, str]:
        if not ciphertext:
            return {}
        plaintext = decrypt(ciphertext)
        if not plaintext.startswith(_PREFIX):
            raise ValueError("unexpected secret payload")
        data = json.loads(plaintext[len(_PREFIX) :])
        return {str(k): str(v) for k, v in data.items()}


_store: SecretStore = EncryptedDatabaseSecretStore()


def get_secret_store() -> SecretStore:
    return _store


def set_secret_store(store: SecretStore) -> None:
    global _store
    _store = store


def mask(value: str | None) -> str | None:
    """What the UI shows for a stored secret."""
    return "********" if value else None
