"""Call reliability and editable content: ENDING state, recovery columns, editable transcript and
summary, system-task read policy for the call recovery sweep.

Revision ID: 0013_call_reliability
Revises: 0012_stakeholder_notes
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_call_reliability"
down_revision: str | None = "0012_stakeholder_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
STATUS_OLD = (
    "'PLANNED', 'INITIATED', 'RINGING', 'CONNECTED', 'ACTIVE', 'COMPLETED', 'NO_ANSWER', "
    "'CANCELLED', 'FAILED'"
)
STATUS_NEW = STATUS_OLD.replace("'ACTIVE',", "'ACTIVE', 'ENDING',")
FIELDS = ("current_solution", "budget", "timeline", "decision_maker", "next_step")
FIELD_STATUS_OLD = "'CONFIRMED', 'INFERRED', 'NOT_DISCUSSED'"
FIELD_STATUS_NEW = FIELD_STATUS_OLD + ", 'EDITED'"
LIVE = "status IN ('INITIATED', 'RINGING', 'CONNECTED', 'ACTIVE', 'ENDING')"


def _replace_check(name: str, table: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    # ---- calls -------------------------------------------------------------------------------
    op.add_column("calls", sa.Column("end_requested_at", TS))
    op.add_column("calls", sa.Column("last_activity_at", TS))
    op.add_column("calls", sa.Column("finalized_at", TS))
    _replace_check("ck_calls_status_valid", "calls", f"status IN ({STATUS_NEW})")
    op.create_index(
        "ix_calls_live_recovery", "calls", ["status", "updated_at"], postgresql_where=sa.text(LIVE)
    )
    # Calls that already ended before this migration have nothing left to finalise. The table
    # FORCEs row-level security, so this cross-tenant data fix runs with it briefly relaxed for the
    # owner (inside the migration transaction).
    op.execute("ALTER TABLE calls NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE calls SET finalized_at = COALESCE(ended_at, now()) WHERE status IN ('COMPLETED', 'NO_ANSWER', 'CANCELLED', 'FAILED')"
    )
    op.execute("ALTER TABLE calls FORCE ROW LEVEL SECURITY")
    # The recovery sweep must find stuck calls across tenants. It may only READ calls, and only in
    # a transaction that declares app.system_task = 'call_recovery'; every change it makes then
    # happens under the owning tenant's normal context (same pattern as the retention sweep).
    op.execute(
        "CREATE POLICY recovery_select ON calls FOR SELECT "
        "USING (current_setting('app.system_task', true) = 'call_recovery')"
    )

    # ---- editable transcript -----------------------------------------------------------------
    op.add_column("transcript_segments", sa.Column("original_text", sa.Text()))
    op.add_column("transcript_segments", sa.Column("edited_at", TS))
    op.add_column("transcript_segments", sa.Column("edited_by_user_id", sa.UUID()))
    op.create_foreign_key(
        "fk_transcript_segments_editor_member",
        "transcript_segments",
        "company_members",
        ["company_id", "edited_by_user_id"],
        ["company_id", "user_id"],
        ondelete="SET NULL (edited_by_user_id)",
    )

    # ---- editable summary --------------------------------------------------------------------
    op.add_column("call_summaries", sa.Column("original_summary", sa.Text()))
    op.add_column("call_summaries", sa.Column("edited_at", TS))
    op.add_column("call_summaries", sa.Column("edited_by_user_id", sa.UUID()))
    op.create_foreign_key(
        "fk_call_summaries_editor_member",
        "call_summaries",
        "company_members",
        ["company_id", "edited_by_user_id"],
        ["company_id", "user_id"],
        ondelete="SET NULL (edited_by_user_id)",
    )
    for f in FIELDS:
        _replace_check(
            f"ck_call_summaries_{f}_status_valid",
            "call_summaries",
            f"{f}_status IN ({FIELD_STATUS_NEW})",
        )


def downgrade() -> None:
    op.execute("ALTER TABLE call_summaries NO FORCE ROW LEVEL SECURITY")
    for f in FIELDS:
        op.execute(f"UPDATE call_summaries SET {f}_status = 'INFERRED' WHERE {f}_status = 'EDITED'")
        _replace_check(
            f"ck_call_summaries_{f}_status_valid",
            "call_summaries",
            f"{f}_status IN ({FIELD_STATUS_OLD})",
        )
    op.execute("ALTER TABLE call_summaries FORCE ROW LEVEL SECURITY")
    op.drop_constraint("fk_call_summaries_editor_member", "call_summaries", type_="foreignkey")
    for col in ("edited_by_user_id", "edited_at", "original_summary"):
        op.drop_column("call_summaries", col)

    op.drop_constraint(
        "fk_transcript_segments_editor_member", "transcript_segments", type_="foreignkey"
    )
    for col in ("edited_by_user_id", "edited_at", "original_text"):
        op.drop_column("transcript_segments", col)

    op.execute("DROP POLICY IF EXISTS recovery_select ON calls")
    op.execute("ALTER TABLE calls NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE calls SET status = 'COMPLETED' WHERE status = 'ENDING'")
    op.execute("ALTER TABLE calls FORCE ROW LEVEL SECURITY")
    _replace_check("ck_calls_status_valid", "calls", f"status IN ({STATUS_OLD})")
    op.drop_index("ix_calls_live_recovery", table_name="calls")
    for col in ("finalized_at", "last_activity_at", "end_requested_at"):
        op.drop_column("calls", col)
