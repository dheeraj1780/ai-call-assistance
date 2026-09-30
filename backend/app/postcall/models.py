"""Post-call intelligence: call summary (1:1 with a call) and follow-up drafts.

Lists (requirements, pain points, objections, competitors) are stored as structured
``call_notes`` so they stay queryable and reviewable in one place. Scalar fields carry a
status: CONFIRMED (backed by a verbatim transcript quote), INFERRED, or NOT_DISCUSSED.

Follow-up drafts are never sent by the system: statuses are DRAFT -> APPROVED/COPIED or
DISCARDED, all set by a human.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, sql_in


class SummaryStatus(enum.StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class FieldStatus(enum.StrEnum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    NOT_DISCUSSED = "NOT_DISCUSSED"
    # Written or corrected by a person (never overwritten by AI reprocessing).
    EDITED = "EDITED"


SCALAR_FIELDS = ("current_solution", "budget", "timeline", "decision_maker", "next_step")


class CallSummary(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "call_summaries"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(SummaryStatus)})", name="status_valid"),
        *(
            CheckConstraint(f"{f}_status IN ({sql_in(FieldStatus)})", name=f"{f}_status_valid")
            for f in SCALAR_FIELDS
        ),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("call_id"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_call_summaries_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "edited_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (edited_by_user_id)",
            name="fk_call_summaries_editor_member",
        ),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    suggested_outcome: Mapped[str | None] = mapped_column(String(32))
    current_solution: Mapped[str | None] = mapped_column(String(500))
    current_solution_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FieldStatus.NOT_DISCUSSED
    )
    budget: Mapped[str | None] = mapped_column(String(500))
    budget_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FieldStatus.NOT_DISCUSSED
    )
    timeline: Mapped[str | None] = mapped_column(String(500))
    timeline_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FieldStatus.NOT_DISCUSSED
    )
    decision_maker: Mapped[str | None] = mapped_column(String(500))
    decision_maker_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FieldStatus.NOT_DISCUSSED
    )
    next_step: Mapped[str | None] = mapped_column(String(500))
    next_step_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FieldStatus.NOT_DISCUSSED
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Human editing: the AI text is kept in ``original_summary`` on the first edit.
    original_summary: Mapped[str | None] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class DraftChannel(enum.StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    GENERAL = "GENERAL"


class DraftStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    COPIED = "COPIED"
    DISCARDED = "DISCARDED"


class FollowUpDraft(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "follow_up_drafts"
    __table_args__ = (
        CheckConstraint(f"channel IN ({sql_in(DraftChannel)})", name="channel_valid"),
        CheckConstraint(f"status IN ({sql_in(DraftStatus)})", name="status_valid"),
        CheckConstraint("source IN ('AI', 'MANUAL')", name="source_valid"),
        CheckConstraint("length(body) <= 5000", name="body_length"),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_follow_up_drafts_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_follow_up_drafts_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "reviewed_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (reviewed_by_user_id)",
            name="fk_follow_up_drafts_reviewer_member",
        ),
        Index("ix_follow_up_drafts_company_id_call_id", "company_id", "call_id"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DraftStatus.DRAFT)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    # Reasons a human should look carefully (e.g. figures not found in the call).
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
