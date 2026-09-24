"""Real-time calling pipeline with the mock telephony + mock STT providers.

Covers: call start, verified/idempotent/out-of-order webhooks, media -> STT -> transcript,
copilot (detectors, agenda tracking, knowledge fallback, LLM pass), STT/LLM/browser failures,
raw-audio non-persistence, retention, tenant isolation of live data.
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update

from app.ai.gateway import set_ai_provider
from app.ai.mock_provider import MockAIProvider
from app.ai.provider import AITask, AIUnavailableError
from app.common.db import TenantContext
from app.intel.models import CallNote
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.privacy.retention import purge_expired
from app.speech.provider import MockSpeechToTextProvider, set_stt_provider
from app.telephony import simulator
from app.telephony.provider import sign_mock_webhook
from tests.conftest import Account, open_session

Register = Callable[..., Awaitable[Account]]

AGENDA = [
    {"title": "Current process", "question": "How do you manage inventory today?"},
    {"title": "Budget", "question": "Have you set aside a budget?"},
    {"title": "Timeline", "question": "By when do you need it?"},
    {"title": "Decision maker", "question": "Who else is involved in the decision?"},
    {"title": "Competitors", "question": "Are you evaluating other vendors?"},
]


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme Traders")
    resp = await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    assert resp.status_code == 200
    return acct


async def started_call(
    client: AsyncClient, acct: Account, *, agenda: bool = True
) -> dict[str, Any]:
    contact = (
        await client.post(
            "/api/v1/contacts",
            headers=acct.headers,
            json={"name": "Ravi Kumar", "phone": "+919876543210"},
        )
    ).json()
    call = (
        await client.post(
            "/api/v1/calls",
            headers=acct.headers,
            json={"contact_id": contact["id"], "objective": "Qualify inventory automation"},
        )
    ).json()
    if agenda:
        r = await client.put(
            f"/api/v1/calls/{call['id']}/agenda", headers=acct.headers, json={"items": AGENDA}
        )
        assert r.status_code == 200, r.text
    started = await client.post(f"/api/v1/calls/{call['id']}/start", headers=acct.headers)
    assert started.status_code == 200, started.text
    body: dict[str, Any] = started.json()
    return body


async def run_default_simulation(call: dict[str, Any]) -> None:
    await simulator.run(uuid.UUID(call["id"]), await provider_call_id(call["id"]), delay=0)


async def snapshot(client: AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/calls/{call_id}/live", headers=acct.headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def buffered_types(call_id: str) -> list[str]:
    return [e.type for e in hub.channel(uuid.UUID(call_id)).buffer]


# ---- Start -------------------------------------------------------------------------------------


async def test_start_requires_phone_numbers(client: AsyncClient, register: Register) -> None:
    acct = await register(client, "nophone@example.com", "Acme")
    contact = (
        await client.post("/api/v1/contacts", headers=acct.headers, json={"name": "X"})
    ).json()
    call = (
        await client.post("/api/v1/calls", headers=acct.headers, json={"contact_id": contact["id"]})
    ).json()
    resp = await client.post(f"/api/v1/calls/{call['id']}/start", headers=acct.headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_phone"


async def test_start_places_call_via_provider(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep, agenda=False)
    assert call["status"] == "INITIATED"
    again = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert again.status_code == 409  # cannot start twice


# ---- Full pipeline ----------------------------------------------------------------------------


async def test_full_call_pipeline(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep)
    await run_default_simulation(call)
    snap = await snapshot(client, rep, call["id"])

    assert snap["call"]["status"] == "COMPLETED"
    assert snap["call"]["started_at"]
    assert snap["call"]["ended_at"]
    transcript = snap["transcript"]
    assert len(transcript) == len(simulator.DEFAULT_SCRIPT)
    assert [s["seq"] for s in transcript] == list(range(1, len(transcript) + 1))
    assert transcript[0]["speaker"] == "SALES_REP"
    assert transcript[1]["speaker"] == "CUSTOMER"
    assert transcript[0]["speaker_confidence"] == 1.0
    assert all(s["is_final"] for s in transcript)

    notes = {(n["kind"], n["category"], n["text"]) for n in snap["notes"]}
    kinds = {k for k, _, _ in notes}
    assert ("CURRENT_SOLUTION", None, "Uses Excel") in notes
    assert ("REQUIREMENT", None, "5 branches") in notes
    assert {"OBJECTION", "BUDGET", "TIMELINE", "NEXT_STEP", "PAIN_POINT"} <= kinds
    categories = {c for k, c, _ in notes if k == "OBJECTION"}
    assert {"PRICE", "AUTHORITY"} <= categories
    assert all(n["status"] == "SUGGESTED" for n in snap["notes"])  # awaiting human review

    types = {i["type"] for i in snap["insights"]}
    assert {
        "OBJECTION",
        "IMPORTANT_FACT",
        "CUSTOMER_REQUIREMENT",
        "NEXT_STEP",
        "KNOWLEDGE_RESULT",
    } <= types
    kb = [i for i in snap["insights"] if i["type"] == "KNOWLEDGE_RESULT"]
    assert kb[0]["content"].startswith("No relevant company information")  # empty knowledge base

    agenda = {a["title"]: a for a in snap["agenda"]}
    assert agenda["Budget"]["status"] == "COMPLETED"
    assert agenda["Budget"]["status_source"] in ("DETERMINISTIC", "AI")
    assert agenda["Current process"]["status"] == "COMPLETED"
    assert agenda["Competitors"]["status"] == "NOT_STARTED"  # never discussed
    missing = [i for i in snap["insights"] if i["type"] == "MISSING_AGENDA_ITEM"]
    assert any("Competitors" in i["content"] for i in missing)

    tl = (
        await client.get(f"/api/v1/contacts/{call['contact_id']}/timeline", headers=rep.headers)
    ).json()
    call_events = [e["to_status"] for e in tl["items"] if e["call_id"] == call["id"]]
    assert "CONNECTED" in call_events
    assert "COMPLETED" in call_events

    events = buffered_types(call["id"])
    assert "transcript.final" in events
    assert "agenda.updated" in events
    assert "insight.created" in events


async def test_llm_pass_runs_incrementally_and_within_budget(
    client: AsyncClient, rep: Account, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = MockAIProvider()
    set_ai_provider(provider)
    monkeypatch.setattr(settings, "copilot_llm_every_n_segments", 4)
    monkeypatch.setattr(settings, "copilot_max_llm_calls_per_call", 2)
    call = await started_call(client, rep)
    await run_default_simulation(call)
    assert provider.calls.count(AITask.COPILOT) == 2  # capped per call, not per segment


async def test_llm_failure_does_not_affect_call(client: AsyncClient, rep: Account) -> None:
    provider = MockAIProvider()
    provider.fail_next(AIUnavailableError("down"), times=50)
    set_ai_provider(provider)
    call = await started_call(client, rep)
    await run_default_simulation(call)
    snap = await snapshot(client, rep, call["id"])
    assert snap["call"]["status"] == "COMPLETED"
    assert len(snap["transcript"]) == len(simulator.DEFAULT_SCRIPT)
    assert any(n["kind"] == "OBJECTION" for n in snap["notes"])  # deterministic path still works
    statuses = [
        e.payload for e in hub.channel(uuid.UUID(call["id"])).buffer if e.type == "copilot.status"
    ]
    assert statuses
    assert statuses[0]["state"] == "degraded"


async def test_stt_failure_does_not_end_call(client: AsyncClient, rep: Account) -> None:
    stt = MockSpeechToTextProvider()
    stt.fail_after = 3
    set_stt_provider(stt)
    call = await started_call(client, rep)
    await run_default_simulation(call)
    snap = await snapshot(client, rep, call["id"])
    assert snap["call"]["status"] == "COMPLETED"  # the phone call finished normally
    assert 0 < len(snap["transcript"]) < len(simulator.DEFAULT_SCRIPT)
    stt_events = [
        e.payload for e in hub.channel(uuid.UUID(call["id"])).buffer if e.type == "stt.status"
    ]
    assert stt_events[0]["state"] == "unavailable"


async def test_raw_audio_is_never_persisted(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep)
    await run_default_simulation(call)
    session = await open_session()
    async with session:
        binary_columns = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type IN ('bytea', 'oid')"
                )
            )
        ).all()
    assert binary_columns == []
    snap = await snapshot(client, rep, call["id"])
    assert [s["text"] for s in snap["transcript"]] == [t for _, t in simulator.DEFAULT_SCRIPT]


async def test_transcript_expiry_follows_company_retention(
    client: AsyncClient, rep: Account
) -> None:
    await client.patch(
        "/api/v1/companies/current", headers=rep.headers, json={"transcript_retention_days": 7}
    )
    call = await started_call(client, rep, agenda=False)
    await run_default_simulation(call)
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        seg = await session.scalar(select(TranscriptSegment).limit(1))
    assert seg is not None
    delta = seg.expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


# ---- Webhooks ---------------------------------------------------------------------------------


async def post_webhook(
    client: AsyncClient,
    payload: dict[str, Any],
    *,
    signature: str | None = None,
    ts: int | None = None,
) -> Any:
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else sign_mock_webhook(body, ts)
    return await client.post(
        "/api/v1/webhooks/telephony/mock",
        content=body,
        headers={"X-Mock-Signature": sig, "Content-Type": "application/json"},
    )


async def provider_call_id(call_id: str) -> str:
    from app.telephony.models import CallRoute

    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call_id))
    assert route is not None
    assert route.provider_call_id
    return route.provider_call_id


async def test_webhook_signature_and_replay_protection(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep, agenda=False)
    sid = await provider_call_id(call["id"])
    event = {"event_id": "e1", "call_sid": sid, "status": "RINGING"}
    forged = await post_webhook(client, event, signature="t=1,v1=deadbeef")
    assert forged.status_code == 401
    stale = await post_webhook(client, event, ts=int(time.time()) - 3600)
    assert stale.status_code == 401
    missing = await client.post("/api/v1/webhooks/telephony/mock", content=b"{}")
    assert missing.status_code == 401
    garbage = await post_webhook(client, {"nope": 1})
    assert garbage.status_code == 401
    ok = await post_webhook(client, event)
    assert ok.status_code == 200
    assert ok.json() == {"received": 1, "applied": 1}
    detail = await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)
    assert detail.json()["status"] == "RINGING"


async def test_webhooks_are_idempotent_and_order_safe(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep, agenda=False)
    sid = await provider_call_id(call["id"])
    first = await post_webhook(client, {"event_id": "a", "call_sid": sid, "status": "CONNECTED"})
    dup = await post_webhook(client, {"event_id": "a", "call_sid": sid, "status": "CONNECTED"})
    assert first.json()["applied"] == 1
    assert dup.json() == {"received": 1, "duplicate": 1}
    done = await post_webhook(
        client, {"event_id": "b", "call_sid": sid, "status": "COMPLETED", "duration": 42}
    )
    assert done.json()["applied"] == 1
    late = await post_webhook(client, {"event_id": "c", "call_sid": sid, "status": "RINGING"})
    assert late.json() == {"received": 1, "ignored": 1}  # out of order: never moves backwards
    after_end = await post_webhook(client, {"event_id": "d", "call_sid": sid, "status": "FAILED"})
    assert after_end.json() == {"received": 1, "ignored": 1}  # terminal is final
    body = (await client.get(f"/api/v1/calls/{call['id']}", headers=rep.headers)).json()
    assert body["status"] == "COMPLETED"
    assert body["duration_seconds"] == 42
    unknown = await post_webhook(client, {"event_id": "z", "call_sid": "nope", "status": "RINGING"})
    assert unknown.json() == {"received": 1, "unknown_call": 1}


async def test_end_call_via_provider(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep, agenda=False)
    ended = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert ended.status_code == 200
    assert ended.json()["status"] == "CANCELLED"  # never connected


# ---- Browser WebSocket ------------------------------------------------------------------------


def test_browser_websocket_auth_resume_and_disconnect() -> None:
    """Runs entirely inside Starlette's TestClient (its own event loop)."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app

    with TestClient(app, base_url="https://testserver") as tc:
        email = f"ws-{uuid.uuid4().hex[:8]}@example.com"
        reg = tc.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "correct-horse-battery",
                "full_name": "W",
                "company_name": "WS Co",
            },
        )
        h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        tc.patch("/api/v1/me", headers=h, json={"phone": "+919800000009"})
        contact = tc.post(
            "/api/v1/contacts", headers=h, json={"name": "C", "phone": "+919876500000"}
        ).json()
        call = tc.post("/api/v1/calls", headers=h, json={"contact_id": contact["id"]}).json()
        tc.post(f"/api/v1/calls/{call['id']}/start", headers=h)

        with tc.websocket_connect(f"/api/v1/calls/{call['id']}/live/ws") as ws:
            ws.send_json({"type": "auth", "token": "not-a-token"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401

        with tc.websocket_connect(f"/api/v1/calls/{call['id']}/live/ws") as ws:
            ws.send_json({"type": "auth", "token": h["Authorization"][7:], "last_seq": None})
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            epoch = hello["epoch"]
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
        # Browser gone. The call still runs end to end.
        assert tc.post(f"/api/v1/calls/{call['id']}/simulate", headers=h).status_code == 202
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = tc.get(f"/api/v1/calls/{call['id']}", headers=h).json()["status"]
            if status == "COMPLETED":
                break
            time.sleep(0.05)
        assert status == "COMPLETED"

        # Reconnect with the old seq: the missed events are replayed.
        with tc.websocket_connect(f"/api/v1/calls/{call['id']}/live/ws") as ws:
            ws.send_json(
                {"type": "auth", "token": h["Authorization"][7:], "last_seq": 0, "epoch": epoch}
            )
            assert ws.receive_json()["type"] == "hello"
            replayed = [ws.receive_json()["type"] for _ in range(5)]
            assert "call.status" in replayed or "transcript.final" in replayed

        # Wrong epoch (e.g. server restart) -> resync instruction.
        with tc.websocket_connect(f"/api/v1/calls/{call['id']}/live/ws") as ws:
            ws.send_json(
                {"type": "auth", "token": h["Authorization"][7:], "last_seq": 3, "epoch": "old"}
            )
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "resync"


# ---- Notes review ------------------------------------------------------------------------------


async def test_human_review_overrides_ai_notes(client: AsyncClient, rep: Account) -> None:
    call = await started_call(client, rep, agenda=False)
    await run_default_simulation(call)
    notes = (await client.get(f"/api/v1/calls/{call['id']}/notes", headers=rep.headers)).json()
    target = next(n for n in notes if n["kind"] == "CURRENT_SOLUTION")
    edited = await client.patch(
        f"/api/v1/calls/{call['id']}/notes/{target['id']}",
        headers=rep.headers,
        json={"text": "Uses Excel + Tally"},
    )
    assert edited.json()["status"] == "EDITED"
    assert edited.json()["text"] == "Uses Excel + Tally"

    objection = next(n for n in notes if n["kind"] == "OBJECTION")
    confirmed = await client.patch(
        f"/api/v1/calls/{call['id']}/notes/{objection['id']}", headers=rep.headers, json={}
    )
    assert confirmed.json()["status"] == "CONFIRMED"

    other = next(n for n in notes if n["kind"] == "TIMELINE")
    assert (
        await client.delete(f"/api/v1/calls/{call['id']}/notes/{other['id']}", headers=rep.headers)
    ).status_code == 204
    remaining = (await client.get(f"/api/v1/calls/{call['id']}/notes", headers=rep.headers)).json()
    assert other["id"] not in {n["id"] for n in remaining}  # hidden (REJECTED), not re-suggested

    manual = await client.post(
        f"/api/v1/calls/{call['id']}/notes",
        headers=rep.headers,
        json={"kind": "PREFERENCE", "text": "Prefers WhatsApp over email"},
    )
    assert manual.status_code == 201
    assert manual.json()["source"] == "MANUAL"
    assert manual.json()["status"] == "CONFIRMED"
    bad = await client.post(
        f"/api/v1/calls/{call['id']}/notes", headers=rep.headers, json={"kind": "NOPE", "text": "x"}
    )
    assert bad.status_code == 422


# ---- Retention ---------------------------------------------------------------------------------


async def test_retention_purges_only_expired_transcripts(
    client: AsyncClient, rep: Account, register: Register
) -> None:
    old_call = await started_call(client, rep, agenda=False)
    await run_default_simulation(old_call)
    other = await register(client, "other@example.com", "Other Co")
    await client.patch("/api/v1/me", headers=other.headers, json={"phone": "+919800000002"})
    fresh_call = await started_call(client, other, agenda=False)
    await run_default_simulation(fresh_call)

    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        await session.execute(
            update(TranscriptSegment).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()

    # A normal tenant session cannot see or delete other tenants' expired rows.
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        deleted = await session.execute(
            text("DELETE FROM transcript_segments WHERE expires_at < now()")
        )
        assert deleted.rowcount == 0  # type: ignore[attr-defined]
        await session.rollback()

    result = await purge_expired()
    assert result["transcript_segments"] == len(simulator.DEFAULT_SCRIPT)
    assert (await snapshot(client, rep, old_call["id"]))["transcript"] == []
    assert len((await snapshot(client, other, fresh_call["id"]))["transcript"]) == len(
        simulator.DEFAULT_SCRIPT
    )

    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        notes = (await session.scalars(select(CallNote))).all()
    assert notes  # structured notes are business records and remain
    assert all(n.source_segment_id is None for n in notes)


# ---- Tenant isolation --------------------------------------------------------------------------


async def test_live_data_is_tenant_isolated(
    client: AsyncClient, rep: Account, register: Register
) -> None:
    call = await started_call(client, rep)
    await run_default_simulation(call)
    other = await register(client, "attacker@example.com", "Attacker Co")
    notes = (await client.get(f"/api/v1/calls/{call['id']}/notes", headers=rep.headers)).json()
    insights = (await snapshot(client, rep, call["id"]))["insights"]
    cid = call["id"]
    for method, url, body in [
        ("GET", f"/api/v1/calls/{cid}/live", None),
        ("GET", f"/api/v1/calls/{cid}/transcript", None),
        ("GET", f"/api/v1/calls/{cid}/notes", None),
        ("POST", f"/api/v1/calls/{cid}/notes", {"kind": "GENERAL", "text": "x"}),
        ("PATCH", f"/api/v1/calls/{cid}/notes/{notes[0]['id']}", {"text": "x"}),
        ("DELETE", f"/api/v1/calls/{cid}/notes/{notes[0]['id']}", None),
        ("POST", f"/api/v1/calls/{cid}/insights/{insights[0]['id']}/dismiss", None),
        ("POST", f"/api/v1/calls/{cid}/start", None),
        ("POST", f"/api/v1/calls/{cid}/end", None),
        ("POST", f"/api/v1/calls/{cid}/simulate", None),
    ]:
        resp = await client.request(method, url, headers=other.headers, json=body)
        assert resp.status_code == 404, (method, url, resp.status_code)
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        assert (await session.scalars(select(TranscriptSegment))).all() == []
        assert (await session.scalars(select(CallNote))).all() == []
