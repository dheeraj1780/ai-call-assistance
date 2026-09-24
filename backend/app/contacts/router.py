import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.db import get_db_session
from app.common.errors import ErrorResponse
from app.common.rate_limit import client_ip
from app.common.schemas import Page, PageParams
from app.contacts import service
from app.contacts.models import ContactStatus
from app.contacts.repository import ContactNoteRepository, ContactRepository
from app.contacts.schemas import (
    ContactCreate,
    ContactOut,
    ContactUpdate,
    NoteCreate,
    NoteOut,
    NoteUpdate,
)
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory
from app.timeline.schemas import TimelineEventOut, TimelinePage

router = APIRouter(
    prefix="/contacts",
    tags=["contacts"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)


@router.get("", response_model=Page[ContactOut])
async def list_contacts(
    page: PageParams = Depends(),
    q: str | None = Query(default=None, max_length=200),
    status_filter: ContactStatus | None = Query(default=None, alias="status"),
    tag: str | None = Query(default=None, max_length=40),
    owner_user_id: uuid.UUID | None = None,
    sort: Literal["-updated_at", "-created_at", "name"] = "-updated_at",
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Page[ContactOut]:
    repo = ContactRepository(session)
    stmt = repo.search(
        principal.company_id,
        q=q,
        status=status_filter,
        tag=tag,
        owner_user_id=owner_user_id,
        sort=sort,
    )
    rows, total = await repo.page(stmt, limit=page.limit, offset=page.offset)
    return Page(
        items=[ContactOut.model_validate(c) for c in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ContactOut)
async def create_contact(
    body: ContactCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ContactOut:
    contact = await service.create_contact(session, principal, body, ip=client_ip(request))
    return ContactOut.model_validate(contact)


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ContactOut:
    return ContactOut.model_validate(await service.get_contact(session, principal, contact_id))


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ContactOut:
    contact = await service.update_contact(
        session, principal, contact_id, body, ip=client_ip(request)
    )
    return ContactOut.model_validate(contact)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.delete_contact(session, principal, contact_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Timeline --------------------------------------------------------------------------


@router.get("/{contact_id}/timeline", response_model=TimelinePage)
async def get_timeline(
    contact_id: uuid.UUID,
    category: list[TimelineCategory] | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None, max_length=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> TimelinePage:
    await service.get_contact(session, principal, contact_id)  # 404 if not in tenant
    events, next_cursor = await timeline.list_events(
        session,
        company_id=principal.company_id,
        contact_id=contact_id,
        categories=category,
        limit=limit,
        before=before,
    )
    return TimelinePage(
        items=[TimelineEventOut.model_validate(e) for e in events], next_cursor=next_cursor
    )


# ---- Notes -----------------------------------------------------------------------------


@router.get("/{contact_id}/notes", response_model=Page[NoteOut])
async def list_notes(
    contact_id: uuid.UUID,
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Page[NoteOut]:
    await service.get_contact(session, principal, contact_id)
    repo = ContactNoteRepository(session)
    rows, total = await repo.page(
        repo.for_contact(principal.company_id, contact_id), limit=page.limit, offset=page.offset
    )
    return Page(
        items=[NoteOut.model_validate(n) for n in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/{contact_id}/notes", status_code=status.HTTP_201_CREATED, response_model=NoteOut)
async def add_note(
    contact_id: uuid.UUID,
    body: NoteCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> NoteOut:
    return NoteOut.model_validate(await service.add_note(session, principal, contact_id, body))


@router.patch("/{contact_id}/notes/{note_id}", response_model=NoteOut)
async def update_note(
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    body: NoteUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> NoteOut:
    note = await service.update_note(session, principal, contact_id, note_id, body)
    return NoteOut.model_validate(note)


@router.delete("/{contact_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.delete_note(session, principal, contact_id, note_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
