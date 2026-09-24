"""Live call API: snapshot + WebSocket event stream, transcript, structured notes review,
insight dismissal.

WebSocket protocol (browser):
  client -> {"type": "auth", "token": <access token>, "last_seq": int|null, "epoch": str|null}
  server -> {"type": "hello", "epoch", "seq"} then missed events, or {"type": "resync"}
  server -> {"type": <event>, "seq", "epoch", "at", "data"}
  client -> {"type": "ping"}  server -> {"type": "pong"}
The token is sent in the first message (not the URL) so it never lands in access logs.
A browser disconnect only unsubscribes; the call and processing continue.
"""

import asyncio
import contextlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agendas.schemas import AgendaItemOut
from app.agendas.service import list_items
from app.auth.dependencies import Principal, get_principal, resolve_principal
from app.calls.schemas import CallOut
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.db import get_db_session, get_session_factory
from app.common.errors import AppError, ErrorResponse
from app.intel import service as intel
from app.intel.models import NoteKind, NoteStatus, ObjectionCategory
from app.live import session as live
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.live.session import segment_payload

router = APIRouter(
    prefix="/calls/{call_id}",
    tags=["live-call"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)

AUTH_TIMEOUT_S = 10.0


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: NoteKind
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    category: ObjectionCategory | None = None


class NoteReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
        | None
    ) = None
    kind: NoteKind | None = None
    status: NoteStatus | None = None


class NoteOut(BaseModel):
    id: uuid.UUID
    kind: str
    category: str | None
    text: str
    confidence: float | None
    source: str
    status: str
    source_segment_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


async def _transcript(
    session: AsyncSession, company_id: uuid.UUID, call_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.company_id == company_id, TranscriptSegment.call_id == call_id)
        .order_by(TranscriptSegment.seq)
    )
    return [segment_payload(s) for s in rows]


@router.get("/live")
async def live_snapshot(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Everything the live screen needs; the WebSocket then streams changes after ``seq``."""
    call = await get_call(session, principal, call_id)
    epoch, seq = hub.state(call.id)
    running = live.get_session(call.id)
    settings = get_settings()
    return {
        "epoch": epoch,
        "seq": seq,
        "call": CallOut.model_validate(call).model_dump(mode="json"),
        "agenda": [
            AgendaItemOut.model_validate(a).model_dump(mode="json")
            for a in await list_items(session, principal.company_id, call.id)
        ],
        "transcript": await _transcript(session, principal.company_id, call.id),
        "insights": [
            intel.insight_payload(i)
            for i in await intel.active_insights(session, principal.company_id, call.id)
        ],
        "notes": [
            intel.note_payload(n) for n in await intel.list_notes(session, principal, call.id)
        ],
        "pipeline": {
            "session_active": running is not None,
            "stt": "ok" if running is None or running.stt_ok else "unavailable",
            "copilot": "degraded" if running is not None and running.engine.degraded else "ok",
        },
        "simulation_available": settings.telephony_provider == "mock"
        and settings.simulation_enabled,
    }


@router.get("/transcript")
async def transcript(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    call = await get_call(session, principal, call_id)
    return await _transcript(session, principal.company_id, call.id)


@router.websocket("/live/ws")
async def live_ws(websocket: WebSocket, call_id: uuid.UUID) -> None:
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT_S)
        hello = json.loads(raw)
        if hello.get("type") != "auth" or not isinstance(hello.get("token"), str):
            raise ValueError("auth expected")
        async with get_session_factory()() as session:
            principal = await resolve_principal(session, get_settings(), hello["token"])
            await get_call(session, principal, call_id)  # 404s for other tenants
    except (TimeoutError, ValueError, AppError, WebSocketDisconnect, json.JSONDecodeError):
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
        return

    last_seq = hello.get("last_seq") if isinstance(hello.get("last_seq"), int) else None
    epoch = hello.get("epoch") if isinstance(hello.get("epoch"), str) else None
    queue, backlog, needs_resync = hub.subscribe(call_id, last_seq=last_seq, epoch=epoch)
    try:
        current_epoch, current_seq = hub.state(call_id)
        await websocket.send_json({"type": "hello", "epoch": current_epoch, "seq": current_seq})
        if needs_resync:
            await websocket.send_json(
                {"type": "resync", "epoch": current_epoch, "seq": current_seq}
            )
        for message in backlog:
            await websocket.send_json(message)

        async def pump() -> None:
            while True:
                await websocket.send_json(await queue.get())

        async def receive() -> None:
            while True:
                msg = await websocket.receive_text()
                with contextlib.suppress(json.JSONDecodeError, AttributeError):
                    if json.loads(msg).get("type") == "ping":
                        await websocket.send_json({"type": "pong"})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(receive())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            with contextlib.suppress(WebSocketDisconnect, Exception):
                t.result()
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(call_id, queue)  # only the subscription ends - never the call


@router.get("/notes", response_model=list[NoteOut])
async def list_notes(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[Any]:
    return await intel.list_notes(session, principal, call_id)


@router.post("/notes", status_code=status.HTTP_201_CREATED, response_model=NoteOut)
async def add_note(
    call_id: uuid.UUID,
    body: NoteIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    return await intel.add_manual_note(
        session, principal, call_id, kind=body.kind, text=body.text, category=body.category
    )


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def review_note(
    call_id: uuid.UUID,
    note_id: uuid.UUID,
    body: NoteReview,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    return await intel.review_note(
        session, principal, call_id, note_id, text=body.text, kind=body.kind, status=body.status
    )


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    call_id: uuid.UUID,
    note_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await intel.delete_note(session, principal, call_id, note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/insights/{insight_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_insight(
    call_id: uuid.UUID,
    insight_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await intel.dismiss_insight(session, principal, call_id, insight_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
