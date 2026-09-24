"""Real-time call channels on the SAME live pipeline: Teams (media gateway contract, mock
gateway + simulator) and Plivo (REST, signed callbacks, answer XML, media stream).

Covers Teams transcript persistence rules (transient by default; stored only after the
gateway confirms Microsoft's recording status), gateway authentication, Plivo lifecycle,
idempotency, cross-tenant protection, inbound calls and track mapping."""

import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.common.config import get_settings
from app.common.db import TenantContext
from app.conversations.models import CommunicationEvent, CommunicationSession
from app.integrations import simulator as integration_simulator
from app.integrations.providers import factory
from app.integrations.providers.plivo import compute_signature_v3
from app.integrations.providers.teams import sign_gateway
from app.intel.models import CallNote
from app.jobs.models import Job
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.telephony.models import CallRoute
from app.telephony.provider import media_parser_for
from app.telephony.service import MediaIngest
from tests.conftest import Account, open_session
from tests.integration_helpers import (
    PLIVO_VALUES,
    PUBLIC,
    FakeProvider,
    capability,
    configure,
    integration_id,
    validate_and_enable,
)

Register = Callable[..., Awaitable[Account]]
MEETING = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d"
SCRIPT = [
    ("agent", "Thanks for joining. How do you manage inventory today?"),
    (
        "customer",
        "Right now we track everything in Excel and we have 5 branches, it takes a lot of time.",
    ),
    ("agent", "What budget have you planned?"),
    ("customer", "Our budget is around 2 lakh for this year."),
]


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme Traders")
    await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    return acct


async def new_contact(client: AsyncClient, acct: Account) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.post(
            "/api/v1/contacts",
            headers=acct.headers,
            json={"name": "Ravi Kumar", "phone": "+919876543210"},
        )
    ).json()
    return body


async def teams_call(client: AsyncClient, acct: Account, persistence: str) -> dict[str, Any]:
    await configure(client, acct, "microsoft-teams", {"call_persistence": persistence}, mode="MOCK")
    await validate_and_enable(client, acct, "microsoft-teams", "REAL_TIME_CALL")
    k = await new_contact(client, acct)
    call = await client.post(
        "/api/v1/calls",
        headers=acct.headers,
        json={
            "contact_id": k["id"],
            "channel": "TEAMS",
            "meeting_url": MEETING,
            "objective": "Demo",
        },
    )
    assert call.status_code == 201, call.text
    assert call.json()["channel"] == "TEAMS"
    started = await client.post(f"/api/v1/calls/{call.json()['id']}/start", headers=acct.headers)
    assert started.status_code == 200, started.text
    body: dict[str, Any] = started.json()
    return body


async def route_of(call_id: str) -> CallRoute:
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call_id))
    assert route is not None
    return route


async def count(model: Any, company_id: uuid.UUID) -> int:
    session = await open_session(TenantContext(company_id=company_id))
    async with session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ---- Teams ---------------------------------------------------------------------------------------


