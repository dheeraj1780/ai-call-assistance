"""Call intelligence: live copilot insights and structured call notes.

- ``CopilotInsight``: ephemeral cards shown during a call (suggested question, missing agenda
  item, objection, knowledge result, ...). They may quote customer speech, so they share the
  transcript retention (``expires_at``) and are purged with it.
- ``CallNote``: structured notes (requirement, pain point, budget, objection, next step, ...)
  from the salesperson or suggested by the system. A human confirmation/edit/rejection always
  overrides automatic extraction. Notes are business records kept until deleted; their link to
  the source transcript segment is cleared when the transcript expires.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import (
    Base,
    CreatedAtMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)


class InsightType(enum.StrEnum):
    QUESTION_SUGGESTION = "QUESTION_SUGGESTION"
    MISSING_AGENDA_ITEM = "MISSING_AGENDA_ITEM"
    CUSTOMER_REQUIREMENT = "CUSTOMER_REQUIREMENT"
    OBJECTION = "OBJECTION"
    IMPORTANT_FACT = "IMPORTANT_FACT"
    NEXT_STEP = "NEXT_STEP"
    KNOWLEDGE_RESULT = "KNOWLEDGE_RESULT"


class InsightPriority(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IntelSource(enum.StrEnum):
    MANUAL = "MANUAL"
    DETERMINISTIC = "DETERMINISTIC"
    AI = "AI"
    KNOWLEDGE = "KNOWLEDGE"


class InsightStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    DISMISSED = "DISMISSED"


class CopilotInsight(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "copilot_insights"
    __table_args__ = (
        CheckConstraint(f"type IN ({sql_in(InsightType)})", name="type_valid"),
        CheckConstraint(f"priority IN ({sql_in(InsightPriority)})", name="priority_valid"),
        CheckConstraint(f"source IN ({sql_in(IntelSource)})", name="source_valid"),
        CheckConstraint(f"status IN ({sql_in(InsightStatus)})", name="status_valid"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("call_id", "dedupe_key"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_copilot_insights_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "source_segment_id"],
            ["transcript_segments.company_id", "transcript_segments.id"],
            ondelete="SET NULL (source_segment_id)",
            name="fk_copilot_insights_segment",
        ),
        Index("ix_copilot_insights_company_id_call_id", "company_id", "call_id"),
        Index("ix_copilot_insights_expires_at", "expires_at"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    content: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Where the content came from, e.g. a knowledge document title (never raw prompt text).
    context: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_segment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=InsightStatus.ACTIVE)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NoteKind(enum.StrEnum):
    REQUIREMENT = "REQUIREMENT"
    PAIN_POINT = "PAIN_POINT"
    CURRENT_SOLUTION = "CURRENT_SOLUTION"
    BUDGET = "BUDGET"
    TIMELINE = "TIMELINE"
    DECISION_MAKER = "DECISION_MAKER"
    COMPETITOR = "COMPETITOR"
    OBJECTION = "OBJECTION"
    PREFERENCE = "PREFERENCE"
    NEXT_STEP = "NEXT_STEP"
    IMPORTANT_FACT = "IMPORTANT_FACT"
    GENERAL = "GENERAL"


class ObjectionCategory(enum.StrEnum):
    PRICE = "PRICE"
    EXISTING_SOLUTION = "EXISTING_SOLUTION"
    TIMING = "TIMING"
    AUTHORITY = "AUTHORITY"
    TRUST = "TRUST"
    FEATURE_GAP = "FEATURE_GAP"
    COMPETITOR = "COMPETITOR"
    IMPLEMENTATION = "IMPLEMENTATION"
    OTHER = "OTHER"


class NoteStatus(enum.StrEnum):
    SUGGESTED = "SUGGESTED"  # system-generated, not yet reviewed
    CONFIRMED = "CONFIRMED"  # accepted by a human (or written by one)
    EDITED = "EDITED"  # human corrected the text
    REJECTED = "REJECTED"  # human rejected; kept hidden so it is not suggested again


class CallNote(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "call_notes"
    __table_args__ = (
        CheckConstraint(f"kind IN ({sql_in(NoteKind)})", name="kind_valid"),
        CheckConstraint(
            f"category IS NULL OR category IN ({sql_in(ObjectionCategory)})", name="category_valid"
        ),
        CheckConstraint(f"source IN ({sql_in(IntelSource)})", name="source_valid"),
        CheckConstraint(f"status IN ({sql_in(NoteStatus)})", name="status_valid"),
        CheckConstraint("length(btrim(text)) > 0", name="text_not_blank"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        # A note belongs to a call or to a message conversation (communication session).
        CheckConstraint("call_id IS NOT NULL OR session_id IS NOT NULL", name="has_source"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("call_id", "dedupe_key"),
        UniqueConstraint("session_id", "dedupe_key"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_call_notes_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE",
            name="fk_call_notes_session",
        ),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_call_notes_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "source_segment_id"],
            ["transcript_segments.company_id", "transcript_segments.id"],
            ondelete="SET NULL (source_segment_id)",
            name="fk_call_notes_segment",
        ),
        ForeignKeyConstraint(
            ["company_id", "author_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (author_user_id)",
            name="fk_call_notes_author_member",
        ),
        Index("ix_call_notes_company_id_call_id", "company_id", "call_id"),
        Index("ix_call_notes_company_id_contact_id_kind", "company_id", "contact_id", "kind"),
    )

    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    category: Mapped[str | None] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_segment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Stable key for idempotent system suggestions (e.g. "objection:price:<hash>").
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
