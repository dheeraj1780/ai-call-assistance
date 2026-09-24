"""Post-call processing, grounding, follow-up drafts, history, dashboard, prep integration."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from app.ai.gateway import set_ai_provider
from app.ai.mock_provider import MockAIProvider
from app.ai.provider import AIResult, AIUnavailableError, AIUsage
from app.jobs.service import drain
from app.main import app
from tests.conftest import Account
from tests.test_realtime import run_default_simulation, started_call

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme Traders")
    await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    return acct


async def completed_call(client: AsyncClient, acct: Account) -> dict[str, Any]:
    call = await started_call(client, acct)
    await run_default_simulation(call)
    await drain()
    return call


async def post_call(client: AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/calls/{call_id}/post-call", headers=acct.headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_post_call_pipeline(client: AsyncClient, rep: Account) -> None:
    call = await completed_call(client, rep)
    body = await post_call(client, rep, call["id"])
    summary = body["summary"]
    assert summary["status"] == "READY"
    assert summary["provider"] == "mock"
    assert "Call with Ravi Kumar" in summary["summary"]
    assert summary["current_solution"] == {"value": "Uses Excel", "status": "INFERRED"} or summary[
        "current_solution"
    ]["status"] in ("CONFIRMED", "INFERRED")
    assert summary["budget"]["status"] == "CONFIRMED"  # quote found verbatim in the transcript
    assert "2 lakh" in summary["budget"]["value"]
    assert summary["suggested_outcome"] == "FOLLOW_UP_REQUIRED"

    assert body["auto_send"] is False
    channels = sorted(d["channel"] for d in body["drafts"])
    assert channels == ["EMAIL", "GENERAL", "WHATSAPP"]
    assert all(d["status"] == "DRAFT" for d in body["drafts"])
    email = next(d for d in body["drafts"] if d["channel"] == "EMAIL")
    assert email["subject"] == "Following up on our discussion"
    assert "Ravi Kumar" in email["body"]

    items = (
        await client.get(
            "/api/v1/action-items", headers=rep.headers, params={"call_id": call["id"]}
        )
    ).json()["items"]
    assert items
    assert all(i["source"] == "AI" and not i["is_confirmed"] for i in items)

    tl = (
        await client.get(f"/api/v1/contacts/{call['contact_id']}/timeline", headers=rep.headers)
    ).json()
    types = [e["event_type"] for e in tl["items"]]
    assert "CALL_SUMMARY" in types
    assert "FOLLOW_UP_DRAFTED" in types

    # Idempotent: processing again creates nothing new.
    import uuid

    from app.postcall.service import process_call

    await process_call(rep.company_id, uuid.UUID(call["id"]))
    again = await post_call(client, rep, call["id"])
    assert len(again["drafts"]) == 3


class _Hallucinating:
    """Claims CONFIRMED facts with fake evidence and promises a discount in the draft."""

    name = "fake"

    async def generate[T: BaseModel](self, *, schema: type[T], **_: Any) -> AIResult[T]:
        out = schema.model_validate(
            {
                "summary": "Customer agreed to buy immediately.",
                "budget": {
                    "value": "10 lakh",
                    "status": "CONFIRMED",
                    "evidence": "we have 10 lakh ready",
                },
                "timeline": {"value": None, "status": "CONFIRMED", "evidence": None},
                "decision_maker": {
                    "value": "Partner",
                    "status": "CONFIRMED",
                    "evidence": "I will have to check with my partner before deciding",
                },
                "follow_up": {
                    "email_subject": "Your 25% discount",
                    "email_body": "As promised, you get 25% off at Rs 99,999 per year.",
                    "whatsapp_body": "Thanks!",
                    "general_body": "Thanks!",
                },
            }
        )
        return AIResult(output=out, usage=AIUsage(provider="fake", model="fake"))


async def test_grounding_downgrades_unsupported_claims(client: AsyncClient, rep: Account) -> None:
    set_ai_provider(_Hallucinating())
    call = await completed_call(client, rep)
    summary = (await post_call(client, rep, call["id"]))["summary"]
    assert summary["budget"] == {"value": "10 lakh", "status": "INFERRED"}  # evidence not in call
    assert summary["timeline"] == {"value": None, "status": "NOT_DISCUSSED"}
    assert summary["decision_maker"]["status"] == "CONFIRMED"  # real quote
    drafts = (await post_call(client, rep, call["id"]))["drafts"]
    email = next(d for d in drafts if d["channel"] == "EMAIL")
    assert any("25%" in w for w in email["warnings"])
    assert any("99,999" in w for w in email["warnings"])
    assert email["status"] == "DRAFT"


async def test_ai_failure_keeps_call_and_allows_retry(client: AsyncClient, rep: Account) -> None:
    provider = MockAIProvider()
    provider.fail_next(AIUnavailableError("down"), times=100)
    set_ai_provider(provider)
    call = await completed_call(client, rep)
    body = await post_call(client, rep, call["id"])
    assert body["summary"]["status"] == "FAILED"
    assert body["summary"]["error_code"] == "ai_unavailable"
    assert body["call"]["status"] == "COMPLETED"  # call record intact
    transcript = await client.get(f"/api/v1/calls/{call['id']}/transcript", headers=rep.headers)
    assert len(transcript.json()) > 0

    set_ai_provider(MockAIProvider())
    retry = await client.post(f"/api/v1/calls/{call['id']}/post-call/retry", headers=rep.headers)
    assert retry.status_code == 202
    await drain()
    assert (await post_call(client, rep, call["id"]))["summary"]["status"] == "READY"
    again = await client.post(f"/api/v1/calls/{call['id']}/post-call/retry", headers=rep.headers)
    assert again.status_code == 409


async def test_follow_up_review_flow_never_sends(client: AsyncClient, rep: Account) -> None:
    call = await completed_call(client, rep)
    drafts = (await post_call(client, rep, call["id"]))["drafts"]
    email = next(d for d in drafts if d["channel"] == "EMAIL")
    url = f"/api/v1/calls/{call['id']}/follow-ups/{email['id']}"
    edited = await client.patch(url, headers=rep.headers, json={"body": "Edited by me"})
    assert edited.json()["body"] == "Edited by me"
    assert edited.json()["status"] == "DRAFT"
    approved = await client.patch(url, headers=rep.headers, json={"status": "APPROVED"})
    assert approved.json()["status"] == "APPROVED"
    copied = await client.patch(url, headers=rep.headers, json={"status": "COPIED"})
    assert copied.json()["status"] == "COPIED"
    whatsapp = next(d for d in drafts if d["channel"] == "WHATSAPP")
    await client.patch(
        f"/api/v1/calls/{call['id']}/follow-ups/{whatsapp['id']}",
        headers=rep.headers,
        json={"status": "DISCARDED"},
    )
    remaining = (await post_call(client, rep, call["id"]))["drafts"]
    assert whatsapp["id"] not in {d["id"] for d in remaining}
    bad = await client.patch(url, headers=rep.headers, json={"status": "SENT"})
    assert bad.status_code == 422
    # There is no endpoint that sends messages to customers.
    paths = app.openapi()["paths"]
    assert not any("send" in p for p in paths)


async def test_post_call_tenant_isolation(
    client: AsyncClient, rep: Account, register: Register
) -> None:
    call = await completed_call(client, rep)
    draft = (await post_call(client, rep, call["id"]))["drafts"][0]
    other = await register(client, "other@example.com", "Other Co")
    for method, url, body in [
        ("GET", f"/api/v1/calls/{call['id']}/post-call", None),
        ("POST", f"/api/v1/calls/{call['id']}/post-call/retry", None),
        ("PATCH", f"/api/v1/calls/{call['id']}/follow-ups/{draft['id']}", {"status": "APPROVED"}),
    ]:
        resp = await client.request(method, url, headers=other.headers, json=body)
        assert resp.status_code == 404, url


async def test_next_call_prep_uses_history(client: AsyncClient, rep: Account) -> None:
    call = await completed_call(client, rep)
    nxt = (
        await client.post(
            "/api/v1/calls", headers=rep.headers, json={"contact_id": call["contact_id"]}
        )
    ).json()
    prep = (await client.get(f"/api/v1/calls/{nxt['id']}/prep", headers=rep.headers)).json()
    assert prep["previous_calls"][0]["id"] == call["id"]
    assert prep["previous_calls"][0]["summary"].startswith("Call with Ravi Kumar")
    assert any("5 branches" in r["text"] for r in prep["known_requirements"])
    assert any(o["kind"] == "OBJECTION" for o in prep["known_objections"])


async def test_call_history_filters(client: AsyncClient, rep: Account) -> None:
    call = await completed_call(client, rep)
    listing = await client.get(
        "/api/v1/calls",
        headers=rep.headers,
        params={"status": "COMPLETED", "user_id": str(rep.user_id)},
    )
    assert [c["id"] for c in listing.json()["items"]] == [call["id"]]
    future = await client.get(
        "/api/v1/calls", headers=rep.headers, params={"from": "2100-01-01T00:00:00+00:00"}
    )
    assert future.json()["total"] == 0
    naive = await client.get("/api/v1/calls", headers=rep.headers, params={"from": "2100-01-01"})
    assert naive.status_code == 422


async def test_dashboard(client: AsyncClient, rep: Account) -> None:
    call = await completed_call(client, rep)
    dash = await client.get("/api/v1/dashboard", headers=rep.headers)
    assert dash.status_code == 200, dash.text
    body = dash.json()
    assert body["calls_today"] >= 1
    assert body["calls_this_week"] >= 1
    assert [c["id"] for c in body["calls_needing_follow_up"]] == [call["id"]]
    assert body["recent_customers"][0]["name"] == "Ravi Kumar"
    assert body["recent_activity"]
    assert body["recent_activity"][0]["contact_name"] == "Ravi Kumar"
    bad = await client.get("/api/v1/dashboard", headers=rep.headers, params={"tz": "Mars/Base"})
    assert bad.status_code == 400
