"""Transcript segments. Personal data: retained only until ``expires_at`` (company policy,
default 30 days), then purged by the retention job. Only FINAL STT results are stored;
partial results are streamed to the browser and never persisted. Raw audio is never stored.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, CreatedAtMixin, TenantScopedMixin, UUIDPrimaryKeyMixin, sql_in

MAX_SEGMENT_CHARS = 5_000


class Speaker(enum.StrEnum):
    SALES_REP = "SALES_REP"
    CUSTOMER = "CUSTOMER"
    UNKNOWN = "UNKNOWN"


class TranscriptSegment(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        CheckConstraint(f"speaker IN ({sql_in(Speaker)})", name="speaker_valid"),
        CheckConstraint(f"length(text) <= {MAX_SEGMENT_CHARS}", name="text_length"),
        CheckConstraint(
            "speaker_confidence IS NULL OR (speaker_confidence >= 0 AND speaker_confidence <= 1)",
            name="speaker_confidence_range",
        ),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("call_id", "seq"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_transcript_segments_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "edited_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (edited_by_user_id)",
            name="fk_transcript_segments_editor_member",
        ),
        Index("ix_transcript_segments_company_id_call_id", "company_id", "call_id", "seq"),
        Index("ix_transcript_segments_expires_at", "expires_at"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    # 1.0 when the speaker comes from a dedicated telephony track; lower for diarization.
    speaker_confidence: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    stt_confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # STT provider name
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Human correction of the STT text. ``text`` always holds the current text (what the copilot,
    # summary and UI use); the first edit keeps the machine output in ``original_text``.
    original_text: Mapped[str | None] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
