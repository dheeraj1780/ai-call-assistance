"""Call lifecycle reliability and user-owned content (audit P0).

Covers: editing a planned call (and re-validating its target), failed-start recovery, idempotent
and concurrent Start, idempotent and bounded End (provider never confirms -> forced local end),
the restart-safe stale-call reaper, browser reconnect/resume, text retention vs audio
non-persistence, transcript and summary editing, knowledge lookup on unattributed (mixed) speech,
the Plivo connect lifecycle (no ACTIVE before the customer answered, stream started only after the
answer, queued cancellation, callback idempotency, media normalisation), tenant isolation of the
new endpoints, and core operation with no optional provider configured.
"""

import asyncio
import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, inspect, select, text, update

from app.calls.models import Call
from app.common.config import get_settings
from app.common.db import TenantContext
from app.integrations.providers import factory
from app.integrations.providers.plivo import compute_signature_v3
from app.jobs.models import Job
from app.knowledge.models import KnowledgeChunk, KnowledgeDocument
from app.live import session as live
from app.live.models import TranscriptSegment
from app.postcall.models import CallSummary
from app.telephony import recovery
from app.telephony import service as telephony
from app.telephony.models import CallRoute
from app.telephony.provider import (
    MockTelephonyProvider,
    TelephonyError,
    get_telephony_provider,
    media_parser_for,
)
from app.telephony.service import MediaIngest, MediaStreamEnded
from app.telephony.simulator import _wait_for_segment
from tests.conftest import Account, open_session
from tests.integration_helpers import (
    PLIVO_VALUES,
    PUBLIC,
    FakeProvider,
    configure,
    integration_id,
    validate_and_enable,
)

Register = Callable[..., Awaitable[Account]]
TEAMS_LINK = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context="
    "%7b%22Tid%22%3a%2200000000-0000-0000-0000-000000000001%22%2c%22Oid%22%3a"
    "%2200000000-0000-0000-0000-000000000002%22%7d"
)
TEAMS_SHORT_LINK = "https://teams.microsoft.com/meet/123456789?p=abcdef"


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme Traders")
    r = await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    assert r.status_code == 200
    return acct


