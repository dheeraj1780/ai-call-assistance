import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.calendar import service
from app.calendar.provider import (
    CalendarAuthError,
    CalendarError,
    CalendarPermissionError,
    CalendarRateLimitError,
)
from app.calendar.schemas import (
    ConnectionOut,
    ConnectStart,
    EventCreate,
    EventOut,
    EventUpdate,
    ExternalEventOut,
)
from app.common.config import get_settings
from app.common.db import get_db_session
from app.common.errors import AppError, ErrorResponse
from app.common.rate_limit import client_ip

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/calendar",
    tags=["calendar"],
    responses={
        401: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)


def _translate(exc: CalendarError) -> AppError:
    if isinstance(exc, CalendarAuthError):
        return AppError(
            "Your calendar connection expired. Please reconnect Google Calendar.",
            code=exc.code,
        ).with_status(409)
    if isinstance(exc, CalendarPermissionError):
        return AppError(
            "Calendar permission is missing. Reconnect and allow calendar access.", code=exc.code
        ).with_status(403)
    if isinstance(exc, CalendarRateLimitError):
        err = AppError(
            "Google Calendar is rate limiting requests. Try again shortly.", code=exc.code
        )
        err.headers = {"Retry-After": str(exc.retry_after or 30)}
        return err.with_status(503)
    return AppError("Google Calendar is unavailable right now.", code=exc.code).with_status(502)


@asynccontextmanager
async def _calendar_errors() -> AsyncIterator[None]:
    try:
        yield
    except CalendarError as exc:
        raise _translate(exc) from None


async def _guard[T](fn: Callable[[], Awaitable[T]]) -> T:
    async with _calendar_errors():
        return await fn()


@router.get("/connection", response_model=ConnectionOut)
async def get_connection(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> ConnectionOut:
    conn = await service.get_connection(session, principal)
    if conn is None:
        return ConnectionOut(connected=False)
    return ConnectionOut(
        connected=True, provider=conn.provider, account_email=conn.account_email, status=conn.status
    )


@router.post("/connect", response_model=ConnectStart)
async def connect(principal: Principal = Depends(get_principal)) -> ConnectStart:
    return ConnectStart(authorization_url=service.start_connect(principal))


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    code: str | None = Query(default=None, max_length=2000),
    state: str | None = Query(default=None, max_length=2000),
    error: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    frontend = get_settings().frontend_base_url.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{frontend}/calendar?error=denied", status_code=303)
    try:
        await service.complete_connect(session, code=code, state=state)
    except ValueError:
        return RedirectResponse(f"{frontend}/calendar?error=invalid_state", status_code=303)
    except (CalendarError, AppError) as exc:
        logger.warning("calendar_connect_failed", extra={"error": getattr(exc, "code", "error")})
        return RedirectResponse(f"{frontend}/calendar?error=connect_failed", status_code=303)
    return RedirectResponse(f"{frontend}/calendar?connected=1", status_code=303)


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> Response:
    await service.disconnect(session, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/upcoming", response_model=list[ExternalEventOut])
async def upcoming(
    days: int = Query(default=7, ge=1, le=60),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExternalEventOut]:
    events = await _guard(lambda: service.upcoming(session, principal, days))
    return [
        ExternalEventOut(
            external_id=e.external_id,
            title=e.title,
            starts_at=e.starts_at,
            ends_at=e.ends_at,
            html_link=e.html_link,
        )
        for e in events
    ]


@router.get("/events", response_model=list[EventOut])
async def list_events(
    contact_id: uuid.UUID | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[EventOut]:
    events = await service.list_app_events(session, principal, contact_id)
    return [EventOut.model_validate(e) for e in events]


@router.post("/events", status_code=status.HTTP_201_CREATED, response_model=EventOut)
async def create_event(
    body: EventCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> EventOut:
    event = await _guard(
        lambda: service.create_event(session, principal, body, ip=client_ip(request))
    )
    return EventOut.model_validate(event)


@router.put("/events/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> EventOut:
    event = await _guard(
        lambda: service.update_event(session, principal, event_id, body, ip=client_ip(request))
    )
    return EventOut.model_validate(event)
