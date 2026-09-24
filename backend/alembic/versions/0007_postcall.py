"""Post-call: call summaries (grounded fields) and follow-up drafts (never auto-sent).

Revision ID: 0007_postcall
Revises: 0006_knowledge
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_postcall"
down_revision: str | None = "0006_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
FIELDS = ("current_solution", "budget", "timeline", "decision_maker", "next_step")
FIELD_STATUS = "('CONFIRMED', 'INFERRED', 'NOT_DISCUSSED')"


def enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )


def upgrade() -> None:
    field_columns: list[sa.Column] = []
    field_checks: list[sa.CheckConstraint] = []
    for f in FIELDS:
        field_columns.append(sa.Column(f, sa.String(500)))
        field_columns.append(sa.Column(f"{f}_status", sa.String(16), nullable=False))
        field_checks.append(
            sa.CheckConstraint(
                f"{f}_status IN {FIELD_STATUS}", name=f"ck_call_summaries_{f}_status_valid"
            )
        )
    op.create_table(
        "call_summaries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("summary", sa.Text),
        sa.Column("suggested_outcome", sa.String(32)),
        *field_columns,
        sa.Column("generated_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_call_summaries"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_call_summaries_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_call_summaries_company_id_id"),
        sa.UniqueConstraint("call_id", name="uq_call_summaries_call_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_call_summaries_call",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'READY', 'FAILED')", name="ck_call_summaries_status_valid"
        ),
        *field_checks,
    )
    enable_rls("call_summaries")

    op.create_table(
        "follow_up_drafts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(300)),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("warnings", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reviewed_by_user_id", UUID),
        sa.Column("reviewed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_follow_up_drafts"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_follow_up_drafts_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_follow_up_drafts_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_follow_up_drafts_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE", name="fk_follow_up_drafts_contact",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "reviewed_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (reviewed_by_user_id)", name="fk_follow_up_drafts_reviewer_member",
        ),
        sa.CheckConstraint(
            "channel IN ('EMAIL', 'WHATSAPP', 'GENERAL')", name="ck_follow_up_drafts_channel_valid"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'COPIED', 'DISCARDED')",
            name="ck_follow_up_drafts_status_valid",
        ),
        sa.CheckConstraint("source IN ('AI', 'MANUAL')", name="ck_follow_up_drafts_source_valid"),
        sa.CheckConstraint("length(body) <= 5000", name="ck_follow_up_drafts_body_length"),
    )
    op.create_index(
        "ix_follow_up_drafts_company_id_call_id", "follow_up_drafts", ["company_id", "call_id"]
    )
    enable_rls("follow_up_drafts")


def downgrade() -> None:
    op.drop_table("follow_up_drafts")
    op.drop_table("call_summaries")
