"""Calendar: per-user connections (encrypted refresh token) and app-created events.
Timeline gains SUMMARY/CALENDAR categories and summary/follow-up/calendar event types.

Revision ID: 0004_calendar
Revises: 0003_call_prep_ai_jobs
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_calendar"
down_revision: str | None = "0003_call_prep_ai_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )


def _timeline_checks(categories: str, types: str) -> None:
    op.drop_constraint("ck_timeline_events_category_valid", "timeline_events", type_="check")
    op.drop_constraint("ck_timeline_events_event_type_valid", "timeline_events", type_="check")
    op.create_check_constraint(
        "ck_timeline_events_category_valid", "timeline_events", f"category IN ({categories})"
    )
    op.create_check_constraint(
        "ck_timeline_events_event_type_valid", "timeline_events", f"event_type IN ({types})"
    )


def upgrade() -> None:
    op.create_table(
        "calendar_connections",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_email", sa.String(320)),
        sa.Column("encrypted_refresh_token", sa.String(2000), nullable=False),
        sa.Column("scopes", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_calendar_connections"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_calendar_connections_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_calendar_connections_company_id_id"),
        sa.UniqueConstraint(
            "company_id", "user_id", "provider",
            name="uq_calendar_connections_company_id_user_id_provider",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "user_id"], ["company_members.company_id", "company_members.user_id"],
            ondelete="CASCADE", name="fk_calendar_connections_member",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REAUTH_REQUIRED')", name="ck_calendar_connections_status_valid"
        ),
    )
    enable_rls("calendar_connections")

    op.create_table(
        "calendar_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("connection_id", UUID),
        sa.Column("created_by_user_id", UUID),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_event_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("starts_at", TS, nullable=False),
        sa.Column("ends_at", TS, nullable=False),
        sa.Column("html_link", sa.String(1000)),
        sa.Column("contact_id", UUID),
        sa.Column("call_id", UUID),
        sa.Column("action_item_id", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_calendar_events"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_calendar_events_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_calendar_events_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "connection_id"],
            ["calendar_connections.company_id", "calendar_connections.id"],
            ondelete="SET NULL (connection_id)", name="fk_calendar_events_connection",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "created_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (created_by_user_id)", name="fk_calendar_events_creator_member",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE", name="fk_calendar_events_contact",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)", name="fk_calendar_events_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "action_item_id"], ["action_items.company_id", "action_items.id"],
            ondelete="SET NULL (action_item_id)", name="fk_calendar_events_action_item",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_calendar_events_ends_after_starts"),
    )
    op.create_index(
        "ix_calendar_events_company_id_contact_id", "calendar_events", ["company_id", "contact_id"]
    )
    enable_rls("calendar_events")

    _timeline_checks("'CONTACT', 'STATUS_CHANGE', 'NOTE', 'CALL', 'TASK', 'FOLLOW_UP', 'APPOINTMENT', 'SUMMARY', 'CALENDAR'", "'CONTACT_CREATED', 'CONTACT_UPDATED', 'STATUS_CHANGED', 'NOTE_ADDED', 'CALL_PLANNED', 'CALL_STATUS_CHANGED', 'ACTION_ITEM_CREATED', 'ACTION_ITEM_COMPLETED', 'CALL_SUMMARY', 'FOLLOW_UP_DRAFTED', 'CALENDAR_EVENT_CREATED'")


def downgrade() -> None:
    op.execute(
        "DELETE FROM timeline_events WHERE category IN ('SUMMARY', 'CALENDAR') "
        "OR event_type IN ('CALL_SUMMARY', 'FOLLOW_UP_DRAFTED', 'CALENDAR_EVENT_CREATED')"
    )
    _timeline_checks("'CONTACT', 'STATUS_CHANGE', 'NOTE', 'CALL', 'TASK', 'FOLLOW_UP', 'APPOINTMENT'", "'CONTACT_CREATED', 'CONTACT_UPDATED', 'STATUS_CHANGED', 'NOTE_ADDED', 'CALL_PLANNED', 'CALL_STATUS_CHANGED', 'ACTION_ITEM_CREATED', 'ACTION_ITEM_COMPLETED'")
    op.drop_table("calendar_events")
    op.drop_table("calendar_connections")
