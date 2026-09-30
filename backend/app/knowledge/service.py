"""Knowledge base use cases: upload (extract + chunk), embedding job, tenant-scoped retrieval
and grounded answers.

Grounding rules: answers may only use retrieved chunks of the caller's own company. If
retrieval confidence is low, or the model cannot support an answer from the chunks, the
result is "Information not found in company knowledge." - never an invented policy/price.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingError, get_embedding_provider
from app.ai.gateway import get_ai_gateway
from app.ai.mock_provider import register_mock
from app.ai.models import AIUsageRecord
from app.ai.provider import AIError, AITask
from app.ai.safety import UNTRUSTED_DATA_RULES, data_block
from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.jobs.service import enqueue, register_job
from app.knowledge.extraction import ExtractionError, chunk_text, extract
from app.knowledge.models import DocumentStatus, KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger(__name__)

NOT_FOUND_ANSWER = "Information not found in company knowledge."
EMBED_BATCH = 64


class UploadRejectedError(AppError):
    status_code = 415
    code = "unsupported_document"


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise ForbiddenError("Only owners and admins can manage company knowledge")


async def upload(
    session: AsyncSession,
    principal: Principal,
    *,
    filename: str,
    data: bytes,
    title: str | None,
    ip: str | None,
) -> KnowledgeDocument:
    _require_admin(principal)
    settings = get_settings()
    if len(data) > settings.knowledge_max_upload_bytes:
        raise AppError(
            f"Files are limited to {settings.knowledge_max_upload_bytes // (1024 * 1024)} MB",
            code="file_too_large",
        ).with_status(413)
    try:
        extracted = extract(filename, data)
    except ExtractionError as exc:
        raise UploadRejectedError(str(exc), code=exc.code) from None

    digest = hashlib.sha256(data).hexdigest()
    # Two concurrent uploads of the same file must not both pass the duplicate check: serialise
    # per (company, checksum) for the rest of this transaction.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended(f"{principal.company_id}:{digest}", 0))
        )
    )
    existing = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.company_id == principal.company_id,
            KnowledgeDocument.sha256 == digest,
            KnowledgeDocument.status != DocumentStatus.FAILED.value,
        )
    )
    if existing is not None:
        # Same bytes already in the knowledge base: never create duplicate chunks.
        raise ConflictError(
            f'This file is already in the knowledge base as "{existing.title}".',
            code="knowledge_duplicate",
            details={"document_id": str(existing.id)},
        )

    safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1][:255] or "document"
    doc_title = (title or safe_name.rsplit(".", 1)[0]).strip()[:200] or "Untitled"
    previous = await session.scalar(
        select(func.max(KnowledgeDocument.version)).where(
            KnowledgeDocument.company_id == principal.company_id,
            KnowledgeDocument.title == doc_title,
        )
    )
    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        title=doc_title,
        filename=safe_name,
        mime_type=extracted.mime_type,
        size_bytes=len(data),
        sha256=digest,
        version=(previous or 0) + 1,
        status=DocumentStatus.UPLOADED.value,
        uploaded_by_user_id=principal.user_id,
    )
    session.add(doc)
    await session.flush()
    pieces = chunk_text(extracted.text)
    for ordinal, piece in enumerate(pieces):
        session.add(
            KnowledgeChunk(
                company_id=principal.company_id, document_id=doc.id, ordinal=ordinal, content=piece
            )
        )
    doc.chunk_count = len(pieces)
    doc.status = DocumentStatus.PROCESSING.value
    await enqueue(
        session,
        "knowledge.embed",
        company_id=principal.company_id,
        payload={"document_id": str(doc.id)},
        dedupe_key=f"knowledge:{doc.id}",
    )
    audit.record(
        session,
        "knowledge.uploaded",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="knowledge_document",
        entity_id=doc.id,
        ip=ip,
        details={"chunks": len(pieces), "bytes": len(data)},
    )
    await session.commit()
    return doc


@register_job("knowledge.embed")
async def embed_document(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    if company_id is None:
        return
    document_id = uuid.UUID(payload["document_id"])
    provider = get_embedding_provider()
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        doc = await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.company_id == company_id, KnowledgeDocument.id == document_id
            )
        )
        if doc is None:  # deleted before processing
            return
        chunks = list(
            (
                await session.scalars(
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.company_id == company_id,
                        KnowledgeChunk.document_id == document_id,
                        KnowledgeChunk.embedding.is_(None),
                    )
                    .order_by(KnowledgeChunk.ordinal)
                )
            ).all()
        )
        try:
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[i : i + EMBED_BATCH]
                vectors = await provider.embed([c.content for c in batch], "document")
                for chunk, vector in zip(batch, vectors, strict=True):
                    chunk.embedding = vector
        except EmbeddingError as exc:
            doc.status = DocumentStatus.FAILED.value
            doc.error_code = "embedding_failed"
            await session.commit()
            logger.warning("knowledge_embedding_failed", extra={"error": str(exc)})
            return
        doc.status = DocumentStatus.READY.value
        doc.error_code = None
        doc.embedding_model = f"{provider.name}:{provider.model}"
        doc.processed_at = datetime.now(UTC)
        session.add(
            AIUsageRecord(
                company_id=company_id,
                task="EMBEDDING",
                provider=provider.name,
                model=provider.model,
                input_tokens=sum(len(c.content) // 4 for c in chunks),
                output_tokens=0,
                units=len(chunks),
                latency_ms=0,
                estimated_cost_usd=0,
                success=True,
            )
        )
        await session.commit()


def current_embedding_model() -> str:
    provider = get_embedding_provider()
    return f"{provider.name}:{provider.model}"


async def reprocess_document(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID, *, ip: str | None
) -> KnowledgeDocument:
    """Re-embed a document (after a failure, or after EMBEDDING_PROVIDER/model changed - vectors
    from different models are not comparable). The stored chunks are reused."""
    _require_admin(principal)
    doc = await get_document(session, principal, document_id)
    await session.execute(
        update(KnowledgeChunk)
        .where(
            KnowledgeChunk.company_id == principal.company_id, KnowledgeChunk.document_id == doc.id
        )
        .values(embedding=None)
    )
    doc.status = DocumentStatus.PROCESSING.value
    doc.error_code = None
    await enqueue(
        session,
        "knowledge.embed",
        company_id=principal.company_id,
        payload={"document_id": str(doc.id)},
        dedupe_key=f"knowledge:{doc.id}:{uuid.uuid4().hex}",
    )
    audit.record(
        session,
        "knowledge.reprocessed",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="knowledge_document",
        entity_id=doc.id,
        ip=ip,
    )
    await session.commit()
    return doc


async def delete_document(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID, *, ip: str | None
) -> None:
    _require_admin(principal)
    doc = await get_document(session, principal, document_id)
    await session.delete(doc)
    audit.record(
        session,
        "knowledge.deleted",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="knowledge_document",
        entity_id=document_id,
        ip=ip,
    )
    await session.commit()


async def get_document(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID
) -> KnowledgeDocument:
    doc = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.company_id == principal.company_id,
            KnowledgeDocument.id == document_id,
        )
    )
    if doc is None:
        raise NotFoundError("Document not found")
    return doc


# ---- Retrieval --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    content: str
    score: float


_SEARCH_SQL = text(
    """
    SELECT c.id, c.document_id, d.title, c.content,
           1 - (c.embedding <=> CAST(:q AS vector)) AS score
    FROM knowledge_chunks c
    JOIN knowledge_documents d ON d.company_id = c.company_id AND d.id = c.document_id
    WHERE c.company_id = :company_id AND d.status = 'READY' AND c.embedding IS NOT NULL
      AND d.embedding_model = :model
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


