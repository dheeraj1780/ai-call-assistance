import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.calls.models import CallStatus
from app.calls.schemas import CallOut
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.db import get_db_session, get_session_factory
from app.common.errors import AppError, ErrorResponse, InvalidStateError, error_response
from app.common.rate_limit import client_ip
from app.telephony import service, simulator
from app.telephony.models import CallRoute
from app.telephony.provider import (
    WebhookVerificationError,
    get_telephony_provider,
    verify_media_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

MAX_WEBHOOK_BYTES = 64 * 1024


@router.post(
    "/calls/{call_id}/start",
    response_model=CallOut,
    responses={
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def start_call(
    call_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallOut:
    """Ask the telephony provider to bridge the salesperson's phone with the customer."""
    call = await service.start_call(session, principal, call_id, ip=client_ip(request))
    return CallOut.model_validate(call)


@router.post("/calls/{call_id}/end", response_model=CallOut)
async def end_call(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> CallOut:
    return CallOut.model_validate(await service.request_end(session, principal, call_id))


class ScriptLine(BaseModel):
    speaker: Literal["agent", "customer"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: Annotated[list[ScriptLine], Field(max_length=100)] | None = None


@router.post("/calls/{call_id}/simulate", status_code=status.HTTP_202_ACCEPTED)
async def simulate(
    call_id: uuid.UUID,
    body: SimulateIn | None = None,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Demo/testing only: plays a scripted conversation through the mock provider."""
    settings = get_settings()
    if settings.telephony_provider != "mock" or not settings.simulation_enabled:
        raise AppError("Simulation is not available", code="simulation_disabled").with_status(404)
    call = await get_call(session, principal, call_id)
    if call.status != CallStatus.INITIATED or not call.provider_call_id:
        raise InvalidStateError("Start the call first; simulation runs on an initiated call")
    if call_id in simulator.running:
        raise InvalidStateError("A simulation is already running for this call")
    script: list[tuple[str, str]] | None = (
        [(line.speaker, line.text) for line in body.script] if body and body.script else None
    )
    simulator.start(
        call.id,
        call.provider_call_id,
        script=script,
        delay=settings.simulation_utterance_delay_seconds,
    )
    return {"status": "started", "provider": "mock"}


@router.post("/webhooks/telephony/{provider_name}", include_in_schema=False)
async def telephony_webhook(provider_name: str, request: Request) -> JSONResponse:
    """Provider status callbacks. Signature + timestamp verified; events applied exactly once."""
    provider = get_telephony_provider()
    if provider_name != provider.name:
        return error_response(404, "not_found", "Unknown provider")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return error_response(413, "payload_too_large", "Webhook payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        provider.verify_webhook(headers, body)
        events = provider.parse_webhook(body)
    except WebhookVerificationError as exc:
        logger.warning("telephony_webhook_rejected", extra={"reason": str(exc)})
        return error_response(401, "invalid_signature", "Webhook verification failed")
    counts = await service.handle_webhook_events(provider.name, events)
    return JSONResponse({"received": len(events), **counts})


@router.websocket("/telephony/media/{provider_name}/{call_id}")
async def media_stream(websocket: WebSocket, provider_name: str, call_id: uuid.UUID) -> None:
    """Provider media stream (fork of the live call audio). Authenticated with a per-call HMAC
    token issued when the call was created. Audio is passed to STT and never stored."""
    provider = get_telephony_provider()
    token = websocket.query_params.get("token", "")
    if provider_name != provider.name or not verify_media_token(call_id, token):
        await websocket.close(code=4401)
        return
    async with get_session_factory()() as session:
        route = await session.get(CallRoute, call_id)
    if route is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    ingest = service.MediaIngest(call_id)
    try:
        while True:
            message = await websocket.receive_text()
            if len(message) > 256 * 1024:
                continue
            await ingest.handle(provider.parse_media_message(message))
    except WebSocketDisconnect:
        pass  # the phone call is unaffected; the provider may reconnect
