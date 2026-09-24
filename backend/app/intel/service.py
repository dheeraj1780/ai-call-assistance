"""Persistence for copilot insights and structured call notes, plus human review actions.

System suggestions are idempotent via ``(call_id, dedupe_key)``; once a human rejects a
suggestion it stays REJECTED (hidden) so it is not suggested again. Human edits/confirmations
always win: automatic extraction never overwrites a reviewed note.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal
from app.calls.service import get_call
from app.common.errors import ForbiddenError, NotFoundError
from app.intel.models import (
    CallNote,
    CopilotInsight,
    InsightPriority,
    InsightStatus,
    InsightType,
    IntelSource,
    NoteKind,
    NoteStatus,
    ObjectionCategory,
)
from app.live.hub import hub

MAX_INSIGHTS_PER_CALL = 60


def short_hash(text: str) -> str:
    return hashlib.sha1(" ".join(text.lower().split()).encode(), usedforsecurity=False).hexdigest()[
        :16
    ]


def insight_payload(i: CopilotInsight) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "type": i.type,
        "priority": i.priority,
        "content": i.content,
        "context": i.context,
        "confidence": i.confidence,
        "source": i.source,
        "status": i.status,
        "source_segment_id": str(i.source_segment_id) if i.source_segment_id else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


def note_payload(n: CallNote) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "category": n.category,
        "text": n.text,
        "confidence": n.confidence,
        "source": n.source,
        "status": n.status,
        "source_segment_id": str(n.source_segment_id) if n.source_segment_id else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


async def add_insight(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    call_id: uuid.UUID,
    type_: InsightType,
    content: str,
    priority: InsightPriority,
    source: IntelSource,
    dedupe_key: str,
    retention_days: int,
    confidence: float | None = None,
    context: str | None = None,
    segment_id: uuid.UUID | None = None,
) -> CopilotInsight | None:
    """Insert unless an insight with the same dedupe key exists; publishes on success."""
    count = await session.scalar(
        select(CopilotInsight.id)
        .where(CopilotInsight.company_id == company_id, CopilotInsight.call_id == call_id)
        .offset(MAX_INSIGHTS_PER_CALL - 1)
        .limit(1)
    )
    if count is not None:
        return None  # hard cap: never flood a call with cards
    row = await session.scalar(
        insert(CopilotInsight)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            call_id=call_id,
            type=type_.value,
            priority=priority.value,
            content=content[:1000],
            context=context[:500] if context else None,
            confidence=confidence,
            source=source.value,
            source_segment_id=segment_id,
            dedupe_key=dedupe_key[:128],
            status=InsightStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(days=retention_days),
        )
        .on_conflict_do_nothing(index_elements=["call_id", "dedupe_key"])
        .returning(CopilotInsight)
    )
    if row is None:
        return None
    await session.commit()
    hub.publish(call_id, "insight.created", insight_payload(row))
    return row


async def suggest_note(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    call_id: uuid.UUID | None,
    contact_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    kind: NoteKind,
    text: str,
    source: IntelSource,
    dedupe_key: str,
    confidence: float | None,
    category: ObjectionCategory | None = None,
    segment_id: uuid.UUID | None = None,
) -> CallNote | None:
    row = await session.scalar(
        insert(CallNote)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            call_id=call_id,
            session_id=session_id,
            contact_id=contact_id,
            kind=kind.value,
            category=category.value if category else None,
            text=text[:1000],
            confidence=confidence,
            source=source.value,
            status=NoteStatus.SUGGESTED.value,
            source_segment_id=segment_id,
            dedupe_key=dedupe_key[:128],
        )
        .on_conflict_do_nothing(
            index_elements=["call_id", "dedupe_key"] if call_id else ["session_id", "dedupe_key"]
        )
        .returning(CallNote)
    )
    if row is None:
        return None
    await session.commit()
    if call_id is not None:
        hub.publish(call_id, "note.upserted", note_payload(row))
    return row


# ---- Human review (API) -----------------------------------------------------------------------


def _require_editor(principal: Principal, user_id: uuid.UUID | None) -> None:
    if not (principal.is_admin or user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can change this call's notes")


async def list_notes(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID
) -> list[CallNote]:
    call = await get_call(session, principal, call_id)
    rows = await session.scalars(
        select(CallNote)
        .where(
            CallNote.company_id == principal.company_id,
            CallNote.call_id == call.id,
            CallNote.status != NoteStatus.REJECTED.value,
        )
        .order_by(CallNote.created_at)
    )
    return list(rows.all())


async def add_manual_note(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    *,
    kind: NoteKind,
    text: str,
    category: ObjectionCategory | None,
) -> CallNote:
    call = await get_call(session, principal, call_id)
    _require_editor(principal, call.user_id)
    note = CallNote(
        company_id=principal.company_id,
        call_id=call.id,
        contact_id=call.contact_id,
        kind=kind.value,
        category=category.value if category else None,
        text=text,
        confidence=None,
        source=IntelSource.MANUAL.value,
        status=NoteStatus.CONFIRMED.value,
        author_user_id=principal.user_id,
        dedupe_key=f"manual:{uuid.uuid4().hex}",
        reviewed_at=datetime.now(UTC),
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    hub.publish(call.id, "note.upserted", note_payload(note))
    return note


async def _get_note(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, note_id: uuid.UUID
) -> tuple[CallNote, uuid.UUID | None]:
    call = await get_call(session, principal, call_id)
    note = await session.scalar(
        select(CallNote).where(
            CallNote.company_id == principal.company_id,
            CallNote.call_id == call.id,
            CallNote.id == note_id,
        )
    )
    if note is None:
        raise NotFoundError("Note not found")
    return note, call.user_id


async def review_note(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    note_id: uuid.UUID,
    *,
    text: str | None,
    kind: NoteKind | None,
    status: NoteStatus | None,
) -> CallNote:
    """Human correction overrides AI inference."""
    note, owner = await _get_note(session, principal, call_id, note_id)
    _require_editor(principal, owner)
    if text is not None and text != note.text:
        note.text = text
        note.status = NoteStatus.EDITED.value
    if kind is not None:
        note.kind = kind.value
    if status is not None:
        note.status = status.value
    elif text is None and note.status == NoteStatus.SUGGESTED:
        note.status = NoteStatus.CONFIRMED.value
    note.author_user_id = principal.user_id
    note.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(note)
    hub.publish(call_id, "note.upserted", note_payload(note))
    return note


async def delete_note(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    note, owner = await _get_note(session, principal, call_id, note_id)
    _require_editor(principal, owner)
    if note.source == IntelSource.MANUAL:
        await session.delete(note)
    else:
        # Keep a hidden REJECTED record so the same suggestion is not generated again.
        note.status = NoteStatus.REJECTED.value
        note.reviewed_at = datetime.now(UTC)
    await session.commit()
    hub.publish(call_id, "note.deleted", {"id": str(note_id)})


async def dismiss_insight(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, insight_id: uuid.UUID
) -> None:
    call = await get_call(session, principal, call_id)
    insight = await session.scalar(
        select(CopilotInsight).where(
            CopilotInsight.company_id == principal.company_id,
            CopilotInsight.call_id == call.id,
            CopilotInsight.id == insight_id,
        )
    )
    if insight is None:
        raise NotFoundError("Insight not found")
    insight.status = InsightStatus.DISMISSED.value
    await session.commit()
    hub.publish(call.id, "insight.dismissed", {"id": str(insight_id)})


async def active_insights(
    session: AsyncSession, company_id: uuid.UUID, call_id: uuid.UUID
) -> list[CopilotInsight]:
    rows = await session.scalars(
        select(CopilotInsight)
        .where(
            CopilotInsight.company_id == company_id,
            CopilotInsight.call_id == call_id,
            CopilotInsight.status == InsightStatus.ACTIVE.value,
        )
        .order_by(CopilotInsight.created_at)
    )
    return list(rows.all())