async def retrieve(
    session: AsyncSession, company_id: uuid.UUID, query: str, *, k: int = 4
) -> list[RetrievedChunk]:
    """Tenant-scoped vector search (explicit company filter; RLS is the backstop). Only chunks
    embedded with the current embedding model are compared with the query vector."""
    provider = get_embedding_provider()
    vector = (await provider.embed([query], "query"))[0]
    rows = await session.execute(
        _SEARCH_SQL,
        {
            "q": str(vector),
            "company_id": company_id,
            "k": k,
            "model": f"{provider.name}:{provider.model}",
        },
    )
    return [RetrievedChunk(r.id, r.document_id, r.title, r.content, float(r.score)) for r in rows]


class KnowledgeAnswerContext(BaseModel):
    question: str
    chunks: list[dict[str, str]]


class KnowledgeAnswer(BaseModel):
    supported: bool
    answer: Annotated[str, Field(max_length=1500)]
    source_chunk_ids: list[str] = []


ANSWER_SYSTEM = (
    "You answer a salesperson's question using ONLY the provided company knowledge excerpts. "
    "If the excerpts do not clearly answer the question, set supported=false and do not "
    "guess. Never invent prices, discounts, policies, deadlines or features. Cite the chunk "
    "ids you used. " + UNTRUSTED_DATA_RULES
)


