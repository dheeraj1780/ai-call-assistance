"""Calendar use cases. Each user connects their own Google Calendar.

Safety rules:
- Events are created/updated only through explicit user requests carrying ``confirm: true``.
- Refresh tokens are encrypted at rest; access tokens are never stored.
- A revoked/expired grant flips the connection to REAUTH_REQUIRED instead of failing silently.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items.repository import ActionItemRepository
from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calendar.models import CalendarConnection, CalendarEvent, ConnectionStatus
from app.calendar.provider import (
    CalendarAuthError,
    CalendarEventData,
    NewEvent,
    get_calendar_provider,
)
from app.calendar.schemas import EventCreate, EventUpdate
from app.calls.repository import CallRepository
from app.common.config import get_settings
from app.common.crypto import decrypt, encrypt, sign_state, verify_state
from app.common.db import TenantContext, set_tenant_context
from app.common.errors import AppError, InvalidReferenceError, NotFoundError
from app.contacts.repository import ContactRepository
from app.tenants.membership import ensure_member
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType

logger = logging.getLogger(__name__)
STATE_PURPOSE = "calendar-oauth"


class CalendarNotConnectedError(AppError):
    status_code = 409
    code = "calendar_not_connected"
    message = "Connect your Google Calendar first."


def redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/calendar/oauth/callback"


async def get_connection(session: AsyncSession, principal: Principal) -> CalendarConnection | None:
    conn: CalendarConnection | None = await session.scalar(
        select(CalendarConnection).where(
            CalendarConnection.company_id == principal.company_id,
            CalendarConnection.user_id == principal.user_id,
        )
    )
    return conn


def start_connect(principal: Principal) -> str:
    state = sign_state(
        {"u": str(principal.user_id), "c": str(principal.company_id), "n": uuid.uuid4().hex},
        purpose=STATE_PURPOSE,
    )
    return get_calendar_provider().authorization_url(state=state, redirect_uri=redirect_uri())


async def complete_connect(session: AsyncSession, *, code: str, state: str) -> None:
    """OAuth callback. Authenticated by the signed, short-lived ``state`` (no bearer token on
    a top-level redirect); membership is re-checked before anything is stored."""
    data = verify_state(state, purpose=STATE_PURPOSE)
    user_id, company_id = uuid.UUID(data["u"]), uuid.UUID(data["c"])
    await set_tenant_context(session, TenantContext(company_id=company_id, user_id=user_id))
    await ensure_member(session, company_id, user_id, field="user")
    provider = get_calendar_provider()
    tokens = await provider.exchange_code(code=code, redirect_uri=redirect_uri())
    if not tokens.refresh_token:
        raise AppError("Google did not return offline access", code="calendar_no_refresh_token")

    conn = await session.scalar(
        select(CalendarConnection).where(
            CalendarConnection.company_id == company_id, CalendarConnection.user_id == user_id
        )
    )
    if conn is None:
        conn = CalendarConnection(company_id=company_id, user_id=user_id, provider=provider.name)
        session.add(conn)
    conn.provider = provider.name
    conn.encrypted_refresh_token = encrypt(tokens.refresh_token)
    conn.scopes = tokens.scope[:1000]
    conn.account_email = tokens.account_email
    conn.status = ConnectionStatus.ACTIVE.value
    conn.last_error = None
    audit.record(
        session,
        "calendar.connected",
        company_id=company_id,
        actor_user_id=user_id,
        entity_type="calendar_connection",
    )
    await session.commit()


async def disconnect(session: AsyncSession, principal: Principal) -> None:
    conn = await get_connection(session, principal)
    if conn is None:
        return
    try:
        await get_calendar_provider().revoke(decrypt(conn.encrypted_refresh_token))
    except Exception:
        logger.warning("calendar_revoke_failed")
    await session.delete(conn)
    audit.record(
        session,
        "calendar.disconnected",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="calendar_connection",
    )
    await session.commit()


async def _access_token(session: AsyncSession, conn: CalendarConnection) -> str:
    try:
        return await get_calendar_provider().access_token(decrypt(conn.encrypted_refresh_token))
    except (CalendarAuthError, ValueError):
        conn.status = ConnectionStatus.REAUTH_REQUIRED.value
        conn.last_error = "reauth_required"
        await session.commit()
        raise CalendarAuthError("reconnect required") from None


async def _require_connection(session: AsyncSession, principal: Principal) -> CalendarConnection:
    conn = await get_connection(session, principal)
    if conn is None:
        raise CalendarNotConnectedError()
    if conn.status != ConnectionStatus.ACTIVE:
        raise CalendarAuthError("reconnect required")
    return conn


async def upcoming(
    session: AsyncSession, principal: Principal, days: int
) -> list[CalendarEventData]:
    conn = await _require_connection(session, principal)
    token = await _access_token(session, conn)
    now = datetime.now(UTC)
    return await get_calendar_provider().list_events(
        token, time_min=now, time_max=now + timedelta(days=days)
    )


async def _validate_links(
    session: AsyncSession, principal: Principal, data: EventCreate
) -> uuid.UUID | None:
    contact_id = data.contact_id
    if data.call_id:
        call = await CallRepository(session).get(principal.company_id, data.call_id)
        if call is None:
            raise InvalidReferenceError("Call not found", details=[{"field": "call_id"}])
        contact_id = contact_id or call.contact_id
    if data.action_item_id:
        item = await ActionItemRepository(session).get(principal.company_id, data.action_item_id)
        if item is None:
            raise InvalidReferenceError(
                "Action item not found", details=[{"field": "action_item_id"}]
            )
        contact_id = contact_id or item.contact_id
    if (
        contact_id
        and await ContactRepository(session).get(principal.company_id, contact_id) is None
    ):
        raise InvalidReferenceError("Contact not found", details=[{"field": "contact_id"}])
    return contact_id


async def create_event(
    session: AsyncSession, principal: Principal, data: EventCreate, *, ip: str | None
) -> CalendarEvent:
    contact_id = await _validate_links(session, principal, data)
    conn = await _require_connection(session, principal)
    token = await _access_token(session, conn)
    created = await get_calendar_provider().create_event(
        token,
        NewEvent(
            title=data.title,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            description=data.description,
        ),
    )
    event = CalendarEvent(
        company_id=principal.company_id,
        connection_id=conn.id,
        created_by_user_id=principal.user_id,
        provider=conn.provider,
        external_event_id=created.external_id,
        title=data.title,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        html_link=created.html_link,
        contact_id=contact_id,
        call_id=data.call_id,
        action_item_id=data.action_item_id,
    )
    session.add(event)
    await session.flush()
    if contact_id:
        timeline.record(
            session,
            company_id=principal.company_id,
            contact_id=contact_id,
            category=TimelineCategory.CALENDAR,
            event_type=TimelineEventType.CALENDAR_EVENT_CREATED,
            summary=f"Calendar event: {data.title} ({data.starts_at:%d %b %Y %H:%M %Z})",
            actor_user_id=principal.user_id,
            call_id=data.call_id,
            action_item_id=data.action_item_id,
        )
    audit.record(
        session,
        "calendar.event_created",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="calendar_event",
        entity_id=event.id,
        ip=ip,
    )
    await session.commit()
    return event


async def update_event(
    session: AsyncSession,
    principal: Principal,
    event_id: uuid.UUID,
    data: EventUpdate,
    *,
    ip: str | None,
) -> CalendarEvent:
    event = await session.scalar(
        select(CalendarEvent).where(
            CalendarEvent.company_id == principal.company_id, CalendarEvent.id == event_id
        )
    )
    if event is None or event.created_by_user_id != principal.user_id:
        raise NotFoundError("Calendar event not found")
    conn = await _require_connection(session, principal)
    token = await _access_token(session, conn)
    await get_calendar_provider().update_event(
        token,
        event.external_event_id,
        NewEvent(
            title=data.title,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            description=data.description,
        ),
    )
    event.title, event.starts_at, event.ends_at = data.title, data.starts_at, data.ends_at
    audit.record(
        session,
        "calendar.event_updated",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="calendar_event",
        entity_id=event.id,
        ip=ip,
    )
    await session.commit()
    return event


async def list_app_events(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID | None
) -> list[CalendarEvent]:
    stmt = select(CalendarEvent).where(CalendarEvent.company_id == principal.company_id)
    if contact_id:
        stmt = stmt.where(CalendarEvent.contact_id == contact_id)
    return list(
        (await session.scalars(stmt.order_by(CalendarEvent.starts_at.desc()).limit(100))).all()
    )
