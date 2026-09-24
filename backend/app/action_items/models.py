"""Action items: tasks, follow-ups and appointments, optionally linked to a contact/call.

Items created by users are confirmed on creation. Items proposed by the AI (later phases,
``source = AI``) stay unconfirmed until a user confirms them.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.models import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin, sql_in
from app.contacts.models import Contact


class ActionItemKind(enum.StrEnum):
    TASK = "TASK"
    FOLLOW_UP = "FOLLOW_UP"
    APPOINTMENT = "APPOINTMENT"


class ActionItemStatus(enum.StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class ActionItemSource(enum.StrEnum):
    MANUAL = "MANUAL"
    AI = "AI"


class ActionItem(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "action_items"
    __table_args__ = (
        CheckConstraint(f"kind IN ({sql_in(ActionItemKind)})", name="kind_valid"),
        CheckConstraint(f"status IN ({sql_in(ActionItemStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({sql_in(ActionItemSource)})", name="source_valid"),
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint(
            "(status = 'DONE') = (completed_at IS NOT NULL)", name="completed_at_matches_status"
        ),
        # A confirmer implies a confirmation time (the confirmer may later be cleared if
        # they leave the company, so the reverse does not hold).
        CheckConstraint(
            "confirmed_by_user_id IS NULL OR confirmed_at IS NOT NULL",
            name="confirmation_consistent",
        ),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_action_items_contact",
        ),
        ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)",
            name="fk_action_items_call",
        ),
        ForeignKeyConstraint(
            ["company_id", "assignee_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (assignee_user_id)",
            name="fk_action_items_assignee_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "created_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (created_by_user_id)",
            name="fk_action_items_creator_member",
        ),
        ForeignKeyConstraint(
            ["company_id", "confirmed_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (confirmed_by_user_id)",
            name="fk_action_items_confirmer_member",
        ),
        Index(
            "ix_action_items_company_id_assignee_status", "company_id", "assignee_user_id", "status"
        ),
        Index("ix_action_items_company_id_contact_id", "company_id", "contact_id"),
        Index("ix_action_items_company_id_call_id", "company_id", "call_id"),
        Index("ix_action_items_company_id_due_at", "company_id", "due_at"),
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ActionItemKind.TASK.value,
        server_default=ActionItemKind.TASK.value,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ActionItemStatus.OPEN.value,
        server_default=ActionItemStatus.OPEN.value,
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ActionItemSource.MANUAL.value,
        server_default=ActionItemSource.MANUAL.value,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    contact: Mapped[Contact | None] = relationship(
        Contact,
        primaryjoin=(
            "and_(ActionItem.company_id == Contact.company_id, ActionItem.contact_id == Contact.id)"
        ),
        foreign_keys="[ActionItem.company_id, ActionItem.contact_id]",
        viewonly=True,
        lazy="raise",
    )
