"""System tables for telephony routing and webhook idempotency (no RLS, no personal data).

Provider webhooks arrive without a tenant context. ``call_routes`` maps a call to its tenant
so the handler can bind the tenant context before touching any RLS-protected row.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, UUIDPrimaryKeyMixin


class CallRoute(Base):
    __tablename__ = "call_routes"
    __table_args__ = (UniqueConstraint("provider", "provider_call_id"),)

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelephonyWebhookEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "telephony_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id"),
        Index("ix_telephony_webhook_events_call_id", "call_id"),
    )

    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    applied: Mapped[str] = mapped_column(String(16), nullable=False, default="applied")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
