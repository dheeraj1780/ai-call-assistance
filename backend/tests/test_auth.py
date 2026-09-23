"""Authentication flows against the real database."""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.audit.models import AuditLog
from app.auth.models import AuthSession
from app.common.config import Settings
from app.common.db import TenantContext
from app.users.models import User
from tests.conftest import CSRF, DEFAULT_PASSWORD, Account, open_session

Register = Callable[..., Awaitable[Account]]


async def test_register_creates_user_company_and_owner(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "  Priya@Example.COM ",
            "password": DEFAULT_PASSWORD,
            "full_name": "Priya Sharma",
            "company_name": "Sharma Traders",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["email"] == "priya@example.com"
    assert body["company"]["name"] == "Sharma Traders"
    assert body["role"] == "OWNER"
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert "password" not in resp.text

    cookie = resp.headers["set-cookie"]
    assert "cc_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/api/v1/auth" in cookie


async def test_password_is_stored_as_argon2id_hash(client: AsyncClient, register: Register) -> None:
    account = await register(client, "hash@example.com", "Hash Co")
    session = await open_session()
    async with session:
        user = await session.scalar(select(User).where(User.id == account.user_id))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert DEFAULT_PASSWORD not in user.password_hash


async def test_duplicate_email_is_rejected_case_insensitively(
    client: AsyncClient, register: Register
) -> None:
    await register(client, "dup@example.com", "First Co")
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "DUP@example.com",
            "password": DEFAULT_PASSWORD,
            "full_name": "Other",
            "company_name": "Second Co",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"


async def test_weak_password_rejected_without_echoing_input(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "w@example.com",
            "password": "Zq9#kept",
            "full_name": "W",
            "company_name": "C",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["loc"] == ["body", "password"]
    assert "Zq9#kept" not in resp.text


async def test_login_success_and_me(client: AsyncClient, register: Register) -> None:
    account = await register(client, "login@example.com", "Login Co")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "LOGIN@example.com", "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["id"] == str(account.user_id)
    assert me.json()["company"]["id"] == str(account.company_id)
    assert me.json()["role"] == "OWNER"


async def test_login_failures_are_indistinguishable(
    client: AsyncClient, register: Register
) -> None:
    await register(client, "known@example.com", "Known Co")
    wrong_pw = await client.post(
        "/api/v1/auth/login", json={"email": "known@example.com", "password": "wrong-password-1"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-1"}
    )
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert (
        wrong_pw.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    )
    assert wrong_pw.headers.get("www-authenticate") == "Bearer"


async def test_failed_login_is_audited_to_the_accounts_tenant(
    client: AsyncClient, register: Register
) -> None:
    account = await register(client, "audit@example.com", "Audit Co")
    await client.post(
        "/api/v1/auth/login", json={"email": "audit@example.com", "password": "wrong-password-1"}
    )
    session = await open_session(TenantContext(company_id=account.company_id))
    async with session:
        rows = (await session.scalars(select(AuditLog))).all()
    by_action = {r.action: r for r in rows}
    assert set(by_action) == {"auth.register", "auth.login_failed"}
    failed = by_action["auth.login_failed"]
    assert failed.actor_user_id == account.user_id
    assert failed.details == {"reason": "invalid_credentials"}
    assert failed.request_id  # correlates with the request log line
    assert "wrong-password-1" not in str(failed.details)


async def test_inactive_user_cannot_login_or_use_tokens(
    client: AsyncClient, register: Register
) -> None:
    account = await register(client, "inactive@example.com", "Inactive Co")
    session = await open_session()
    async with session:
        user = await session.get(User, account.user_id)
        assert user is not None
        user.is_active = False
        await session.commit()

    login = await client.post(
        "/api/v1/auth/login", json={"email": "inactive@example.com", "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 401
    me = await client.get("/api/v1/me", headers=account.headers)
    assert me.status_code == 401


async def test_refresh_rotates_token(client: AsyncClient, register: Register) -> None:
    await register(client, "rot@example.com", "Rot Co")
    first_cookie = client.cookies.get("cc_refresh", path="/api/v1/auth")
    resp = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert resp.status_code == 200, resp.text
    second_cookie = client.cookies.get("cc_refresh", path="/api/v1/auth")
    assert first_cookie
    assert second_cookie
    assert first_cookie != second_cookie

    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_refresh_requires_csrf_header(client: AsyncClient, register: Register) -> None:
    await register(client, "csrf@example.com", "Csrf Co")
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_refresh_rejects_foreign_origin(client: AsyncClient, register: Register) -> None:
    await register(client, "origin@example.com", "Origin Co")
    evil = await client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Origin": "https://evil.example.net"}
    )
    assert evil.status_code == 403
    allowed = await client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Origin": "https://app.example.com"}
    )
    assert allowed.status_code == 200


async def test_refresh_without_cookie_is_401(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"


async def test_refresh_token_reuse_revokes_session(
    client: AsyncClient, register: Register, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "refresh_reuse_grace_seconds", 0)
    account = await register(client, "reuse@example.com", "Reuse Co")
    stolen = client.cookies.get("cc_refresh", path="/api/v1/auth")
    assert stolen

    rotated = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert rotated.status_code == 200
    new_access = rotated.json()["access_token"]

    # Attacker replays the old (already used) refresh token.
    client.cookies.clear()
    replay = await client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"cc_refresh={stolen}"}
    )
    assert replay.status_code == 401

    # The whole session is now dead: both the latest access token and original are rejected.
    for token in (new_access, account.access_token):
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 401

    session = await open_session()
    async with session:
        revoked = (await session.scalars(select(AuthSession.revoke_reason))).all()
    assert revoked == ["refresh_token_reuse"]


async def test_logout_revokes_session_and_access_token(
    client: AsyncClient, register: Register
) -> None:
    account = await register(client, "logout@example.com", "Logout Co")
    resp = await client.post("/api/v1/auth/logout", headers=CSRF)
    assert resp.status_code == 204
    assert (
        'cc_refresh=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
    )

    me = await client.get("/api/v1/me", headers=account.headers)
    assert me.status_code == 401
    refresh = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert refresh.status_code == 401

    # Idempotent: logging out again without a valid session still succeeds.
    again = await client.post("/api/v1/auth/logout", headers=CSRF)
    assert again.status_code == 204


async def test_logout_of_one_session_keeps_other_sessions(
    make_client: Callable[[], AsyncClient], register: Register
) -> None:
    async with make_client() as laptop, make_client() as phone:
        account = await register(laptop, "multi@example.com", "Multi Co")
        login = await phone.post(
            "/api/v1/auth/login", json={"email": "multi@example.com", "password": DEFAULT_PASSWORD}
        )
        phone_token = login.json()["access_token"]
        await laptop.post("/api/v1/auth/logout", headers=CSRF)
        assert (await laptop.get("/api/v1/me", headers=account.headers)).status_code == 401
        ok = await phone.get("/api/v1/me", headers={"Authorization": f"Bearer {phone_token}"})
        assert ok.status_code == 200


async def test_update_me(client: AsyncClient, register: Register) -> None:
    account = await register(client, "me@example.com", "Me Co")
    resp = await client.patch(
        "/api/v1/me",
        headers=account.headers,
        json={"full_name": "New Name", "phone": "+91 98765 43210"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["full_name"] == "New Name"
    assert resp.json()["user"]["phone"] == "+91 98765 43210"

    bad = await client.patch("/api/v1/me", headers=account.headers, json={"email": "x@example.com"})
    assert bad.status_code == 422  # email change is not an allowed field


async def test_concurrent_refresh_within_grace_window_is_not_treated_as_theft(
    client: AsyncClient, register: Register
) -> None:
    """Two tabs refreshing with the same cookie at once must not log the user out."""
    await register(client, "tabs@example.com", "Tabs Co")
    shared = client.cookies.get("cc_refresh", path="/api/v1/auth")
    client.cookies.clear()
    cookie = {**CSRF, "Cookie": f"cc_refresh={shared}"}
    tab1 = await client.post("/api/v1/auth/refresh", headers=cookie)
    tab2 = await client.post("/api/v1/auth/refresh", headers=cookie)
    assert tab1.status_code == 200
    assert tab2.status_code == 200
    for resp in (tab1, tab2):
        me = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
        )
        assert me.status_code == 200
