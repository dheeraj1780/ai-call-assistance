"""Call agenda items. Tenant-owned, RLS-protected."""

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

from app.common.models import (
    Base,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)

MAX_AGENDA_ITEMS = 15


class AgendaItemStatus(enum.StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class AgendaItemSource(enum.StrEnum):
    MANUAL = "MANUAL"
    AI = "AI"  # accepted AI suggestion, unedited
    AI_EDITED = "AI_EDITED"


class StatusSource(enum.StrEnum):
    """Who set the current status. MANUAL always wins over automatic inference."""

    DEFAULT = "DEFAULT"
    DETERMINISTIC = "DETERMINISTIC"
    AI = "AI"
    MANUAL = "MANUAL"


class AgendaItem(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "agenda_items"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(AgendaItemStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({sql_in(AgendaItemSource)})", name="source_valid"),
        CheckConstraint(f"status_source IN ({sql_in(StatusSource)})", name="status_source_valid"),
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint(
            "status_confidence IS NULL OR (status_confidence >= 0 AND status_confidence <= 1)",
            name="confidence_range",
        ),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("call_id", "position"),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_agenda_items_call",
        ),
        Index("ix_agenda_items_company_id_call_id", "company_id", "call_id"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    # Lower-case keywords used by the deterministic tracker (derived from title/question).
    keywords: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=AgendaItemSource.MANUAL)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgendaItemStatus.NOT_STARTED
    )
    status_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=StatusSource.DEFAULT
    )
    status_confidence: Mapped[float | None] = mapped_column(Float)
    status_reason: Mapped[str | None] = mapped_column(String(300))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
