import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.config import get_settings
from app.common.db import get_db_session
from app.common.errors import AppError, ErrorResponse
from app.common.rate_limit import client_ip, enforce_ai_rate_limit
from app.knowledge import service
from app.knowledge.models import KnowledgeDocument

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
    },
)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    filename: str
    mime_type: str
    size_bytes: int
    version: int
    source: str
    status: str
    error_code: str | None
    chunk_count: int
    embedding_model: str | None
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime
    processed_at: datetime | None
    # READY but embedded with another model than the current EMBEDDING_PROVIDER: not searched
    # until re-processed.
    needs_reprocess: bool = False


def _out(doc: KnowledgeDocument) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.needs_reprocess = (
        doc.status == "READY" and doc.embedding_model != service.current_embedding_model()
    )
    return out


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class SourceOut(BaseModel):
    document_id: uuid.UUID
    title: str
    snippet: str
    score: float


class AnswerOut(BaseModel):
    answer: str
    supported: bool
    label: str
    sources: list[SourceOut]


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> list[DocumentOut]:
    rows = await session.scalars(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.company_id == principal.company_id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    return [_out(d) for d in rows.all()]


@router.post("/documents", status_code=status.HTTP_201_CREATED, response_model=DocumentOut)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentOut:
    limit = get_settings().knowledge_max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise AppError("File is too large", code="file_too_large").with_status(413)
    doc = await service.upload(
        session,
        principal,
        filename=file.filename or "document",
        data=data,
        title=title,
        ip=client_ip(request),
    )
    return _out(doc)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentOut:
    return _out(await service.get_document(session, principal, document_id))


@router.post("/documents/{document_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    document_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentOut:
    """Re-embed a document (failed, or embedded with a previous embedding model)."""
    doc = await service.reprocess_document(session, principal, document_id, ip=client_ip(request))
    return _out(doc)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.delete_document(session, principal, document_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/query", response_model=AnswerOut)
async def query(
    body: QueryIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> AnswerOut:
    enforce_ai_rate_limit(principal.company_id)
    result = await service.answer_question(session, principal.company_id, body.question)
    return AnswerOut(
        answer=result.answer,
        supported=result.supported,
        label="COMPANY KNOWLEDGE" if result.supported else "NOT FOUND",
        sources=[
            SourceOut(
                document_id=s.document_id,
                title=s.title,
                snippet=s.content[:300],
                score=round(s.score, 3),
            )
            for s in result.sources
        ],
    )
