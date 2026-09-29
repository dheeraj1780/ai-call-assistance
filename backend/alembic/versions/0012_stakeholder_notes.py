"""Structured notes can record a stakeholder (a person/role involved in the customer's decision).

Revision ID: 0012_stakeholder_notes
Revises: 0011_draft_knowledge_sources
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_stakeholder_notes"
down_revision: str | None = "0011_draft_knowledge_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'REQUIREMENT', 'PAIN_POINT', 'CURRENT_SOLUTION', 'BUDGET', 'TIMELINE', "
    "'DECISION_MAKER', 'COMPETITOR', 'OBJECTION', 'PREFERENCE', 'NEXT_STEP', "
    "'IMPORTANT_FACT', 'GENERAL'"
)


def upgrade() -> None:
    op.drop_constraint("ck_call_notes_kind_valid", "call_notes", type_="check")
    op.create_check_constraint(
        "ck_call_notes_kind_valid", "call_notes", f"kind IN ({_KINDS}, 'STAKEHOLDER')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM call_notes WHERE kind = 'STAKEHOLDER'")
    op.drop_constraint("ck_call_notes_kind_valid", "call_notes", type_="check")
    op.create_check_constraint("ck_call_notes_kind_valid", "call_notes", f"kind IN ({_KINDS})")
