"""CRM: contacts, contact notes, calls, action items, timeline events (all tenant-scoped, RLS).

Revision ID: 0002_crm
Revises: 0001_foundation
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_crm"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
TABLES = ("contacts", "contact_notes", "calls", "action_items", "timeline_events")


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id"], ["companies.id"], ondelete="CASCADE", name=f"fk_{table}_company_id_companies"
    )


def _member_fk(table: str, column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id", column],
        ["company_members.company_id", "company_members.user_id"],
        ondelete=f"SET NULL ({column})",
        name=name,
    )


def enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_company_members_company_id_user_id", "company_members", ["company_id", "user_id"]
    )

    op.create_table(
        "contacts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("organization", sa.String(200)),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(320)),
        sa.Column("designation", sa.String(120)),
        sa.Column("status", sa.String(16), nullable=False, server_default="NEW"),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String(40)), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("notes", sa.Text),
        sa.Column("source", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column(
            "attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("owner_user_id", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_contacts"),
        _tenant_fk("contacts"),
        sa.UniqueConstraint("company_id", "id", name="uq_contacts_company_id_id"),
        _member_fk("contacts", "owner_user_id", "fk_contacts_owner_member"),
        sa.CheckConstraint(
            "status IN ('NEW', 'CONTACTED', 'QUALIFIED', 'PROPOSAL', 'NEGOTIATION', 'WON', 'LOST')",
            name="ck_contacts_status_valid",
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL', 'REFERRAL', 'WEBSITE', 'INBOUND_CALL', 'EVENT', 'IMPORT', 'OTHER')",
            name="ck_contacts_source_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="ck_contacts_attributes_object"
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_contacts_name_not_blank"),
        sa.CheckConstraint("cardinality(tags) <= 20", name="ck_contacts_tags_max"),
        sa.CheckConstraint(
            "email IS NULL OR email = lower(email)", name="ck_contacts_email_lowercase"
        ),
    )
    op.create_index("ix_contacts_company_id_updated_at", "contacts", ["company_id", "updated_at"])
    op.create_index("ix_contacts_company_id_status", "contacts", ["company_id", "status"])
    op.create_index(
        "ix_contacts_company_id_owner_user_id", "contacts", ["company_id", "owner_user_id"]
    )
    op.create_index("ix_contacts_tags", "contacts", ["tags"], postgresql_using="gin")

    op.create_table(
        "contact_notes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("author_user_id", UUID),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_contact_notes"),
        _tenant_fk("contact_notes"),
        sa.UniqueConstraint("company_id", "id", name="uq_contact_notes_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_contact_notes_contact",
        ),
        _member_fk("contact_notes", "author_user_id", "fk_contact_notes_author_member"),
        sa.CheckConstraint(
            "length(btrim(body)) > 0 AND length(body) <= 10000", name="ck_contact_notes_body_length"
        ),
    )
    op.create_index(
        "ix_contact_notes_company_id_contact_id",
        "contact_notes",
        ["company_id", "contact_id", "created_at"],
    )

    op.create_table(
        "calls",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("user_id", UUID),
        sa.Column("objective", sa.Text),
        sa.Column("desired_outcome", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="PLANNED"),
        sa.Column("scheduled_at", TS),
        sa.Column("started_at", TS),
        sa.Column("ended_at", TS),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("outcome", sa.String(32)),
        sa.Column("outcome_notes", sa.Text),
        sa.Column("next_step", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_calls"),
        _tenant_fk("calls"),
        sa.UniqueConstraint("company_id", "id", name="uq_calls_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_calls_contact",
        ),
        _member_fk("calls", "user_id", "fk_calls_user_member"),
        sa.CheckConstraint(
            "status IN ('PLANNED', 'INITIATED', 'RINGING', 'CONNECTED', 'ACTIVE', 'COMPLETED', "
            "'NO_ANSWER', 'CANCELLED', 'FAILED')",
            name="ck_calls_status_valid",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('INTERESTED', 'NOT_INTERESTED', 'FOLLOW_UP_REQUIRED', "
            "'MEETING_BOOKED', 'WON', 'LOST', 'NO_DECISION')",
            name="ck_calls_outcome_valid",
        ),
        sa.CheckConstraint(
            "objective IS NULL OR length(objective) <= 2000", name="ck_calls_objective_length"
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ck_calls_ended_after_started",
        ),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_calls_duration_non_negative",
        ),
    )
    op.create_index(
        "ix_calls_company_id_contact_id", "calls", ["company_id", "contact_id", "created_at"]
    )
    op.create_index("ix_calls_company_id_status", "calls", ["company_id", "status"])
    op.create_index("ix_calls_company_id_user_id", "calls", ["company_id", "user_id"])

    op.create_table(
        "action_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("contact_id", UUID),
        sa.Column("call_id", UUID),
        sa.Column("kind", sa.String(16), nullable=False, server_default="TASK"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("assignee_user_id", UUID),
        sa.Column("created_by_user_id", UUID),
        sa.Column("due_at", TS),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("source", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column("confirmed_at", TS),
        sa.Column("confirmed_by_user_id", UUID),
        sa.Column("completed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_action_items"),
        _tenant_fk("action_items"),
        sa.UniqueConstraint("company_id", "id", name="uq_action_items_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_action_items_contact",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)",
            name="fk_action_items_call",
        ),
        _member_fk("action_items", "assignee_user_id", "fk_action_items_assignee_member"),
        _member_fk("action_items", "created_by_user_id", "fk_action_items_creator_member"),
        _member_fk("action_items", "confirmed_by_user_id", "fk_action_items_confirmer_member"),
        sa.CheckConstraint(
            "kind IN ('TASK', 'FOLLOW_UP', 'APPOINTMENT')", name="ck_action_items_kind_valid"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'DONE', 'CANCELLED')",
            name="ck_action_items_status_valid",
        ),
        sa.CheckConstraint("source IN ('MANUAL', 'AI')", name="ck_action_items_source_valid"),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_action_items_title_not_blank"),
        sa.CheckConstraint(
            "(status = 'DONE') = (completed_at IS NOT NULL)",
            name="ck_action_items_completed_at_matches_status",
        ),
        sa.CheckConstraint(
            "confirmed_by_user_id IS NULL OR confirmed_at IS NOT NULL",
            name="ck_action_items_confirmation_consistent",
        ),
    )
    op.create_index(
        "ix_action_items_company_id_assignee_status",
        "action_items",
        ["company_id", "assignee_user_id", "status"],
    )
    op.create_index(
        "ix_action_items_company_id_contact_id", "action_items", ["company_id", "contact_id"]
    )
    op.create_index("ix_action_items_company_id_call_id", "action_items", ["company_id", "call_id"])
    op.create_index("ix_action_items_company_id_due_at", "action_items", ["company_id", "due_at"])

    op.create_table(
        "timeline_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False, server_default=NOW),
        sa.Column("actor_user_id", UUID),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32)),
        sa.Column("note_id", UUID),
        sa.Column("call_id", UUID),
        sa.Column("action_item_id", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_timeline_events"),
        _tenant_fk("timeline_events"),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"],
            ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_contact",
        ),
        _member_fk("timeline_events", "actor_user_id", "fk_timeline_events_actor_member"),
        sa.ForeignKeyConstraint(
            ["company_id", "note_id"],
            ["contact_notes.company_id", "contact_notes.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_note",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"],
            ["calls.company_id", "calls.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "action_item_id"],
            ["action_items.company_id", "action_items.id"],
            ondelete="CASCADE",
            name="fk_timeline_events_action_item",
        ),
        sa.CheckConstraint(
            "category IN ('CONTACT', 'STATUS_CHANGE', 'NOTE', 'CALL', 'TASK', 'FOLLOW_UP', "
            "'APPOINTMENT')",
            name="ck_timeline_events_category_valid",
        ),
        sa.CheckConstraint(
            "event_type IN ('CONTACT_CREATED', 'CONTACT_UPDATED', 'STATUS_CHANGED', 'NOTE_ADDED', 'CALL_PLANNED', "
            "'CALL_STATUS_CHANGED', 'ACTION_ITEM_CREATED', 'ACTION_ITEM_COMPLETED')",
            name="ck_timeline_events_event_type_valid",
        ),
    )
    op.create_index(
        "ix_timeline_events_contact_occurred",
        "timeline_events",
        ["company_id", "contact_id", "occurred_at", "id"],
    )

    for table in TABLES:
        enable_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_constraint(
        "uq_company_members_company_id_user_id", "company_members", type_="unique"
    )