@register_mock(AITask.KNOWLEDGE_ANSWER)
def _mock_answer(context: BaseModel | None) -> KnowledgeAnswer:
    """Extractive: returns the best chunk's most relevant sentence if it shares words."""
    if not isinstance(context, KnowledgeAnswerContext) or not context.chunks:
        return KnowledgeAnswer(supported=False, answer=NOT_FOUND_ANSWER)
    q_words = {w for w in context.question.lower().split() if len(w) > 3}
    best = context.chunks[0]
    sentences = [s.strip() for s in best["content"].replace("\n", " ").split(". ") if s.strip()]
    scored = sorted(sentences, key=lambda s: -len(q_words & set(s.lower().split())))
    if not scored or not (q_words & set(scored[0].lower().split())):
        return KnowledgeAnswer(supported=False, answer=NOT_FOUND_ANSWER)
    return KnowledgeAnswer(supported=True, answer=scored[0][:500], source_chunk_ids=[best["id"]])


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    supported: bool
    sources: list[RetrievedChunk]
    provider: str | None


async def answer_question(
    session: AsyncSession, company_id: uuid.UUID, question: str, *, call_id: uuid.UUID | None = None
) -> AnswerResult:
    settings = get_settings()
    hits = [
        c
        for c in await retrieve(session, company_id, question)
        if c.score >= settings.knowledge_score_threshold
    ]
    if not hits:
        return AnswerResult(NOT_FOUND_ANSWER, False, [], None)
    ctx = KnowledgeAnswerContext(
        question=question,
        chunks=[{"id": str(c.chunk_id), "title": c.title, "content": c.content} for c in hits],
    )
    prompt = (
        data_block("question", question)
        + "\n\n"
        + "\n\n".join(data_block(f"chunk_{c.chunk_id}", f"[{c.title}]\n{c.content}") for c in hits)
    )
    try:
        result = await get_ai_gateway().run(
            company_id=company_id,
            call_id=call_id,
            task=AITask.KNOWLEDGE_ANSWER,
            system=ANSWER_SYSTEM,
            prompt=prompt,
            schema=KnowledgeAnswer,
            context=ctx,
            max_tokens=800,
        )
    except AIError:
        # Degrade to showing the retrieved excerpt itself, clearly labelled.
        return AnswerResult(hits[0].content[:500], True, hits[:1], None)
    out = result.output
    valid = {str(c.chunk_id) for c in hits}
    cited = [c for c in hits if str(c.chunk_id) in set(out.source_chunk_ids) & valid]
    if not out.supported or not cited:
        return AnswerResult(NOT_FOUND_ANSWER, False, [], result.usage.provider)
    return AnswerResult(out.answer, True, cited, result.usage.provider)
