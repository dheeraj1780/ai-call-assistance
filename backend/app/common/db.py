"""Database engine, sessions and tenant context.

Tenant isolation is enforced twice:

1. Application layer: services/repositories only ever query with the tenant derived from
   the authenticated principal (never from client input).
2. Database layer: PostgreSQL Row-Level Security (FORCE'd, so it applies to the table
   owner too). Policies read the transaction-local settings ``app.company_id`` and
   ``app.user_id``, which are applied at the start of every transaction from
   ``session.info[TENANT_CONTEXT_KEY]``. A session without tenant context sees no
   tenant-owned rows (fail closed).
"""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import NullPool

from app.common.config import Settings, get_settings

logger = logging.getLogger(__name__)

TENANT_CONTEXT_KEY = "tenant_context"

_SET_CONTEXT_SQL = text(
    "SELECT set_config('app.company_id', :company_id, true), "
    "set_config('app.user_id', :user_id, true)"
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    company_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    def as_params(self) -> dict[str, str]:
        return {
            "company_id": str(self.company_id) if self.company_id else "",
            "user_id": str(self.user_id) if self.user_id else "",
        }


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    # Always executed - even with an empty context - so a pooled connection can never
    # carry settings over from a previous transaction.
    ctx: TenantContext = session.info.get(TENANT_CONTEXT_KEY) or TenantContext()
    connection.execute(_SET_CONTEXT_SQL, ctx.as_params())


async def set_tenant_context(session: AsyncSession, ctx: TenantContext) -> None:
    """Bind a tenant context to the session, applying it immediately if a transaction is open."""
    session.info[TENANT_CONTEXT_KEY] = ctx
    if session.in_transaction():
        await session.execute(_SET_CONTEXT_SQL, ctx.as_params())


def create_engine(settings: Settings) -> AsyncEngine:
    url, connect_args = settings.sqlalchemy_url
    connect_args = {
        **connect_args,
        "server_settings": {"statement_timeout": str(settings.db_statement_timeout_ms)},
    }
    # hide_parameters: DB error messages (which may end up in logged tracebacks) must never
    # contain bound values such as transcript text or personal data.
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        "connect_args": connect_args,
        "hide_parameters": True,
    }
    if settings.app_env == "test":
        # Each test may run on its own event loop; asyncpg connections are loop-bound.
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return create_async_engine(url, **kwargs)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings())
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _session_factory is None:  # pragma: no cover - set by get_engine()
        raise RuntimeError("Session factory not initialised")
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Services commit explicitly; anything uncommitted is rolled back."""
    async with get_session_factory()() as session:
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


async def check_db_role_safety(engine: AsyncEngine, *, strict: bool) -> None:
    """RLS is bypassed by superusers and BYPASSRLS roles; refuse/warn if we connect as one."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    if row.rolsuper or row.rolbypassrls:
        message = (
            "Database role is a superuser or has BYPASSRLS; row-level tenant isolation is "
            "NOT enforced. Connect with a dedicated non-superuser role."
        )
        if strict:
            raise RuntimeError(message)
        logger.warning("db_role_bypasses_rls")
