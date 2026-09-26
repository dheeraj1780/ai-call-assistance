"""Integration configuration: provider metadata, write-only secrets, validation, connection
tests (LIVE adapters via httpx.MockTransport - no real provider is contacted), capability
enablement rules, disconnect, mock mode and tenant isolation."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.common.config import get_settings
from app.common.db import TenantContext
from app.integrations.models import Integration, IntegrationRoute
from app.tenants.models import MemberRole
from tests.conftest import Account, add_member_directly, open_session
from tests.integration_helpers import (
    PLIVO_VALUES,
    PUBLIC,
    TEAMS_VALUES,
    WA_VALUES,
    FakeProvider,
    capability,
    configure,
    validate_and_enable,
    wa_fake,
)

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def admin(client: AsyncClient, register: Register) -> Account:
    return await register(client, "owner@example.com", "Acme Traders")


@pytest.fixture
def public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", PUBLIC)


async def test_list_shows_all_providers_not_configured(client: AsyncClient, admin: Account) -> None:
    resp = await client.get("/api/v1/integrations", headers=admin.headers)
    assert resp.status_code == 200
    items = {i["slug"]: i for i in resp.json()}
    assert set(items) == {"microsoft-teams", "whatsapp", "plivo", "google-meet"}
    assert all(i["status"] == "NOT_CONFIGURED" for i in items.values())
    voice = capability(items["whatsapp"], "WHATSAPP_VOICE_CALL")
    assert voice["state"] == "NOT_AVAILABLE"
    assert not voice["available"]
    assert "WebRTC" in voice["reasons"][0]
    assert {c["capability"] for c in items["microsoft-teams"]["capabilities"]} == {
        "MESSAGE",
        "REAL_TIME_CALL",
    }
    assert {c["capability"] for c in items["plivo"]["capabilities"]} == {
        "PHONE_CALL",
        "MEDIA_STREAM",
    }


async def test_only_admins_configure(client: AsyncClient, admin: Account, make_client: Any) -> None:
    await add_member_directly(admin.company_id, "member@example.com", MemberRole.MEMBER)
    async with make_client() as other:
        login = await other.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": "correct-horse-battery"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        resp = await other.put(
            "/api/v1/integrations/whatsapp/config",
            headers=headers,
            json={"mode": "LIVE", "values": WA_VALUES},
        )
        assert resp.status_code == 403
        assert (await other.get("/api/v1/integrations", headers=headers)).status_code == 200
        assert (
            await other.post("/api/v1/integrations/whatsapp/test", headers=headers)
        ).status_code in (403, 404)


async def test_secrets_are_write_only_and_encrypted(
    client: AsyncClient, admin: Account, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    detail = await configure(client, admin, "whatsapp", WA_VALUES)
    fields = {f["key"]: f for f in detail["fields"]}
    assert fields["access_token"]["is_set"] is True
    assert fields["access_token"]["value"] is None
    assert fields["access_token"]["display"] == "********"
    assert fields["phone_number_id"]["value"] == "111222333"
    again = await client.get("/api/v1/integrations/whatsapp", headers=admin.headers)
    for secret_key in ("access_token", "app_secret", "verify_token"):
        assert WA_VALUES[secret_key] not in again.text
        assert WA_VALUES[secret_key] not in str(detail)
        assert WA_VALUES[secret_key] not in caplog.text
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        row = await session.scalar(
            select(Integration).where(Integration.company_id == admin.company_id)
        )
    assert row is not None
    assert row.secrets_ciphertext
    assert WA_VALUES["access_token"] not in row.secrets_ciphertext
    assert WA_VALUES["access_token"] not in str(row.config)
    assert sorted(row.secret_keys) == ["access_token", "app_secret", "verify_token"]


async def test_secret_kept_when_omitted_and_clearable(client: AsyncClient, admin: Account) -> None:
    await configure(client, admin, "whatsapp", WA_VALUES)
    detail = await configure(client, admin, "whatsapp", {"app_id": "123456"})
    fields = {f["key"]: f for f in detail["fields"]}
    assert fields["access_token"]["is_set"]
    assert fields["app_id"]["value"] == "123456"
    resp = await client.put(
        "/api/v1/integrations/whatsapp/config",
        headers=admin.headers,
        json={"mode": "LIVE", "clear_secrets": ["access_token"]},
    )
    fields = {f["key"]: f for f in resp.json()["fields"]}
    assert fields["access_token"]["is_set"] is False
    assert capability(resp.json(), "MESSAGE")["state"] == "NOT_CONFIGURED"


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"phone_number_id": "abc"}, "invalid_integration_config"),
        ({"unknown_field": "x"}, "invalid_integration_config"),
        ({"verify_token": "short"}, "invalid_integration_config"),
    ],
)
async def test_config_validation(
    client: AsyncClient, admin: Account, values: dict[str, str], code: str
) -> None:
    resp = await client.put(
        "/api/v1/integrations/whatsapp/config",
        headers=admin.headers,
        json={"mode": "LIVE", "values": values},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


async def test_whatsapp_connection_test_and_enable(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    fake = wa_fake()
    await configure(client, admin, "whatsapp", WA_VALUES)
    detail = await validate_and_enable(client, admin, "whatsapp")
    assert detail["status"] == "VALIDATED"
    assert detail["last_test"]["ok"] is True
    assert detail["last_test"]["account_label"] == "Acme +91 80000 00000"
    assert capability(detail, "MESSAGE")["state"] == "VALIDATED"
    [req] = fake.calls("GET", "/111222333?")
    assert req.headers["authorization"] == "Bearer " + WA_VALUES["access_token"]
    assert "/v23.0/" in str(req.url)
    enabled = await validate_and_enable(client, admin, "whatsapp", "MESSAGE")
    assert enabled["status"] == "ENABLED"
    assert capability(enabled, "MESSAGE")["enabled"] is True


async def test_whatsapp_bad_token_reports_error(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    fake = FakeProvider().install()
    fake.on(
        "GET",
        "/111222333",
        httpx.Response(401, json={"error": {"code": 190, "type": "OAuthException"}}),
    )
    await configure(client, admin, "whatsapp", WA_VALUES)
    detail = (await client.post("/api/v1/integrations/whatsapp/test", headers=admin.headers)).json()
    assert detail["status"] == "ERROR"
    assert detail["last_test"]["ok"] is False
    assert "rejected the access token" in detail["last_test"]["checks"][0]["detail"]
    resp = await client.post(
        "/api/v1/integrations/whatsapp/capabilities/MESSAGE/enable", headers=admin.headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "integration_not_ready"


async def test_live_capability_needs_public_https_url(client: AsyncClient, admin: Account) -> None:
    wa_fake()
    await configure(client, admin, "whatsapp", WA_VALUES)
    detail = (await client.post("/api/v1/integrations/whatsapp/test", headers=admin.headers)).json()
    cap = capability(detail, "MESSAGE")
    assert cap["state"] == "NOT_CONFIGURED"
    assert any("PUBLIC_BASE_URL" in r for r in cap["reasons"])
    check = (
        await client.get("/api/v1/integrations/whatsapp/config-check", headers=admin.headers)
    ).json()
    assert check["complete"] is False
    missing = {r["key"] for r in check["requirements"] if not r["ok"]}
    assert {"public_url", "webhook_verified"} <= missing


async def test_changing_credentials_requires_revalidation(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    wa_fake()
    await configure(client, admin, "whatsapp", WA_VALUES)
    await validate_and_enable(client, admin, "whatsapp", "MESSAGE")
    detail = await configure(client, admin, "whatsapp", {"access_token": "EAAG-rotated-token"})
    assert detail["status"] == "CONFIGURED"
    assert capability(detail, "MESSAGE")["enabled"] is False
    assert detail["last_test"] is None
    same = await configure(client, admin, "whatsapp", {"access_token": "EAAG-rotated-token"})
    assert same["status"] == "CONFIGURED"


async def test_plivo_connection_test(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    fake = FakeProvider().install()
    fake.on(
        "GET",
        "/Account/MAXXXXXXXXXXXXXXXXXX/Number/918000000000/",
        httpx.Response(200, json={"number": "918000000000"}),
    )
    fake.on(
        "GET", "/Account/MAXXXXXXXXXXXXXXXXXX/", httpx.Response(200, json={"name": "Acme Voice"})
    )
    await configure(client, admin, "plivo", PLIVO_VALUES)
    detail = await validate_and_enable(client, admin, "plivo", "PHONE_CALL")
    assert detail["last_test"]["ok"] is True
    assert capability(detail, "PHONE_CALL")["state"] == "ENABLED"
    # Real-time audio needs a real streaming STT provider, which this build does not include.
    media = capability(detail, "MEDIA_STREAM")
    assert media["state"] == "NOT_CONFIGURED"
    assert any("speech-to-text" in r for r in media["reasons"])
    auth = fake.calls("GET", "/Account/MAXXXXXXXXXXXXXXXXXX/")[0].headers["authorization"]
    assert auth.startswith("Basic ")


async def test_plivo_number_not_on_account(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    fake = FakeProvider().install()
    fake.on("GET", "/Account/MAXXXXXXXXXXXXXXXXXX/", httpx.Response(200, json={"name": "Acme"}))
    fake.on("GET", "/Number/", httpx.Response(404, json={"error": "not found"}))
    await configure(client, admin, "plivo", PLIVO_VALUES)
    detail = (await client.post("/api/v1/integrations/plivo/test", headers=admin.headers)).json()
    assert detail["last_test"]["ok"] is False
    assert "not rented" in detail["last_test"]["checks"][1]["detail"]


def _jwt(claims: dict[str, Any]) -> str:
    import base64
    import json

    def enc(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


async def test_teams_reports_missing_calling_permissions(
    client: AsyncClient, admin: Account, public_https: None
) -> None:
    fake = FakeProvider().install()
    fake.on(
        "POST",
        "/oauth2/v2.0/token",
        httpx.Response(
            200,
            json={
                "access_token": _jwt({"roles": ["Calls.JoinGroupCall.All"]}),
                "token_type": "Bearer",
            },
        ),
    )
    await configure(
        client, admin, "microsoft-teams", {**TEAMS_VALUES, "call_persistence": "TRANSIENT"}
    )
    detail = (
        await client.post("/api/v1/integrations/microsoft-teams/test", headers=admin.headers)
    ).json()
    assert detail["last_test"]["ok"] is True  # credentials are valid
    assert detail["last_test"]["missing_permissions"] == ["Calls.AccessMedia.All"]
    assert capability(detail, "MESSAGE")["state"] == "VALIDATED"
    call_cap = capability(detail, "REAL_TIME_CALL")
    assert call_cap["state"] in ("ERROR", "NOT_CONFIGURED")
    [token_req] = fake.calls("POST", "/oauth2/v2.0/token")
    assert TEAMS_VALUES["tenant_id"] in str(token_req.url)
    assert b"grant_type=client_credentials" in token_req.content
    reqs = {r["key"]: r for r in detail["requirements"]}
    assert reqs["media_gateway"]["ok"] is False
    assert reqs["stt"]["ok"] is False
    assert reqs["user_connection"]["ok"] is False
    assert reqs["user_connection"]["scope"] == "user"
    assert TEAMS_VALUES["client_secret"] not in str(detail)


async def test_teams_bad_secret(client: AsyncClient, admin: Account) -> None:
    fake = FakeProvider().install()
    fake.on("POST", "/oauth2/v2.0/token", httpx.Response(401, json={"error": "invalid_client"}))
    await configure(client, admin, "microsoft-teams", TEAMS_VALUES)
    detail = (
        await client.post("/api/v1/integrations/microsoft-teams/test", headers=admin.headers)
    ).json()
    assert detail["status"] == "ERROR"
    assert "rejected the credentials" in detail["last_test"]["checks"][0]["detail"]


async def test_mock_mode_needs_no_credentials(client: AsyncClient, admin: Account) -> None:
    for slug, caps in (
        ("whatsapp", ["MESSAGE"]),
        ("microsoft-teams", ["MESSAGE", "REAL_TIME_CALL"]),
        ("plivo", ["PHONE_CALL", "MEDIA_STREAM"]),
    ):
        detail = await configure(client, admin, slug, mode="MOCK")
        assert detail["mode"] == "MOCK"
        detail = await validate_and_enable(client, admin, slug, *caps)
        assert detail["status"] == "ENABLED", slug
        assert all(capability(detail, c)["state"] == "ENABLED" for c in caps)
        assert [r["key"] for r in detail["requirements"]] == ["mock_mode"]
    wa = (await client.get("/api/v1/integrations/whatsapp", headers=admin.headers)).json()
    assert capability(wa, "WHATSAPP_VOICE_CALL")["state"] == "NOT_AVAILABLE"
    resp = await client.post(
        "/api/v1/integrations/whatsapp/capabilities/WHATSAPP_VOICE_CALL/enable",
        headers=admin.headers,
    )
    assert resp.status_code == 409


async def test_mock_mode_refused_in_production(
    client: AsyncClient, admin: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "app_env", "production")
    resp = await client.put(
        "/api/v1/integrations/whatsapp/config",
        headers=admin.headers,
        json={"mode": "MOCK", "values": {}},
    )
    assert resp.status_code == 422


async def test_disable_and_disconnect(client: AsyncClient, admin: Account) -> None:
    await configure(client, admin, "whatsapp", mode="MOCK")
    await validate_and_enable(client, admin, "whatsapp", "MESSAGE")
    resp = await client.post(
        "/api/v1/integrations/whatsapp/capabilities/MESSAGE/disable", headers=admin.headers
    )
    assert capability(resp.json(), "MESSAGE")["state"] == "VALIDATED"
    assert (
        await client.delete("/api/v1/integrations/whatsapp", headers=admin.headers)
    ).status_code == 204
    detail = (await client.get("/api/v1/integrations/whatsapp", headers=admin.headers)).json()
    assert detail["status"] == "NOT_CONFIGURED"
    session = await open_session()
    async with session:
        assert (await session.scalar(select(IntegrationRoute))) is None


async def test_provider_account_cannot_be_attached_to_two_workspaces(
    client: AsyncClient, admin: Account, register: Register, make_client: Any
) -> None:
    await configure(client, admin, "whatsapp", WA_VALUES)
    async with make_client() as other_client:
        other = await register(other_client, "b@example.com", "Beta Corp")
        resp = await other_client.put(
            "/api/v1/integrations/whatsapp/config",
            headers=other.headers,
            json={"mode": "LIVE", "values": WA_VALUES},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "integration_account_in_use"
        # Tenant B sees nothing of tenant A's configuration.
        mine = (
            await other_client.get("/api/v1/integrations/whatsapp", headers=other.headers)
        ).json()
        assert mine["status"] == "NOT_CONFIGURED"
        assert "111222333" not in str(mine)


async def test_integration_rows_are_isolated_by_rls(
    client: AsyncClient, admin: Account, register: Register, make_client: Any
) -> None:
    await configure(client, admin, "plivo", mode="MOCK")
    async with make_client() as c2:
        other = await register(c2, "c@example.com", "Gamma")
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        rows = (await session.scalars(select(Integration))).all()
    assert rows == []
