import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.calls import service
from app.calls.models import CallStatus
from app.calls.repository import CallRepository
from app.calls.schemas import CallCreate, CallOut, CallUpdate
from app.common.db import get_db_session
from app.common.errors import ErrorResponse
from app.common.rate_limit import client_ip
from app.common.schemas import Page, PageParams

router = APIRouter(
    prefix="/calls",
    tags=["calls"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)


@router.get("", response_model=Page[CallOut])
async def list_calls(
    page: PageParams = Depends(),
    contact_id: uuid.UUID | None = None,
    status_filter: CallStatus | None = Query(default=None, alias="status"),
    user_id: uuid.UUID | None = None,
    since: AwareDatetime | None = Query(default=None, alias="from"),
    until: AwareDatetime | None = Query(default=None, alias="to"),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Page[CallOut]:
    repo = CallRepository(session)
    stmt = repo.search(
        principal.company_id,
        contact_id=contact_id,
        status=status_filter,
        user_id=user_id,
        since=since,
        until=until,
    )
    rows, total = await repo.page(stmt, limit=page.limit, offset=page.offset)
    return Page(
        items=[CallOut.model_validate(c) for c in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CallOut)
async def create_call(
    body: CallCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallOut:
    call = await service.create_call(session, principal, body, ip=client_ip(request))
    return CallOut.model_validate(call)


@router.get("/{call_id}", response_model=CallOut)
async def get_call(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallOut:
    return CallOut.model_validate(await service.get_call(session, principal, call_id))


@router.patch("/{call_id}", response_model=CallOut)
async def update_call(
    call_id: uuid.UUID,
    body: CallUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallOut:
    call = await service.update_call(session, principal, call_id, body, ip=client_ip(request))
    return CallOut.model_validate(call)
