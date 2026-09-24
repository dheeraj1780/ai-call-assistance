"""Symmetric encryption for secrets at rest (OAuth refresh tokens) and signed short-lived
state tokens (OAuth ``state``)."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.common.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    if settings.token_encryption_key is not None:
        key = settings.token_encryption_key.get_secret_value().encode()
    else:
        # Development/test only (production requires TOKEN_ENCRYPTION_KEY, see config).
        digest = hashlib.sha256(b"dev-token-key:" + settings.jwt_secret.get_secret_value().encode())
        key = base64.urlsafe_b64encode(digest.digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("cannot decrypt value") from exc


def _sign(payload: bytes, purpose: str) -> str:
    secret = get_settings().jwt_secret.get_secret_value().encode()
    return hmac.new(secret, purpose.encode() + b"." + payload, hashlib.sha256).hexdigest()


def sign_state(data: dict[str, Any], *, purpose: str, ttl_seconds: int = 600) -> str:
    body = dict(data, exp=int(time.time()) + ttl_seconds)
    payload = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode())
    return f"{payload.decode()}.{_sign(payload, purpose)}"


def verify_state(token: str, *, purpose: str) -> dict[str, Any]:
    try:
        payload_b64, signature = token.rsplit(".", 1)
    except ValueError:
        raise ValueError("malformed state") from None
    if not hmac.compare_digest(signature, _sign(payload_b64.encode(), purpose)):
        raise ValueError("bad state signature")
    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload_b64))
    if int(data.get("exp", 0)) < time.time():
        raise ValueError("state expired")
    return data
