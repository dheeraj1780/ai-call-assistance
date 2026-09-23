"""Foundation: users, companies, membership, auth sessions, audit log, RLS, pgvector.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    # pgvector is needed from the knowledge-base phase on; enabling it now verifies the
    # target database supports it. Requires sufficient privileges (see docs/DEVELOPMENT.md).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Helpers used by Row-Level Security policies. Settings are transaction-local and set by
    # the application at the start of every transaction (app/common/db.py).
    op.execute(
        """
        CREATE FUNCTION app_current_company_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE AS
        $$ SELECT NULLIF(current_setting('app.company_id', true), '')::uuid $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE AS
        $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
        """
    )

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(32)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.Column("updated_at", TS, nullable=False, server_default=_now()),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        sa.CheckConstraint("length(btrim(full_name)) > 0", name="ck_users_full_name_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "companies",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("industry", sa.String(120)),
        sa.Column("description", sa.Text),
        sa.Column("website", sa.String(500)),
        sa.Column("products_services", sa.Text),
        sa.Column("target_customer", sa.Text),
        sa.Column("ai_instructions", sa.Text),
        sa.Column(
            "transcript_retention_days", sa.Integer, nullable=False, server_default=sa.text("30")
        ),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.Column("updated_at", TS, nullable=False, server_default=_now()),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_companies_name_not_blank"),
        sa.CheckConstraint(
            "transcript_retention_days BETWEEN 1 AND 365",
            name="ck_companies_transcript_retention_days_range",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
    )

    op.create_table(
        "company_members",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.CheckConstraint(
            "role IN ('OWNER', 'ADMIN', 'MEMBER')", name="ck_company_members_role_valid"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_company_members_company_id_companies",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_company_members_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_company_members"),
        sa.UniqueConstraint("user_id", name="uq_company_members_user_id"),
        sa.UniqueConstraint("company_id", "id", name="uq_company_members_company_id_id"),
    )
    op.create_index("ix_company_members_company_id", "company_members", ["company_id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_used_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("revoke_reason", sa.String(32)),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_auth_sessions_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.ForeignKeyConstraint(
            ["session_id"], ["auth_sessions.id"], ondelete="CASCADE",
            name="fk_refresh_tokens_session_id_auth_sessions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID),
        sa.Column("actor_user_id", UUID),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64)),
        sa.Column("entity_id", UUID),
        sa.Column("request_id", sa.String(64)),
        sa.Column("ip", sa.String(64)),
        sa.Column(
            "details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", TS, nullable=False, server_default=_now()),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="SET NULL",
            name="fk_audit_logs_company_id_companies",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_audit_logs_actor_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index(
        "ix_audit_logs_company_id_created_at", "audit_logs", ["company_id", "created_at"]
    )

    # ---- Row-Level Security --------------------------------------------------------------
    # FORCE applies policies to the table owner as well (the app's DB role usually owns the
    # tables on managed Postgres). Superusers / BYPASSRLS roles still bypass RLS; the app
    # checks for that at startup.
    for table in ("companies", "company_members", "audit_logs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY tenant_isolation ON companies
        USING (id = app_current_company_id())
        WITH CHECK (id = app_current_company_id())
        """
    )

    # A user may read their own membership before the tenant is known (login/refresh).
    op.execute(
        """
        CREATE POLICY member_select ON company_members FOR SELECT
        USING (company_id = app_current_company_id() OR user_id = app_current_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY member_insert ON company_members FOR INSERT
        WITH CHECK (company_id = app_current_company_id())
        """
    )
    op.execute(
        """
        CREATE POLICY member_update ON company_members FOR UPDATE
        USING (company_id = app_current_company_id())
        WITH CHECK (company_id = app_current_company_id())
        """
    )
    op.execute(
        """
        CREATE POLICY member_delete ON company_members FOR DELETE
        USING (company_id = app_current_company_id())
        """
    )

    # Audit log is append-only for the application: no UPDATE/DELETE policies exist.
    op.execute(
        """
        CREATE POLICY audit_select ON audit_logs FOR SELECT
        USING (company_id = app_current_company_id())
        """
    )
    op.execute(
        """
        CREATE POLICY audit_insert ON audit_logs FOR INSERT
        WITH CHECK (company_id IS NULL OR company_id = app_current_company_id())
        """
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("company_members")
    op.drop_table("companies")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.execute("DROP FUNCTION IF EXISTS app_current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS app_current_company_id()")
    # The vector extension is intentionally left installed (it may be shared/managed).
