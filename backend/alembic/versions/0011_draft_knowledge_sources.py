"""Record which company-knowledge excerpts (and which prompt version) an AI draft used.

Revision ID: 0011_draft_knowledge_sources
Revises: 0010_google_meet
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_draft_knowledge_sources"
down_revision: str | None = "0010_google_meet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_drafts",
        sa.Column("knowledge_sources", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("message_drafts", sa.Column("prompt_version", sa.String(40)))


def downgrade() -> None:
    op.drop_column("message_drafts", "prompt_version")
    op.drop_column("message_drafts", "knowledge_sources")
