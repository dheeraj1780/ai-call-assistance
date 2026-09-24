"""Integration records.

Tenant-owned (RLS): ``integrations`` (one per company and provider; non-secret config in
JSONB, secrets as one encrypted blob), ``integration_user_connections`` (per-user delegated
OAuth, e.g. Teams messaging) and ``integration_subscriptions`` (provider change-notification
subscriptions).

System tables (no RLS, no personal data): ``integration_routes`` maps an integration id to its
tenant so that provider webhooks - which arrive without any tenant context - can bind the
tenant before touching RLS-protected rows; ``integration_webhook_receipts`` makes webhook
handling idempotent. Neither stores payloads.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import (
    Base,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)
from app.integrations.domain import IntegrationMode, Provider


class Integration(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (
        CheckConstraint(f"provider IN ({sql_in(Provider)})", name="provider_valid"),
        CheckConstraint(f"mode IN ({sql_in(IntegrationMode)})", name="mode_valid"),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_object"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("company_id", "provider"),
    )

    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default=IntegrationMode.LIVE)
    # Non-secret configuration (ids, phone numbers, options). Returned to admins.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Fernet-encrypted JSON of secret values. NEVER returned by the API or logged.
    secrets_ciphertext: Mapped[str | None] = mapped_column(Text)
    # Names of the secret fields that are set (so the UI can show "********").
    secret_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    enabled_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Incremented on every configuration change; a test result is valid only for the version
    # it was run against.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Safe summary of the last connection test (check names, pass/fail, short reasons).
    last_test: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Runtime failure (e.g. provider rejected our credentials during a send).
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationRoute(Base):
    __tablename__ = "integration_routes"
    __table_args__ = (UniqueConstraint("provider", "external_account_id"),)

    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    # Provider account identifier (WhatsApp phone_number_id, Plivo auth id, Entra tenant id).
    # Unique per provider so one provider account can never be attached to two workspaces.
    external_account_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookReceipt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "integration_webhook_receipts"
    __table_args__ = (
        UniqueConstraint("provider", "dedupe_key"),
        Index("ix_integration_webhook_receipts_received_at", "received_at"),
    )

    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserConnectionStatus:
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class IntegrationUserConnection(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "integration_user_connections"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'REAUTH_REQUIRED')", name="status_valid"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("company_id", "user_id", "provider"),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="CASCADE",
            name="fk_integration_user_connections_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "integration_id"],
            ["integrations.company_id", "integrations.id"],
            ondelete="CASCADE",
            name="fk_integration_user_connections_integration",
        ),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    external_user_id: Mapped[str | None] = mapped_column(String(128))
    account_email: Mapped[str | None] = mapped_column(String(320))
    # Fernet-encrypted refresh token; never returned by the API or logged.
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    last_error: Mapped[str | None] = mapped_column(String(64))


class IntegrationSubscription(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "integration_subscriptions"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'FAILED', 'DELETED')", name="status_valid"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("provider", "external_subscription_id"),
        ForeignKeyConstraint(
            ["company_id", "integration_id"],
            ["integrations.company_id", "integrations.id"],
            ondelete="CASCADE",
            name="fk_integration_subscriptions_integration",
        ),
        ForeignKeyConstraint(
            ["company_id", "user_connection_id"],
            ["integration_user_connections.company_id", "integration_user_connections.id"],
            ondelete="CASCADE",
            name="fk_integration_subscriptions_user_connection",
        ),
        Index("ix_integration_subscriptions_expires_at", "expires_at"),
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    resource: Mapped[str] = mapped_column(String(500), nullable=False)
    external_subscription_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    last_error: Mapped[str | None] = mapped_column(String(64))
