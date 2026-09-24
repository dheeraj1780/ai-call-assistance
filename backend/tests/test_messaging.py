"""Messaging channels end to end (WhatsApp Cloud API + Microsoft Teams) through the common
conversation model: webhook verification, idempotency, normalisation, safe contact matching,
AI suggestions, human-approved sending, timeline, retention and tenant isolation.

LIVE adapters run against httpx.MockTransport; no real provider is ever contacted."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update

from app.ai.gateway import set_ai_provider
from app.ai.mock_provider import MockAIProvider
from app.ai.provider import AIUnavailableError
from app.common.config import get_settings
from app.common.db import TenantContext
from app.conversations.models import CommunicationMessage, CommunicationSession, ContactIdentity
from app.integrations.models import IntegrationUserConnection
from app.integrations.providers.teams import MockTeamsMessageProvider, client_state_for
from app.integrations.providers.whatsapp import MockWhatsAppClient
from app.jobs.service import drain
from app.privacy.retention import purge_expired
from tests.conftest import Account, open_session
from tests.integration_helpers import (
    PUBLIC,
    TEAMS_VALUES,
    WA_VALUES,
    FakeProvider,
    configure,
    integration_id,
    validate_and_enable,
    wa_fake,
    wa_payload,
    wa_sign,
    wa_status_payload,
)

Register = Callable[..., Awaitable[Account]]
WA_ID = "919876543210"


@pytest.fixture
def public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", PUBLIC)


@pytest.fixture
async def owner(client: AsyncClient, register: Register) -> Account:
    return await register(client, "owner@example.com", "Acme Traders")


@pytest.fixture
async def wa_live(client: AsyncClient, owner: Account, public_https: None) -> dict[str, Any]:
    fake = wa_fake()
    detail = await configure(client, owner, "whatsapp", WA_VALUES)
    detail = await validate_and_enable(client, owner, "whatsapp", "MESSAGE")
    return {"fake": fake, "id": integration_id(detail), "detail": detail}


async def contact(
    client: AsyncClient,
    acct: Account,
    name: str = "Ravi Kumar",
    phone: str | None = "+91 98765 43210",
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/contacts", headers=acct.headers, json={"name": name, "phone": phone}
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def deliver(
    client: AsyncClient, iid: str, body: bytes, secret: str = WA_VALUES["app_secret"]
) -> httpx.Response:
    return await client.post(
        f"/api/v1/integrations/whatsapp/webhooks/{iid}", content=body, headers=wa_sign(body, secret)
    )


async def conversations(client: AsyncClient, acct: Account, **params: str) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/conversations", headers=acct.headers, params=params)
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


# ---- WhatsApp webhooks ---------------------------------------------------------------------------


async def test_whatsapp_webhook_verification(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    url = f"/api/v1/integrations/whatsapp/webhooks/{wa_live['id']}"
    ok = await client.get(
        url,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": WA_VALUES["verify_token"],
            "hub.challenge": "1158201444",
        },
    )
    assert ok.status_code == 200
    assert ok.text == "1158201444"
    bad = await client.get(
        url,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token-000000",
            "hub.challenge": "1",
        },
    )
    assert bad.status_code == 403
    unknown = await client.get(
        f"/api/v1/integrations/whatsapp/webhooks/{uuid.uuid4()}", params={"hub.mode": "subscribe"}
    )
    assert unknown.status_code == 404
    check = (
        await client.get("/api/v1/integrations/whatsapp/config-check", headers=owner.headers)
    ).json()
    assert {r["key"]: r["ok"] for r in check["requirements"]}["webhook_verified"] is True


async def test_whatsapp_webhook_requires_valid_signature(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    body = wa_payload(wa_id=WA_ID, text="hello", message_id="wamid.1")
    url = f"/api/v1/integrations/whatsapp/webhooks/{wa_live['id']}"
    assert (await client.post(url, content=body)).status_code == 401
    assert (await deliver(client, wa_live["id"], body, secret="wrong-secret")).status_code == 401
    tampered = body.replace(b"hello", b"HELLO")
    assert (await client.post(url, content=tampered, headers=wa_sign(body))).status_code == 401
    assert await conversations(client, owner) == []


async def test_inbound_message_matches_contact_and_gets_ai_suggestion(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    k = await contact(client, owner)
    resp = await deliver(
        client,
        wa_live["id"],
        wa_payload(
            wa_id=WA_ID,
            text="Can you send the quotation for 100 units? We use Excel today.",
            message_id="wamid.in.1",
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == 1
    [conv] = await conversations(client, owner)
    assert conv["match_status"] == "MATCHED"
    assert conv["contact"]["id"] == k["id"]
    assert conv["channel"] == "WHATSAPP"
    assert conv["display_name"] == "Ravi"
    assert await drain() >= 1  # conversation.assist job
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert [m["content"] for m in detail["messages"]] == [
        "Can you send the quotation for 100 units? We use Excel today."
    ]
    [draft] = detail["drafts"]
    assert draft["source"] == "AI"
    assert draft["status"] == "SUGGESTED"
    assert "quotation" in draft["body"].lower()
    assert draft["ai_provider"] == "mock"
    assert any(n["kind"] == "CURRENT_SOLUTION" for n in detail["notes"])
    assert detail["can_send"] is True
    # AI suggestions never send anything by themselves.
    assert wa_live["fake"].calls("POST", "/messages") == []
    items = (
        await client.get(
            "/api/v1/action-items", headers=owner.headers, params={"contact_id": k["id"]}
        )
    ).json()
    assert [(i["title"], i["source"], i["confirmed_at"]) for i in items["items"]] == [
        ("Send quotation", "AI", None)
    ]
    timeline = (
        await client.get(f"/api/v1/contacts/{k['id']}/timeline", headers=owner.headers)
    ).json()
    msg_events = [e for e in timeline["items"] if e["category"] == "MESSAGE"]
    assert msg_events[0]["event_type"] == "MESSAGE_RECEIVED"
    assert msg_events[0]["channel"] == "WHATSAPP"
    assert "quotation" not in msg_events[0]["summary"]  # no message content in the timeline


async def test_webhooks_are_idempotent(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner)
    body = wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.dup")
    first = await deliver(client, wa_live["id"], body)
    second = await deliver(client, wa_live["id"], body)
    assert first.json()["applied"] == 1
    assert second.json()["duplicate"] == 1
    [conv] = await conversations(client, owner)
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert len(detail["messages"]) == 1


async def test_unknown_sender_is_unmatched_until_a_human_links_it(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await deliver(
        client,
        wa_live["id"],
        wa_payload(wa_id="919000011111", text="Hello?", message_id="wamid.u1"),
    )
    [conv] = await conversations(client, owner, match_status="UNMATCHED")
    assert conv["contact"] is None
    k = await contact(client, owner, name="New Lead", phone=None)
    linked = await client.post(
        f"/api/v1/conversations/{conv['id']}/link",
        headers=owner.headers,
        json={"contact_id": k["id"]},
    )
    assert linked.status_code == 200
    assert linked.json()["match_status"] == "MATCHED"
    # The confirmed identity matches future messages although the contact has no phone.
    await deliver(
        client, wa_live["id"], wa_payload(wa_id="919000011111", text="Again", message_id="wamid.u2")
    )
    [conv2] = await conversations(client, owner, contact_id=k["id"])
    assert conv2["id"] == conv["id"]
    other = await contact(client, owner, name="Someone else", phone=None)
    clash = await client.post(
        f"/api/v1/conversations/{conv['id']}/link",
        headers=owner.headers,
        json={"contact_id": other["id"]},
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "identity_linked_elsewhere"


async def test_ambiguous_phone_is_never_auto_matched(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner, name="Ravi Kumar")
    await contact(client, owner, name="Ravi K (duplicate)", phone="09876543210")
    await deliver(client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.a1"))
    [conv] = await conversations(client, owner)
    assert conv["match_status"] == "AMBIGUOUS"
    assert conv["contact"] is None


async def test_names_alone_never_match(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner, name="Ravi", phone="+91 91111 22222")
    await deliver(
        client,
        wa_live["id"],
        wa_payload(wa_id="919333344444", text="Hi", message_id="wamid.n1", name="Ravi"),
    )
    [conv] = await conversations(client, owner)
    assert conv["match_status"] == "UNMATCHED"


async def test_cross_tenant_webhooks_are_rejected(
    client: AsyncClient,
    owner: Account,
    wa_live: dict[str, Any],
    register: Register,
    make_client: Any,
) -> None:
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        other_values = {
            **WA_VALUES,
            "phone_number_id": "999888777",
            "app_secret": "beta-app-secret-000",
        }
        wa_fake()
        await configure(c2, other, "whatsapp", other_values)
        beta = await validate_and_enable(c2, other, "whatsapp")
        beta_id = integration_id(beta)
        payload_for_a = wa_payload(wa_id=WA_ID, text="Hi A", message_id="wamid.x1")
        # Tenant A's payload signed with A's secret, sent to B's URL -> signature fails.
        assert (await deliver(client, beta_id, payload_for_a)).status_code == 401
        # Signed with B's secret but for A's phone number id -> ignored (never trusted).
        forged = await deliver(client, beta_id, payload_for_a, secret="beta-app-secret-000")
        assert forged.status_code == 200
        assert forged.json()["received"] == 0
        assert await conversations(c2, other) == []
    assert await conversations(client, owner) == []


async def test_human_send_via_cloud_api(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    k = await contact(client, owner)
    await deliver(
        client,
        wa_live["id"],
        wa_payload(wa_id=WA_ID, text="Price for 100 units?", message_id="wamid.q1"),
    )
    await drain()
    [conv] = await conversations(client, owner)
    draft = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()[
        "drafts"
    ][0]
    cmid = str(uuid.uuid4())
    body = {
        "text": "Edited: I will confirm the price today.",
        "client_message_id": cmid,
        "draft_id": draft["id"],
    }
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", headers=owner.headers, json=body
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["status"] == "SENT"
    again = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", headers=owner.headers, json=body
    )
    assert again.json()["id"] == sent.json()["id"]  # idempotent: provider called once
    [req] = wa_live["fake"].calls("POST", "/111222333/messages")
    import json

    payload = json.loads(req.content)
    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": WA_ID,
        "type": "text",
        "text": {"preview_url": False, "body": "Edited: I will confirm the price today."},
    }
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["drafts"] == []  # the draft is marked SENT
    timeline = (
        await client.get(f"/api/v1/contacts/{k['id']}/timeline", headers=owner.headers)
    ).json()
    assert "MESSAGE_SENT" in {e["event_type"] for e in timeline["items"]}
    # Delivery status webhooks move forward only.
    wamid = str(json.loads(req.content)) and detail["messages"][-1]
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        out_id = await session.scalar(
            select(CommunicationMessage.external_message_id).where(
                CommunicationMessage.direction == "OUTBOUND"
            )
        )
    for status in ("delivered", "sent", "read"):
        await deliver(
            client,
            wa_live["id"],
            wa_status_payload(wa_id=WA_ID, message_id=str(out_id), status=status),
        )
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["messages"][-1]["status"] == "READ"
    assert wamid


async def test_24_hour_window_blocks_free_form_messages(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner)
    await deliver(client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.w1"))
    [conv] = await conversations(client, owner)
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        await session.execute(
            update(CommunicationSession).values(
                last_inbound_at=datetime.now(UTC) - timedelta(hours=25)
            )
        )
        await session.commit()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        headers=owner.headers,
        json={"text": "Hello", "client_message_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "whatsapp_window_closed"
    assert wa_live["fake"].calls("POST", "/messages") == []
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["can_send"] is False
    assert "24 hours" in detail["send_blocked_reason"]


async def test_send_failure_is_recorded_and_flags_integration(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner)
    await deliver(client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.f1"))
    [conv] = await conversations(client, owner)
    wa_live["fake"].on(
        "POST", "/111222333/messages", httpx.Response(401, json={"error": {"code": 190}})
    )
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        headers=owner.headers,
        json={"text": "Hello", "client_message_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "message_send_failed"
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["messages"][-1]["status"] == "FAILED"
    assert detail["messages"][-1]["error_code"] == "provider_auth_failed"
    integ = (await client.get("/api/v1/integrations/whatsapp", headers=owner.headers)).json()
    assert integ["status"] == "ERROR"
    assert integ["error_state"] == "provider_auth_failed"
    # CRM data is intact.
    assert (await client.get("/api/v1/contacts", headers=owner.headers)).json()["total"] == 1


async def test_ai_outage_keeps_message_and_deterministic_notes(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    ai = MockAIProvider()
    ai.fail_next(AIUnavailableError("down"), times=5)
    set_ai_provider(ai)
    await contact(client, owner)
    await deliver(
        client,
        wa_live["id"],
        wa_payload(wa_id=WA_ID, text="Right now we use Excel for stock.", message_id="wamid.ai1"),
    )
    await drain()
    [conv] = await conversations(client, owner)
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert len(detail["messages"]) == 1
    assert detail["drafts"] == []
    assert any(n["source"] == "DETERMINISTIC" for n in detail["notes"])


async def test_disabled_messaging_ignores_webhooks(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await client.post(
        "/api/v1/integrations/whatsapp/capabilities/MESSAGE/disable", headers=owner.headers
    )
    resp = await deliver(
        client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.d1")
    )
    assert resp.status_code == 200
    assert resp.json()["ignored_disabled"] == 1
    assert await conversations(client, owner) == []


async def test_messages_follow_retention(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    await contact(client, owner)
    await deliver(client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.r1"))
    await drain()
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        await session.execute(
            text("UPDATE communication_messages SET expires_at = now() - interval '1 day'")
        )
        await session.execute(
            text("UPDATE message_drafts SET expires_at = now() - interval '1 day'")
        )
        await session.commit()
    result = await purge_expired()
    assert result["communication_messages"] == 1
    assert result["message_drafts"] == 1
    [conv] = await conversations(client, owner)
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["messages"] == []
    assert detail["drafts"] == []


async def test_conversations_are_tenant_isolated(
    client: AsyncClient,
    owner: Account,
    wa_live: dict[str, Any],
    register: Register,
    make_client: Any,
) -> None:
    await contact(client, owner)
    await deliver(client, wa_live["id"], wa_payload(wa_id=WA_ID, text="Hi", message_id="wamid.t1"))
    [conv] = await conversations(client, owner)
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        assert (
            await c2.get(f"/api/v1/conversations/{conv['id']}", headers=other.headers)
        ).status_code == 404
        resp = await c2.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers=other.headers,
            json={"text": "x", "client_message_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404
        assert await conversations(c2, other) == []
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        assert (await session.scalars(select(CommunicationMessage))).all() == []


# ---- mock mode + developer simulation ------------------------------------------------------------


async def test_simulated_whatsapp_message_uses_real_pipeline(
    client: AsyncClient, owner: Account
) -> None:
    await configure(client, owner, "whatsapp", mode="MOCK")
    await validate_and_enable(client, owner, "whatsapp", "MESSAGE")
    k = await contact(client, owner)
    resp = await client.post(
        "/api/v1/integrations/dev/simulate/whatsapp-message",
        headers=owner.headers,
        json={"contact_id": k["id"], "text": "Can you send the quotation?"},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] == 1
    await drain()
    [conv] = await conversations(client, owner)
    assert conv["mock"] is True
    assert conv["contact"]["id"] == k["id"]
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    draft = detail["drafts"][0]
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        headers=owner.headers,
        json={
            "text": draft["body"],
            "client_message_id": str(uuid.uuid4()),
            "draft_id": draft["id"],
        },
    )
    assert sent.status_code == 201
    assert [m.text for m in MockWhatsAppClient.sent] == [draft["body"]]


async def test_simulation_requires_mock_mode(
    client: AsyncClient, owner: Account, wa_live: dict[str, Any]
) -> None:
    resp = await client.post(
        "/api/v1/integrations/dev/simulate/whatsapp-message",
        headers=owner.headers,
        json={"phone": "+919876543210", "text": "hi"},
    )
    assert resp.status_code == 409


async def test_contact_communication_options(client: AsyncClient, owner: Account) -> None:
    k = await contact(client, owner)
    opts = {
        o["key"]: o
        for o in (
            await client.get(f"/api/v1/contacts/{k['id']}/communication", headers=owner.headers)
        ).json()
    }
    assert opts["whatsapp_message"]["available"] is False
    assert opts["teams_call"]["available"] is False
    assert opts["phone_call"]["available"] is True
    assert opts["phone_call"]["mock"] is True
    await configure(client, owner, "whatsapp", mode="MOCK")
    await validate_and_enable(client, owner, "whatsapp", "MESSAGE")
    await configure(client, owner, "microsoft-teams", mode="MOCK")
    await validate_and_enable(client, owner, "microsoft-teams", "MESSAGE", "REAL_TIME_CALL")
    opts = {
        o["key"]: o
        for o in (
            await client.get(f"/api/v1/contacts/{k['id']}/communication", headers=owner.headers)
        ).json()
    }
    assert all(
        opts[key]["available"]
        for key in ("whatsapp_message", "teams_message", "teams_call", "phone_call")
    )
    opened = await client.post(
        f"/api/v1/contacts/{k['id']}/conversations",
        headers=owner.headers,
        json={"channel": "WHATSAPP"},
    )
    assert opened.status_code == 200
    assert opened.json()["contact"]["id"] == k["id"]
    teams = await client.post(
        f"/api/v1/contacts/{k['id']}/conversations",
        headers=owner.headers,
        json={"channel": "TEAMS"},
    )
    assert teams.status_code == 409
    assert teams.json()["error"]["code"] == "teams_chat_not_linked"


# ---- Microsoft Teams messaging -------------------------------------------------------------------


async def test_teams_mock_chat_link_message_and_reply(client: AsyncClient, owner: Account) -> None:
    await configure(client, owner, "microsoft-teams", mode="MOCK")
    await validate_and_enable(client, owner, "microsoft-teams", "MESSAGE")
    k = await contact(client, owner)
    chats = (
        await client.get("/api/v1/integrations/microsoft-teams/chats", headers=owner.headers)
    ).json()
    link = await client.post(
        "/api/v1/integrations/microsoft-teams/chats/link",
        headers=owner.headers,
        json={"chat_id": chats[0]["id"], "contact_id": k["id"]},
    )
    assert link.status_code == 200, link.text
    sim = await client.post(
        "/api/v1/integrations/dev/simulate/teams-message",
        headers=owner.headers,
        json={"chat_id": chats[0]["id"], "text": "We need a single dashboard for all branches."},
    )
    assert sim.json()["applied"] == 1
    await drain()
    [conv] = await conversations(client, owner, channel="TEAMS")
    assert conv["contact"]["id"] == k["id"]
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["messages"][0]["content"] == "We need a single dashboard for all branches."
    assert detail["drafts"]
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        headers=owner.headers,
        json={"text": "Yes, we support multiple branches.", "client_message_id": str(uuid.uuid4())},
    )
    assert sent.status_code == 201
    assert MockTeamsMessageProvider.sent[0].to == chats[0]["id"]
    opened = await client.post(
        f"/api/v1/contacts/{k['id']}/conversations",
        headers=owner.headers,
        json={"channel": "TEAMS"},
    )
    assert opened.json()["id"] == conv["id"]


@pytest.fixture
async def teams_live(client: AsyncClient, owner: Account, public_https: None) -> FakeProvider:
    fake = FakeProvider().install()
    fake.on("POST", "/oauth2/v2.0/token", _token_response)
    await configure(client, owner, "microsoft-teams", TEAMS_VALUES)
    await validate_and_enable(client, owner, "microsoft-teams", "MESSAGE")
    return fake


def _jwt(claims: dict[str, Any]) -> str:
    import base64
    import json

    def enc(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


SCOPES = "User.Read Chat.Read ChatMessage.Send"


def _token_response(request: httpx.Request) -> httpx.Response:
    form = parse_qs(request.content.decode())
    if form["grant_type"] == ["client_credentials"]:
        return httpx.Response(200, json={"access_token": _jwt({"roles": []})})
    return httpx.Response(
        200,
        json={
            "access_token": _jwt({"scp": SCOPES}),
            "refresh_token": "ms-refresh-token-secret",
            "scope": SCOPES + " offline_access",
        },
    )


async def test_teams_oauth_connect_stores_encrypted_refresh_token(
    client: AsyncClient, owner: Account, teams_live: FakeProvider
) -> None:
    teams_live.on(
        "GET",
        "/me",
        httpx.Response(
            200, json={"id": "me-aad", "displayName": "Owner", "mail": "owner@acme.com"}
        ),
    )
    start = await client.post("/api/v1/integrations/microsoft-teams/connect", headers=owner.headers)
    url = start.json()["authorization_url"]
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert TEAMS_VALUES["tenant_id"] in parts.path
    assert query["scope"] == ["offline_access User.Read Chat.Read ChatMessage.Send"]
    assert query["client_id"] == [TEAMS_VALUES["client_id"]]
    callback = await client.get(
        "/api/v1/integrations/microsoft-teams/oauth/callback",
        params={"code": "auth-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].endswith("teams=connected")
    conn = (
        await client.get("/api/v1/integrations/microsoft-teams/connection", headers=owner.headers)
    ).json()
    assert conn == {"connected": True, "account_email": "owner@acme.com", "status": "ACTIVE"}
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        row = await session.scalar(select(IntegrationUserConnection))
    assert row is not None
    assert "ms-refresh-token-secret" not in row.encrypted_refresh_token
    tampered = await client.get(
        "/api/v1/integrations/microsoft-teams/oauth/callback",
        params={"code": "x", "state": query["state"][0] + "x"},
        follow_redirects=False,
    )
    assert tampered.headers["location"].endswith("teams=invalid_state")


async def test_teams_connect_reports_missing_permissions(
    client: AsyncClient, owner: Account, teams_live: FakeProvider
) -> None:
    teams_live.on(
        "POST",
        "/oauth2/v2.0/token",
        httpx.Response(
            200,
            json={
                "access_token": _jwt({"scp": "User.Read"}),
                "refresh_token": "r",
                "scope": "User.Read",
            },
        ),
    )
    start = await client.post("/api/v1/integrations/microsoft-teams/connect", headers=owner.headers)
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    callback = await client.get(
        "/api/v1/integrations/microsoft-teams/oauth/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert callback.headers["location"].endswith("teams=teams_permissions_missing")


async def test_teams_notifications_validation_and_message_fetch(
    client: AsyncClient, owner: Account, teams_live: FakeProvider
) -> None:
    detail = (
        await client.get("/api/v1/integrations/microsoft-teams", headers=owner.headers)
    ).json()
    iid = integration_id(detail)
    url = f"/api/v1/integrations/teams/webhooks/{iid}"
    handshake = await client.post(url, params={"validationToken": "Validation: Token 123"})
    assert handshake.status_code == 200
    assert handshake.text == "Validation: Token 123"
    assert handshake.headers["content-type"].startswith("text/plain")

    # Connect the user, link a chat (creates a subscription), then receive a notification.
    teams_live.on(
        "GET",
        "/me/chats",
        httpx.Response(200, json={"value": [{"id": "19:chat-1", "chatType": "oneOnOne"}]}),
    )
    teams_live.on(
        "GET", "/v1.0/me?", httpx.Response(200, json={"id": "me-aad", "mail": "owner@acme.com"})
    )
    teams_live.on("POST", "/subscriptions", httpx.Response(201, json={"id": "sub-1"}))
    teams_live.on(
        "GET",
        "/chats/19%3Achat-1/messages/1700",
        httpx.Response(
            200,
            json={
                "id": "1700",
                "messageType": "message",
                "createdDateTime": "2026-09-24T10:00:00Z",
                "from": {"user": {"id": "customer-aad", "displayName": "Ravi"}},
                "body": {
                    "contentType": "html",
                    "content": "<p>Do you support multiple branches?</p>",
                },
            },
        ),
    )
    start = await client.post("/api/v1/integrations/microsoft-teams/connect", headers=owner.headers)
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    await client.get(
        "/api/v1/integrations/microsoft-teams/oauth/callback", params={"code": "c", "state": state}
    )
    k = await contact(client, owner)
    link = await client.post(
        "/api/v1/integrations/microsoft-teams/chats/link",
        headers=owner.headers,
        json={"chat_id": "19:chat-1", "contact_id": k["id"]},
    )
    assert link.status_code == 200, link.text
    [sub_req] = teams_live.calls("POST", "/subscriptions")
    import json

    sub_body = json.loads(sub_req.content)
    assert sub_body["resource"] == "/chats/19:chat-1/messages"
    assert sub_body["notificationUrl"] == f"{PUBLIC}/api/v1/integrations/teams/webhooks/{iid}"
    assert sub_body["includeResourceData"] is False
    assert sub_body["clientState"] == client_state_for(uuid.UUID(iid))

    note = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "changeType": "created",
                "clientState": sub_body["clientState"],
                "resource": "chats('19:chat-1')/messages('1700')",
            }
        ]
    }
    bad = await client.post(url, json={"value": [{**note["value"][0], "clientState": "forged"}]})
    assert bad.status_code == 401
    ok = await client.post(url, json=note)
    assert ok.status_code == 202
    assert ok.json()["queued"] == 1
    dup = await client.post(url, json=note)
    assert dup.json()["queued"] == 0
    await drain()
    [conv] = await conversations(client, owner, channel="TEAMS")
    assert conv["contact"]["id"] == k["id"]
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=owner.headers)).json()
    assert detail["messages"][0]["content"] == "Do you support multiple branches?"
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        ident = await session.scalar(select(ContactIdentity.kind))
    assert ident == "TEAMS_CHAT"
