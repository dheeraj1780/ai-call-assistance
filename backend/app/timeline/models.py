"""Customer timeline.

Events are written by the services that perform the underlying change (contact created,
status changed, note added, call planned/status changed, action item created/completed).
Stored events preserve history that cannot be derived later (e.g. status changes).
Events tied to a note/call/action item are removed when that record is deleted.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, CreatedAtMixin, TenantScopedMixin, UUIDPrimaryKeyMixin, sql_in



class TimelineCategory(enum.StrEnum):
    CONTACT = "CONTACT"
    STATUS_CHANGE = "STATUS_CHANGE"
    NOTE = "NOTE"
    CALL = "CALL"
    TASK = "TASK"
    FOLLOW_UP = "FOLLOW_UP"
    APPOINTMENT = "APPOINTMENT"


class TimelineEventType(enum.StrEnum):
    CONTACT_CREATED = "CONTACT_CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    NOTE_ADDED = "NOTE_ADDED"
    CALL_PLANNED = "CALL_PLANNED"
    CALL_STATUS_CHANGED = "CALL_STATUS_CHANGED"
    ACTION_ITEM_CREATED = "ACTION_ITEM_CREATED"
    ACTION_ITEM_COMPLETED = "ACTION_ITEM_COMPLETED"


class TimelineEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "timeline_events"
    __table_args__ = (
        CheckConstraint(f"category IN ({sql_in(TimelineCategory)})", name="category_valid"),
        CheckConstraint(f"event_type IN ({sql_in(TimelineEventType)})", name="event_type_valid"),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "actor_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (actor_user_id)",
            name="fk_timeline_events_actor_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "note_id"],
            ["contact_notes.company_id", "contact_notes.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_note",
        ),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "action_item_id"],
            ["action_items.company_id", "action_items.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_action_item",
        ),
        Index(
            "ix_timeline_events_contact_occurred",
            "company_id",
            "contact_id",
            "occurred_at",
            "id",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