async def test_teams_call_requires_meeting_link_and_capability(
    client: AsyncClient, rep: Account
) -> None:
    k = await new_contact(client, rep)
    bad = await client.post(
        "/api/v1/calls",
        headers=rep.headers,
        json={
            "contact_id": k["id"],
            "channel": "TEAMS",
            "meeting_url": "https://evil.example.com/x",
        },
    )
    assert bad.status_code == 422
    call = await client.post(
        "/api/v1/calls",
        headers=rep.headers,
        json={"contact_id": k["id"], "channel": "TEAMS", "meeting_url": MEETING},
    )
    resp = await client.post(f"/api/v1/calls/{call.json()['id']}/start", headers=rep.headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "integration_not_ready"


async def test_teams_transient_call_persists_nothing_derived_from_media(
    client: AsyncClient, rep: Account
) -> None:
    call = await teams_call(client, rep, "TRANSIENT")
    assert call["provider"] == "teams-mock"
    assert call["transcript_persistence"] == "TRANSIENT"
    [joined] = factory.mock_teams_gateway().joined
    assert joined["join_url"] == MEETING
    assert joined["receive_only"] is True
    assert joined["persistence"] == "TRANSIENT"
    assert "/api/v1/telephony/media/teams-mock/" in joined["media_ws_url"]
    route = await route_of(call["id"])
    await integration_simulator.run_teams_call(
        uuid.UUID(call["id"]), route.provider_call_id or "", persistence="TRANSIENT", script=SCRIPT
    )
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "COMPLETED"
    assert snap["channel"] == "TEAMS"
    # Live screen got the transcript and copilot output...
    events = [e.type for e in hub.channel(uuid.UUID(call["id"])).buffer]
    assert events.count("transcript.final") == len(SCRIPT)
    assert "note.upserted" in events
    # ...but nothing derived from the media was written to the database.
    assert await count(TranscriptSegment, rep.company_id) == 0
    assert await count(CallNote, rep.company_id) == 0
    session = await open_session()
    async with session:
        jobs = (await session.scalars(select(Job.kind))).all()
    assert "postcall.process" not in jobs
    timeline = (
        await client.get(
            f"/api/v1/contacts/{snap['call']['contact_id']}/timeline", headers=rep.headers
        )
    ).json()
    summaries = [e["summary"] for e in timeline["items"] if e["category"] == "CALL"]
    assert "Teams call completed" in summaries
    assert all(e["channel"] == "TEAMS" for e in timeline["items"] if e["category"] == "CALL")


async def test_teams_recording_declared_call_is_stored_after_confirmation(
    client: AsyncClient, rep: Account
) -> None:
    call = await teams_call(client, rep, "RECORDING_DECLARED")
    assert call["transcript_persistence"] == "PENDING_RECORDING_STATUS"
    route = await route_of(call["id"])
    await integration_simulator.run_teams_call(
        uuid.UUID(call["id"]),
        route.provider_call_id or "",
        persistence="RECORDING_DECLARED",
        script=SCRIPT,
    )
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["transcript_persistence"] == "PERSISTED"
    assert len(snap["transcript"]) == len(SCRIPT)
    assert snap["transcript"][1]["speaker"] == "CUSTOMER"
    kinds = {n["kind"] for n in snap["notes"]}
    assert {"CURRENT_SOLUTION", "BUDGET"} <= kinds
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        types = set((await session.scalars(select(CommunicationEvent.type))).all())
        comm = await session.scalar(select(CommunicationSession))
    assert {
        "CALL_STARTED",
        "CALL_CONNECTED",
        "CALL_ENDED",
        "AUDIO_STREAM_STARTED",
        "AUDIO_STREAM_ENDED",
        "RECORDING_STATUS_CHANGED",
    } <= types
    assert comm is not None
    assert comm.channel == "TEAMS"
    assert comm.capability == "REAL_TIME_CALL"
    assert comm.status == "ENDED"


async def test_teams_call_simulation_endpoint(
    client: AsyncClient, rep: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "simulation_utterance_delay_seconds", 0)
    call = await teams_call(client, rep, "TRANSIENT")
    resp = await client.post(f"/api/v1/calls/{call['id']}/simulate", headers=rep.headers)
    assert resp.status_code == 202
    assert resp.json()["provider"] == "teams-mock"
    task = integration_simulator.running.get(uuid.UUID(call["id"]))
    if task is not None:
        await task
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "COMPLETED"


async def test_teams_gateway_failure_fails_call_cleanly(client: AsyncClient, rep: Account) -> None:
    await configure(client, rep, "microsoft-teams", mode="MOCK")
    await validate_and_enable(client, rep, "microsoft-teams", "REAL_TIME_CALL")
    factory.mock_teams_gateway().fail_join = True
    k = await new_contact(client, rep)
    call = (
        await client.post(
            "/api/v1/calls",
            headers=rep.headers,
            json={"contact_id": k["id"], "channel": "TEAMS", "meeting_url": MEETING},
        )
    ).json()
    resp = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert resp.status_code == 502
    assert (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()[
        "status"
    ] == "FAILED"


@pytest.fixture
def gateway_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "g" * 40
    s = get_settings()
    from pydantic import SecretStr

    monkeypatch.setattr(s, "teams_media_gateway_secret", SecretStr(secret))
    monkeypatch.setattr(s, "teams_media_gateway_url", "https://gateway.example.com")
    return secret


async def test_gateway_events_require_signature(
    client: AsyncClient, rep: Account, gateway_secret: str
) -> None:
    k = await new_contact(client, rep)
    call = (
        await client.post(
            "/api/v1/calls",
            headers=rep.headers,
            json={"contact_id": k["id"], "channel": "TEAMS", "meeting_url": MEETING},
        )
    ).json()
    # A live Teams call placed through the gateway (route written as start_call would).
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        from sqlalchemy import update

        from app.calls.models import Call

        await session.execute(
            update(Call).values(status="INITIATED", provider="teams", provider_call_id="gw-1")
        )
        session.add(
            CallRoute(
                call_id=uuid.UUID(call["id"]),
                company_id=rep.company_id,
                provider="teams",
                provider_call_id="gw-1",
            )
        )
        await session.commit()
    body = json.dumps(
        {"event_id": "e1", "call_id": call["id"], "gateway_call_id": "gw-1", "state": "ESTABLISHED"}
    ).encode()
    url = "/api/v1/integrations/teams/gateway/events"
    assert (await client.post(url, content=body)).status_code == 401
    forged = sign_gateway("x" * 40, body)
    assert (await client.post(url, content=body, headers=forged)).status_code == 401
    ok = await client.post(url, content=body, headers=sign_gateway(gateway_secret, body))
    assert ok.status_code == 200
    assert ok.json()["outcome"] == "applied"
    dup = await client.post(url, content=body, headers=sign_gateway(gateway_secret, body))
    assert dup.json()["outcome"] == "duplicate"
    assert (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()[
        "status"
    ] == "CONNECTED"


async def test_gateway_client_signs_requests(gateway_secret: str) -> None:
    from app.integrations.providers.teams import TeamsGatewayClient, verify_gateway_signature

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        verify_gateway_signature(
            gateway_secret, {k.lower(): v for k, v in request.headers.items()}, request.content
        )
        return httpx.Response(200, json={"gateway_call_id": "gw-9"})

    client = TeamsGatewayClient(
        "https://gateway.example.com", gateway_secret, httpx.MockTransport(handler)
    )
    assert await client.join({"call_id": "x"}) == "gw-9"
    await client.leave("gw-9")
    assert [r.method for r in seen] == ["POST", "DELETE"]


# ---- Plivo ---------------------------------------------------------------------------------------


@pytest.fixture
def public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", PUBLIC)


@pytest.fixture
async def plivo_live(client: AsyncClient, rep: Account, public_https: None) -> dict[str, Any]:
    fake = FakeProvider().install()
    fake.on("GET", "/Account/MAXXXXXXXXXXXXXXXXXX/", httpx.Response(200, json={"name": "Acme"}))
    fake.on("GET", "/Number/918000000000/", httpx.Response(200, json={}))
    fake.on(
        "POST",
        "/Call/",
        httpx.Response(
            201, json={"api_id": "a", "message": "call fired", "request_uuid": "req-uuid-1"}
        ),
    )
    fake.on("DELETE", "/Call/", httpx.Response(204))
    await configure(client, rep, "plivo", PLIVO_VALUES)
    detail = await validate_and_enable(client, rep, "plivo", "PHONE_CALL")
    assert capability(detail, "PHONE_CALL")["state"] == "ENABLED"
    return {"fake": fake, "id": integration_id(detail)}


async def plivo_post(
    client: AsyncClient,
    path_and_query: str,
    params: dict[str, str],
    token: str = PLIVO_VALUES["auth_token"],
) -> httpx.Response:
    url = f"{PUBLIC}{path_and_query}"
    sig = compute_signature_v3(token, url, "nonce-1", "POST", params)
    return await client.post(
        path_and_query,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Plivo-Signature-V3": sig,
            "X-Plivo-Signature-V3-Nonce": "nonce-1",
        },
    )


async def test_plivo_outbound_call_lifecycle(
    client: AsyncClient, rep: Account, plivo_live: dict[str, Any]
) -> None:
    k = await new_contact(client, rep)
    call = (
        await client.post("/api/v1/calls", headers=rep.headers, json={"contact_id": k["id"]})
    ).json()
    started = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert started.status_code == 200, started.text
    assert started.json()["provider"] == "plivo"
    [create] = plivo_live["fake"].calls("POST", "/Call/")
    body = json.loads(create.content)
    assert body["from"] == "918000000000"
    assert body["to"] == "919800000001"
    iid = plivo_live["id"]
    assert (
        body["answer_url"]
        == f"{PUBLIC}/api/v1/integrations/plivo/webhooks/{iid}/answer?cid={call['id']}"
    )
    base = f"/api/v1/integrations/plivo/webhooks/{iid}"

    bad = await plivo_post(
        client, f"{base}/answer?cid={call['id']}", {"CallUUID": "cu-1"}, token="wrong"
    )
    assert bad.status_code == 401
    answer = await plivo_post(
        client, f"{base}/answer?cid={call['id']}", {"CallUUID": "cu-1", "CallStatus": "in-progress"}
    )
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("application/xml")
    assert "<Number>+919876543210</Number>" in answer.text
    assert "<Stream" not in answer.text  # real-time audio capability not enabled
    connected = await plivo_post(
        client, f"{base}/dial?cid={call['id']}", {"CallUUID": "cu-1", "DialAction": "answer"}
    )
    assert connected.status_code == 200
    assert (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()[
        "status"
    ] == "CONNECTED"
    params = {"CallUUID": "cu-1", "CallStatus": "completed", "Duration": "95"}
    await plivo_post(client, f"{base}/hangup?cid={call['id']}", params)
    await plivo_post(client, f"{base}/hangup?cid={call['id']}", params)  # duplicate delivery
    final = (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()
    assert final["status"] == "COMPLETED"
    assert final["duration_seconds"] == 95


async def test_plivo_end_call_uses_rest_api(
    client: AsyncClient, rep: Account, plivo_live: dict[str, Any]
) -> None:
    k = await new_contact(client, rep)
    call = (
        await client.post("/api/v1/calls", headers=rep.headers, json={"contact_id": k["id"]})
    ).json()
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await plivo_post(
        client,
        f"/api/v1/integrations/plivo/webhooks/{plivo_live['id']}/ring?cid={call['id']}",
        {"CallUUID": "cu-7"},
    )
    resp = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert resp.status_code == 200
    assert plivo_live["fake"].calls("DELETE", "/Call/cu-7/")


async def test_plivo_callbacks_cannot_touch_another_tenant(
    client: AsyncClient,
    rep: Account,
    plivo_live: dict[str, Any],
    register: Register,
    make_client: Any,
) -> None:
    k = await new_contact(client, rep)
    call = (
        await client.post("/api/v1/calls", headers=rep.headers, json={"contact_id": k["id"]})
    ).json()
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        other_values = {
            **PLIVO_VALUES,
            "auth_id": "MBYYYYYYYYYYYYYYYYYY",
            "auth_token": "beta-token",
        }
        fake = plivo_live["fake"]
        fake.on("GET", "/Account/MBYYYYYYYYYYYYYYYYYY/", httpx.Response(200, json={}))
        await configure(c2, other, "plivo", other_values)
        beta = await validate_and_enable(c2, other, "plivo", "PHONE_CALL")
        path = f"/api/v1/integrations/plivo/webhooks/{integration_id(beta)}/hangup?cid={call['id']}"
        # Correctly signed for tenant B, but the call belongs to tenant A.
        resp = await plivo_post(
            client, path, {"CallUUID": "cu-x", "CallStatus": "completed"}, token="beta-token"
        )
        assert resp.status_code == 200
    assert (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()[
        "status"
    ] == "INITIATED"


async def test_plivo_inbound_call_rings_salesperson(
    client: AsyncClient, rep: Account, plivo_live: dict[str, Any]
) -> None:
    k = await new_contact(client, rep)
    await client.patch(
        f"/api/v1/contacts/{k['id']}", headers=rep.headers, json={"owner_user_id": str(rep.user_id)}
    )
    base = f"/api/v1/integrations/plivo/webhooks/{plivo_live['id']}"
    resp = await plivo_post(
        client,
        f"{base}/inbound",
        {"CallUUID": "in-1", "From": "919876543210", "To": "918000000000", "Direction": "inbound"},
    )
    assert resp.status_code == 200
    assert "<Number>+919800000001</Number>" in resp.text  # the salesperson's phone
    calls = (
        await client.get("/api/v1/calls", headers=rep.headers, params={"contact_id": k["id"]})
    ).json()
    [call] = calls["items"]
    assert call["status"] == "RINGING"
    assert call["provider"] == "plivo"
    unknown = await plivo_post(
        client,
        f"{base}/inbound",
        {"CallUUID": "in-2", "From": "919111111111", "To": "918000000000"},
    )
    assert unknown.status_code == 200
    contacts = (await client.get("/api/v1/contacts", headers=rep.headers)).json()
    assert any(
        c["source"] == "INBOUND_CALL" and c["phone"] == "+919111111111" for c in contacts["items"]
    )


async def test_plivo_media_stream_feeds_live_pipeline(
    client: AsyncClient, rep: Account, plivo_live: dict[str, Any]
) -> None:
    k = await new_contact(client, rep)
    call = (
        await client.post("/api/v1/calls", headers=rep.headers, json={"contact_id": k["id"]})
    ).json()
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    parse = media_parser_for("plivo")
    ingest = MediaIngest(uuid.UUID(call["id"]))
    await ingest.handle(
        parse(
            json.dumps(
                {
                    "event": "start",
                    "start": {"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}},
                }
            )
        )
    )

    def frame(track: str, seq: int, text: str) -> str:
        return json.dumps(
            {
                "event": "media",
                "sequenceNumber": seq,
                "media": {"track": track, "payload": base64.b64encode(text.encode()).decode()},
            }
        )

    await ingest.handle(parse(frame("inbound", 1, "Hello this is the salesperson")))
    from app.telephony.simulator import _wait_for_segment

    await _wait_for_segment(uuid.UUID(call["id"]), 1)
    await ingest.handle(parse(frame("outbound", 2, "We use Excel for stock today")))
    await _wait_for_segment(uuid.UUID(call["id"]), 2)
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "ACTIVE"
    assert [s["speaker"] for s in snap["transcript"]] == ["SALES_REP", "CUSTOMER"]


def test_media_websocket_rejects_wrong_provider() -> None:
    from starlette.testclient import TestClient

    from app.main import app
    from app.telephony.provider import media_token

    call_id = uuid.uuid4()
    with TestClient(app) as tc:
        unknown = f"/api/v1/telephony/media/unknown/{call_id}?token={media_token(call_id)}"
        with pytest.raises(Exception), tc.websocket_connect(unknown):  # noqa: B017, PT011
            pass
        bad_token = f"/api/v1/telephony/media/plivo/{call_id}?token=bad"
        with pytest.raises(Exception), tc.websocket_connect(bad_token):  # noqa: B017, PT011
            pass
