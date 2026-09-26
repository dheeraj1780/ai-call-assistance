"""Google Meet endpoints: per-user Google sign-in, meeting lookup and attaching the live copilot
to a started Google Meet call (SDP signalling proxy + session state from the browser client)."""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.config import get_settings
from app.common.db import get_db_session
from app.common.errors import AppError, ErrorResponse
from app.integrations import google_meet_service as meet_service
from app.integrations.providers.base import ProviderError
from app.integrations.providers.factory import CredentialsMissingError
from app.integrations.providers.google_meet import MAX_SDP_BYTES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["google-meet"], responses={409: {"model": ErrorResponse}})


class MeetConnectionOut(BaseModel):
    connected: bool
    account_email: str | None = None
    status: str | None = None


@router.get("/integrations/google-meet/connection", response_model=MeetConnectionOut)
async def connection(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> MeetConnectionOut:
    conn = await meet_service.get_connection(session, principal)
    if conn is None:
        return MeetConnectionOut(connected=False)
    return MeetConnectionOut(
        connected=conn.status == "ACTIVE", account_email=conn.account_email, status=conn.status
    )


@router.post("/integrations/google-meet/connect")
async def connect(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> dict[str, str]:
    return {"authorization_url": await meet_service.start_connect(session, principal)}


@router.get("/integrations/google-meet/oauth/callback", include_in_schema=False)
async def oauth_callback(
    code: str | None = Query(default=None, max_length=4000),
    state: str | None = Query(default=None, max_length=2000),
    error: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    target = f"{get_settings().frontend_base_url.rstrip('/')}/settings/integrations"
    if error or not code or not state:
        return RedirectResponse(f"{target}?google_meet=denied", status_code=303)
    try:
        await meet_service.complete_connect(session, code=code, state=state)
    except ValueError:
        return RedirectResponse(f"{target}?google_meet=invalid_state", status_code=303)
    except AppError as exc:
        return RedirectResponse(f"{target}?google_meet={exc.code}", status_code=303)
    except (ProviderError, CredentialsMissingError) as exc:
        logger.warning("google_meet_oauth_failed", extra={"error": getattr(exc, "code", "config")})
        return RedirectResponse(f"{target}?google_meet=connect_failed", status_code=303)
    return RedirectResponse(f"{target}?google_meet=connected", status_code=303)


@router.delete("/integrations/google-meet/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> Response:
    await meet_service.disconnect_user(session, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MeetingOut(BaseModel):
    space: str
    meeting_code: str | None
    meeting_uri: str | None
    active_conference: bool


@router.get("/integrations/google-meet/meetings", response_model=MeetingOut)
async def lookup(
    link: Annotated[str, Query(min_length=3, max_length=2000)],
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> MeetingOut:
    space = await meet_service.lookup_meeting(session, principal, link)
    return MeetingOut(
        space=space.name,
        meeting_code=space.meeting_code,
        meeting_uri=space.meeting_uri,
        active_conference=space.active_conference,
    )


class ConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer: Annotated[str, StringConstraints(min_length=10, max_length=MAX_SDP_BYTES)]


class ConnectOut(BaseModel):
    answer: str
    trace_id: str | None
    space: str
    # Relative path (with the per-call media token) of the media WebSocket on this API.
    media_ws_path: str
    mock: bool


@router.post("/calls/{call_id}/google-meet/connect", response_model=ConnectOut)
async def connect_call(
    call_id: uuid.UUID,
    body: ConnectIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ConnectOut:
    result = await meet_service.connect_call(session, principal, call_id, body.offer)
    return ConnectOut(
        answer=result.answer,
        trace_id=result.trace_id,
        space=result.space,
        media_ws_path=result.media_ws_path,
        mock=result.mock,
    )


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    state: Literal["waiting", "joined", "disconnected", "failed"]
    reason: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,64}$")] | None = None


@router.post("/calls/{call_id}/google-meet/events")
async def report_event(
    call_id: uuid.UUID,
    body: EventIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    outcome = await meet_service.report_event(
        session, principal, call_id, event_id=body.event_id, state=body.state, reason=body.reason
    )
    return {"outcome": outcome}
