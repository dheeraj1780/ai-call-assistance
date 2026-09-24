"""Multi-channel communication: integrations (encrypted credentials), webhook routing and
idempotency, the provider-independent conversation model, call channels (Teams) with explicit
transcript persistence, message-conversation notes and timeline channels.

Revision ID: 0008_integrations
Revises: 0007_postcall
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_integrations"
down_revision: str | None = "0007_postcall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
JSON_OBJ = sa.text("'{}'::jsonb")
JSON_ARR = sa.text("'[]'::jsonb")

PROVIDERS = "'MICROSOFT_TEAMS', 'WHATSAPP', 'PLIVO'"
CHANNELS = "'PHONE', 'TEAMS', 'WHATSAPP'"
CAPABILITIES = (
    "'MESSAGE', 'REAL_TIME_CALL', 'PHONE_CALL', 'MEDIA_STREAM', 'WHATSAPP_VOICE_CALL', "
    "'CONTACT_SYNC', 'WEBHOOK'"
)
EVENT_TYPES = (
    "'MESSAGE_RECEIVED', 'MESSAGE_SENT', 'MESSAGE_STATUS_UPDATED', 'CALL_STARTED', "
    "'CALL_CONNECTED', 'CALL_ENDED', 'AUDIO_STREAM_STARTED', 'AUDIO_STREAM_ENDED', "
    "'TRANSCRIPT_PARTIAL', 'TRANSCRIPT_FINAL', 'CUSTOMER_JOINED', 'SALESPERSON_JOINED', "
    "'RECORDING_STATUS_CHANGED'"
)
TIMELINE_CATEGORIES_OLD = (
    "'CONTACT', 'STATUS_CHANGE', 'NOTE', 'CALL', 'TASK', 'FOLLOW_UP', 'APPOINTMENT', "
    "'SUMMARY', 'CALENDAR'"
)
TIMELINE_TYPES_OLD = (
    "'CONTACT_CREATED', 'CONTACT_UPDATED', 'STATUS_CHANGED', 'NOTE_ADDED', 'CALL_PLANNED', "
    "'CALL_STATUS_CHANGED', 'ACTION_ITEM_CREATED', 'ACTION_ITEM_COMPLETED', 'CALL_SUMMARY', "
    "'FOLLOW_UP_DRAFTED', 'CALENDAR_EVENT_CREATED'"
)


def enable_rls(table: str, *, retention: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )
    if retention:
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


def _member_fk(table: str, column: str, name: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["company_id", column],
        ["company_members.company_id", "company_members.user_id"],
        ondelete=ondelete, name=name,
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
    # ---- integrations ----------------------------------------------------------------------
    op.create_table(
        "integrations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default=JSON_OBJ),
        sa.Column("secrets_ciphertext", sa.Text),
        sa.Column("secret_keys", postgresql.JSONB, nullable=False, server_default=JSON_ARR),
        sa.Column(
            "enabled_capabilities", postgresql.JSONB, nullable=False, server_default=JSON_ARR
        ),
        sa.Column("config_version", sa.Integer, nullable=False),
        sa.Column("last_test", postgresql.JSONB),
        sa.Column("last_tested_at", TS),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_integrations"),
        *_tenant("integrations"),
        sa.UniqueConstraint("company_id", "provider", name="uq_integrations_company_id_provider"),
        sa.CheckConstraint(f"provider IN ({PROVIDERS})", name="ck_integrations_provider_valid"),
        sa.CheckConstraint("mode IN ('LIVE', 'MOCK')", name="ck_integrations_mode_valid"),
        sa.CheckConstraint("jsonb_typeof(config) = 'object'", name="ck_integrations_config_object"),
    )
    enable_rls("integrations")

    # System tables (no RLS, no personal data, no payloads).
    op.create_table(
        "integration_routes",
        sa.Column("integration_id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("external_account_id", sa.String(128)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("integration_id", name="pk_integration_routes"),
        sa.UniqueConstraint(
            "provider", "external_account_id",
            name="uq_integration_routes_provider_external_account_id",
        ),
    )
    op.create_table(
        "integration_webhook_receipts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("integration_id", UUID),
        sa.Column("dedupe_key", sa.String(300), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_integration_webhook_receipts"),
        sa.UniqueConstraint(
            "provider", "dedupe_key", name="uq_integration_webhook_receipts_provider_dedupe_key"
        ),
    )
    op.create_index(
        "ix_integration_webhook_receipts_received_at",
        "integration_webhook_receipts",
        ["received_at"],
    )

    op.create_table(
        "integration_user_connections",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("integration_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("external_user_id", sa.String(128)),
        sa.Column("account_email", sa.String(320)),
        sa.Column("encrypted_refresh_token", sa.Text, nullable=False),
        sa.Column("scopes", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_integration_user_connections"),
        *_tenant("integration_user_connections"),
        sa.UniqueConstraint(
            "company_id", "user_id", "provider",
            name="uq_integration_user_connections_company_id_user_id_provider",
        ),
        _member_fk(
            "integration_user_connections", "user_id",
            "fk_integration_user_connections_member", "CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "integration_id"], ["integrations.company_id", "integrations.id"],
            ondelete="CASCADE", name="fk_integration_user_connections_integration",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REAUTH_REQUIRED')",
            name="ck_integration_user_connections_status_valid",
        ),
    )
    enable_rls("integration_user_connections")

    op.create_table(
        "integration_subscriptions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("integration_id", UUID, nullable=False),
        sa.Column("user_connection_id", UUID),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("resource", sa.String(500), nullable=False),
        sa.Column("external_subscription_id", sa.String(128), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_integration_subscriptions"),
        *_tenant("integration_subscriptions"),
        sa.UniqueConstraint(
            "provider", "external_subscription_id",
            name="uq_integration_subscriptions_provider_external_subscription_id",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "integration_id"], ["integrations.company_id", "integrations.id"],
            ondelete="CASCADE", name="fk_integration_subscriptions_integration",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "user_connection_id"],
            ["integration_user_connections.company_id", "integration_user_connections.id"],
            ondelete="CASCADE", name="fk_integration_subscriptions_user_connection",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'FAILED', 'DELETED')",
            name="ck_integration_subscriptions_status_valid",
        ),
    )
    op.create_index(
        "ix_integration_subscriptions_expires_at", "integration_subscriptions", ["expires_at"]
    )
    enable_rls("integration_subscriptions")

    # ---- calls: channel + explicit transcript persistence ---------------------------------
    op.add_column(
        "calls", sa.Column("channel", sa.String(16), nullable=False, server_default="PHONE")
    )
    op.add_column("calls", sa.Column("meeting_url", sa.String(2000)))
    op.add_column(
        "calls",
        sa.Column(
            "transcript_persistence", sa.String(32), nullable=False, server_default="PERSISTED"
        ),
    )
    op.create_check_constraint("ck_calls_channel_valid", "calls", "channel IN ('PHONE', 'TEAMS')")
    op.create_check_constraint(
        "ck_calls_transcript_persistence_valid",
        "calls",
        "transcript_persistence IN ('PERSISTED', 'TRANSIENT', 'PENDING_RECORDING_STATUS')",
    )
    op.create_check_constraint(
        "ck_calls_teams_requires_meeting_url", "calls",
        "channel <> 'TEAMS' OR meeting_url IS NOT NULL",
    )

    # ---- conversation model ----------------------------------------------------------------
    op.create_table(
        "communication_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("integration_id", UUID),
        sa.Column("contact_id", UUID),
        sa.Column("call_id", UUID),
        sa.Column("owner_user_id", UUID),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("capability", sa.String(24), nullable=False),
        sa.Column("external_session_id", sa.String(256)),
        sa.Column("external_participant_id", sa.String(256)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("match_status", sa.String(16), nullable=False),
        sa.Column("started_at", TS),
        sa.Column("ended_at", TS),
        sa.Column("last_message_at", TS),
        sa.Column("last_inbound_at", TS),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=JSON_OBJ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_communication_sessions"),
        *_tenant("communication_sessions"),
        sa.UniqueConstraint(
            "company_id", "provider", "capability", "external_session_id",
            name="uq_communication_sessions_external_session",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE", name="fk_communication_sessions_contact",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "call_id"], ["calls.company_id", "calls.id"],
            ondelete="CASCADE", name="fk_communication_sessions_call",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "integration_id"], ["integrations.company_id", "integrations.id"],
            ondelete="SET NULL (integration_id)", name="fk_communication_sessions_integration",
        ),
        _member_fk(
            "communication_sessions", "owner_user_id",
            "fk_communication_sessions_owner_member", "SET NULL (owner_user_id)",
        ),
        sa.CheckConstraint(
            f"provider IN ({PROVIDERS}, 'MOCK')", name="ck_communication_sessions_provider_valid"
        ),
        sa.CheckConstraint(
            f"channel IN ({CHANNELS})", name="ck_communication_sessions_channel_valid"
        ),
        sa.CheckConstraint(
            f"capability IN ({CAPABILITIES})", name="ck_communication_sessions_capability_valid"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ACTIVE', 'ENDED')", name="ck_communication_sessions_status_valid"
        ),
        sa.CheckConstraint(
            "match_status IN ('MATCHED', 'UNMATCHED', 'AMBIGUOUS')",
            name="ck_communication_sessions_match_status_valid",
        ),
        sa.CheckConstraint(
            "(match_status = 'MATCHED') = (contact_id IS NOT NULL)",
            name="ck_communication_sessions_match_has_contact",
        ),
    )
    op.create_index(
        "ix_communication_sessions_company_id_contact_id", "communication_sessions",
        ["company_id", "contact_id", "last_message_at"],
    )
    op.create_index(
        "ix_communication_sessions_company_id_last_message_at", "communication_sessions",
        ["company_id", "last_message_at"],
    )
    op.create_index(
        "ix_communication_sessions_company_id_call_id", "communication_sessions",
        ["company_id", "call_id"],
    )
    enable_rls("communication_sessions")

    op.create_table(
        "communication_messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("sender_type", sa.String(16), nullable=False),
        sa.Column("sender_external_id", sa.String(256)),
        sa.Column("sender_user_id", UUID),
        sa.Column("message_type", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("external_message_id", sa.String(256)),
        sa.Column("client_message_id", UUID),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=JSON_OBJ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_communication_messages"),
        *_tenant("communication_messages"),
        sa.UniqueConstraint(
            "session_id", "external_message_id",
            name="uq_communication_messages_session_id_external_message_id",
        ),
        sa.UniqueConstraint(
            "session_id", "client_message_id",
            name="uq_communication_messages_session_id_client_message_id",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE", name="fk_communication_messages_session",
        ),
        _member_fk(
            "communication_messages", "sender_user_id",
            "fk_communication_messages_sender_member", "SET NULL (sender_user_id)",
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND', 'OUTBOUND')", name="ck_communication_messages_direction_valid"
        ),
        sa.CheckConstraint(
            "sender_type IN ('CUSTOMER', 'SALESPERSON', 'BOT', 'SYSTEM')",
            name="ck_communication_messages_sender_type_valid",
        ),
        sa.CheckConstraint(
            "message_type IN ('TEXT', 'IMAGE', 'DOCUMENT', 'AUDIO', 'VIDEO', 'LOCATION', "
            "'CONTACTS', 'INTERACTIVE', 'TEMPLATE', 'UNSUPPORTED')",
            name="ck_communication_messages_message_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'SENDING', 'SENT', 'DELIVERED', 'READ', 'FAILED')",
            name="ck_communication_messages_status_valid",
        ),
        sa.CheckConstraint(
            "length(content) <= 10000", name="ck_communication_messages_content_length"
        ),
    )
    op.create_index(
        "ix_communication_messages_company_id_session_id", "communication_messages",
        ["company_id", "session_id", "occurred_at"],
    )
    op.create_index(
        "ix_communication_messages_expires_at", "communication_messages", ["expires_at"]
    )
    enable_rls("communication_messages", retention=True)

    op.create_table(
        "communication_participants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("user_id", UUID),
        sa.Column("contact_id", UUID),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("joined_at", TS),
        sa.Column("left_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_communication_participants"),
        *_tenant("communication_participants"),
        sa.UniqueConstraint(
            "session_id", "role", "external_id",
            name="uq_communication_participants_session_id_role_external_id",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE", name="fk_communication_participants_session",
        ),
        _member_fk(
            "communication_participants", "user_id",
            "fk_communication_participants_member", "SET NULL (user_id)",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="SET NULL (contact_id)", name="fk_communication_participants_contact",
        ),
        sa.CheckConstraint(
            "role IN ('SALESPERSON', 'CUSTOMER', 'BOT', 'SYSTEM')",
            name="ck_communication_participants_role_valid",
        ),
    )
    enable_rls("communication_participants")

    op.create_table(
        "communication_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("external_event_id", sa.String(300)),
        sa.Column("data", postgresql.JSONB, nullable=False, server_default=JSON_OBJ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_communication_events"),
        *_tenant("communication_events"),
        sa.UniqueConstraint(
            "session_id", "external_event_id",
            name="uq_communication_events_session_id_external_event_id",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE", name="fk_communication_events_session",
        ),
        sa.CheckConstraint(f"type IN ({EVENT_TYPES})", name="ck_communication_events_type_valid"),
    )
    op.create_index(
        "ix_communication_events_company_id_session_id", "communication_events",
        ["company_id", "session_id", "occurred_at"],
    )
    enable_rls("communication_events")

    op.create_table(
        "contact_identities",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("contact_id", UUID, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(256), nullable=False),
        sa.Column("created_by_user_id", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_contact_identities"),
        *_tenant("contact_identities"),
        sa.UniqueConstraint(
            "company_id", "kind", "value", name="uq_contact_identities_company_id_kind_value"
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "contact_id"], ["contacts.company_id", "contacts.id"],
            ondelete="CASCADE", name="fk_contact_identities_contact",
        ),
        _member_fk(
            "contact_identities", "created_by_user_id",
            "fk_contact_identities_creator_member", "SET NULL (created_by_user_id)",
        ),
        sa.CheckConstraint(
            "kind IN ('PHONE', 'EMAIL', 'WHATSAPP', 'TEAMS_USER', 'TEAMS_CHAT', 'EXTERNAL')",
            name="ck_contact_identities_kind_valid",
        ),
    )
    op.create_index(
        "ix_contact_identities_company_id_contact_id", "contact_identities",
        ["company_id", "contact_id"],
    )
    enable_rls("contact_identities")

    op.create_table(
        "message_drafts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("reply_to_message_id", UUID),
        sa.Column("sent_message_id", UUID),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("warnings", postgresql.JSONB, nullable=False, server_default=JSON_ARR),
        sa.Column("ai_provider", sa.String(32)),
        sa.Column("reviewed_by_user_id", UUID),
        sa.Column("reviewed_at", TS),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_message_drafts"),
        *_tenant("message_drafts"),
        sa.UniqueConstraint(
            "session_id", "reply_to_message_id",
            name="uq_message_drafts_session_id_reply_to_message_id",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "session_id"],
            ["communication_sessions.company_id", "communication_sessions.id"],
            ondelete="CASCADE", name="fk_message_drafts_session",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "reply_to_message_id"],
            ["communication_messages.company_id", "communication_messages.id"],
            ondelete="SET NULL (reply_to_message_id)", name="fk_message_drafts_reply_to_message",
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "sent_message_id"],
            ["communication_messages.company_id", "communication_messages.id"],
            ondelete="SET NULL (sent_message_id)", name="fk_message_drafts_sent_message",
        ),
        _member_fk(
            "message_drafts", "reviewed_by_user_id",
            "fk_message_drafts_reviewer_member", "SET NULL (reviewed_by_user_id)",
        ),
        sa.CheckConstraint("source IN ('AI', 'MANUAL')", name="ck_message_drafts_source_valid"),
        sa.CheckConstraint(
            "status IN ('SUGGESTED', 'SENT', 'DISCARDED')", name="ck_message_drafts_status_valid"
        ),
        sa.CheckConstraint("length(body) <= 10000", name="ck_message_drafts_body_length"),
    )
    op.create_index(
        "ix_message_drafts_company_id_session_id", "message_drafts", ["company_id", "session_id"]
    )
    op.create_index("ix_message_drafts_expires_at", "message_drafts", ["expires_at"])
    enable_rls("message_drafts", retention=True)

    # ---- call_notes can also belong to a message conversation ------------------------------
    op.add_column("call_notes", sa.Column("session_id", UUID))
    op.alter_column("call_notes", "call_id", nullable=True)
    op.create_unique_constraint(
        "uq_call_notes_session_id_dedupe_key", "call_notes", ["session_id", "dedupe_key"]
    )
    op.create_foreign_key(
        "fk_call_notes_session", "call_notes", "communication_sessions",
        ["company_id", "session_id"], ["company_id", "id"], ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_call_notes_has_source", "call_notes", "call_id IS NOT NULL OR session_id IS NOT NULL"
    )

    # ---- timeline: messages + channel ------------------------------------------------------
    op.add_column("timeline_events", sa.Column("channel", sa.String(16)))
    op.add_column("timeline_events", sa.Column("session_id", UUID))
    op.create_foreign_key(
        "fk_timeline_events_session", "timeline_events", "communication_sessions",
        ["company_id", "session_id"], ["company_id", "id"], ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_timeline_events_channel_valid", "timeline_events",
        f"channel IS NULL OR channel IN ({CHANNELS})",
    )
    _timeline_checks(
        TIMELINE_CATEGORIES_OLD + ", 'MESSAGE'",
        TIMELINE_TYPES_OLD + ", 'MESSAGE_RECEIVED', 'MESSAGE_SENT', 'CONVERSATION_LINKED'",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM timeline_events WHERE category = 'MESSAGE' "
        "OR event_type IN ('MESSAGE_RECEIVED', 'MESSAGE_SENT', 'CONVERSATION_LINKED')"
    )
    _timeline_checks(TIMELINE_CATEGORIES_OLD, TIMELINE_TYPES_OLD)
    op.drop_constraint("ck_timeline_events_channel_valid", "timeline_events", type_="check")
    op.drop_constraint("fk_timeline_events_session", "timeline_events", type_="foreignkey")
    op.drop_column("timeline_events", "session_id")
    op.drop_column("timeline_events", "channel")

    op.execute("DELETE FROM call_notes WHERE call_id IS NULL")
    op.drop_constraint("ck_call_notes_has_source", "call_notes", type_="check")
    op.drop_constraint("fk_call_notes_session", "call_notes", type_="foreignkey")
    op.drop_constraint("uq_call_notes_session_id_dedupe_key", "call_notes", type_="unique")
    op.alter_column("call_notes", "call_id", nullable=False)
    op.drop_column("call_notes", "session_id")

    op.drop_table("message_drafts")
    op.drop_table("contact_identities")
    op.drop_table("communication_events")
    op.drop_table("communication_participants")
    op.drop_table("communication_messages")
    op.drop_table("communication_sessions")

    op.execute("DELETE FROM calls WHERE channel <> 'PHONE'")
    op.drop_constraint("ck_calls_teams_requires_meeting_url", "calls", type_="check")
    op.drop_constraint("ck_calls_transcript_persistence_valid", "calls", type_="check")
    op.drop_constraint("ck_calls_channel_valid", "calls", type_="check")
    op.drop_column("calls", "transcript_persistence")
    op.drop_column("calls", "meeting_url")
    op.drop_column("calls", "channel")

    op.drop_table("integration_subscriptions")
    op.drop_table("integration_user_connections")
    op.drop_table("integration_webhook_receipts")
    op.drop_table("integration_routes")
    op.drop_table("integrations")