async def contact(client: AsyncClient, acct: Account, phone: str = "+919876543210") -> str:
    r = await client.post(
        "/api/v1/contacts", headers=acct.headers, json={"name": "Ravi Kumar", "phone": phone}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def planned(client: AsyncClient, acct: Account, **extra: Any) -> dict[str, Any]:
    cid = await contact(client, acct)
    r = await client.post(
        "/api/v1/calls",
        headers=acct.headers,
        json={"contact_id": cid, "objective": "Demo", **extra},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def get(client: AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.get(f"/api/v1/calls/{call_id}", headers=acct.headers)
    ).json()
    return body


async def teams_enabled(client: AsyncClient, acct: Account) -> None:
    await configure(client, acct, "microsoft-teams", {"call_persistence": "TRANSIENT"}, mode="MOCK")
    await validate_and_enable(client, acct, "microsoft-teams", "REAL_TIME_CALL")


# ---- 1. Editable planned call -------------------------------------------------------------------


async def test_planned_call_details_are_editable_and_target_is_revalidated(
    client: AsyncClient, rep: Account
) -> None:
    call = await planned(client, rep, channel="TEAMS", meeting_url=TEAMS_LINK)
    url = f"/api/v1/calls/{call['id']}"
    # A link the Teams gateway cannot join is refused while the call is still editable.
    bad = await client.patch(url, headers=rep.headers, json={"meeting_url": TEAMS_SHORT_LINK})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_meeting_url"
    assert "meetup-join" in bad.json()["error"]["message"]
    wrong = await client.patch(url, headers=rep.headers, json={"meeting_url": "https://x.io/a"})
    assert wrong.status_code == 422
    new_link = TEAMS_LINK.replace("meeting_abc", "meeting_xyz")
    ok = await client.patch(
        url,
        headers=rep.headers,
        json={
            "meeting_url": new_link,
            "language": "hi-IN",
            "objective": "New objective",
            "desired_outcome": "Demo booked",
            "scheduled_at": "2026-10-02T10:00:00+05:30",
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["meeting_url"] == new_link
    assert body["language"] == "hi-IN"
    assert body["objective"] == "New objective"
    # The channel is never editable (plan a new call instead).
    assert (
        await client.patch(url, headers=rep.headers, json={"channel": "PHONE"})
    ).status_code == 422


async def test_meet_link_is_normalised_on_edit_and_phone_rejects_a_link(
    client: AsyncClient, rep: Account
) -> None:
    meet = await planned(client, rep, channel="GOOGLE_MEET", meeting_url="abc-defg-hij")
    r = await client.patch(
        f"/api/v1/calls/{meet['id']}", headers=rep.headers, json={"meeting_url": "xyz-abcd-efg"}
    )
    assert r.status_code == 200
    assert r.json()["meeting_url"] == "https://meet.google.com/xyz-abcd-efg"
    phone = await planned(client, rep)
    r = await client.patch(
        f"/api/v1/calls/{phone['id']}",
        headers=rep.headers,
        json={"meeting_url": "https://meet.google.com/xyz-abcd-efg"},
    )
    assert r.status_code == 422


async def test_target_and_language_are_fixed_once_the_call_started(
    client: AsyncClient, rep: Account
) -> None:
    call = await planned(client, rep)
    assert (
        await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    ).status_code == 200
    for body in ({"language": "de-DE"}, {"objective": "changed"}, {"meeting_url": None}):
        r = await client.patch(f"/api/v1/calls/{call['id']}", headers=rep.headers, json=body)
        assert r.status_code == 409, body
        assert r.json()["error"]["code"] == "call_already_started"
    # A provider-placed call is never ended by hand-setting its status.
    r = await client.patch(
        f"/api/v1/calls/{call['id']}", headers=rep.headers, json={"status": "CANCELLED"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "use_end_call"


# ---- 2. Failed-start recovery -----------------------------------------------------------------


async def test_failed_start_can_be_corrected_and_retried(client: AsyncClient, rep: Account) -> None:
    await teams_enabled(client, rep)
    call = await planned(client, rep, channel="TEAMS", meeting_url=TEAMS_LINK)
    factory.mock_teams_gateway().fail_join = True
    r = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert r.status_code == 502
    failed = await get(client, rep, call["id"])
    assert failed["status"] == "FAILED"
    assert failed["telephony_error"]
    # A failed call cannot be edited or started directly...
    assert (
        await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    ).status_code == 409
    # ...but it can be re-opened, corrected and started again.
    reopened = await client.post(f"/api/v1/calls/{call['id']}/reopen", headers=rep.headers)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "PLANNED"
    assert reopened.json()["telephony_error"] is None
    assert reopened.json()["provider"] is None
    again = await client.post(f"/api/v1/calls/{call['id']}/reopen", headers=rep.headers)
    assert again.status_code == 200  # idempotent
    new_link = TEAMS_LINK.replace("meeting_abc", "meeting_fixed")
    r = await client.patch(
        f"/api/v1/calls/{call['id']}", headers=rep.headers, json={"meeting_url": new_link}
    )
    assert r.status_code == 200
    factory.mock_teams_gateway().fail_join = False
    started = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "INITIATED"
    assert factory.mock_teams_gateway().joined[-1]["join_url"] == new_link


async def test_a_call_that_connected_cannot_be_reopened(client: AsyncClient, rep: Account) -> None:
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _connect_mock(call["id"])
    await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    r = await client.post(f"/api/v1/calls/{call['id']}/reopen", headers=rep.headers)
    assert r.status_code == 409


async def test_provider_crash_during_start_fails_the_call_instead_of_leaving_it_initiated(
    client: AsyncClient, rep: Account
) -> None:
    class Boom(MockTelephonyProvider):
        async def create_call(self, request: Any) -> str:
            raise RuntimeError("unexpected SDK bug")

    from app.telephony.provider import set_telephony_provider

    set_telephony_provider(Boom())
    call = await planned(client, rep)
    r = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert r.status_code == 502
    assert (await get(client, rep, call["id"]))["status"] == "FAILED"


# ---- 5. Idempotent / concurrent Start -----------------------------------------------------------


async def test_concurrent_starts_place_exactly_one_provider_call(
    client: AsyncClient, rep: Account, make_client: Any
) -> None:
    call = await planned(client, rep)
    provider = get_telephony_provider()
    assert isinstance(provider, MockTelephonyProvider)

    async def start() -> httpx.Response:
        async with make_client() as c:
            resp: httpx.Response = await c.post(
                f"/api/v1/calls/{call['id']}/start", headers=rep.headers
            )
            return resp

    results = await asyncio.gather(*(start() for _ in range(5)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len(provider.created) == 1
    assert {r.json()["id"] for r in results} == {call["id"]}


# ---- 3. Idempotent, bounded End ------------------------------------------------------------------


class SilentProvider(MockTelephonyProvider):
    """A real-provider stand-in: accepts the hang-up but never sends the final callback."""

    emits_end_events = False


class UnreachableProvider(SilentProvider):
    async def end_call(self, provider_call_id: str) -> None:
        raise TelephonyError("provider down")


async def _connect_mock(call_id: str) -> None:
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call_id))
    assert route is not None
    assert route.provider_call_id
    from app.telephony.provider import ProviderCallState, TelephonyEvent

    for state in (ProviderCallState.RINGING, ProviderCallState.CONNECTED):
        await telephony.process_event(
            route.provider,
            TelephonyEvent(
                f"t-{call_id}-{state}", route.provider_call_id, state, datetime.now(UTC)
            ),
        )


@pytest.mark.parametrize("provider_cls", [SilentProvider, UnreachableProvider])
async def test_end_completes_locally_when_the_provider_never_confirms(
    client: AsyncClient, rep: Account, provider_cls: type[MockTelephonyProvider]
) -> None:
    from app.telephony.provider import set_telephony_provider

    provider = provider_cls()
    set_telephony_provider(provider)
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _connect_mock(call["id"])
    r = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert r.status_code == 200, r.text
    ended = r.json()
    assert ended["status"] == "COMPLETED"  # it connected: completed, not cancelled
    assert ended["end_requested_at"]
    assert ended["ended_at"]
    await telephony.wait_finalized(uuid.UUID(call["id"]))
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        row = await session.get(Call, uuid.UUID(call["id"]))
        jobs = (await session.scalars(select(Job.kind))).all()
    assert row is not None
    assert row.finalized_at is not None
    assert "postcall.process" in jobs
    assert live.get_session(uuid.UUID(call["id"])) is None


async def test_end_is_idempotent_and_hangs_up_once(client: AsyncClient, rep: Account) -> None:
    from app.telephony.provider import set_telephony_provider

    provider = SilentProvider()
    set_telephony_provider(provider)
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _connect_mock(call["id"])
    results = await asyncio.gather(
        *(client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers) for _ in range(4))
    )
    assert all(r.status_code == 200 for r in results)
    assert {r.json()["status"] for r in results} == {"COMPLETED"}
    assert len(provider.ended) == 1  # one hang-up request, however many clicks
    again = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert again.status_code == 200
    assert again.json()["status"] == "COMPLETED"
    assert len(provider.ended) == 1


async def test_end_before_connect_cancels(client: AsyncClient, rep: Account) -> None:
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    r = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert r.json()["status"] == "CANCELLED"
    assert (
        await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    ).status_code == 200


async def test_provider_events_after_end_cannot_revive_the_call(
    client: AsyncClient, rep: Account
) -> None:
    from app.telephony.provider import ProviderCallState, TelephonyEvent, set_telephony_provider

    set_telephony_provider(SilentProvider())
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _connect_mock(call["id"])
    await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    outcome = await telephony.process_event(
        "mock",
        TelephonyEvent(
            "late-active", route.provider_call_id or "", ProviderCallState.ACTIVE, datetime.now(UTC)
        ),
    )
    assert outcome == "ignored"
    assert (await get(client, rep, call["id"]))["status"] == "COMPLETED"


# ---- 4. Stale-call recovery (restart-safe) -------------------------------------------------------


async def _age(call_id: str, company_id: uuid.UUID, **values: Any) -> None:
    session = await open_session(TenantContext(company_id=company_id))
    async with session:
        await session.execute(update(Call).where(Call.id == uuid.UUID(call_id)).values(**values))
        await session.commit()


async def test_reaper_recovers_stuck_calls_and_is_idempotent(
    client: AsyncClient, rep: Account, register: Register, make_client: Any
) -> None:
    from app.telephony.provider import set_telephony_provider

    set_telephony_provider(SilentProvider())
    old = datetime.now(UTC) - timedelta(days=1)
    # (a) crash window: INITIATED with no provider id (the server died before create_call).
    a = await planned(client, rep)
    await client.post(f"/api/v1/calls/{a['id']}/start", headers=rep.headers)
    await _age(a["id"], rep.company_id, provider_call_id=None, updated_at=old)
    # (b) ACTIVE call with no media/events for a day (provider callback lost).
    b = await planned(client, rep)
    await client.post(f"/api/v1/calls/{b['id']}/start", headers=rep.headers)
    await _connect_mock(b["id"])
    await _age(b["id"], rep.company_id, last_activity_at=old, updated_at=old)
    # (c) ENDING for longer than the grace period (the server restarted during End).
    c = await planned(client, rep)
    await client.post(f"/api/v1/calls/{c['id']}/start", headers=rep.headers)
    await _connect_mock(c["id"])
    await _age(c["id"], rep.company_id, status="ENDING", end_requested_at=old)
    # (d) terminal but end-of-call processing never recorded (restart mid-finalisation).
    d = await planned(client, rep)
    await client.post(f"/api/v1/calls/{d['id']}/start", headers=rep.headers)
    await _connect_mock(d["id"])
    await client.post(f"/api/v1/calls/{d['id']}/end", headers=rep.headers)
    await telephony.wait_finalized(uuid.UUID(d["id"]))
    await _age(
        d["id"],
        rep.company_id,
        finalized_at=None,
        updated_at=old,
        started_at=old - timedelta(minutes=5),
        ended_at=old,
    )
    # (e) a healthy live call in ANOTHER tenant must not be touched.
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        await c2.patch("/api/v1/me", headers=other.headers, json={"phone": "+919800000002"})
        e = await planned(c2, other)
        await c2.post(f"/api/v1/calls/{e['id']}/start", headers=other.headers)
        await _connect_mock(e["id"])

    counts = await recovery.recover_calls()
    assert counts == {"fail_start": 1, "end_inactive": 1, "end_timeout": 1, "finalize": 1}
    assert (await get(client, rep, a["id"]))["status"] == "FAILED"
    assert (await get(client, rep, a["id"]))["telephony_error"] == "start_interrupted"
    assert (await get(client, rep, b["id"]))["status"] == "COMPLETED"
    # The reason is kept so the UI can explain why the call was closed automatically.
    assert (await get(client, rep, b["id"]))["telephony_error"] == "inactivity_timeout"
    assert (await get(client, rep, c["id"]))["status"] == "COMPLETED"
    async with make_client() as c2:
        from tests.conftest import DEFAULT_PASSWORD

        login = await c2.post(
            "/api/v1/auth/login", json={"email": "b@example.com", "password": DEFAULT_PASSWORD}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (await c2.get(f"/api/v1/calls/{e['id']}", headers=headers)).json()[
            "status"
        ] == "CONNECTED"
    await telephony.wait_all_finalized()
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        unfinished = await session.scalar(
            select(func.count()).select_from(Call).where(Call.finalized_at.is_(None))
        )
    assert unfinished == 0
    # Running it again does nothing.
    assert await recovery.recover_calls() == {}


async def test_reaper_never_sees_calls_without_its_system_task(rep: Account) -> None:
    """The cross-tenant read used by the sweep only exists inside its declared system task."""
    session = await open_session()
    async with session:
        assert await session.scalar(select(func.count()).select_from(Call)) == 0
        await session.execute(text("SELECT set_config('app.system_task', 'call_recovery', true)"))
        # Still read-only: the policy grants SELECT, never UPDATE/DELETE.
        await session.execute(update(Call).values(objective="x"))
        await session.rollback()


# ---- 6. Browser reconnect / resume ------------------------------------------------------------


async def test_reconnect_resumes_the_same_call_and_sees_an_end_that_happened_meanwhile(
    client: AsyncClient, rep: Account
) -> None:
    from starlette.testclient import TestClient

    from app.main import app
    from app.telephony import simulator

    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    await simulator.run(uuid.UUID(call["id"]), route.provider_call_id or "", delay=0)
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    with TestClient(app) as tc, tc.websocket_connect(f"/api/v1/calls/{call['id']}/live/ws") as ws:
        ws.send_json(
            {"type": "auth", "token": rep.access_token, "last_seq": 1, "epoch": snap["epoch"]}
        )
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["status"] == "COMPLETED"  # the client learns the call ended while away
    # Starting again (e.g. a replayed request) never creates a new call.
    r = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert r.status_code == 409
    calls = (await client.get("/api/v1/calls", headers=rep.headers)).json()
    assert calls["total"] == 1


# ---- 7/8. Text retention vs audio ---------------------------------------------------------------


async def test_live_only_call_keeps_text_in_memory_for_the_call_and_never_stores_audio(
    client: AsyncClient, rep: Account
) -> None:
    from app.integrations import simulator as integration_simulator

    await teams_enabled(client, rep)
    call = await planned(client, rep, channel="TEAMS", meeting_url=TEAMS_LINK)
    started = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert started.json()["transcript_persistence"] == "TRANSIENT"
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    script = [
        ("customer", "Right now we use Excel for stock and it takes too long every week."),
        ("customer", "Do you offer a warranty on the scanners?"),
    ]
    await integration_simulator.run_teams_call(
        uuid.UUID(call["id"]),
        route.provider_call_id or "",
        persistence="TRANSIENT",
        script=script,
        complete=False,
    )
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    # Reload mid-call: the server resumes the live-only transcript and notes from memory.
    assert [s["text"] for s in snap["transcript"]] == [t for _, t in script]
    assert snap["notes"], "live-only notes must survive a refresh during the call"
    assert snap["audio_stored"] is False
    note = snap["notes"][0]
    edited = await client.patch(
        f"/api/v1/calls/{call['id']}/notes/{note['id']}",
        headers=rep.headers,
        json={"text": "Uses Excel for stock (weekly)"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "EDITED"
    seg = snap["transcript"][0]
    fixed = await client.patch(
        f"/api/v1/calls/{call['id']}/transcript/{seg['id']}",
        headers=rep.headers,
        json={"text": "Right now we use Excel for stock."},
    )
    assert fixed.status_code == 200
    assert fixed.json()["original_text"] == seg["text"]
    # Nothing derived from the media reached the database; the call record itself remains.
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        assert await session.scalar(select(func.count()).select_from(TranscriptSegment)) == 0
    await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    ended = await get(client, rep, call["id"])
    assert ended["status"] == "COMPLETED"


async def test_no_table_can_hold_raw_audio() -> None:
    """RAW AUDIO IS NEVER STORED: no column anywhere is a binary type."""
    session = await open_session()
    async with session:
        conn = await session.connection()
        binary = await conn.run_sync(
            lambda sync: [
                (table, col["name"])
                for table in inspect(sync).get_table_names()
                for col in inspect(sync).get_columns(table)
                if "BYTEA" in str(col["type"]).upper() or "BLOB" in str(col["type"]).upper()
            ]
        )
    assert binary == []


async def test_phone_call_text_is_retained_with_an_expiry(
    client: AsyncClient, rep: Account
) -> None:
    from app.telephony import simulator

    call = await planned(client, rep)
    started = await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    assert started.json()["transcript_persistence"] == "PERSISTED"
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    await simulator.run(uuid.UUID(call["id"]), route.provider_call_id or "", delay=0)
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        rows = (await session.scalars(select(TranscriptSegment))).all()
    assert rows
    assert all(r.expires_at > datetime.now(UTC) for r in rows)


# ---- 9. Transcript and summary editing -----------------------------------------------------------


async def _completed_call(client: AsyncClient, acct: Account) -> dict[str, Any]:
    from app.jobs.service import drain
    from app.telephony import simulator

    call = await planned(client, acct)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=acct.headers)
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    await simulator.run(uuid.UUID(call["id"]), route.provider_call_id or "", delay=0)
    await telephony.wait_finalized(uuid.UUID(call["id"]))
    await drain()
    return call


async def test_transcript_edit_keeps_the_original_and_is_audited(
    client: AsyncClient, rep: Account, register: Register, make_client: Any
) -> None:
    call = await _completed_call(client, rep)
    transcript = (
        await client.get(f"/api/v1/calls/{call['id']}/transcript", headers=rep.headers)
    ).json()
    seg = transcript[1]
    url = f"/api/v1/calls/{call['id']}/transcript/{seg['id']}"
    r = await client.patch(url, headers=rep.headers, json={"text": "Corrected wording."})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "Corrected wording."
    assert r.json()["edited"] is True
    assert r.json()["original_text"] == seg["text"]
    r2 = await client.patch(url, headers=rep.headers, json={"text": "Corrected again."})
    assert r2.json()["original_text"] == seg["text"]  # the machine output is kept once
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        actions = (
            await session.execute(
                text("SELECT action FROM audit_logs WHERE action = 'transcript.edited'")
            )
        ).all()
    assert len(actions) == 2
    assert (await client.patch(url, headers=rep.headers, json={"text": ""})).status_code == 422
    # Another tenant cannot see or edit it.
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        r = await c2.patch(url, headers=other.headers, json={"text": "hijack"})
        assert r.status_code == 404


async def test_summary_is_editable_and_marked_as_edited(
    client: AsyncClient, rep: Account, register: Register, make_client: Any
) -> None:
    call = await _completed_call(client, rep)
    post = (await client.get(f"/api/v1/calls/{call['id']}/post-call", headers=rep.headers)).json()
    assert post["summary"]["status"] == "READY"
    original = post["summary"]["summary"]
    r = await client.patch(
        f"/api/v1/calls/{call['id']}/post-call/summary",
        headers=rep.headers,
        json={"summary": "My own summary.", "budget": "Rs 2 lakh", "timeline": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == "My own summary."
    assert body["budget"] == {"value": "Rs 2 lakh", "status": "EDITED"}
    assert body["timeline"] == {"value": None, "status": "NOT_DISCUSSED"}
    assert body["edited_at"]
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        row = await session.scalar(select(CallSummary))
    assert row is not None
    assert row.original_summary == original
    # AI reprocessing can never overwrite the person's version.
    retry = await client.post(f"/api/v1/calls/{call['id']}/post-call/retry", headers=rep.headers)
    assert retry.status_code == 409
    bad = await client.patch(
        f"/api/v1/calls/{call['id']}/post-call/summary",
        headers=rep.headers,
        json={"suggested_outcome": "MAYBE"},
    )
    assert bad.status_code == 409
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        r = await c2.patch(
            f"/api/v1/calls/{call['id']}/post-call/summary",
            headers=other.headers,
            json={"summary": "hijack"},
        )
        assert r.status_code == 404


# ---- 10. Mixed (UNKNOWN speaker) audio -----------------------------------------------------------


async def test_unknown_speaker_questions_trigger_company_knowledge_lookup(
    client: AsyncClient, rep: Account
) -> None:
    from app.integrations import simulator as integration_simulator
    from app.jobs.service import drain

    up = await client.post(
        "/api/v1/knowledge/documents",
        headers=rep.headers,
        files={
            "file": (
                "warranty.txt",
                b"All barcode scanners come with a two year replacement warranty.",
                "text/plain",
            )
        },
    )
    assert up.status_code == 201, up.text
    await drain()
    await configure(
        client, rep, "microsoft-teams", {"call_persistence": "RECORDING_DECLARED"}, mode="MOCK"
    )
    await validate_and_enable(client, rep, "microsoft-teams", "REAL_TIME_CALL")
    call = await planned(client, rep, channel="TEAMS", meeting_url=TEAMS_LINK)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call["id"]))
    assert route is not None
    # "mixed": no salesperson id -> speaker UNKNOWN (never guessed)
    await integration_simulator.run_teams_call(
        uuid.UUID(call["id"]),
        route.provider_call_id or "",
        persistence="RECORDING_DECLARED",
        script=[("mixed", "What warranty do the barcode scanners come with?")],
        complete=False,
    )
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["transcript"][0]["speaker"] == "UNKNOWN"
    knowledge = [i for i in snap["insights"] if i["type"] == "KNOWLEDGE_RESULT"]
    assert knowledge, "knowledge lookup must run for unattributed speech"
    assert "two year" in knowledge[0]["content"]
    assert "warranty.txt"[:-4] in (knowledge[0]["context"] or "")
    assert "possible customer question" in (knowledge[0]["context"] or "")


# ---- 15-17. Plivo lifecycle ----------------------------------------------------------------------


@pytest.fixture
def public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "public_base_url", PUBLIC)
    # Real-time audio can only be enabled with a real streaming STT configured on the server
    # (the test process still uses the mock STT instance installed by conftest).
    monkeypatch.setattr(get_settings(), "stt_provider", "google")


@pytest.fixture
async def plivo(client: AsyncClient, rep: Account, public_https: None) -> dict[str, Any]:
    fake = FakeProvider().install()
    fake.on("GET", "/Account/MAXXXXXXXXXXXXXXXXXX/", httpx.Response(200, json={"name": "Acme"}))
    fake.on("GET", "/Number/918000000000/", httpx.Response(200, json={}))
    fake.on("POST", "/Stream/", httpx.Response(201, json={"stream_id": "st-1"}))
    fake.on(
        "POST", "/Call/", httpx.Response(201, json={"message": "fired", "request_uuid": "req-1"})
    )
    fake.on("DELETE", "/Request/", httpx.Response(204))
    fake.on("DELETE", "/Call/", httpx.Response(404))  # not answered yet -> cancel the request
    await configure(client, rep, "plivo", PLIVO_VALUES)
    await validate_and_enable(client, rep, "plivo", "PHONE_CALL")
    detail = await validate_and_enable(client, rep, "plivo", "MEDIA_STREAM")
    return {"fake": fake, "id": integration_id(detail)}


async def plivo_post(client: AsyncClient, path: str, params: dict[str, str]) -> httpx.Response:
    sig = compute_signature_v3(PLIVO_VALUES["auth_token"], f"{PUBLIC}{path}", "n-1", "POST", params)
    return await client.post(
        path,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Plivo-Signature-V3": sig,
            "X-Plivo-Signature-V3-Nonce": "n-1",
        },
    )


async def test_plivo_call_is_not_active_before_the_customer_answers(
    client: AsyncClient, rep: Account, plivo: dict[str, Any]
) -> None:
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    base = f"/api/v1/integrations/plivo/webhooks/{plivo['id']}"
    answer = await plivo_post(client, f"{base}/answer?cid={call['id']}", {"CallUUID": "cu-1"})
    # The answer XML only bridges; no XML <Stream> (would block <Dial> / stream ringback).
    assert "<Dial" in answer.text
    assert "<Stream" not in answer.text
    assert not plivo["fake"].calls("POST", "/Stream/")
    # Early media on the salesperson's leg (ringback) is NOT call audio: dropped, status unchanged.
    ingest = MediaIngest(uuid.UUID(call["id"]), require_connected=True)
    parse = media_parser_for("plivo")
    start = {
        "event": "start",
        "start": {"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}},
    }
    await ingest.handle(parse(json.dumps(start)))
    await ingest.handle(
        parse(
            json.dumps(
                {
                    "event": "media",
                    "media": {
                        "track": "outbound",
                        "payload": base64.b64encode(b"ring ring").decode(),
                    },
                }
            )
        )
    )
    assert (await get(client, rep, call["id"]))["status"] == "RINGING"
    assert live.get_session(uuid.UUID(call["id"])) is None
    # The customer answers: CONNECTED, and only now is the audio stream requested from Plivo.
    params = {"DialALegUUID": "cu-1", "DialAction": "answer", "DialBLegUUID": "b-1"}
    await plivo_post(client, f"{base}/dial?cid={call['id']}&leg=agent", params)
    assert (await get(client, rep, call["id"]))["status"] == "CONNECTED"
    [stream] = plivo["fake"].calls("POST", "/Stream/")
    assert "/Call/cu-1/Stream/" in str(stream.url)
    body = json.loads(stream.content)
    assert body["audio_track"] == "both"
    assert body["bidirectional"] is False
    assert f"/telephony/media/plivo/{call['id']}" in body["service_url"]
    # Duplicate callback delivery: applied once, no second stream.
    await plivo_post(client, f"{base}/dial?cid={call['id']}&leg=agent", params)
    assert len(plivo["fake"].calls("POST", "/Stream/")) == 1
    # Now audio is call audio: ACTIVE, transcribed.
    await ingest.handle(
        parse(
            json.dumps(
                {
                    "event": "media",
                    "media": {
                        "track": "outbound",
                        "payload": base64.b64encode(b"We use Excel today").decode(),
                    },
                }
            )
        )
    )
    await _wait_for_segment(uuid.UUID(call["id"]), 1)
    got = await get(client, rep, call["id"])
    assert got["status"] == "ACTIVE"


async def test_plivo_cancel_while_ringing_leaves_no_orphan_and_no_stream(
    client: AsyncClient, rep: Account, plivo: dict[str, Any]
) -> None:
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    # End while Plivo is still ringing the salesperson (only the request uuid exists).
    r = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert r.json()["status"] == "CANCELLED"
    assert plivo["fake"].calls("DELETE", "/Call/req-1/")
    assert plivo["fake"].calls("DELETE", "/Request/req-1/")
    # A late answer for the cancelled call is refused (hang up), a late connect starts nothing.
    base = f"/api/v1/integrations/plivo/webhooks/{plivo['id']}"
    answer = await plivo_post(client, f"{base}/answer?cid={call['id']}", {"CallUUID": "cu-9"})
    assert "<Hangup/>" in answer.text
    await plivo_post(
        client,
        f"{base}/dial?cid={call['id']}",
        {"DialALegUUID": "cu-9", "DialAction": "answer"},
    )
    assert not plivo["fake"].calls("POST", "/Stream/")
    assert (await get(client, rep, call["id"]))["status"] == "CANCELLED"


async def test_plivo_media_socket_closes_once_the_call_is_ending(
    client: AsyncClient, rep: Account, plivo: dict[str, Any]
) -> None:
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _age(call["id"], rep.company_id, status="ENDING")
    ingest = MediaIngest(uuid.UUID(call["id"]), require_connected=True)
    with pytest.raises(MediaStreamEnded):
        await ingest.handle(
            media_parser_for("plivo")(
                json.dumps({"event": "media", "media": {"track": "inbound", "payload": ""}})
            )
        )


async def test_plivo_stream_is_not_started_without_the_media_capability(
    client: AsyncClient, rep: Account, public_https: None
) -> None:
    fake = FakeProvider().install()
    fake.on("GET", "/Account/MAXXXXXXXXXXXXXXXXXX/", httpx.Response(200, json={"name": "Acme"}))
    fake.on("GET", "/Number/918000000000/", httpx.Response(200, json={}))
    fake.on("POST", "/Call/", httpx.Response(201, json={"request_uuid": "req-2"}))
    await configure(client, rep, "plivo", PLIVO_VALUES)
    detail = await validate_and_enable(client, rep, "plivo", "PHONE_CALL")
    call = await planned(client, rep)
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    base = f"/api/v1/integrations/plivo/webhooks/{integration_id(detail)}"
    await plivo_post(
        client, f"{base}/dial?cid={call['id']}", {"DialALegUUID": "cu-2", "DialAction": "answer"}
    )
    assert not fake.calls("POST", "/Stream/")
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["pipeline"]["media"] == "unavailable"
    assert snap["pipeline"]["media_reason"] == "media_stream_disabled"


# ---- 21. Core operation without optional providers -----------------------------------------------


async def test_core_product_works_with_no_optional_provider_configured(
    client: AsyncClient, rep: Account
) -> None:
    """No Microsoft 365, Google or WhatsApp integration: plan, edit, start, end, history."""
    cid = await contact(client, rep)
    options = (
        await client.get(f"/api/v1/contacts/{cid}/communication", headers=rep.headers)
    ).json()
    by_key = {o["key"]: o for o in options}
    assert by_key["phone_call"]["available"] is True
    assert by_key["teams_call"]["available"] is False
    assert by_key["google_meet_call"]["available"] is False
    call = await planned(client, rep)
    await client.patch(
        f"/api/v1/calls/{call['id']}", headers=rep.headers, json={"language": "hi-IN"}
    )
    await client.post(f"/api/v1/calls/{call['id']}/start", headers=rep.headers)
    await _connect_mock(call["id"])
    r = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert r.json()["status"] == "COMPLETED"
    history = (await client.get("/api/v1/calls", headers=rep.headers)).json()
    assert history["total"] == 1
    # A Teams call without the Teams integration is refused cleanly - nothing else is affected.
    t = await planned(client, rep, channel="TEAMS", meeting_url=TEAMS_LINK)
    r = await client.post(f"/api/v1/calls/{t['id']}/start", headers=rep.headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "integration_not_ready"
    assert (await get(client, rep, t["id"]))["status"] == "PLANNED"


# ---- 32. Tenant isolation of the new endpoints ---------------------------------------------------


async def test_lifecycle_endpoints_are_tenant_isolated(
    client: AsyncClient, rep: Account, register: Register, make_client: Any
) -> None:
    call = await planned(client, rep)
    async with make_client() as c2:
        other = await register(c2, "b@example.com", "Beta")
        for method, path, body in (
            ("PATCH", f"/api/v1/calls/{call['id']}", {"language": "de-DE"}),
            ("POST", f"/api/v1/calls/{call['id']}/start", None),
            ("POST", f"/api/v1/calls/{call['id']}/end", None),
            ("POST", f"/api/v1/calls/{call['id']}/reopen", None),
        ):
            r = await c2.request(method, path, headers=other.headers, json=body)
            assert r.status_code == 404, (method, path, r.status_code)
    assert (await get(client, rep, call["id"]))["status"] == "PLANNED"


# ---- 40. Knowledge duplicate upload --------------------------------------------------------------


async def test_concurrent_duplicate_knowledge_uploads_create_one_document(
    client: AsyncClient, rep: Account, make_client: Any
) -> None:
    payload = b"Our delivery time is 3 working days within Pune."

    async def upload() -> httpx.Response:
        async with make_client() as c:
            resp: httpx.Response = await c.post(
                "/api/v1/knowledge/documents",
                headers=rep.headers,
                files={"file": ("delivery.txt", payload, "text/plain")},
            )
            return resp

    results = await asyncio.gather(*(upload() for _ in range(4)))
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409]
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1
        chunks = await session.scalar(select(func.count()).select_from(KnowledgeChunk))
    assert chunks == 1
