"""Calendar integration via the mock provider: connect, confirm-only event creation, failure
handling, tenant isolation."""

import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.calendar.models import CalendarConnection
from app.calendar.provider import (
    CalendarAuthError,
    CalendarRateLimitError,
    MockCalendarProvider,
    set_calendar_provider,
)
from app.common.db import TenantContext
from tests.conftest import Account, open_session

Register = Callable[..., Awaitable[Account]]

EVENT = {
    "title": "Follow-up call with Ravi",
    "starts_at": "2030-01-10T10:00:00+05:30",
    "ends_at": "2030-01-10T10:30:00+05:30",
}


@pytest.fixture(autouse=True)
def provider() -> MockCalendarProvider:
    p = MockCalendarProvider()
    set_calendar_provider(p)
    return p


@pytest.fixture
async def owner(client: AsyncClient, register: Register) -> Account:
    return await register(client, "owner@example.com", "Acme")


async def connect(client: AsyncClient, acct: Account) -> None:
    start = await client.post("/api/v1/calendar/connect", headers=acct.headers)
    assert start.status_code == 200
    url = urllib.parse.urlsplit(start.json()["authorization_url"])
    params = dict(urllib.parse.parse_qsl(url.query))
    cb = await client.get(
        "/api/v1/calendar/oauth/callback", params={"code": params["code"], "state": params["state"]}
    )
    assert cb.status_code == 303
    assert cb.headers["location"].endswith("/calendar?connected=1")


@pytest.fixture
async def connected(client: AsyncClient, owner: Account) -> Account:
    await connect(client, owner)
    return owner


async def test_not_connected(client: AsyncClient, owner: Account) -> None:
    assert (await client.get("/api/v1/calendar/connection", headers=owner.headers)).json() == {
        "connected": False,
        "provider": None,
        "account_email": None,
        "status": None,
    }
    resp = await client.post(
        "/api/v1/calendar/events", headers=owner.headers, json={**EVENT, "confirm": True}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "calendar_not_connected"


async def test_connect_stores_encrypted_token(client: AsyncClient, connected: Account) -> None:
    info = (await client.get("/api/v1/calendar/connection", headers=connected.headers)).json()
    assert info["connected"] is True
    assert info["status"] == "ACTIVE"
    session = await open_session(TenantContext(company_id=connected.company_id))
    async with session:
        conn = await session.scalar(select(CalendarConnection))
    assert conn is not None
    assert conn.encrypted_refresh_token != "mock-refresh"
    assert "mock-refresh" not in conn.encrypted_refresh_token


async def test_forged_or_expired_state_rejected(client: AsyncClient, owner: Account) -> None:
    start = await client.post("/api/v1/calendar/connect", headers=owner.headers)
    state = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(start.json()["authorization_url"]).query)
    )["state"]
    tampered = state[:-4] + ("0000" if not state.endswith("0000") else "1111")
    cb = await client.get(
        "/api/v1/calendar/oauth/callback", params={"code": "mock-code", "state": tampered}
    )
    assert cb.headers["location"].endswith("error=invalid_state")
    denied = await client.get("/api/v1/calendar/oauth/callback", params={"error": "access_denied"})
    assert denied.headers["location"].endswith("error=denied")
    info = (await client.get("/api/v1/calendar/connection", headers=owner.headers)).json()
    assert info["connected"] is False


async def test_event_requires_explicit_confirmation(
    client: AsyncClient, connected: Account
) -> None:
    for body in (EVENT, {**EVENT, "confirm": False}):
        resp = await client.post("/api/v1/calendar/events", headers=connected.headers, json=body)
        assert resp.status_code == 422
    bad_times = {**EVENT, "ends_at": EVENT["starts_at"], "confirm": True}
    assert (
        await client.post("/api/v1/calendar/events", headers=connected.headers, json=bad_times)
    ).status_code == 422


async def test_create_event_links_timeline(
    client: AsyncClient, connected: Account, provider: MockCalendarProvider
) -> None:
    contact = (
        await client.post("/api/v1/contacts", headers=connected.headers, json={"name": "Ravi"})
    ).json()
    resp = await client.post(
        "/api/v1/calendar/events",
        headers=connected.headers,
        json={**EVENT, "contact_id": contact["id"], "confirm": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["external_event_id"] in provider.events
    tl = (
        await client.get(f"/api/v1/contacts/{contact['id']}/timeline", headers=connected.headers)
    ).json()
    assert tl["items"][0]["event_type"] == "CALENDAR_EVENT_CREATED"

    upcoming = await client.get(
        "/api/v1/calendar/upcoming", headers=connected.headers, params={"days": 60}
    )
    assert upcoming.status_code == 200
    # 2030 is outside the 60-day window, so nothing upcoming yet; listing app events works.
    events = await client.get(
        "/api/v1/calendar/events", headers=connected.headers, params={"contact_id": contact["id"]}
    )
    assert [e["title"] for e in events.json()] == ["Follow-up call with Ravi"]

    updated = await client.put(
        f"/api/v1/calendar/events/{resp.json()['id']}",
        headers=connected.headers,
        json={**EVENT, "title": "Moved", "confirm": True},
    )
    assert updated.json()["title"] == "Moved"


async def test_revoked_grant_requires_reconnect(
    client: AsyncClient, connected: Account, provider: MockCalendarProvider
) -> None:
    provider.fail_with = CalendarAuthError("invalid_grant")
    resp = await client.get("/api/v1/calendar/upcoming", headers=connected.headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "calendar_reauth_required"
    info = (await client.get("/api/v1/calendar/connection", headers=connected.headers)).json()
    assert info["status"] == "REAUTH_REQUIRED"
    provider.fail_with = None
    # Reconnecting restores it.
    await connect(client, connected)
    info = (await client.get("/api/v1/calendar/connection", headers=connected.headers)).json()
    assert info["status"] == "ACTIVE"


async def test_rate_limit_is_surfaced(
    client: AsyncClient, connected: Account, provider: MockCalendarProvider
) -> None:
    err = CalendarRateLimitError("slow down")
    err.retry_after = 42
    provider.fail_with = err
    resp = await client.get("/api/v1/calendar/upcoming", headers=connected.headers)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "42"


async def test_calendar_is_per_user_and_tenant(
    client: AsyncClient, connected: Account, register: Register
) -> None:
    other = await register(client, "other@example.com", "Other Co")
    info = (await client.get("/api/v1/calendar/connection", headers=other.headers)).json()
    assert info["connected"] is False
    await connect(client, other)
    b_contact = (
        await client.post("/api/v1/contacts", headers=other.headers, json={"name": "B customer"})
    ).json()
    cross: dict[str, Any] = {**EVENT, "contact_id": b_contact["id"], "confirm": True}
    resp = await client.post("/api/v1/calendar/events", headers=connected.headers, json=cross)
    assert resp.status_code == 422
    b_event = await client.post(
        "/api/v1/calendar/events", headers=other.headers, json={**EVENT, "confirm": True}
    )
    resp = await client.put(
        f"/api/v1/calendar/events/{b_event.json()['id']}",
        headers=connected.headers,
        json={**EVENT, "confirm": True},
    )
    assert resp.status_code == 404


async def test_disconnect(client: AsyncClient, connected: Account) -> None:
    assert (
        await client.delete("/api/v1/calendar/connection", headers=connected.headers)
    ).status_code == 204
    info = (await client.get("/api/v1/calendar/connection", headers=connected.headers)).json()
    assert info["connected"] is False
