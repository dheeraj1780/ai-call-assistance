"""Call records. Phase 2 covers planning and manual status tracking only; telephony, STT and
AI fields are added by later phases' migrations."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.models import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, sql_in
from app.contacts.models import Contact

MAX_OBJECTIVE_LENGTH = 2_000


class CallStatus(enum.StrEnum):
    PLANNED = "PLANNED"
    # Telephony lifecycle (set from provider webhooks; see app/telephony).
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    CONNECTED = "CONNECTED"
    ACTIVE = "ACTIVE"
    # Terminal
    COMPLETED = "COMPLETED"
    NO_ANSWER = "NO_ANSWER"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TERMINAL_CALL_STATUSES = frozenset(
    {CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.CANCELLED, CallStatus.FAILED}
)
LIVE_CALL_STATUSES = frozenset(
    {CallStatus.INITIATED, CallStatus.RINGING, CallStatus.CONNECTED, CallStatus.ACTIVE}
)


class CallChannel(enum.StrEnum):
    PHONE = "PHONE"
    TEAMS = "TEAMS"
    GOOGLE_MEET = "GOOGLE_MEET"


# Channels where the copilot attaches to an online meeting (identified by its link) instead of
# bridging two phone numbers.
MEETING_CHANNELS = frozenset({CallChannel.TEAMS, CallChannel.GOOGLE_MEET})


class TranscriptPersistence(enum.StrEnum):
    """Whether media-derived data (transcript, AI notes/insights) may be stored for a call.

    PERSISTED: stored with retention (phone calls). TRANSIENT: processed in memory only.
    PENDING_RECORDING_STATUS: a Teams call whose persistence depends on Microsoft's
    updateRecordingStatus succeeding first; treated as TRANSIENT until confirmed.
    """

    PERSISTED = "PERSISTED"
    TRANSIENT = "TRANSIENT"
    PENDING_RECORDING_STATUS = "PENDING_RECORDING_STATUS"


class CallOutcome(enum.StrEnum):
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"
    MEETING_BOOKED = "MEETING_BOOKED"
    WON = "WON"
    LOST = "LOST"
    NO_DECISION = "NO_DECISION"


# Allowed MANUAL status transitions (calls logged by hand, e.g. made from a personal phone).
# Provider-driven transitions are handled by app/telephony with ordering rules.
CALL_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.PLANNED: frozenset(
        {CallStatus.ACTIVE, CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.CANCELLED}
    ),
    CallStatus.ACTIVE: frozenset({CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.FAILED}),
    CallStatus.INITIATED: frozenset({CallStatus.CANCELLED}),
    CallStatus.RINGING: frozenset({CallStatus.CANCELLED}),
    CallStatus.CONNECTED: frozenset(),
    CallStatus.COMPLETED: frozenset(),
    CallStatus.NO_ANSWER: frozenset(),
    CallStatus.CANCELLED: frozenset(),
    CallStatus.FAILED: frozenset(),
}


class Call(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(CallStatus)})", name="status_valid"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({sql_in(CallOutcome)})", name="outcome_valid"
        ),
        CheckConstraint(
            f"objective IS NULL OR length(objective) <= {MAX_OBJECTIVE_LENGTH}",
            name="objective_length",
        ),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ended_after_started",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
        CheckConstraint(f"channel IN ({sql_in(CallChannel)})", name="channel_valid"),
        CheckConstraint(
            f"transcript_persistence IN ({sql_in(TranscriptPersistence)})",
            name="transcript_persistence_valid",
        ),
        CheckConstraint(
            "channel <> 'TEAMS' OR meeting_url IS NOT NULL", name="teams_requires_meeting_url"
        ),
        CheckConstraint(
            "channel <> 'GOOGLE_MEET' OR meeting_url IS NOT NULL", name="meet_requires_meeting_url"
        ),
        CheckConstraint(
            "language IS NULL OR language IN ('en-IN', 'en-US', 'hi-IN', 'de-DE')",
            name="language_valid",
        ),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_calls_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (user_id)",
            name="fk_calls_user_member",
        ),
        Index("ix_calls_company_id_contact_id", "company_id", "contact_id", "created_at"),
        Index("ix_calls_company_id_status", "company_id", "status"),
        Index("ix_calls_company_id_user_id", "company_id", "user_id"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The salesperson responsible for the call.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    objective: Mapped[str | None] = mapped_column(Text)
    desired_outcome: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CallStatus.PLANNED.value,
        server_default=CallStatus.PLANNED.value,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(32))
    outcome_notes: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    # Telephony (set by app/telephony; the provider is the source of truth for live states).
    provider: Mapped[str | None] = mapped_column(String(16))
    provider_call_id: Mapped[str | None] = mapped_column(String(128))
    telephony_error: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CallChannel.PHONE.value,
        server_default=CallChannel.PHONE.value,
    )
    # Teams meeting join link the copilot bot joins (TEAMS channel only).
    meeting_url: Mapped[str | None] = mapped_column(String(2000))
    # Speech recognition language for this call (explicit; no automatic detection).
    language: Mapped[str | None] = mapped_column(String(8))
    transcript_persistence: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TranscriptPersistence.PERSISTED.value,
        server_default=TranscriptPersistence.PERSISTED.value,
    )

    contact: Mapped[Contact] = relationship(
        Contact,
        primaryjoin="and_(Call.company_id == Contact.company_id, Call.contact_id == Contact.id)",
        foreign_keys="[Call.company_id, Call.contact_id]",
        viewonly=True,
        lazy="raise",
    )
