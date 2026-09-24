"""Invalid / expired / tampered access tokens are rejected."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient

from app.auth.tokens import create_access_token
from app.common.config import Settings
from tests.conftest import Account

Register = Callable[..., Awaitable[Account]]


def _claims(account: Account, settings: Settings, **overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(account.user_id),
        "cid": str(account.company_id),
        "sid": str(uuid.uuid4()),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _sign(claims: dict[str, object], settings: Settings, key: str | None = None) -> str:
    return jwt.encode(claims, key or settings.jwt_secret.get_secret_value(), algorithm="HS256")


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-jwt",
        "Bearer a.b.c",
    ],
)
async def test_missing_or_malformed_authorization(client: AsyncClient, header: str | None) -> None:
    headers = {"Authorization": header} if header is not None else {}
    resp = await client.get("/api/v1/me", headers=headers)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] in {"unauthorized", "invalid_token"}
    assert body["error"]["request_id"]


async def test_expired_token_rejected(
    client: AsyncClient, register: Register, settings: Settings
) -> None:
    account = await register(client, "exp@example.com", "Exp Co")
    past = datetime.now(UTC) - timedelta(hours=1)
    me = await client.get("/api/v1/me", headers=account.headers)
    assert me.status_code == 200
    from app.auth.tokens import decode_access_token

    sid = decode_access_token(settings, account.access_token).session_id
    expired = create_access_token(
        settings,
        user_id=account.user_id,
        company_id=account.company_id,
        session_id=sid,
        now=past,
    )
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_wrong_signature_rejected(
    client: AsyncClient, register: Register, settings: Settings
) -> None:
    account = await register(client, "sig@example.com", "Sig Co")
    token = _sign(_claims(account, settings), settings, key="another-secret-" + "y" * 40)
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_tampered_payload_rejected(client: AsyncClient, register: Register) -> None:
    account = await register(client, "tamper@example.com", "Tamper Co")
    header, payload, sig = account.access_token.split(".")
    tampered = f"{header}.{payload[:-2]}AA.{sig}"
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


async def test_alg_none_rejected(
    client: AsyncClient, register: Register, settings: Settings
) -> None:
    account = await register(client, "none@example.com", "None Co")
    token = jwt.encode(_claims(account, settings), key=None, algorithm="none")  # type: ignore[arg-type]
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "override",
    [
        {"aud": "some-other-api"},
        {"iss": "someone-else"},
        {"typ": "refresh"},
        {"typ": None},
        {"exp": None},
        {"sub": None},
        {"sub": "not-a-uuid"},
        {"cid": None},
    ],
)
async def test_invalid_claims_rejected(
    client: AsyncClient, register: Register, settings: Settings, override: dict[str, object]
) -> None:
    account = await register(client, f"claims-{uuid.uuid4().hex[:8]}@example.com", "Claims Co")
    token = _sign(_claims(account, settings, **override), settings)
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_token_for_deleted_user_rejected(
    client: AsyncClient, register: Register, settings: Settings
) -> None:
    account = await register(client, "gone@example.com", "Gone Co")
    ghost = create_access_token(
        settings, user_id=uuid.uuid4(), company_id=account.company_id, session_id=uuid.uuid4()
    )
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {ghost}"})
    assert resp.status_code == 401
