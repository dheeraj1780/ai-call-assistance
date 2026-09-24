import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items import service
from app.action_items.models import ActionItemKind, ActionItemStatus
from app.action_items.repository import ActionItemRepository
from app.action_items.schemas import ActionItemCreate, ActionItemOut, ActionItemUpdate
from app.auth.dependencies import Principal, get_principal
from app.common.db import get_db_session
from app.common.errors import ErrorResponse
from app.common.rate_limit import client_ip
from app.common.schemas import Page, PageParams

router = APIRouter(
    prefix="/action-items",
    tags=["action-items"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)


@router.get("", response_model=Page[ActionItemOut])
async def list_action_items(
    page: PageParams = Depends(),
    status_filter: list[ActionItemStatus] | None = Query(default=None, alias="status"),
    kind: ActionItemKind | None = None,
    assignee_user_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    call_id: uuid.UUID | None = None,
    confirmed: bool | None = None,
    due_before: AwareDatetime | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Page[ActionItemOut]:
    repo = ActionItemRepository(session)
    stmt = repo.search(
        principal.company_id,
        statuses=status_filter,
        kind=kind,
        assignee_user_id=assignee_user_id,
        contact_id=contact_id,
        call_id=call_id,
        confirmed=confirmed,
        due_before=due_before,
    )
    rows, total = await repo.page(stmt, limit=page.limit, offset=page.offset)
    return Page(
        items=[ActionItemOut.model_validate(i) for i in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ActionItemOut)
async def create_action_item(
    body: ActionItemCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ActionItemOut:
    item = await service.create_item(session, principal, body, ip=client_ip(request))
    return ActionItemOut.model_validate(item)


@router.get("/{item_id}", response_model=ActionItemOut)
async def get_action_item(
    item_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ActionItemOut:
    return ActionItemOut.model_validate(await service.get_item(session, principal, item_id))


@router.patch("/{item_id}", response_model=ActionItemOut)
async def update_action_item(
    item_id: uuid.UUID,
    body: ActionItemUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ActionItemOut:
    item = await service.update_item(session, principal, item_id, body, ip=client_ip(request))
    return ActionItemOut.model_validate(item)


@router.post("/{item_id}/confirm", response_model=ActionItemOut)
async def confirm_action_item(
    item_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ActionItemOut:
    item = await service.confirm_item(session, principal, item_id, ip=client_ip(request))
    return ActionItemOut.model_validate(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_action_item(
    item_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.delete_item(session, principal, item_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
