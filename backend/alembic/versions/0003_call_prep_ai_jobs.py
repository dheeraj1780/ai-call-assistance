"""Call preparation: agenda items; AI usage records; background jobs.

Revision ID: 0003_call_prep_ai_jobs
Revises: 0002_crm
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_call_prep_ai_jobs"
down_revision: str | None = "0002_crm"
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


def upgrade() -> None:
    op.create_table(
        "agenda_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.String(500)),
        sa.Column("description", sa.Text),
        sa.Column("keywords", sa.String(500)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_source", sa.String(16), nullable=False),
        sa.Column("status_confidence", sa.Float),
        sa.Column("status_reason", sa.String(300)),
        sa.Column("completed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_agenda_items"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_agenda_items_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_agenda_items_company_id_id"),
        sa.UniqueConstraint("call_id", "position", name="uq_agenda_items_call_id_position"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_agenda_items_call",
        ),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_agenda_items_status_valid",
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL', 'AI', 'AI_EDITED')", name="ck_agenda_items_source_valid"
        ),
        sa.CheckConstraint(
            "status_source IN ('DEFAULT', 'DETERMINISTIC', 'AI', 'MANUAL')",
            name="ck_agenda_items_status_source_valid",
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_agenda_items_title_not_blank"),
        sa.CheckConstraint(
            "status_confidence IS NULL OR (status_confidence >= 0 AND status_confidence <= 1)",
            name="ck_agenda_items_confidence_range",
        ),
    )
    op.create_index("ix_agenda_items_company_id_call_id", "agenda_items", ["company_id", "call_id"])
    enable_rls("agenda_items")

    op.create_table(
        "ai_usage_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("units", sa.Integer, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error_code", sa.String(32)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_ai_usage_records"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_ai_usage_records_company_id_companies",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="SET NULL (call_id)", name="fk_ai_usage_records_call",
        ),
    )
    op.create_index(
        "ix_ai_usage_records_company_id_created_at", "ai_usage_records", ["company_id", "created_at"]
    )
    op.create_index(
        "ix_ai_usage_records_company_id_call_id", "ai_usage_records", ["company_id", "call_id"]
    )
    enable_rls("ai_usage_records")

    # System table: no RLS (the worker claims jobs across tenants). Holds ids only.
    op.create_table(
        "jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("run_after", TS, nullable=False, server_default=NOW),
        sa.Column("locked_until", TS),
        sa.Column("dedupe_key", sa.String(128)),
        sa.Column("last_error", sa.String(200)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')", name="ck_jobs_status_valid"
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name="ck_jobs_attempts_valid"
        ),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])
    op.create_index(
        "uq_jobs_dedupe_key", "jobs", ["dedupe_key"], unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("ai_usage_records")
    op.drop_table("agenda_items")
