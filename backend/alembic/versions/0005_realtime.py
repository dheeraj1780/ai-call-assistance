"""Real-time calling: telephony routing + webhook idempotency, transcript segments (retention),
copilot insights, structured call notes.

Revision ID: 0005_realtime
Revises: 0004_calendar
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_realtime"
down_revision: str | None = "0004_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def enable_rls(table: str, *, retention: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )
    if retention:
        # The retention sweep may see and delete ONLY expired rows, across tenants, and only
        # in a transaction that declares app.system_task = 'retention'.
        cond = "current_setting('app.system_task', true) = 'retention' AND expires_at < now()"
        op.execute(f"CREATE POLICY retention_select ON {table} FOR SELECT USING ({cond})")
        op.execute(f"CREATE POLICY retention_delete ON {table} FOR DELETE USING ({cond})")


def _tenant(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name=f"fk_{table}_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id"),
    ]


def upgrade() -> None:
    op.add_column("calls", sa.Column("provider", sa.String(16)))
    op.add_column("calls", sa.Column("provider_call_id", sa.String(128)))
    op.add_column("calls", sa.Column("telephony_error", sa.String(64)))

    # System tables (no RLS, no personal data): tenant routing for webhooks, idempotency.
    op.create_table(
        "call_routes",
        sa.Column("call_id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_call_id", sa.String(128)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("call_id", name="pk_call_routes"),
        sa.UniqueConstraint(
            "provider", "provider_call_id", name="uq_call_routes_provider_provider_call_id"
        ),
    )
    op.create_table(
        "telephony_webhook_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("provider_call_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("call_id", UUID),
        sa.Column("applied", sa.String(16), nullable=False, server_default="applied"),
        sa.Column("received_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_telephony_webhook_events"),
        sa.UniqueConstraint(
            "provider", "event_id", name="uq_telephony_webhook_events_provider_event_id"
        ),
    )
    op.create_index(
        "ix_telephony_webhook_events_call_id", "telephony_webhook_events", ["call_id"]
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("speaker", sa.String(16), nullable=False),
        sa.Column("speaker_confidence", sa.Float),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("start_ms", sa.Integer, nullable=False),
        sa.Column("end_ms", sa.Integer, nullable=False),
        sa.Column("stt_confidence", sa.Float),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_transcript_segments"),
        *_tenant("transcript_segments"),
        sa.UniqueConstraint("call_id", "seq", name="uq_transcript_segments_call_id_seq"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_transcript_segments_call",
        ),
        sa.CheckConstraint(
            "speaker IN ('SALES_REP', 'CUSTOMER', 'UNKNOWN')",
            name="ck_transcript_segments_speaker_valid",
        ),
        sa.CheckConstraint("length(text) <= 5000", name="ck_transcript_segments_text_length"),
        sa.CheckConstraint(
            "speaker_confidence IS NULL OR (speaker_confidence >= 0 AND speaker_confidence <= 1)",
            name="ck_transcript_segments_speaker_confidence_range",
        ),
    )
    op.create_index(
        "ix_transcript_segments_company_id_call_id", "transcript_segments",
        ["company_id", "call_id", "seq"],
    )
    op.create_index("ix_transcript_segments_expires_at", "transcript_segments", ["expires_at"])
    enable_rls("transcript_segments", retention=True)

    op.create_table(
        "copilot_insights",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(8), nullable=False),
        sa.Column("content", sa.String(1000), nullable=False),
        sa.Column("context", sa.String(500)),
        sa.Column("confidence", sa.Float),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_segment_id", UUID),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_copilot_insights"),
        *_tenant("copilot_insights"),
        sa.UniqueConstraint("call_id", "dedupe_key", name="uq_copilot_insights_call_id_dedupe_key"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_copilot_insights_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "source_segment_id"],
            ["transcript_segments.company_id", "transcript_segments.id"],
            ondelete="SET NULL (source_segment_id)", name="fk_copilot_insights_segment",
        ),
        sa.CheckConstraint(
            "type IN ('QUESTION_SUGGESTION', 'MISSING_AGENDA_ITEM', 'CUSTOMER_REQUIREMENT', "
            "'OBJECTION', 'IMPORTANT_FACT', 'NEXT_STEP', 'KNOWLEDGE_RESULT')",
            name="ck_copilot_insights_type_valid",
        ),
        sa.CheckConstraint(
            "priority IN ('LOW', 'MEDIUM', 'HIGH')", name="ck_copilot_insights_priority_valid"
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL', 'DETERMINISTIC', 'AI', 'KNOWLEDGE')",
            name="ck_copilot_insights_source_valid",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISMISSED')", name="ck_copilot_insights_status_valid"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_copilot_insights_confidence_range",
        ),
    )
    op.create_index(
        "ix_copilot_insights_company_id_call_id", "copilot_insights", ["company_id", "call_id"]
    )
    op.create_index("ix_copilot_insights_expires_at", "copilot_insights", ["expires_at"])
    enable_rls("copilot_insights", retention=True)

    op.create_table(
        "call_notes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("call_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("category", sa.String(24)),
        sa.Column("text", sa.String(1000), nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_segment_id", UUID),
        sa.Column("author_user_id", UUID),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("reviewed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_call_notes"),
        *_tenant("call_notes"),
        sa.UniqueConstraint("call_id", "dedupe_key", name="uq_call_notes_call_id_dedupe_key"),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_call_notes_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE", name="fk_call_notes_contact",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "source_segment_id"],
            ["transcript_segments.company_id", "transcript_segments.id"],
            ondelete="SET NULL (source_segment_id)", name="fk_call_notes_segment",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "author_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (author_user_id)", name="fk_call_notes_author_member",
        ),
        sa.CheckConstraint(
            "kind IN ('REQUIREMENT', 'PAIN_POINT', 'CURRENT_SOLUTION', 'BUDGET', 'TIMELINE', "
            "'DECISION_MAKER', 'COMPETITOR', 'OBJECTION', 'PREFERENCE', 'NEXT_STEP', "
            "'IMPORTANT_FACT', 'GENERAL')",
            name="ck_call_notes_kind_valid",
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('PRICE', 'EXISTING_SOLUTION', 'TIMING', "
            "'AUTHORITY', 'TRUST', 'FEATURE_GAP', 'COMPETITOR', 'IMPLEMENTATION', 'OTHER')",
            name="ck_call_notes_category_valid",
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL', 'DETERMINISTIC', 'AI', 'KNOWLEDGE')",
            name="ck_call_notes_source_valid",
        ),
        sa.CheckConstraint(
            "status IN ('SUGGESTED', 'CONFIRMED', 'EDITED', 'REJECTED')",
            name="ck_call_notes_status_valid",
        ),
        sa.CheckConstraint("length(btrim(text)) > 0", name="ck_call_notes_text_not_blank"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_call_notes_confidence_range",
        ),
    )
    op.create_index("ix_call_notes_company_id_call_id", "call_notes", ["company_id", "call_id"])
    op.create_index(
        "ix_call_notes_company_id_contact_id_kind", "call_notes", ["company_id", "contact_id", "kind"]
    )
    enable_rls("call_notes")


def downgrade() -> None:
    op.drop_table("call_notes")
    op.drop_table("copilot_insights")
    op.drop_table("transcript_segments")
    op.drop_table("telephony_webhook_events")
    op.drop_table("call_routes")
    op.drop_column("calls", "telephony_error")
    op.drop_column("calls", "provider_call_id")
    op.drop_column("calls", "provider")
