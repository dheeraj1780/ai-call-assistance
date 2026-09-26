"""Google Meet as a provider (integrations, communication sessions) and a call channel.

Revision ID: 0010_google_meet
Revises: 0009_call_language
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_google_meet"
down_revision: str | None = "0009_call_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDERS_OLD = "'MICROSOFT_TEAMS', 'WHATSAPP', 'PLIVO'"
PROVIDERS_NEW = PROVIDERS_OLD + ", 'GOOGLE_MEET'"
CHANNELS_OLD = "'PHONE', 'TEAMS', 'WHATSAPP'"
CHANNELS_NEW = CHANNELS_OLD + ", 'GOOGLE_MEET'"
CALL_CHANNELS_OLD = "'PHONE', 'TEAMS'"
CALL_CHANNELS_NEW = CALL_CHANNELS_OLD + ", 'GOOGLE_MEET'"


def _replace(name: str, table: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    _replace("ck_integrations_provider_valid", "integrations", f"provider IN ({PROVIDERS_NEW})")
    _replace(
        "ck_communication_sessions_provider_valid",
        "communication_sessions",
        f"provider IN ({PROVIDERS_NEW}, 'MOCK')",
    )
    _replace(
        "ck_communication_sessions_channel_valid",
        "communication_sessions",
        f"channel IN ({CHANNELS_NEW})",
    )
    _replace("ck_calls_channel_valid", "calls", f"channel IN ({CALL_CHANNELS_NEW})")
    # 0008 created this one with a doubled prefix (naming convention applied to a full name).
    op.execute(
        "ALTER TABLE timeline_events DROP CONSTRAINT IF EXISTS "
        "ck_timeline_events_ck_timeline_events_channel_valid"
    )
    op.execute("ALTER TABLE timeline_events DROP CONSTRAINT IF EXISTS ck_timeline_events_channel_valid")
    op.create_check_constraint(
        "ck_timeline_events_channel_valid",
        "timeline_events",
        f"channel IS NULL OR channel IN ({CHANNELS_NEW})",
    )
    op.create_check_constraint(
        "ck_calls_meet_requires_meeting_url",
        "calls",
        "channel <> 'GOOGLE_MEET' OR meeting_url IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_calls_meet_requires_meeting_url", "calls", type_="check")
    op.drop_constraint("ck_timeline_events_channel_valid", "timeline_events", type_="check")
    op.create_check_constraint(
        "ck_timeline_events_channel_valid",
        "timeline_events",
        f"channel IS NULL OR channel IN ({CHANNELS_OLD})",
    )
    _replace("ck_calls_channel_valid", "calls", f"channel IN ({CALL_CHANNELS_OLD})")
    _replace(
        "ck_communication_sessions_channel_valid",
        "communication_sessions",
        f"channel IN ({CHANNELS_OLD})",
    )
    _replace(
        "ck_communication_sessions_provider_valid",
        "communication_sessions",
        f"provider IN ({PROVIDERS_OLD}, 'MOCK')",
    )
    _replace("ck_integrations_provider_valid", "integrations", f"provider IN ({PROVIDERS_OLD})")
