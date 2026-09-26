"""Provider-independent conversation model (tenant-owned, RLS).

    communication_sessions      one conversation thread or call on one channel
    communication_messages      normalised messages (retention-controlled, like transcripts)
    communication_participants  salesperson / customer / bot / system in a session
    communication_events        normalised lifecycle events (no message content)
    contact_identities          confirmed external identifiers of a contact (for matching)
    message_drafts              AI/manual reply drafts; only a human can send them

Every provider (Teams, WhatsApp, Plivo, mock) is converted into these tables; the AI and the
CRM timeline read only these tables, never provider payloads.
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

from app.common.models import (
    Base,
    CreatedAtMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)
from app.integrations.domain import Capability, Channel, ConversationEventType, Provider

MAX_MESSAGE_LENGTH = 10_000


class SessionStatus(enum.StrEnum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class MatchStatus(enum.StrEnum):
    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"


class Direction(enum.StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class SenderType(enum.StrEnum):
    CUSTOMER = "CUSTOMER"
    SALESPERSON = "SALESPERSON"
    BOT = "BOT"
    SYSTEM = "SYSTEM"


class MessageType(enum.StrEnum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    DOCUMENT = "DOCUMENT"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    LOCATION = "LOCATION"
    CONTACTS = "CONTACTS"
    INTERACTIVE = "INTERACTIVE"
    TEMPLATE = "TEMPLATE"
    UNSUPPORTED = "UNSUPPORTED"


class MessageStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


# Delivery states only move forward; FAILED is terminal.
MESSAGE_STATUS_RANK = {
    MessageStatus.SENDING: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
    MessageStatus.FAILED: 4,
    MessageStatus.RECEIVED: 0,
}


class ParticipantRole(enum.StrEnum):
    SALESPERSON = "SALESPERSON"
    CUSTOMER = "CUSTOMER"
    BOT = "BOT"
    SYSTEM = "SYSTEM"


class IdentityKind(enum.StrEnum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    TEAMS_USER = "TEAMS_USER"
    TEAMS_CHAT = "TEAMS_CHAT"
    EXTERNAL = "EXTERNAL"


class DraftSource(enum.StrEnum):
    AI = "AI"
    MANUAL = "MANUAL"


class MessageDraftStatus(enum.StrEnum):
    SUGGESTED = "SUGGESTED"
    SENT = "SENT"
    DISCARDED = "DISCARDED"


class CommunicationSession(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "communication_sessions"
    __table_args__ = (
        CheckConstraint(f"provider IN ({sql_in(Provider)}, 'MOCK')", name="provider_valid"),
        CheckConstraint(f"channel IN ({sql_in(Channel)})", name="channel_valid"),
        CheckConstraint(f"capability IN ({sql_in(Capability)})", name="capability_valid"),
        CheckConstraint(f"status IN ({sql_in(SessionStatus)})", name="status_valid"),
        CheckConstraint(f"match_status IN ({sql_in(MatchStatus)})", name="match_status_valid"),
        CheckConstraint(
            "(match_status = 'MATCHED') = (contact_id IS NOT NULL)", name="match_has_contact"
        ),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint(
            "company_id",
            "provider",
            "capability",
            "external_session_id",
            name="uq_communication_sessions_external_session",
        ),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_communication_sessions_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_communication_sessions_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "integration_id"],
            ["integrations.company_id", "integrations.id"],
            ondelete="SET NULL (integration_id)",
            name="fk_communication_sessions_integration",
        ),
        ForeignKeyConstraint(
            ["company_id", "owner_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (owner_user_id)",
            name="fk_communication_sessions_owner_member",
        ),
        Index(
            "ix_communication_sessions_company_id_contact_id",
            "company_id",
            "contact_id",
            "last_message_at",
        ),
        Index(
            "ix_communication_sessions_company_id_last_message_at",
            "company_id",
            "last_message_at",
        ),
        Index("ix_communication_sessions_company_id_call_id", "company_id", "call_id"),
    )

    integration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    capability: Mapped[str] = mapped_column(String(24), nullable=False)
    # Provider thread/call id (WhatsApp customer wa_id, Teams chat id, provider call id).
    external_session_id: Mapped[str | None] = mapped_column(String(256))
    # The customer's provider identity (wa_id, Entra user id) used for matching.
    external_participant_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SessionStatus.OPEN)
    match_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MatchStatus.UNMATCHED
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Small, non-content metadata (e.g. display name hints). Never message text.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class CommunicationMessage(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "communication_messages"
    __table_args__ = (
        CheckConstraint(f"direction IN ({sql_in(Direction)})", name="direction_valid"),
        CheckConstraint(f"sender_type IN ({sql_in(SenderType)})", name="sender_type_valid"),
        CheckConstraint(f"message_type IN ({sql_in(MessageType)})", name="message_type_valid"),
        CheckConstraint(f"status IN ({sql_in(MessageStatus)})", name="status_valid"),
        CheckConstraint(f"length(content) <= {MAX_MESSAGE_LENGTH}", name="content_length"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("session_id", "external_message_id"),
        UniqueConstraint("session_id", "client_message_id"),
        ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE",
            name="fk_communication_messages_session",
        ),
        ForeignKeyConstraint(
            ["company_id", "sender_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (sender_user_id)",
            name="fk_communication_messages_sender_member",
        ),
        Index(
            "ix_communication_messages_company_id_session_id",
            "company_id",
            "session_id",
            "occurred_at",
        ),
        Index("ix_communication_messages_expires_at", "expires_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_external_id: Mapped[str | None] = mapped_column(String(256))
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    external_message_id: Mapped[str | None] = mapped_column(String(256))
    # Idempotency key supplied by the browser for outbound sends (double-click safety).
    client_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class CommunicationParticipant(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "communication_participants"
    __table_args__ = (
        CheckConstraint(f"role IN ({sql_in(ParticipantRole)})", name="role_valid"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("session_id", "role", "external_id"),
        ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE",
            name="fk_communication_participants_session",
        ),
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (user_id)",
            name="fk_communication_participants_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="SET NULL (contact_id)",
            name="fk_communication_participants_contact",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommunicationEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "communication_events"
    __table_args__ = (
        CheckConstraint(f"type IN ({sql_in(ConversationEventType)})", name="type_valid"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("session_id", "external_event_id"),
        ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE",
            name="fk_communication_events_session",
        ),
        Index(
            "ix_communication_events_company_id_session_id",
            "company_id",
            "session_id",
            "occurred_at",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String(300))
    # Small non-content attributes (status, error code, duration). Never message text/audio.
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ContactIdentity(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "contact_identities"
    __table_args__ = (
        CheckConstraint(f"kind IN ({sql_in(IdentityKind)})", name="kind_valid"),
        UniqueConstraint("company_id", "id"),
        # One identifier maps to at most one contact inside a company.
        UniqueConstraint("company_id", "kind", "value"),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_contact_identities_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "created_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (created_by_user_id)",
            name="fk_contact_identities_creator_member",
        ),
        Index("ix_contact_identities_company_id_contact_id", "company_id", "contact_id"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(256), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class MessageDraft(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "message_drafts"
    __table_args__ = (
        CheckConstraint(f"source IN ({sql_in(DraftSource)})", name="source_valid"),
        CheckConstraint(f"status IN ({sql_in(MessageDraftStatus)})", name="status_valid"),
        CheckConstraint(f"length(body) <= {MAX_MESSAGE_LENGTH}", name="body_length"),
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("session_id", "reply_to_message_id"),
        ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE",
            name="fk_message_drafts_session",
        ),
        ForeignKeyConstraint(
            ["company_id", "reply_to_message_id"],
            ["communication_messages.company_id", "communication_messages.id"],
            ondelete="SET NULL (reply_to_message_id)",
            name="fk_message_drafts_reply_to_message",
        ),
        ForeignKeyConstraint(
            ["company_id", "sent_message_id"],
            ["communication_messages.company_id", "communication_messages.id"],
            ondelete="SET NULL (sent_message_id)",
            name="fk_message_drafts_sent_message",
        ),
        ForeignKeyConstraint(
            ["company_id", "reviewed_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (reviewed_by_user_id)",
            name="fk_message_drafts_reviewer_member",
        ),
        Index("ix_message_drafts_company_id_session_id", "company_id", "session_id"),
        Index("ix_message_drafts_expires_at", "expires_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sent_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MessageDraftStatus.SUGGESTED
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    ai_provider: Mapped[str | None] = mapped_column(String(32))
    # Company-knowledge excerpts the draft relies on: [{document_id, title, chunk_id, score}].
    knowledge_sources: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
