"""Cross-tenant attacks on CRM resources: API layer and database (RLS) layer."""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.action_items.models import ActionItem
from app.calls.models import Call
from app.common.db import TenantContext
from app.contacts.models import Contact, ContactNote
from app.timeline.models import TimelineEvent
from tests.conftest import Account, open_session

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def world(client: AsyncClient, register: Register) -> dict[str, object]:
    a = await register(client, "a@example.com", "Company A")
    b = await register(client, "b@example.com", "Company B")
    contact = (
        await client.post("/api/v1/contacts", headers=b.headers, json={"name": "B's customer"})
    ).json()
    note = (
        await client.post(
            f"/api/v1/contacts/{contact['id']}/notes", headers=b.headers, json={"body": "secret"}
        )
    ).json()
    call = (
        await client.post("/api/v1/calls", headers=b.headers, json={"contact_id": contact["id"]})
    ).json()
    item = (
        await client.post(
            "/api/v1/action-items",
            headers=b.headers,
            json={"title": "B task", "contact_id": contact["id"]},
        )
    ).json()
    return {"a": a, "b": b, "contact": contact, "note": note, "call": call, "item": item}


async def test_api_cannot_read_or_modify_other_tenant(
    client: AsyncClient, world: dict[str, object]
) -> None:
    a = world["a"]
    assert isinstance(a, Account)
    contact_id = world["contact"]["id"]  # type: ignore[index]
    note_id = world["note"]["id"]  # type: ignore[index]
    call_id = world["call"]["id"]  # type: ignore[index]
    item_id = world["item"]["id"]  # type: ignore[index]
    h = a.headers

    for method, url, body in [
        ("GET", f"/api/v1/contacts/{contact_id}", None),
        ("PATCH", f"/api/v1/contacts/{contact_id}", {"name": "pwned"}),
        ("DELETE", f"/api/v1/contacts/{contact_id}", None),
        ("GET", f"/api/v1/contacts/{contact_id}/timeline", None),
        ("GET", f"/api/v1/contacts/{contact_id}/notes", None),
        ("POST", f"/api/v1/contacts/{contact_id}/notes", {"body": "x"}),
        ("PATCH", f"/api/v1/contacts/{contact_id}/notes/{note_id}", {"body": "x"}),
        ("DELETE", f"/api/v1/contacts/{contact_id}/notes/{note_id}", None),
        ("GET", f"/api/v1/calls/{call_id}", None),
        ("PATCH", f"/api/v1/calls/{call_id}", {"status": "CANCELLED"}),
        ("GET", f"/api/v1/action-items/{item_id}", None),
        ("PATCH", f"/api/v1/action-items/{item_id}", {"title": "x"}),
        ("POST", f"/api/v1/action-items/{item_id}/confirm", None),
        ("DELETE", f"/api/v1/action-items/{item_id}", None),
    ]:
        resp = await client.request(method, url, headers=h, json=body)
        assert resp.status_code == 404, (method, url, resp.status_code)

    # Creating resources that reference another tenant's records is rejected.
    for url, body in [
        ("/api/v1/calls", {"contact_id": contact_id}),
        ("/api/v1/action-items", {"title": "x", "contact_id": contact_id}),
        ("/api/v1/action-items", {"title": "x", "call_id": call_id}),
    ]:
        resp = await client.post(url, headers=h, json=body)
        assert resp.status_code == 422, (url, resp.text)

    # Lists never include the other tenant's rows.
    for url in ("/api/v1/contacts", "/api/v1/calls", "/api/v1/action-items"):
        assert (await client.get(url, headers=h)).json()["total"] == 0
    filtered = await client.get("/api/v1/calls", headers=h, params={"contact_id": contact_id})
    assert filtered.json()["total"] == 0


async def test_b_data_intact_after_attacks(client: AsyncClient, world: dict[str, object]) -> None:
    await test_api_cannot_read_or_modify_other_tenant(client, world)
    b = world["b"]
    assert isinstance(b, Account)
    contact_id = world["contact"]["id"]  # type: ignore[index]
    resp = await client.get(f"/api/v1/contacts/{contact_id}", headers=b.headers)
    assert resp.json()["name"] == "B's customer"
    notes = await client.get(f"/api/v1/contacts/{contact_id}/notes", headers=b.headers)
    assert notes.json()["total"] == 1


@pytest.mark.parametrize("model", [Contact, ContactNote, Call, ActionItem, TimelineEvent])
async def test_rls_hides_other_tenant_rows(world: dict[str, object], model: type) -> None:
    a = world["a"]
    assert isinstance(a, Account)
    session = await open_session(TenantContext(company_id=a.company_id, user_id=a.user_id))
    async with session:
        count = await session.scalar(select(func.count()).select_from(model))
    assert count == 0
    no_ctx = await open_session()
    async with no_ctx:
        assert await no_ctx.scalar(select(func.count()).select_from(model)) == 0


async def test_rls_blocks_cross_tenant_writes(world: dict[str, object]) -> None:
    a = world["a"]
    b = world["b"]
    assert isinstance(a, Account)
    assert isinstance(b, Account)
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        session.add(Contact(id=uuid.uuid4(), company_id=b.company_id, name="injected"))
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await session.flush()
        assert "row-level security" in str(exc.value)
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        result = await session.execute(text("UPDATE contacts SET name = 'pwned'"))
        assert result.rowcount == 0  # type: ignore[attr-defined]
        result = await session.execute(text("DELETE FROM calls"))
        assert result.rowcount == 0  # type: ignore[attr-defined]


async def test_composite_fk_blocks_cross_tenant_reference(world: dict[str, object]) -> None:
    """Even inside A's own tenant context, a row cannot point at B's contact."""
    a = world["a"]
    assert isinstance(a, Account)
    b_contact = uuid.UUID(world["contact"]["id"])  # type: ignore[index]
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        session.add(Call(id=uuid.uuid4(), company_id=a.company_id, contact_id=b_contact))
        with pytest.raises(DBAPIError) as exc:
            await session.flush()
        assert "fk_calls_contact" in str(exc.value)
