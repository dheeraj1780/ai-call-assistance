"""Contacts (leads/customers) and free-form contact notes. Tenant-owned, RLS-protected."""

import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import (
    Base,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)

MAX_TAGS = 20
MAX_NOTE_LENGTH = 10_000


class ContactStatus(enum.StrEnum):
    NEW = "NEW"
    CONTACTED = "CONTACTED"
    QUALIFIED = "QUALIFIED"
    PROPOSAL = "PROPOSAL"
    NEGOTIATION = "NEGOTIATION"
    WON = "WON"
    LOST = "LOST"


class ContactSource(enum.StrEnum):
    MANUAL = "MANUAL"
    REFERRAL = "REFERRAL"
    WEBSITE = "WEBSITE"
    INBOUND_CALL = "INBOUND_CALL"
    EVENT = "EVENT"
    IMPORT = "IMPORT"
    OTHER = "OTHER"


MAX_ATTRIBUTES = 20


class Contact(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ContactStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({sql_in(ContactSource)})", name="source_valid"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes_object"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(f"cardinality(tags) <= {MAX_TAGS}", name="tags_max"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
        UniqueConstraint("company_id", "id"),
        # The owner must be a member of the same company. Removing the member only clears
        # the owner (PostgreSQL 15+ column-list SET NULL keeps company_id intact).
        ForeignKeyConstraint(
            ["company_id", "owner_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (owner_user_id)",
            name="fk_contacts_owner_member",
        ),
        Index("ix_contacts_company_id_updated_at", "company_id", "updated_at"),
        Index("ix_contacts_company_id_status", "company_id", "status"),
        Index("ix_contacts_company_id_owner_user_id", "company_id", "owner_user_id"),
        Index("ix_contacts_tags", "tags", postgresql_using="gin"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    designation: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ContactStatus.NEW.value,
        server_default=ContactStatus.NEW.value,
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list, server_default=text("'{}'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ContactSource.MANUAL.value,
        server_default=ContactSource.MANUAL.value,
    )
    # Small display-only key/value extras (e.g. {"branches": "5"}); queryable facts belong in
    # real columns. Max 20 short string pairs (validated in the API schema).
    attributes: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ContactNote(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "contact_notes"
    __table_args__ = (
        CheckConstraint(
            f"length(btrim(body)) > 0 AND length(body) <= {MAX_NOTE_LENGTH}", name="body_length"
        ),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_contact_notes_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "author_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (author_user_id)",
            name="fk_contact_notes_author_member",
        ),
        Index("ix_contact_notes_company_id_contact_id", "company_id", "contact_id", "created_at"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
