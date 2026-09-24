"""Write and read timeline events."""

import base64
import binascii
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError
from app.timeline.models import TimelineCategory, TimelineEvent, TimelineEventType

SUMMARY_MAX = 500


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


def record(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    category: TimelineCategory,
    event_type: TimelineEventType,
    summary: str,
    actor_user_id: uuid.UUID | None,
    from_status: str | None = None,
    to_status: str | None = None,
    note_id: uuid.UUID | None = None,
    call_id: uuid.UUID | None = None,
    action_item_id: uuid.UUID | None = None,
) -> TimelineEvent:
    """Add an event to the caller's transaction."""
    event = TimelineEvent(
        company_id=company_id,
        contact_id=contact_id,
        category=category,
        event_type=event_type,
        summary=_clip(summary),
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        note_id=note_id,
        call_id=call_id,
        action_item_id=action_item_id,
        # Set here (not DB now(), which is fixed per transaction) so several events written
        # in one transaction keep their order.
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    return event


async def update_note_summary(
    session: AsyncSession, company_id: uuid.UUID, note_id: uuid.UUID, body: str
) -> None:
    """Keep the timeline snapshot in sync when a note is edited."""
    await session.execute(
        update(TimelineEvent)
        .where(TimelineEvent.company_id == company_id, TimelineEvent.note_id == note_id)
        .values(summary=_clip(body))
    )


def encode_cursor(event: TimelineEvent) -> str:
    raw = f"{event.occurred_at.isoformat()}|{event.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        occurred, _, event_id = base64.urlsafe_b64decode(padded).decode().partition("|")
        return datetime.fromisoformat(occurred), uuid.UUID(event_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise AppError("Invalid cursor", code="invalid_cursor") from None


async def list_events(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    categories: list[TimelineCategory] | None,
    limit: int,
    before: str | None,
) -> tuple[list[TimelineEvent], str | None]:
    """Newest first, keyset-paginated on (occurred_at, id)."""
    stmt = select(TimelineEvent).where(
        TimelineEvent.company_id == company_id, TimelineEvent.contact_id == contact_id
    )
    if categories:
        stmt = stmt.where(TimelineEvent.category.in_([c.value for c in categories]))
    if before:
        at, event_id = decode_cursor(before)
        stmt = stmt.where(
            or_(
                TimelineEvent.occurred_at < at,
                and_(TimelineEvent.occurred_at == at, TimelineEvent.id < event_id),
            )
        )
    stmt = stmt.order_by(TimelineEvent.occurred_at.desc(), TimelineEvent.id.desc()).limit(
        limit + 1
    )
    rows = list((await session.scalars(stmt)).all())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor
