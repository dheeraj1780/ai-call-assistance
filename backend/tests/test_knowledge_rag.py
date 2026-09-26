"""Knowledge base for the POC: PDF/DOCX/Markdown ingestion, chunking, duplicates, failed
ingestion + re-processing, embedding-model changes, similarity threshold and tenant isolation.
Embeddings use the offline hashing provider or fakes - no external API is called."""

import io
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ai import embeddings
from app.common.config import get_settings
from app.common.db import TenantContext
from app.jobs.service import drain
from app.knowledge.extraction import CHUNK_TARGET, chunk_text
from app.knowledge.models import KnowledgeChunk
from app.knowledge.service import retrieve
from tests.conftest import Account, open_session
from tests.test_knowledge import POLICY, upload

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def admin(client: AsyncClient, register: Register) -> Account:
    return await register(client, "kb-admin@example.com", "Acme")


def text_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF with a text layer (no external tools)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


async def chunks_of(acct: Account, document_id: str) -> list[str]:
    session = await open_session(TenantContext(company_id=acct.company_id))
    async with session:
        rows = await session.scalars(
            select(KnowledgeChunk.content)
            .where(KnowledgeChunk.document_id == uuid.UUID(document_id))
            .order_by(KnowledgeChunk.ordinal)
        )
        return list(rows.all())


async def doc_after_jobs(client: AsyncClient, acct: Account, doc_id: str) -> dict[str, Any]:
    await drain()
    body: dict[str, Any] = (
        await client.get(f"/api/v1/knowledge/documents/{doc_id}", headers=acct.headers)
    ).json()
    return body


async def uploaded(client: AsyncClient, acct: Account, name: str, data: bytes) -> dict[str, Any]:
    resp: Any = await upload(client, acct, name, data)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_pdf_ingestion_extracts_text(client: AsyncClient, admin: Account) -> None:
    doc = await uploaded(client, admin, "price-list.pdf", text_pdf("Annual plan costs Rs 30000"))
    assert doc["mime_type"] == "application/pdf"
    assert (await doc_after_jobs(client, admin, doc["id"]))["status"] == "READY"
    assert "Annual plan costs Rs 30000" in " ".join(await chunks_of(admin, doc["id"]))


async def test_docx_and_markdown_ingestion(client: AsyncClient, admin: Account) -> None:
    from docx import Document

    buf = io.BytesIO()
    document = Document()
    document.add_paragraph("Warranty: all hardware carries a one year warranty.")
    document.save(buf)
    docx = await uploaded(client, admin, "warranty.docx", buf.getvalue())
    md_doc = await uploaded(
        client,
        admin,
        "faq.md",
        b"# Delivery\n\n**Pan-India delivery** in 5-7 days. See [terms](https://example.com/t).\n",
    )
    assert md_doc["mime_type"] == "text/markdown"
    await drain()
    assert "one year warranty" in " ".join(await chunks_of(admin, docx["id"]))
    md_text = " ".join(await chunks_of(admin, md_doc["id"]))
    assert "Pan-India delivery in 5-7 days" in md_text
    assert "terms (https://example.com/t)" in md_text
    assert "**" not in md_text
    assert "# " not in md_text


def test_long_text_is_chunked() -> None:
    paragraphs = [f"Paragraph {i}: " + "stock reconciliation detail. " * 20 for i in range(12)]
    chunks = chunk_text("\n\n".join(paragraphs))
    assert len(chunks) > 3
    assert all(len(c) <= CHUNK_TARGET + 200 for c in chunks)
    assert "Paragraph 11" in chunks[-1]


async def test_duplicate_upload_is_refused(client: AsyncClient, admin: Account) -> None:
    first = await uploaded(client, admin, "guide.txt", POLICY.encode())
    again: Any = await upload(client, admin, "guide-copy.txt", POLICY.encode(), "Another title")
    assert again.status_code == 409
    err = again.json()["error"]
    assert err["code"] == "knowledge_duplicate"
    assert err["details"]["document_id"] == first["id"]
    docs = (await client.get("/api/v1/knowledge/documents", headers=admin.headers)).json()
    assert len(docs) == 1


class _Broken:
    name = "hashing"
    model = "hashing-v1-1024"

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        raise embeddings.EmbeddingError("provider down")


async def test_failed_ingestion_can_be_reprocessed(
    client: AsyncClient, admin: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "_provider", _Broken())
    doc = await uploaded(client, admin, "guide.txt", POLICY.encode())
    failed = await doc_after_jobs(client, admin, doc["id"])
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "embedding_failed"
    monkeypatch.setattr(embeddings, "_provider", embeddings.HashingEmbeddingProvider())
    again = await client.post(
        f"/api/v1/knowledge/documents/{doc['id']}/reprocess", headers=admin.headers
    )
    assert again.status_code == 200, again.text
    assert (await doc_after_jobs(client, admin, doc["id"]))["status"] == "READY"


async def test_failed_document_may_be_uploaded_again(
    client: AsyncClient, admin: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "_provider", _Broken())
    await uploaded(client, admin, "guide.txt", POLICY.encode())
    await drain()
    # Not a duplicate of a usable document: the same file can be tried again.
    await uploaded(client, admin, "guide.txt", POLICY.encode())


async def test_retrieval_threshold_and_tenant_isolation(
    client: AsyncClient, admin: Account, register: Register
) -> None:
    await uploaded(client, admin, "guide.txt", POLICY.encode())
    await drain()
    threshold = get_settings().knowledge_score_threshold
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        relevant = await retrieve(session, admin.company_id, "Do you generate GST invoices?")
        unrelated = await retrieve(session, admin.company_id, "football world cup schedule")
    assert relevant[0].score >= threshold
    assert "GST" in relevant[0].content
    assert all(r.score < threshold for r in unrelated)
    other = await register(client, "rival@example.com", "Rival Co")
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        assert await retrieve(session, other.company_id, "Do you generate GST invoices?") == []
        # Even naming the other tenant's company id returns nothing (RLS backstop).
        assert await retrieve(session, admin.company_id, "Do you generate GST invoices?") == []


async def test_changing_embedding_model_requires_reprocessing(
    client: AsyncClient, admin: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await uploaded(client, admin, "guide.txt", POLICY.encode())
    await drain()

    class NewModel(embeddings.HashingEmbeddingProvider):
        name = "gemini"
        model = "gemini-embedding-001"

    monkeypatch.setattr(embeddings, "_provider", NewModel())
    listed = (await client.get("/api/v1/knowledge/documents", headers=admin.headers)).json()
    assert listed[0]["needs_reprocess"] is True
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        assert await retrieve(session, admin.company_id, "GST invoices") == []  # not comparable
    await client.post(f"/api/v1/knowledge/documents/{doc['id']}/reprocess", headers=admin.headers)
    ready = await doc_after_jobs(client, admin, doc["id"])
    assert ready["embedding_model"] == "gemini:gemini-embedding-001"
    assert ready["needs_reprocess"] is False
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        assert await retrieve(session, admin.company_id, "GST invoices")


async def test_deleting_a_document_removes_it_from_retrieval(
    client: AsyncClient, admin: Account
) -> None:
    doc = await uploaded(client, admin, "guide.txt", POLICY.encode())
    await drain()
    resp = await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=admin.headers)
    assert resp.status_code == 204
    session = await open_session(TenantContext(company_id=admin.company_id))
    async with session:
        assert await retrieve(session, admin.company_id, "GST invoices") == []
