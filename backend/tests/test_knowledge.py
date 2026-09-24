"""Knowledge base: upload validation, processing, grounded retrieval, tenant isolation."""

import io
import zipfile
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.common.db import TenantContext
from app.jobs.service import drain
from app.knowledge.models import KnowledgeChunk
from app.knowledge.service import NOT_FOUND_ANSWER
from app.tenants.models import MemberRole
from tests.conftest import DEFAULT_PASSWORD, Account, add_member_directly, open_session

Register = Callable[..., Awaitable[Account]]

POLICY = (
    "Acme Inventory Suite product guide.\n\n"
    "Multi-branch support: Acme supports multiple branches and warehouses with a single "
    "dashboard. Stock transfers between branches are tracked automatically.\n\n"
    "GST invoicing: Acme generates GST compliant invoices and e-way bills.\n\n"
    "Onboarding: our team migrates your Excel data and trains your staff in two days."
)


@pytest.fixture
async def admin(client: AsyncClient, register: Register) -> Account:
    return await register(client, "admin@example.com", "Acme")


async def upload(
    client: AsyncClient, acct: Account, name: str, data: bytes, title: str | None = None
) -> "object":
    files = {"file": (name, data, "application/octet-stream")}
    form = {"title": title} if title else None
    return await client.post(
        "/api/v1/knowledge/documents", headers=acct.headers, files=files, data=form
    )


async def test_upload_process_and_answer(client: AsyncClient, admin: Account) -> None:
    resp = await upload(client, admin, "guide.txt", POLICY.encode(), "Product guide")
    assert resp.status_code == 201, resp.text  # type: ignore[attr-defined]
    doc = resp.json()  # type: ignore[attr-defined]
    assert doc["status"] == "PROCESSING"
    assert doc["chunk_count"] >= 1
    assert await drain() >= 1
    ready = (
        await client.get(f"/api/v1/knowledge/documents/{doc['id']}", headers=admin.headers)
    ).json()
    assert ready["status"] == "READY"
    assert ready["embedding_model"] == "hashing:hashing-v1-1024"

    answer = await client.post(
        "/api/v1/knowledge/query",
        headers=admin.headers,
        json={"question": "Do you support multiple branches?"},
    )
    body = answer.json()
    assert body["supported"] is True
    assert body["label"] == "COMPANY KNOWLEDGE"
    assert "multiple branches" in body["answer"].lower()
    assert body["sources"][0]["title"] == "Product guide"

    unknown = await client.post(
        "/api/v1/knowledge/query",
        headers=admin.headers,
        json={"question": "What is the refund policy for cancelled subscriptions in Dubai?"},
    )
    assert unknown.json()["answer"] == NOT_FOUND_ANSWER
    assert unknown.json()["supported"] is False


async def test_docx_upload(client: AsyncClient, admin: Account) -> None:
    from docx import Document

    buf = io.BytesIO()
    document = Document()
    document.add_paragraph("Warranty: all hardware carries a one year warranty.")
    document.save(buf)
    resp = await upload(client, admin, "warranty.docx", buf.getvalue())
    assert resp.status_code == 201, resp.text  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("virus.exe", b"MZ\x90\x00", "unsupported_type"),
        ("fake.pdf", b"just text pretending", "content_mismatch"),
        ("fake.docx", b"not a zip", "content_mismatch"),
        ("empty.txt", b"   \n  ", "no_text"),
        ("binary.txt", b"abc\x00def", "binary_text"),
        ("latin1.txt", "café".encode("latin-1"), "not_utf8"),
        ("broken.pdf", b"%PDF-1.4 garbage", "unreadable_pdf"),
    ],
)
async def test_upload_validation(
    client: AsyncClient, admin: Account, name: str, data: bytes, code: str
) -> None:
    resp = await upload(client, admin, name, data)
    assert resp.status_code == 415, resp.text  # type: ignore[attr-defined]
    assert resp.json()["error"]["code"] == code  # type: ignore[attr-defined]


async def test_zip_bomb_rejected(client: AsyncClient, admin: Account) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<w/>")
        zf.writestr("bomb.bin", b"\x00" * (70 * 1024 * 1024))
    resp = await upload(client, admin, "bomb.docx", buf.getvalue())
    assert resp.json()["error"]["code"] == "docx_too_large"  # type: ignore[attr-defined]


async def test_size_limit(
    client: AsyncClient, admin: Account, settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_max_upload_bytes", 1024)
    resp = await upload(client, admin, "big.txt", b"a" * 2048)
    assert resp.status_code == 413  # type: ignore[attr-defined]


async def test_only_admins_manage_knowledge(client: AsyncClient, admin: Account) -> None:
    await add_member_directly(admin.company_id, "rep@example.com", MemberRole.MEMBER)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "rep@example.com", "password": DEFAULT_PASSWORD}
    )
    rep = Account(
        "rep@example.com",
        DEFAULT_PASSWORD,
        login.json()["access_token"],
        admin.user_id,
        admin.company_id,
        "MEMBER",
    )
    assert (await upload(client, rep, "x.txt", b"hello world")).status_code == 403  # type: ignore[attr-defined]
    doc = (await upload(client, admin, "x.txt", b"hello world")).json()  # type: ignore[attr-defined]
    assert (await client.get("/api/v1/knowledge/documents", headers=rep.headers)).status_code == 200
    assert (
        await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=rep.headers)
    ).status_code == 403


async def test_knowledge_is_tenant_isolated(
    client: AsyncClient, admin: Account, register: Register
) -> None:
    doc = (await upload(client, admin, "guide.txt", POLICY.encode(), "Product guide")).json()  # type: ignore[attr-defined]
    await drain()
    other = await register(client, "other@example.com", "Other Co")
    answer = await client.post(
        "/api/v1/knowledge/query",
        headers=other.headers,
        json={"question": "Do you support multiple branches?"},
    )
    assert answer.json()["answer"] == NOT_FOUND_ANSWER
    assert answer.json()["sources"] == []
    for method in ("GET", "DELETE"):
        resp = await client.request(
            method, f"/api/v1/knowledge/documents/{doc['id']}", headers=other.headers
        )
        assert resp.status_code == 404
    assert (await client.get("/api/v1/knowledge/documents", headers=other.headers)).json() == []
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0


async def test_delete_removes_chunks(client: AsyncClient, admin: Account) -> None:
    doc = (await upload(client, admin, "guide.txt", POLICY.encode())).json()  # type: ignore[attr-defined]
    assert (
        await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=admin.headers)
    ).status_code == 204
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
    assert await drain() >= 0  # embedding job for a deleted document is a no-op


async def test_knowledge_surfaces_during_live_call(client: AsyncClient, admin: Account) -> None:
    await upload(client, admin, "guide.txt", POLICY.encode(), "Product guide")
    await drain()
    from tests.test_realtime import run_default_simulation, snapshot, started_call

    await client.patch("/api/v1/me", headers=admin.headers, json={"phone": "+919800000001"})
    call = await started_call(client, admin, agenda=False)
    await run_default_simulation(call)
    snap = await snapshot(client, admin, call["id"])
    kb = [
        i for i in snap["insights"] if i["type"] == "KNOWLEDGE_RESULT" and i["priority"] == "HIGH"
    ]
    assert kb, snap["insights"]
    assert kb[0]["context"] == "Company knowledge: Product guide"
    assert "branches" in kb[0]["content"].lower()
