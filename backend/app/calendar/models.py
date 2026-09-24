"""Calendar connections (per user) and events created through the app. Tenant-owned, RLS."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class ConnectionStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class CalendarConnection(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "calendar_connections"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ConnectionStatus)})", name="status_valid"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("company_id", "user_id", "provider"),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="CASCADE",
            name="fk_calendar_connections_member",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_email: Mapped[str | None] = mapped_column(String(320))
    # Fernet-encrypted refresh token; never returned by the API or logged.
    encrypted_refresh_token: Mapped[str] = mapped_column(String(2000), nullable=False)
    scopes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ConnectionStatus.ACTIVE)
    last_error: Mapped[str | None] = mapped_column(String(64))


class CalendarEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "calendar_events"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "connection_id"],
            ["calendar_connections.company_id", "calendar_connections.id"],
            ondelete="SET NULL (connection_id)",
            name="fk_calendar_events_connection",
        ),
        ForeignKeyConstraint(
            ["company_id", "created_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (created_by_user_id)",
            name="fk_calendar_events_creator_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_calendar_events_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)",
            name="fk_calendar_events_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "action_item_id"],
            ["action_items.company_id", "action_items.id"],
            ondelete="SET NULL (action_item_id)",
            name="fk_calendar_events_action_item",
        ),
        Index("ix_calendar_events_company_id_contact_id", "company_id", "contact_id"),
    )

    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    html_link: Mapped[str | None] = mapped_column(String(1000))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
