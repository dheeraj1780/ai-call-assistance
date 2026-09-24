"""Call preparation, agendas, AI agenda suggestions (grounding, failures, budget), jobs."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import select

from app.agendas.models import AgendaItem, AgendaItemStatus, StatusSource
from app.agendas.service import apply_status
from app.ai.gateway import set_ai_provider
from app.ai.mock_provider import MockAIProvider
from app.ai.models import AIUsageRecord
from app.ai.provider import AIResult, AITask, AIUnavailableError, AIUsage
from app.common.db import TenantContext, get_session_factory
from app.jobs import service as jobs
from app.jobs.models import Job
from app.tenants.models import MemberRole
from tests.conftest import DEFAULT_PASSWORD, Account, add_member_directly, open_session

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def owner(client: AsyncClient, register: Register) -> Account:
    return await register(client, "owner@example.com", "Acme Traders")


async def planned_call(client: AsyncClient, acct: Account) -> tuple[str, str]:
    contact = (
        await client.post(
            "/api/v1/contacts",
            headers=acct.headers,
            json={"name": "Ravi Kumar", "organization": "ABC Industries", "notes": "Runs 3 shops"},
        )
    ).json()
    await client.post(
        f"/api/v1/contacts/{contact['id']}/notes",
        headers=acct.headers,
        json={"body": "Currently tracks stock in Excel"},
    )
    await client.post(
        "/api/v1/action-items",
        headers=acct.headers,
        json={"title": "Share brochure", "contact_id": contact["id"]},
    )
    call = (
        await client.post(
            "/api/v1/calls",
            headers=acct.headers,
            json={"contact_id": contact["id"], "objective": "Qualify inventory automation need"},
        )
    ).json()
    return contact["id"], call["id"]


async def test_prep_context(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    resp = await client.get(f"/api/v1/calls/{call_id}/prep", headers=owner.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contact"]["name"] == "Ravi Kumar"
    assert [n["body"] for n in body["recent_notes"]] == ["Currently tracks stock in Excel"]
    assert [i["title"] for i in body["open_action_items"]] == ["Share brochure"]
    assert body["agenda"] == []
    assert body["recent_timeline"]


async def test_replace_and_override_agenda(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    url = f"/api/v1/calls/{call_id}/agenda"
    resp = await client.put(
        url,
        headers=owner.headers,
        json={
            "items": [
                {"title": "Current process", "question": "How do you track stock today?"},
                {"title": "Budget", "source": "AI"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [(i["position"], i["title"], i["status"]) for i in items] == [
        (0, "Current process", "NOT_STARTED"),
        (1, "Budget", "NOT_STARTED"),
    ]
    # Replacing again fully replaces (no unique-position clash).
    again = await client.put(url, headers=owner.headers, json={"items": [{"title": "Only one"}]})
    assert [i["title"] for i in again.json()] == ["Only one"]

    item_id = again.json()[0]["id"]
    override = await client.patch(
        f"{url}/{item_id}", headers=owner.headers, json={"status": "SKIPPED"}
    )
    assert override.json()["status"] == "SKIPPED"
    assert override.json()["status_source"] == "MANUAL"


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"title": ""}]},
        {"items": [{"title": f"t{i}"} for i in range(16)]},
        {"items": [{"title": "x", "company_id": str(uuid.uuid4())}]},
        {"items": [{"title": "x" * 201}]},
    ],
)
async def test_agenda_validation(client: AsyncClient, owner: Account, body: dict[str, Any]) -> None:
    _, call_id = await planned_call(client, owner)
    resp = await client.put(f"/api/v1/calls/{call_id}/agenda", headers=owner.headers, json=body)
    assert resp.status_code == 422


async def test_agenda_locked_after_call_starts(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    await client.patch(f"/api/v1/calls/{call_id}", headers=owner.headers, json={"status": "ACTIVE"})
    resp = await client.put(
        f"/api/v1/calls/{call_id}/agenda", headers=owner.headers, json={"items": [{"title": "x"}]}
    )
    assert resp.status_code == 409


async def test_member_cannot_edit_others_agenda(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    await add_member_directly(owner.company_id, "rep@example.com", MemberRole.MEMBER)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "rep@example.com", "password": DEFAULT_PASSWORD}
    )
    rep = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get(f"/api/v1/calls/{call_id}/prep", headers=rep)).status_code == 200
    resp = await client.put(
        f"/api/v1/calls/{call_id}/agenda", headers=rep, json={"items": [{"title": "x"}]}
    )
    assert resp.status_code == 403


async def test_ai_agenda_suggestion_is_grounded_and_not_saved(
    client: AsyncClient, owner: Account
) -> None:
    _, call_id = await planned_call(client, owner)
    resp = await client.post(f"/api/v1/calls/{call_id}/agenda/suggest", headers=owner.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "mock"
    assert body["ai_suggested_agenda"]
    assert body["ai_suggested_agenda"][0]["title"] == "Confirm the purpose of the call"
    refs = {f["source_ref"] for f in body["customer_facts"]}
    assert "contact" in refs
    assert any(r.startswith("note:") for r in refs)
    assert any("Excel" in f["text"] for f in body["customer_facts"])
    # Suggestion only: agenda still empty.
    agenda = await client.get(f"/api/v1/calls/{call_id}/agenda", headers=owner.headers)
    assert agenda.json() == []
    # Usage recorded.
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        usage = (await session.scalars(select(AIUsageRecord))).all()
    assert [(u.task, u.provider, u.success) for u in usage] == [("AGENDA", "mock", True)]


class _InventingProvider:
    """Returns a 'fact' citing a record that was never provided (hallucination)."""

    name = "fake"

    async def generate[T: BaseModel](self, *, schema: type[T], **_: Any) -> AIResult[T]:
        out = schema.model_validate(
            {
                "customer_facts": [
                    {"text": "Customer already signed a contract", "source_ref": "call:made-up"},
                    {"text": "Name is Ravi", "source_ref": "contact"},
                ],
                "agenda": [{"title": "Budget"}],
            }
        )
        return AIResult(output=out, usage=AIUsage(provider="fake", model="fake"))


async def test_ungrounded_facts_are_discarded(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    set_ai_provider(_InventingProvider())
    body = (
        await client.post(f"/api/v1/calls/{call_id}/agenda/suggest", headers=owner.headers)
    ).json()
    assert [f["text"] for f in body["customer_facts"]] == ["Name is Ravi"]
    assert body["discarded_ungrounded_facts"] == 1


async def test_ai_failure_degrades_gracefully(client: AsyncClient, owner: Account) -> None:
    _, call_id = await planned_call(client, owner)
    provider = MockAIProvider()
    provider.fail_next(AIUnavailableError("down"))
    set_ai_provider(provider)
    resp = await client.post(f"/api/v1/calls/{call_id}/agenda/suggest", headers=owner.headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "ai_unavailable"
    # Manual agenda still works while AI is down.
    ok = await client.put(
        f"/api/v1/calls/{call_id}/agenda", headers=owner.headers, json={"items": [{"title": "x"}]}
    )
    assert ok.status_code == 200
    session = await open_session(TenantContext(company_id=owner.company_id))
    async with session:
        usage = (await session.scalars(select(AIUsageRecord))).all()
    assert [(u.success, u.error_code) for u in usage] == [(False, "ai_unavailable")]


async def test_daily_ai_budget_fails_closed(
    client: AsyncClient, owner: Account, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, call_id = await planned_call(client, owner)
    monkeypatch.setattr(settings, "ai_daily_token_budget", 1)
    first = await client.post(f"/api/v1/calls/{call_id}/agenda/suggest", headers=owner.headers)
    assert first.status_code == 200  # usage was 0 before this call
    second = await client.post(f"/api/v1/calls/{call_id}/agenda/suggest", headers=owner.headers)
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "ai_budget_exceeded"


async def test_prep_and_agenda_are_tenant_isolated(
    client: AsyncClient, owner: Account, register: Register
) -> None:
    _, call_id = await planned_call(client, owner)
    other = await register(client, "other@example.com", "Other Co")
    for method, path in [
        ("GET", "prep"),
        ("GET", "agenda"),
        ("POST", "agenda/suggest"),
    ]:
        resp = await client.request(
            method, f"/api/v1/calls/{call_id}/{path}", headers=other.headers
        )
        assert resp.status_code == 404, path
    resp = await client.put(
        f"/api/v1/calls/{call_id}/agenda", headers=other.headers, json={"items": [{"title": "x"}]}
    )
    assert resp.status_code == 404


def _item(status: AgendaItemStatus, source: StatusSource) -> AgendaItem:
    return AgendaItem(status=status.value, status_source=source.value, title="t", position=0)


def test_status_precedence_rules() -> None:
    manual = _item(AgendaItemStatus.IN_PROGRESS, StatusSource.MANUAL)
    assert not apply_status(
        manual, AgendaItemStatus.COMPLETED, StatusSource.AI, confidence=0.9, reason="x"
    )
    assert manual.status == AgendaItemStatus.IN_PROGRESS

    done = _item(AgendaItemStatus.COMPLETED, StatusSource.AI)
    assert not apply_status(
        done, AgendaItemStatus.IN_PROGRESS, StatusSource.DETERMINISTIC, confidence=None, reason="x"
    )

    fresh = _item(AgendaItemStatus.NOT_STARTED, StatusSource.DEFAULT)
    assert apply_status(
        fresh,
        AgendaItemStatus.IN_PROGRESS,
        StatusSource.DETERMINISTIC,
        confidence=0.5,
        reason="topic raised",
    )
    assert apply_status(
        fresh, AgendaItemStatus.COMPLETED, StatusSource.AI, confidence=0.8, reason="answered"
    )
    assert fresh.completed_at is not None
    # A human can always move it back.
    assert apply_status(
        fresh, AgendaItemStatus.NOT_STARTED, StatusSource.MANUAL, confidence=None, reason=None
    )
    assert fresh.completed_at is None


# ---- Jobs ---------------------------------------------------------------------------------


async def test_jobs_dedupe_retry_and_fail() -> None:
    attempts: list[int] = []

    @jobs.register_job("test.flaky")
    async def flaky(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
        attempts.append(payload["n"])
        raise RuntimeError("boom")

    @jobs.register_job("test.ok")
    async def ok(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
        attempts.append(-1)

    async with get_session_factory()() as session:
        await jobs.enqueue(session, "test.ok", company_id=None, dedupe_key="k1")
        await jobs.enqueue(session, "test.ok", company_id=None, dedupe_key="k1")  # duplicate
        await jobs.enqueue(session, "test.flaky", company_id=None, payload={"n": 1}, max_attempts=2)
        await session.commit()

    assert await jobs.drain() == 2  # ok once, flaky first attempt (retry is scheduled later)
    async with get_session_factory()() as session:
        rows = {j.kind: j for j in (await session.scalars(select(Job))).all()}
        assert rows["test.ok"].status == "SUCCEEDED"
        flaky_job = rows["test.flaky"]
        assert (flaky_job.status, flaky_job.attempts, flaky_job.last_error) == (
            "PENDING",
            1,
            "RuntimeError",
        )
        flaky_job.run_after = flaky_job.created_at  # make the retry due now
        await session.commit()
    assert await jobs.drain() == 1
    async with get_session_factory()() as session:
        flaky_row = await session.scalar(select(Job).where(Job.kind == "test.flaky"))
        assert flaky_row is not None
        assert (flaky_row.status, flaky_row.attempts) == ("FAILED", 2)
    assert await jobs.drain() == 0  # no infinite retry
    assert attempts.count(-1) == 1
    assert attempts.count(1) == 2


async def test_mock_provider_is_labelled() -> None:
    provider = MockAIProvider()
    assert provider.name == "mock"
    assert AITask.AGENDA in {AITask(t) for t in AITask}
