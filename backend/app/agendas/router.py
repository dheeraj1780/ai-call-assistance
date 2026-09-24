import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agendas import service
from app.agendas.schemas import (
    AgendaItemOut,
    AgendaItemStatusUpdate,
    AgendaReplace,
    AgendaSuggestionOut,
)
from app.ai.provider import AIBudgetExceededError, AIError
from app.auth.dependencies import Principal, get_principal
from app.call_prep.schemas import CallPrepOut
from app.call_prep.service import build_prep
from app.calls.service import get_call
from app.common.db import get_db_session
from app.common.errors import AppError, ErrorResponse
from app.common.rate_limit import enforce_ai_rate_limit
from app.live import session as live_sessions
from app.live.hub import hub

router = APIRouter(
    prefix="/calls/{call_id}",
    tags=["call-preparation"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)


class AIUnavailableAppError(AppError):
    status_code = 503
    code = "ai_unavailable"
    message = "AI assistance is temporarily unavailable. You can still edit the agenda manually."


@router.get("/prep", response_model=CallPrepOut)
async def get_prep(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallPrepOut:
    return await build_prep(session, principal, call_id)


@router.get("/agenda", response_model=list[AgendaItemOut])
async def get_agenda(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[AgendaItemOut]:
    call = await get_call(session, principal, call_id)
    items = await service.list_items(session, principal.company_id, call.id)
    return [AgendaItemOut.model_validate(i) for i in items]


@router.put("/agenda", response_model=list[AgendaItemOut])
async def replace_agenda(
    call_id: uuid.UUID,
    body: AgendaReplace,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[AgendaItemOut]:
    items = await service.replace_agenda(session, principal, call_id, body.items)
    return [AgendaItemOut.model_validate(i) for i in items]


@router.patch("/agenda/{item_id}", response_model=AgendaItemOut)
async def override_agenda_item(
    call_id: uuid.UUID,
    item_id: uuid.UUID,
    body: AgendaItemStatusUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> AgendaItemOut:
    item = await service.override_status(session, principal, call_id, item_id, body.status)
    running = live_sessions.get_session(item.call_id)
    if running is not None:
        running.engine.mark_manual(item.id, body.status)
    out = AgendaItemOut.model_validate(item)
    hub.publish(item.call_id, "agenda.updated", out.model_dump(mode="json"))
    return out


@router.post("/agenda/suggest", response_model=AgendaSuggestionOut)
async def suggest_agenda(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> AgendaSuggestionOut:
    """Returns an AI suggestion only; nothing is saved until the user PUTs an agenda."""
    enforce_ai_rate_limit(principal.company_id)
    try:
        return await service.suggest_agenda(session, principal, call_id)
    except AIBudgetExceededError:
        raise AIUnavailableAppError(
            "Today's AI usage limit for your company has been reached.", code="ai_budget_exceeded"
        ) from None
    except AIError:
        raise AIUnavailableAppError() from None
