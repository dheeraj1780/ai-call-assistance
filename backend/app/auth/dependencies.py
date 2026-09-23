"""Authentication / authorization dependencies.

The tenant (company) scope is derived exclusively from the verified access token and
re-checked against the database on every request.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.auth.tokens import InvalidTokenError, decode_access_token
from app.common.config import Settings, get_settings
from app.common.db import TenantContext, get_db_session, set_tenant_context
from app.common.errors import ForbiddenError, UnauthorizedError
from app.common.logging import company_id_var, user_id_var
from app.tenants.models import CompanyMember, MemberRole
from app.users.models import User

_bearer = HTTPBearer(auto_error=False)

CSRF_HEADER = "X-CSRF-Protection"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    company_id: uuid.UUID
    session_id: uuid.UUID
    role: MemberRole


async def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if credentials is None:
        raise UnauthorizedError()
    try:
        claims = decode_access_token(settings, credentials.credentials)
    except InvalidTokenError:
        raise UnauthorizedError("Invalid or expired token", code="invalid_token") from None

    await set_tenant_context(
        session, TenantContext(company_id=claims.company_id, user_id=claims.user_id)
    )
    row = (
        await session.execute(
            select(
                CompanyMember.role, User.is_active, AuthSession.revoked_at, AuthSession.expires_at
            )
            .join(User, User.id == CompanyMember.user_id)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                CompanyMember.user_id == claims.user_id,
                CompanyMember.company_id == claims.company_id,
                AuthSession.id == claims.session_id,
            )
        )
    ).one_or_none()

    if (
        row is None
        or not row.is_active
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC)
    ):
        raise UnauthorizedError("Invalid or expired token", code="invalid_token")

    user_id_var.set(str(claims.user_id))
    company_id_var.set(str(claims.company_id))
    return Principal(
        user_id=claims.user_id,
        company_id=claims.company_id,
        session_id=claims.session_id,
        role=MemberRole(row.role),
    )


def require_roles(*roles: MemberRole) -> Callable[..., Awaitable[Principal]]:
    async def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise ForbiddenError()
        return principal

    return _dependency


def require_csrf_protection(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Protect cookie-authenticated endpoints (refresh/logout) against CSRF.

    A custom header cannot be sent cross-origin without a CORS preflight, which only
    succeeds for allowed origins. If an Origin header is present it must be allowed too.
    """
    if request.headers.get(CSRF_HEADER) != "1":
        raise ForbiddenError("Missing CSRF protection header", code="csrf_failed")
    origin = request.headers.get("origin")
    if origin is None:
        return
    origin = origin.rstrip("/")
    same_origin = origin.split("://", 1)[-1] == request.headers.get("host")
    if not same_origin and origin not in settings.cors_origin_list:
        raise ForbiddenError("Origin not allowed", code="csrf_failed")
