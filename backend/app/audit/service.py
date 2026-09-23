"""Audit logging helpers."""

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditLog
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.logging import request_id_var

logger = logging.getLogger(__name__)


def _build(
    action: str,
    *,
    company_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    ip: str | None,
    details: dict[str, Any] | None,
) -> AuditLog:
    return AuditLog(
        company_id=company_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id_var.get(),
        ip=ip,
        details=details or {},
    )


def record(
    session: AsyncSession,
    action: str,
    *,
    company_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the caller's transaction (committed together with the change)."""
    session.add(
        _build(
            action,
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            ip=ip,
            details=details,
        )
    )


async def record_standalone(
    action: str,
    *,
    company_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit row in its own transaction.

    Used for events that accompany a failed request (e.g. failed login), where the request's
    own transaction is rolled back. Failures here are logged, never raised.
    """
    try:
        async with get_session_factory()() as session:
            await set_tenant_context(
                session, TenantContext(company_id=company_id, user_id=actor_user_id)
            )
            session.add(
                _build(
                    action,
                    company_id=company_id,
                    actor_user_id=actor_user_id,
                    entity_type=None,
                    entity_id=None,
                    ip=ip,
                    details=details,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("audit_write_failed", extra={"action": action})
