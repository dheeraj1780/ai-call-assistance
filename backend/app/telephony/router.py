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
from app.integrations import service as integrations
from app.integrations import simulator as integration_simulator
from app.integrations.domain import Provider
from app.telephony import service, simulator
from app.telephony.models import CallRoute
from app.telephony.provider import (
    WebhookVerificationError,
    get_telephony_provider,
    media_parser_for,
    verify_media_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

MAX_WEBHOOK_BYTES = 64 * 1024
SIMULATABLE_PROVIDERS = {"mock", "teams-mock"}
MEDIA_PROVIDERS = {"mock", "plivo", "teams", "teams-mock", "google-meet", "google-meet-mock"}


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
    """Demo/testing only: plays a scripted conversation through a MOCK provider (phone or
    Teams). Real provider calls can never be simulated."""
    settings = get_settings()
    if not settings.simulation_enabled or settings.is_production:
        raise AppError("Simulation is not available", code="simulation_disabled").with_status(404)
    call = await get_call(session, principal, call_id)
    if call.provider not in SIMULATABLE_PROVIDERS:
        raise AppError(
            "Only calls placed through a mock provider can be simulated",
            code="simulation_disabled",
        ).with_status(404)
    if call.status != CallStatus.INITIATED or not call.provider_call_id:
        raise InvalidStateError("Start the call first; simulation runs on an initiated call")
    if call_id in simulator.running or call_id in integration_simulator.running:
        raise InvalidStateError("A simulation is already running for this call")
    script: list[tuple[str, str]] | None = (
        [(line.speaker, line.text) for line in body.script] if body and body.script else None
    )
    if call.provider == "teams-mock":
        record = await integrations.get_record(
            session, principal.company_id, Provider.MICROSOFT_TEAMS
        )
        integration_simulator.start_teams_call(
            call.id,
            call.provider_call_id,
            persistence=str((record.config if record else {}).get("call_persistence", "")),
            script=script or simulator.DEFAULT_SCRIPT,
            delay=settings.simulation_utterance_delay_seconds,
        )
        return {"status": "started", "provider": "teams-mock"}
    simulator.start(
        call.id,
        call.provider_call_id,
        script=script,
        delay=settings.simulation_utterance_delay_seconds,
    )
    return {"status": "started", "provider": "mock"}


@router.post("/calls/{call_id}/dev/audio-source")
async def dev_audio_source(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """LOCAL DEVELOPMENT AUDIO SOURCE (not Teams media): attach a local audio feeder
    (backend/scripts/local_audio_call.py) to a call placed through a MOCK provider.

    Performs the same connect handshake a real media gateway performs (Teams: ESTABLISHING ->
    ESTABLISHED -> media AVAILABLE through the gateway-event handler; phone mock: RINGING ->
    CONNECTED) and returns the call's media WebSocket URL (per-call HMAC token). Audio sent there
    goes through the normal MediaIngest -> LiveSession -> SpeechToTextProvider pipeline and is
    never stored. Development only; never available for real provider calls or in production."""
    settings = get_settings()
    if not settings.simulation_enabled or settings.is_production:
        raise AppError("Not available", code="simulation_disabled").with_status(404)
    call = await get_call(session, principal, call_id)
    if call.provider not in SIMULATABLE_PROVIDERS or not call.provider_call_id:
        raise AppError(
            "Only calls placed through a mock provider accept a local audio source",
            code="simulation_disabled",
        ).with_status(404)
    if call.status in ("COMPLETED", "NO_ANSWER", "CANCELLED", "FAILED", "PLANNED"):
        raise InvalidStateError("Start the call first; it must be in progress")
    if call.status == CallStatus.INITIATED:
        await _connect_for_dev_audio(
            call.id,
            call.provider,
            call.provider_call_id,
            recording_declared=call.transcript_persistence == "PENDING_RECORDING_STATUS",
        )
    _, media_url = service._callback_urls(call.provider, call.id)
    path = media_url.split("://", 1)[1]
    path = path[path.index("/") :]
    return {
        "label": "LOCAL DEVELOPMENT AUDIO SOURCE",
        "media_ws_path": path,  # relative: connect on the same host the API is reached on
        "format": {"encoding": "linear16", "sample_rate": 16000, "channels": 1},
        "tracks": ["agent", "customer"],
        "language": call.language or settings.stt_language,
    }


async def _connect_for_dev_audio(
    call_id: uuid.UUID, provider: str, provider_call_id: str, *, recording_declared: bool
) -> None:
    from datetime import UTC, datetime

    if provider == "teams-mock":
        from app.integrations.webhooks import GatewayEvent, handle_gateway_event

        steps: list[dict[str, str]] = [{"state": "ESTABLISHING"}]
        if recording_declared:
            # Same order as the real gateway: updateRecordingStatus first, then audio.
            steps.append({"recording_status": "RECORDING_CONFIRMED"})
        steps += [{"state": "ESTABLISHED"}, {"media_status": "AVAILABLE"}]
        for kw in steps:
            await handle_gateway_event(
                "teams-mock",
                GatewayEvent(
                    event_id=f"dev-audio-{call_id}-{next(iter(kw.values()))}",
                    call_id=call_id,
                    gateway_call_id=provider_call_id,
                    **kw,
                ),
            )
        return
    from app.telephony.provider import ProviderCallState, TelephonyEvent

    for state in (ProviderCallState.RINGING, ProviderCallState.CONNECTED):
        await service.process_event(
            provider,
            TelephonyEvent(
                f"dev-audio-{call_id}-{state}", provider_call_id, state, datetime.now(UTC)
            ),
        )


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
    """Provider media stream (fork of the live call audio: mock, Plivo, Teams media gateway).
    Authenticated with a per-call HMAC token issued when the call was created, and the call
    must have been placed through the same provider. Audio is passed to STT, never stored."""
    token = websocket.query_params.get("token", "")
    if provider_name not in MEDIA_PROVIDERS or not verify_media_token(call_id, token):
        await websocket.close(code=4401)
        return
    async with get_session_factory()() as session:
        route = await session.get(CallRoute, call_id)
    if route is None or route.provider != provider_name:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    # Plivo inbound calls: the first (A) leg is the customer, so tracks are swapped.
    parse = media_parser_for(provider_name, a_leg=websocket.query_params.get("leg", "agent"))
    ingest = service.MediaIngest(call_id)
    try:
        while True:
            message = await websocket.receive_text()
            if len(message) > 256 * 1024:
                continue
            await ingest.handle(parse(message))
    except WebSocketDisconnect:
        pass  # the phone call is unaffected; the provider may reconnect
