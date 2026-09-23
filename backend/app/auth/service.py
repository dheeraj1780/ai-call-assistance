"""Authentication use cases: register, login, refresh (with rotation), logout."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.models import AuthSession, RefreshToken
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from app.common.config import Settings
from app.common.db import TenantContext, set_tenant_context
from app.common.errors import ConflictError, UnauthorizedError
from app.tenants.models import Company, CompanyMember, MemberRole
from app.tenants.repository import CompanyRepository, MemberRepository
from app.users.models import User

logger = logging.getLogger(__name__)

_INVALID_CREDENTIALS = "Invalid email or password"
_INVALID_REFRESH = "Session expired. Please sign in again."


@dataclass(frozen=True, slots=True)
class AuthResult:
    access_token: str
    refresh_token: str
    user: User
    company: Company
    role: MemberRole


def _now() -> datetime:
    return datetime.now(UTC)


async def _find_user_by_email(session: AsyncSession, email: str) -> User | None:
    user: User | None = await session.scalar(select(User).where(User.email == email))
    return user


def _issue_refresh_token(
    session: AsyncSession, auth_session: AuthSession, settings: Settings, now: datetime
) -> str:
    raw = new_refresh_token()
    expires_at = min(now + timedelta(days=settings.refresh_token_ttl_days), auth_session.expires_at)
    session.add(
        RefreshToken(
            session_id=auth_session.id, token_hash=hash_refresh_token(raw), expires_at=expires_at
        )
    )
    return raw


def _start_session(
    session: AsyncSession, user: User, company: Company, role: MemberRole, settings: Settings
) -> AuthResult:
    now = _now()
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        expires_at=now + timedelta(days=settings.session_max_age_days),
        last_used_at=now,
    )
    session.add(auth_session)
    refresh = _issue_refresh_token(session, auth_session, settings, now)
    access = create_access_token(
        settings, user_id=user.id, company_id=company.id, session_id=auth_session.id, now=now
    )
    return AuthResult(access, refresh, user, company, role)


async def register(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    password: str,
    full_name: str,
    company_name: str,
    ip: str | None,
) -> AuthResult:
    """Create a user, their company and an OWNER membership in one transaction."""
    if await _find_user_by_email(session, email) is not None:
        raise ConflictError("An account with this email already exists", code="email_taken")

    user = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password(password), full_name=full_name
    )
    company = Company(id=uuid.uuid4(), name=company_name)
    # RLS: inserting the company/membership requires the new tenant to be the active context.
    await set_tenant_context(session, TenantContext(company_id=company.id, user_id=user.id))
    session.add_all([user, company])
    await session.flush()
    session.add(CompanyMember(company_id=company.id, user_id=user.id, role=MemberRole.OWNER))
    result = _start_session(session, user, company, MemberRole.OWNER, settings)
    audit.record(
        session,
        "auth.register",
        company_id=company.id,
        actor_user_id=user.id,
        entity_type="company",
        entity_id=company.id,
        ip=ip,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Concurrent registration with the same email won the race.
        raise ConflictError(
            "An account with this email already exists", code="email_taken"
        ) from None
    return result


async def _bind_user_tenant(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Set the tenant context for a user whose company is not yet known; returns the company id."""
    await set_tenant_context(session, TenantContext(user_id=user_id))
    member = await MemberRepository(session).get_for_user(user_id)
    if member is None:
        return None
    await set_tenant_context(session, TenantContext(company_id=member.company_id, user_id=user_id))
    return member.company_id


async def _load_membership(session: AsyncSession, user_id: uuid.UUID) -> tuple[Company, MemberRole]:
    company_id = await _bind_user_tenant(session, user_id)
    member = await MemberRepository(session).get_for_user(user_id) if company_id else None
    company = await CompanyRepository(session).get(company_id) if company_id else None
    if member is None or company is None:
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")
    return company, MemberRole(member.role)


async def login(
    session: AsyncSession, settings: Settings, *, email: str, password: str, ip: str | None
) -> AuthResult:
    user = await _find_user_by_email(session, email)
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok or not user.is_active:
        # Attribute the failure to the account's tenant (if the account exists) so the
        # company can see failed attempts on its users; unknown emails stay tenant-less.
        user_id = user.id if user else None
        reason = "inactive" if user and password_ok else "invalid_credentials"
        company_id = await _bind_user_tenant(session, user_id) if user_id else None
        # Rollback expires ORM objects; only plain values captured above are used after it.
        await session.rollback()
        await audit.record_standalone(
            "auth.login_failed",
            company_id=company_id,
            actor_user_id=user_id,
            ip=ip,
            details={"reason": reason},
        )
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")

    company, role = await _load_membership(session, user.id)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = _now()
    result = _start_session(session, user, company, role, settings)
    audit.record(session, "auth.login", company_id=company.id, actor_user_id=user.id, ip=ip)
    await session.commit()
    return result


def _revoke(auth_session: AuthSession, reason: str, now: datetime) -> None:
    if auth_session.revoked_at is None:
        auth_session.revoked_at = now
        auth_session.revoke_reason = reason


async def refresh(
    session: AsyncSession, settings: Settings, *, raw_token: str | None, ip: str | None
) -> AuthResult:
    if not raw_token:
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")

    now = _now()
    token = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    if token is None:
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")
    auth_session = await session.get(AuthSession, token.session_id, with_for_update=True)
    if auth_session is None or auth_session.revoked_at is not None:
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")

    reuse_grace = timedelta(seconds=settings.refresh_reuse_grace_seconds)
    if token.used_at is not None and now - token.used_at > reuse_grace:
        # A rotated-out token was presented again: assume it was stolen, kill the session.
        # (Within the grace window a reuse is treated as a benign concurrent refresh, e.g.
        # two tabs restoring at once, and simply gets a fresh token in the same session.)
        _revoke(auth_session, "refresh_token_reuse", now)
        company_id = await _bind_user_tenant(session, auth_session.user_id)
        audit.record(
            session,
            "auth.refresh_token_reuse",
            company_id=company_id,
            actor_user_id=auth_session.user_id,
            ip=ip,
        )
        await session.commit()
        logger.warning("refresh_token_reuse_detected", extra={"session_id": str(auth_session.id)})
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")

    if token.expires_at <= now or auth_session.expires_at <= now:
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")

    user = await session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        _revoke(auth_session, "user_inactive", now)
        await session.commit()
        raise UnauthorizedError(_INVALID_REFRESH, code="invalid_refresh_token")

    company, role = await _load_membership(session, user.id)
    if token.used_at is None:
        token.used_at = now
    auth_session.last_used_at = now
    new_raw = _issue_refresh_token(session, auth_session, settings, now)
    access = create_access_token(
        settings, user_id=user.id, company_id=company.id, session_id=auth_session.id, now=now
    )
    await session.commit()
    return AuthResult(access, new_raw, user, company, role)


async def logout(session: AsyncSession, *, raw_token: str | None, ip: str | None) -> None:
    """Revoke the session behind the refresh cookie. Idempotent; unknown tokens are ignored."""
    if not raw_token:
        return
    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
    )
    if token is None:
        return
    auth_session = await session.get(AuthSession, token.session_id, with_for_update=True)
    if auth_session is None or auth_session.revoked_at is not None:
        return
    _revoke(auth_session, "logout", _now())
    company_id = await _bind_user_tenant(session, auth_session.user_id)
    audit.record(
        session,
        "auth.logout",
        company_id=company_id,
        actor_user_id=auth_session.user_id,
        ip=ip,
    )
    await session.commit()
