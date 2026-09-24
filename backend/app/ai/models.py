"""AI usage records (cost observability). Tenant-owned, RLS-protected."""

import uuid

from sqlalchemy import Boolean, ForeignKeyConstraint, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, CreatedAtMixin, TenantScopedMixin, UUIDPrimaryKeyMixin


class AIUsageRecord(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "ai_usage_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)",
            name="fk_ai_usage_records_call",
        ),
        Index("ix_ai_usage_records_company_id_created_at", "company_id", "created_at"),
        Index("ix_ai_usage_records_company_id_call_id", "company_id", "call_id"),
    )

    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Task or pipeline stage, e.g. AGENDA, COPILOT, POST_CALL, EMBEDDING, STT.
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # e.g. audio seconds
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
