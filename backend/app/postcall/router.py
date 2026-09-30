import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.calls.schemas import CallOut
from app.common.db import get_db_session
from app.common.errors import ErrorResponse, NotFoundError
from app.common.rate_limit import client_ip
from app.postcall import service
from app.postcall.models import DraftStatus

router = APIRouter(
    prefix="/calls/{call_id}",
    tags=["post-call"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)


class GroundedFieldOut(BaseModel):
    value: str | None
    status: str


class SummaryOut(BaseModel):
    status: str
    provider: str | None
    error_code: str | None
    summary: str | None
    suggested_outcome: str | None
    current_solution: GroundedFieldOut
    budget: GroundedFieldOut
    timeline: GroundedFieldOut
    decision_maker: GroundedFieldOut
    next_step: GroundedFieldOut
    generated_at: datetime | None
    # Provenance: a person edited the AI output (the original AI text is kept server-side).
    edited_at: datetime | None = None


class SummaryEdit(BaseModel):
    """Fields a person may change; only the ones sent are updated (null/empty clears)."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=8000)] | None = None
    suggested_outcome: str | None = None
    current_solution: Annotated[str, StringConstraints(max_length=500)] | None = None
    budget: Annotated[str, StringConstraints(max_length=500)] | None = None
    timeline: Annotated[str, StringConstraints(max_length=500)] | None = None
    decision_maker: Annotated[str, StringConstraints(max_length=500)] | None = None
    next_step: Annotated[str, StringConstraints(max_length=500)] | None = None


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    subject: str | None
    body: str
    status: str
    source: str
    warnings: list[str]
    reviewed_at: datetime | None


class PostCallOut(BaseModel):
    call: CallOut
    summary: SummaryOut | None
    drafts: list[DraftOut]
    # The system never sends messages; this makes the contract explicit to API clients.
    auto_send: bool = False


class DraftReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    body: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
        | None
    ) = None
    status: DraftStatus | None = None


def _summary_out(s: Any) -> SummaryOut | None:
    if s is None:
        return None
    fields = {
        name: GroundedFieldOut(value=getattr(s, name), status=getattr(s, f"{name}_status"))
        for name in ("current_solution", "budget", "timeline", "decision_maker", "next_step")
    }
    return SummaryOut(
        status=s.status,
        provider=s.provider,
        error_code=s.error_code,
        summary=s.summary,
        suggested_outcome=s.suggested_outcome,
        generated_at=s.generated_at,
        edited_at=s.edited_at,
        **fields,
    )


@router.get("/post-call", response_model=PostCallOut)
async def get_post_call(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> PostCallOut:
    bundle = await service.get_bundle(session, principal, call_id)
    return PostCallOut(
        call=CallOut.model_validate(bundle["call"]),
        summary=_summary_out(bundle["summary"]),
        drafts=[DraftOut.model_validate(d) for d in bundle["drafts"]],
    )


@router.post("/post-call/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_post_call(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.retry(session, principal, call_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.patch("/post-call/summary", response_model=SummaryOut)
async def edit_summary(
    call_id: uuid.UUID,
    body: SummaryEdit,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> SummaryOut:
    """Edit the AI-generated summary and its key fields (the user's version is the record)."""
    summary = await service.edit_summary(
        session,
        principal,
        call_id,
        body.model_dump(exclude_unset=True),
        ip=client_ip(request),
    )
    out = _summary_out(summary)
    if out is None:  # pragma: no cover - edit_summary returns an existing row
        raise NotFoundError("Summary not found")
    return out


@router.patch("/follow-ups/{draft_id}", response_model=DraftOut)
async def review_draft(
    call_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: DraftReview,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    draft = await service.review_draft(
        session,
        principal,
        call_id,
        draft_id,
        subject=body.subject,
        body=body.body,
        status=body.status,
    )
    return DraftOut.model_validate(draft)
