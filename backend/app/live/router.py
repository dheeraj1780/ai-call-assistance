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
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agendas.schemas import AgendaItemOut
from app.agendas.service import list_items
from app.audit import service as audit
from app.auth.dependencies import Principal, get_principal, resolve_principal
from app.calls.schemas import CallOut
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.db import get_db_session, get_session_factory
from app.common.errors import (
    AppError,
    ErrorResponse,
    ForbiddenError,
    NotFoundError,
)
from app.common.rate_limit import client_ip
from app.intel import service as intel
from app.intel.models import NoteKind, NoteStatus, ObjectionCategory
from app.live import session as live
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.live.session import segment_payload
from app.tenants.models import Company

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
    """Everything the live screen needs; the WebSocket then streams changes after ``seq``.

    The server is authoritative: a reloaded or reconnected browser rebuilds the whole screen from
    here (stored data, or - for a live-only call - the in-memory state of the running session)."""
    call = await get_call(session, principal, call_id)
    epoch, seq = hub.state(call.id)
    running = live.get_session(call.id)
    settings = get_settings()
    live_only = running is not None and not running.persist
    agenda = [
        AgendaItemOut.model_validate(a).model_dump(mode="json")
        for a in await list_items(session, principal.company_id, call.id)
    ]
    if live_only and running is not None:
        for item in agenda:
            patch = running.engine.transient_agenda.get(str(item["id"]))
            if patch:
                item.update(
                    status=patch["status"],
                    status_source=patch["status_source"],
                    status_reason=patch["status_reason"],
                )
        transcript = list(running.transient_transcript)
        insights = [
            i for i in running.engine.transient_insights.values() if i["status"] == "ACTIVE"
        ]
        notes = [n for n in running.engine.transient_notes.values() if n["status"] != "REJECTED"]
    else:
        transcript = await _transcript(session, principal.company_id, call.id)
        insights = [
            intel.insight_payload(i)
            for i in await intel.active_insights(session, principal.company_id, call.id)
        ]
        notes = [intel.note_payload(n) for n in await intel.list_notes(session, principal, call.id)]
    retention_days = await session.scalar(
        select(Company.transcript_retention_days).where(Company.id == principal.company_id)
    )
    return {
        "epoch": epoch,
        "seq": seq,
        "call": CallOut.model_validate(call).model_dump(mode="json"),
        "agenda": agenda,
        "transcript": transcript,
        "insights": insights,
        "notes": notes,
        "pipeline": {
            "session_active": running is not None,
            "stt": "ok" if running is None or running.stt_ok else "unavailable",
            "copilot": "degraded" if running is not None and running.engine.degraded else "ok",
            "media": (live.media_status(call.id) or {}).get("state") or "unknown",
            "media_reason": (live.media_status(call.id) or {}).get("reason"),
            "copilot_processing": running is not None and running.engine.processing,
        },
        "simulation_available": settings.simulation_enabled
        and not settings.is_production
        and (call.provider in ("mock", "teams-mock") or call.status == "PLANNED"),
        "channel": call.channel,
        "transcript_persistence": call.transcript_persistence,
        # Audio is never stored. Derived text is kept for this many days (company policy) when
        # the call is not live-only.
        "audio_stored": False,
        "text_retention_days": retention_days,
    }


@router.get("/transcript")
async def transcript(
    call_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    call = await get_call(session, principal, call_id)
    return await _transcript(session, principal.company_id, call.id)


async def _editor_call(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> None:
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can change this call's notes")


class SegmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


@router.patch("/transcript/{segment_id}")
async def edit_segment(
    call_id: uuid.UUID,
    segment_id: uuid.UUID,
    body: SegmentEdit,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Correct the text of a transcript segment. The speech-to-text output is kept as
    ``original_text`` (first edit) so the UI can show what was heard vs what a person fixed.
    Only the call's assigned user or an admin can edit."""
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can edit this transcript")
    segment = await session.scalar(
        select(TranscriptSegment).where(
            TranscriptSegment.company_id == principal.company_id,
            TranscriptSegment.call_id == call.id,
            TranscriptSegment.id == segment_id,
        )
    )
    if segment is None:
        running = live.get_session(call.id)
        memory = running.find_transient_segment(str(segment_id)) if running else None
        if memory is None:
            raise NotFoundError("Transcript segment not found")
        if memory["text"] != body.text:
            memory.setdefault("original_text", memory["text"])
            if memory["original_text"] is None:
                memory["original_text"] = memory["text"]
            memory["text"] = body.text
            memory["edited"] = True
        hub.publish(call.id, "transcript.edited", memory)
        return memory
    if segment.text != body.text:
        if segment.original_text is None:
            segment.original_text = segment.text
        segment.text = body.text
        segment.edited_at = datetime.now(UTC)
        segment.edited_by_user_id = principal.user_id
        audit.record(
            session,
            "transcript.edited",
            company_id=principal.company_id,
            actor_user_id=principal.user_id,
            entity_type="transcript_segment",
            entity_id=segment.id,
            ip=client_ip(request),
        )
        await session.commit()
        await session.refresh(segment)
    payload = segment_payload(segment)
    hub.publish(call.id, "transcript.edited", payload)
    return payload


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
            call_status = (await get_call(session, principal, call_id)).status  # 404: other tenant
    except (TimeoutError, ValueError, AppError, WebSocketDisconnect, json.JSONDecodeError):
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
        return

    last_seq = hello.get("last_seq") if isinstance(hello.get("last_seq"), int) else None
    epoch = hello.get("epoch") if isinstance(hello.get("epoch"), str) else None
    queue, backlog, needs_resync = hub.subscribe(call_id, last_seq=last_seq, epoch=epoch)
    try:
        current_epoch, current_seq = hub.state(call_id)
        await websocket.send_json(
            {
                "type": "hello",
                "epoch": current_epoch,
                "seq": current_seq,
                # Lets a reconnecting client notice a call that ended while it was away.
                "status": call_status,
            }
        )
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
    try:
        return await intel.review_note(
            session, principal, call_id, note_id, text=body.text, kind=body.kind, status=body.status
        )
    except NotFoundError:
        # A live-only call keeps its notes in server memory: the same review actions apply.
        running = live.get_session(call_id)
        await _editor_call(session, principal, call_id)
        note = (
            running.engine.edit_transient_note(
                str(note_id),
                text=body.text,
                kind=body.kind.value if body.kind else None,
                status=body.status.value if body.status else None,
            )
            if running
            else None
        )
        if note is None:
            raise
        return note


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    call_id: uuid.UUID,
    note_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        await intel.delete_note(session, principal, call_id, note_id)
    except NotFoundError:
        running = live.get_session(call_id)
        await _editor_call(session, principal, call_id)
        if running is None or not running.engine.delete_transient_note(str(note_id)):
            raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/insights/{insight_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_insight(
    call_id: uuid.UUID,
    insight_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        await intel.dismiss_insight(session, principal, call_id, insight_id)
    except NotFoundError:
        running = live.get_session(call_id)
        await _editor_call(session, principal, call_id)
        if running is None or not running.engine.dismiss_transient_insight(str(insight_id)):
            raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)
