"""Explicit per-call speech recognition language.

Revision ID: 0009_call_language
Revises: 0008_integrations
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_call_language"
down_revision: str | None = "0008_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("language", sa.String(8)))
    op.create_check_constraint(
        "ck_calls_language_valid",
        "calls",
        "language IS NULL OR language IN ('en-IN', 'en-US', 'hi-IN', 'de-DE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_calls_language_valid", "calls", type_="check")
    op.drop_column("calls", "language")
