"""Tenant (company) and membership models. Both tables are protected by Row-Level Security."""

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.models import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_TRANSCRIPT_RETENTION_DAYS = 30


class MemberRole(enum.StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "transcript_retention_days BETWEEN 1 AND 365", name="transcript_retention_days_range"
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(500))
    products_services: Mapped[str | None] = mapped_column(Text)
    target_customer: Mapped[str | None] = mapped_column(Text)
    # Free-text guidance for the AI. Treated as tenant-authored, lower-trust input in prompts.
    ai_instructions: Mapped[str | None] = mapped_column(Text)
    transcript_retention_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_TRANSCRIPT_RETENTION_DAYS,
        server_default=text(str(DEFAULT_TRANSCRIPT_RETENTION_DAYS)),
    )


class CompanyMember(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "company_members"
    __table_args__ = (
        CheckConstraint("role IN ('OWNER', 'ADMIN', 'MEMBER')", name="role_valid"),
        # MVP decision (ADR-004): a user belongs to exactly one company.
        UniqueConstraint("user_id"),
        # Target for composite FKs that reference a member (owner/assignee/author) so the
        # referenced user is guaranteed to belong to the same company.
        UniqueConstraint("company_id", "user_id"),
        # Target for composite (company_id, id) foreign keys from tenant-owned child tables.
        UniqueConstraint("company_id", "id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
