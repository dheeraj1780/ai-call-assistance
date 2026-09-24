"""CRM: contacts, notes, timeline, calls, action items — behaviour, validation, authorization."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.action_items.models import ActionItem
from app.audit.models import AuditLog
from app.common.db import TenantContext
from app.tenants.models import MemberRole
from tests.conftest import DEFAULT_PASSWORD, Account, add_member_directly, open_session

Register = Callable[..., Awaitable[Account]]


async def make_contact(client: AsyncClient, acct: Account, **fields: Any) -> dict[str, Any]:
    body = {"name": "Ravi Kumar", "organization": "ABC Industries", **fields}
    resp = await client.post("/api/v1/contacts", headers=acct.headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def owner(client: AsyncClient, register: Register) -> Account:
    return await register(client, "owner@example.com", "Acme Traders")


# ---- Contacts ----------------------------------------------------------------------------


async def test_create_contact_defaults_and_normalisation(
    client: AsyncClient, owner: Account
) -> None:
    c = await make_contact(
        client,
        owner,
        email="  Ravi@ABC.in ",
        phone="+91 98765-43210",
        tags=["VIP", "vip", " north "],
        attributes={"branches": "5"},
    )
    assert c["email"] == "ravi@abc.in"
    assert c["phone"] == "+919876543210"
    assert c["tags"] == ["vip", "north"]
    assert c["status"] == "NEW"
    assert c["source"] == "MANUAL"
    assert c["owner_user_id"] == str(owner.user_id)
    assert c["attributes"] == {"branches": "5"}


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "A", "email": "not-an-email"},
        {"name": "A", "phone": "12"},
        {"name": "A", "status": "BOGUS"},
        {"name": "A", "tags": [f"t{i}" for i in range(21)]},
        {"name": "A", "company_id": str(uuid.uuid4())},
        {"name": "A", "attributes": {str(i): "v" for i in range(21)}},
    ],
)
async def test_contact_validation(
    client: AsyncClient, owner: Account, body: dict[str, Any]
) -> None:
    resp = await client.post("/api/v1/contacts", headers=owner.headers, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_owner_must_be_member_of_same_company(
    client: AsyncClient, owner: Account, register: Register
) -> None:
    other = await register(client, "other@example.com", "Other Co")
    resp = await client.post(
        "/api/v1/contacts",
        headers=owner.headers,
        json={"name": "X", "owner_user_id": str(other.user_id)},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_reference"


async def test_search_filter_sort_paginate(client: AsyncClient, owner: Account) -> None:
    await make_contact(client, owner, name="Anita Rao", organization="Sunrise Foods", tags=["food"])
    await make_contact(client, owner, name="Bala Iyer", email="bala@steel.in", status="QUALIFIED")
    await make_contact(client, owner, name="Chetan 50%_off", phone="9876500000")

    async def names(**params: Any) -> list[str]:
        r = await client.get("/api/v1/contacts", headers=owner.headers, params=params)
        assert r.status_code == 200, r.text
        return [c["name"] for c in r.json()["items"]]

    assert await names(q="sunrise") == ["Anita Rao"]
    assert await names(q="STEEL") == ["Bala Iyer"]
    assert await names(q="98765") == ["Chetan 50%_off"]
    assert await names(q="%") == ["Chetan 50%_off"]  # wildcard is escaped, matched literally
    assert await names(status="QUALIFIED") == ["Bala Iyer"]
    assert await names(tag="FOOD") == ["Anita Rao"]
    assert await names(sort="name") == ["Anita Rao", "Bala Iyer", "Chetan 50%_off"]
    page = await client.get(
        "/api/v1/contacts", headers=owner.headers, params={"limit": 2, "offset": 2, "sort": "name"}
    )
    assert page.json()["total"] == 3
    assert [c["name"] for c in page.json()["items"]] == ["Chetan 50%_off"]


async def test_update_contact_records_timeline_and_audit(
    client: AsyncClient, owner: Account
) -> None:
    c = await make_contact(client, owner)
    resp = await client.patch(
        f"/api/v1/contacts/{c['id']}",
        headers=owner.headers,
        json={"status": "QUALIFIED", "designation": "Owner", "email": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "QUALIFIED"

    tl = await client.get(f"/api/v1/contacts/{c['id']}/timeline", headers=owner.headers)
    events = tl.json()["items"]
    types = [e["event_type"] for e in events]
    assert types == ["CONTACT_UPDATED", "STATUS_CHANGED", "CONTACT_CREATED"]
    status_event = events[1]
    assert (status_event["from_status"], status_event["to_status"]) == ("NEW", "QUALIFIED")

    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        actions = (await session.scalars(select(AuditLog.action))).all()
        updated = await session.scalar(select(AuditLog).where(AuditLog.action == "contact.updated"))
    assert "contact.created" in actions
    assert updated is not None
    assert updated.details == {"fields": ["designation", "email", "status"]}


async def test_null_does_not_clear_required_fields(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    resp = await client.patch(
        f"/api/v1/contacts/{c['id']}", headers=owner.headers, json={"name": None, "status": None}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ravi Kumar"
    assert resp.json()["status"] == "NEW"


async def test_member_permissions_on_contacts(client: AsyncClient, owner: Account) -> None:
    await add_member_directly(owner.company_id, "rep@example.com", MemberRole.MEMBER)
    rep = await login(client, "rep@example.com")
    owned_by_owner = await make_contact(client, owner)
    unowned = await make_contact(client, owner, name="Unowned", owner_user_id=None)

    # Everyone in the company can read.
    assert (
        await client.get(f"/api/v1/contacts/{owned_by_owner['id']}", headers=rep)
    ).status_code == 200
    # A member cannot edit/delete a contact owned by someone else...
    r = await client.patch(
        f"/api/v1/contacts/{owned_by_owner['id']}", headers=rep, json={"name": "Hijack"}
    )
    assert r.status_code == 403
    assert (
        await client.delete(f"/api/v1/contacts/{owned_by_owner['id']}", headers=rep)
    ).status_code == 403
    # ...but can edit an unowned one, and create their own.
    assert (
        await client.patch(f"/api/v1/contacts/{unowned['id']}", headers=rep, json={"notes": "ok"})
    ).status_code == 200
    mine = await client.post("/api/v1/contacts", headers=rep, json={"name": "Mine"})
    assert mine.status_code == 201
    assert (
        await client.delete(f"/api/v1/contacts/{mine.json()['id']}", headers=rep)
    ).status_code == 204
    # The admin/owner can delete anything.
    assert (
        await client.delete(f"/api/v1/contacts/{owned_by_owner['id']}", headers=owner.headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/contacts/{owned_by_owner['id']}", headers=owner.headers)
    ).status_code == 404


async def test_delete_contact_cascades(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    cid = c["id"]
    await client.post(f"/api/v1/contacts/{cid}/notes", headers=owner.headers, json={"body": "hi"})
    call = await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": cid})
    await client.post(
        "/api/v1/action-items",
        headers=owner.headers,
        json={"title": "Send quote", "contact_id": cid},
    )
    assert (
        await client.delete(f"/api/v1/contacts/{cid}", headers=owner.headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/calls/{call.json()['id']}", headers=owner.headers)
    ).status_code == 404
    items = await client.get("/api/v1/action-items", headers=owner.headers)
    assert items.json()["total"] == 0


# ---- Notes & timeline ---------------------------------------------------------------------


async def test_notes_crud_and_timeline_sync(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    base = f"/api/v1/contacts/{c['id']}/notes"
    note = (
        await client.post(base, headers=owner.headers, json={"body": "Wants 5 branches"})
    ).json()
    assert note["author_user_id"] == str(owner.user_id)

    edited = await client.patch(
        f"{base}/{note['id']}", headers=owner.headers, json={"body": "Wants 6 branches"}
    )
    assert edited.status_code == 200
    tl = (await client.get(f"/api/v1/contacts/{c['id']}/timeline", headers=owner.headers)).json()
    note_events = [e for e in tl["items"] if e["event_type"] == "NOTE_ADDED"]
    assert note_events[0]["summary"] == "Wants 6 branches"

    listing = await client.get(base, headers=owner.headers)
    assert listing.json()["total"] == 1

    assert (await client.delete(f"{base}/{note['id']}", headers=owner.headers)).status_code == 204
    tl = (await client.get(f"/api/v1/contacts/{c['id']}/timeline", headers=owner.headers)).json()
    assert all(e["event_type"] != "NOTE_ADDED" for e in tl["items"])


async def test_note_validation_and_author_permissions(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner, owner_user_id=None)
    base = f"/api/v1/contacts/{c['id']}/notes"
    assert (await client.post(base, headers=owner.headers, json={"body": "  "})).status_code == 422
    assert (
        await client.post(base, headers=owner.headers, json={"body": "x" * 10_001})
    ).status_code == 422

    await add_member_directly(owner.company_id, "rep@example.com", MemberRole.MEMBER)
    rep = await login(client, "rep@example.com")
    owners_note = (await client.post(base, headers=owner.headers, json={"body": "owner"})).json()
    assert (
        await client.patch(f"{base}/{owners_note['id']}", headers=rep, json={"body": "x"})
    ).status_code == 403
    rep_note = (await client.post(base, headers=rep, json={"body": "rep"})).json()
    # Admin/owner may moderate any note.
    assert (
        await client.delete(f"{base}/{rep_note['id']}", headers=owner.headers)
    ).status_code == 204


async def test_timeline_pagination_and_category_filter(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    for i in range(5):
        await client.post(
            f"/api/v1/contacts/{c['id']}/notes", headers=owner.headers, json={"body": f"n{i}"}
        )
    url = f"/api/v1/contacts/{c['id']}/timeline"
    first = (await client.get(url, headers=owner.headers, params={"limit": 4})).json()
    assert len(first["items"]) == 4
    assert first["next_cursor"]
    second = (
        await client.get(
            url, headers=owner.headers, params={"limit": 4, "before": first["next_cursor"]}
        )
    ).json()
    assert len(second["items"]) == 2  # 5 notes + contact created = 6 events
    assert second["next_cursor"] is None
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert len(set(ids)) == 6

    notes_only = (await client.get(url, headers=owner.headers, params={"category": "NOTE"})).json()
    assert [e["summary"] for e in notes_only["items"]] == ["n4", "n3", "n2", "n1", "n0"]

    bad = await client.get(url, headers=owner.headers, params={"before": "garbage!!"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_cursor"


# ---- Calls --------------------------------------------------------------------------------


async def test_plan_call_and_manual_lifecycle(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    resp = await client.post(
        "/api/v1/calls",
        headers=owner.headers,
        json={
            "contact_id": c["id"],
            "objective": "Understand inventory needs",
            "desired_outcome": "Demo booked",
            "scheduled_at": "2026-10-01T10:00:00+05:30",
        },
    )
    assert resp.status_code == 201, resp.text
    call = resp.json()
    assert call["status"] == "PLANNED"
    assert call["contact"]["name"] == "Ravi Kumar"
    assert call["user_id"] == str(owner.user_id)
    url = f"/api/v1/calls/{call['id']}"

    # Outcome only after completion.
    early = await client.patch(url, headers=owner.headers, json={"outcome": "INTERESTED"})
    assert early.status_code == 409

    started = (await client.patch(url, headers=owner.headers, json={"status": "ACTIVE"})).json()
    assert started["started_at"] is not None
    # Planning fields frozen once started.
    frozen = await client.patch(url, headers=owner.headers, json={"objective": "changed"})
    assert frozen.status_code == 409

    done = await client.patch(
        url,
        headers=owner.headers,
        json={"status": "COMPLETED", "outcome": "FOLLOW_UP_REQUIRED", "next_step": "Send quote"},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["ended_at"] is not None
    assert body["duration_seconds"] is not None
    assert body["outcome"] == "FOLLOW_UP_REQUIRED"

    # Terminal: no further transitions.
    again = await client.patch(url, headers=owner.headers, json={"status": "ACTIVE"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_status_transition"

    tl = (await client.get(f"/api/v1/contacts/{c['id']}/timeline", headers=owner.headers)).json()
    call_events = [e for e in tl["items"] if e["category"] == "CALL"]
    assert [e["to_status"] for e in call_events] == ["COMPLETED", "ACTIVE", "PLANNED"]


async def test_call_validation(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    naive = await client.post(
        "/api/v1/calls",
        headers=owner.headers,
        json={"contact_id": c["id"], "scheduled_at": "2026-10-01T10:00:00"},
    )
    assert naive.status_code == 422  # timezone required
    missing = await client.post(
        "/api/v1/calls", headers=owner.headers, json={"contact_id": str(uuid.uuid4())}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_reference"


async def test_call_list_filters(client: AsyncClient, owner: Account) -> None:
    a = await make_contact(client, owner, name="A")
    b = await make_contact(client, owner, name="B")
    await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": a["id"]})
    call_b = (
        await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": b["id"]})
    ).json()
    await client.patch(
        f"/api/v1/calls/{call_b['id']}", headers=owner.headers, json={"status": "CANCELLED"}
    )
    by_contact = await client.get(
        "/api/v1/calls", headers=owner.headers, params={"contact_id": a["id"]}
    )
    assert [x["contact"]["name"] for x in by_contact.json()["items"]] == ["A"]
    cancelled = await client.get(
        "/api/v1/calls", headers=owner.headers, params={"status": "CANCELLED"}
    )
    assert [x["id"] for x in cancelled.json()["items"]] == [call_b["id"]]


async def test_member_cannot_update_someone_elses_call(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    call = (
        await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": c["id"]})
    ).json()
    await add_member_directly(owner.company_id, "rep@example.com", MemberRole.MEMBER)
    rep = await login(client, "rep@example.com")
    assert (await client.get(f"/api/v1/calls/{call['id']}", headers=rep)).status_code == 200
    resp = await client.patch(f"/api/v1/calls/{call['id']}", headers=rep, json={"status": "ACTIVE"})
    assert resp.status_code == 403


# ---- Action items -------------------------------------------------------------------------


async def test_action_item_lifecycle(client: AsyncClient, owner: Account) -> None:
    c = await make_contact(client, owner)
    call = (
        await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": c["id"]})
    ).json()
    resp = await client.post(
        "/api/v1/action-items",
        headers=owner.headers,
        json={
            "title": "Send pricing",
            "kind": "FOLLOW_UP",
            "call_id": call["id"],
            "due_at": "2026-10-02T09:00:00+05:30",
        },
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["contact_id"] == c["id"]  # derived from the call
    assert item["source"] == "MANUAL"
    assert item["is_confirmed"] is True
    assert item["assignee_user_id"] == str(owner.user_id)

    done = await client.patch(
        f"/api/v1/action-items/{item['id']}", headers=owner.headers, json={"status": "DONE"}
    )
    assert done.json()["completed_at"] is not None
    reopened = await client.patch(
        f"/api/v1/action-items/{item['id']}", headers=owner.headers, json={"status": "OPEN"}
    )
    assert reopened.json()["completed_at"] is None

    tl = (await client.get(f"/api/v1/contacts/{c['id']}/timeline", headers=owner.headers)).json()
    follow_ups = [e["event_type"] for e in tl["items"] if e["category"] == "FOLLOW_UP"]
    assert follow_ups == ["ACTION_ITEM_COMPLETED", "ACTION_ITEM_CREATED"]


async def test_action_item_link_validation(client: AsyncClient, owner: Account) -> None:
    a = await make_contact(client, owner, name="A")
    b = await make_contact(client, owner, name="B")
    call = (
        await client.post("/api/v1/calls", headers=owner.headers, json={"contact_id": a["id"]})
    ).json()
    mismatch = await client.post(
        "/api/v1/action-items",
        headers=owner.headers,
        json={"title": "x", "call_id": call["id"], "contact_id": b["id"]},
    )
    assert mismatch.status_code == 422
    standalone = await client.post(
        "/api/v1/action-items", headers=owner.headers, json={"title": "Internal task"}
    )
    assert standalone.status_code == 201
    assert standalone.json()["contact_id"] is None


async def test_unconfirmed_ai_item_must_be_confirmed(client: AsyncClient, owner: Account) -> None:
    item_id = uuid.uuid4()
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        session.add(
            ActionItem(
                id=item_id,
                company_id=owner.company_id,
                title="AI suggested: send brochure",
                source="AI",
                created_by_user_id=owner.user_id,
            )
        )
        await session.commit()
    url = f"/api/v1/action-items/{item_id}"
    blocked = await client.patch(url, headers=owner.headers, json={"status": "DONE"})
    assert blocked.status_code == 409
    unconfirmed = await client.get(
        "/api/v1/action-items", headers=owner.headers, params={"confirmed": "false"}
    )
    assert [i["id"] for i in unconfirmed.json()["items"]] == [str(item_id)]

    confirmed = await client.post(f"{url}/confirm", headers=owner.headers)
    assert confirmed.json()["is_confirmed"] is True
    assert confirmed.json()["confirmed_by_user_id"] == str(owner.user_id)
    again = await client.post(f"{url}/confirm", headers=owner.headers)  # idempotent
    assert again.json()["confirmed_at"] == confirmed.json()["confirmed_at"]
    assert (
        await client.patch(url, headers=owner.headers, json={"status": "DONE"})
    ).status_code == 200


async def test_action_item_assignment_rules(
    client: AsyncClient, owner: Account, register: Register
) -> None:
    rep_id = await add_member_directly(owner.company_id, "rep@example.com", MemberRole.MEMBER)
    rep = await login(client, "rep@example.com")
    outsider = await register(client, "out@example.com", "Outsider Co")

    assigned = await client.post(
        "/api/v1/action-items",
        headers=owner.headers,
        json={"title": "Call back", "assignee_user_id": str(rep_id)},
    )
    assert assigned.status_code == 201
    bad = await client.post(
        "/api/v1/action-items",
        headers=owner.headers,
        json={"title": "x", "assignee_user_id": str(outsider.user_id)},
    )
    assert bad.status_code == 422

    item_url = f"/api/v1/action-items/{assigned.json()['id']}"
    # The assignee can update status but not delete (only creator/admin can).
    assert (
        await client.patch(item_url, headers=rep, json={"status": "IN_PROGRESS"})
    ).status_code == 200
    assert (await client.delete(item_url, headers=rep)).status_code == 403

    # An unrelated member cannot touch it.
    await add_member_directly(owner.company_id, "rep2@example.com", MemberRole.MEMBER)
    rep2 = await login(client, "rep2@example.com")
    assert (await client.patch(item_url, headers=rep2, json={"title": "x"})).status_code == 403

    mine = await client.get(
        "/api/v1/action-items", headers=owner.headers, params={"assignee_user_id": str(rep_id)}
    )
    assert mine.json()["total"] == 1
    assert (await client.delete(item_url, headers=owner.headers)).status_code == 204
