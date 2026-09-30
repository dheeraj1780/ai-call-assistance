"""Telephony use cases: start/end calls, provider webhooks (verified, idempotent, order-safe),
media stream ingestion.

The provider is authoritative for call state: the browser never sets telephony states.
Duplicate webhooks are ignored via a unique (provider, event_id); out-of-order webhooks never
move a call backwards and nothing changes a call after it reaches a terminal state.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.models import (
    LIVE_CALL_STATUSES,
    MEETING_CHANNELS,
    TERMINAL_CALL_STATUSES,
    Call,
    CallChannel,
    CallStatus,
    TranscriptPersistence,
)
from app.calls.repository import CallRepository
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import AppError, ForbiddenError, InvalidStateError, NotFoundError
from app.contacts.repository import ContactRepository
from app.conversations import service as conversations
from app.integrations import calling
from app.integrations.domain import ConversationEventType
from app.jobs import service as jobs
from app.live import session as live
from app.live.hub import hub
from app.telephony.models import CallRoute, TelephonyWebhookEvent
from app.telephony.provider import (
    MediaControl,
    MediaFrame,
    OutboundCallRequest,
    ProviderCallState,
    TelephonyError,
    TelephonyEvent,
    media_token,
)
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType
from app.users.models import User

logger = logging.getLogger(__name__)

RANK: dict[CallStatus, int] = {
    CallStatus.PLANNED: 0,
    CallStatus.INITIATED: 1,
    CallStatus.RINGING: 2,
    CallStatus.CONNECTED: 3,
    CallStatus.ACTIVE: 4,
    # Once the user asked to end, late "connected/active" events can no longer move the call
    # backwards; only a terminal state follows.
    CallStatus.ENDING: 5,
    CallStatus.COMPLETED: 6,
    CallStatus.NO_ANSWER: 6,
    CallStatus.FAILED: 6,
    CallStatus.CANCELLED: 6,
}
TIMELINE_STATES = {CallStatus.CONNECTED, *TERMINAL_CALL_STATUSES}
TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_CALL_STATUSES)


class MissingPhoneError(AppError):
    status_code = 422
    code = "missing_phone"


class TelephonyUnavailableError(AppError):
    status_code = 502
    code = "telephony_unavailable"
    message = "The calling provider could not start the call. Please try again."


def _callback_urls(provider: str, call_id: uuid.UUID) -> tuple[str, str]:
    base = get_settings().public_base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return (
        f"{base}/api/v1/webhooks/telephony/{provider}",
        f"{ws_base}/api/v1/telephony/media/{provider}/{call_id}?token={media_token(call_id)}",
    )


async def _get_locked(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> Call:
    """The call row under ``FOR UPDATE``: lifecycle commands serialise per call, so repeated
    clicks, browser retries and duplicate requests cannot run the same transition twice."""
    call = await CallRepository(session).get_locked(principal.company_id, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    return call


def _require_owner(principal: Principal, call: Call, action: str) -> None:
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError(f"Only the assigned user or an admin can {action} this call")


def _telephony_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return str(code)[:64] if isinstance(code, str) and code else TelephonyError.code


async def start_call(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, *, ip: str | None
) -> Call:
    """Start a PLANNED call. Idempotent: starting a call that is already starting/live returns it
    unchanged (double clicks, browser retries); a finished call must be re-opened first."""
    call = await _get_locked(session, principal, call_id)
    _require_owner(principal, call, "start")
    current = CallStatus(call.status)
    if current in LIVE_CALL_STATUSES:
        return await _unlocked(session, principal, call)
    if current != CallStatus.PLANNED:
        raise InvalidStateError(
            "Only a planned call can be started. Re-open a failed call to try again.",
            code="invalid_state",
        )
    contact = await ContactRepository(session).get(principal.company_id, call.contact_id)
    user = await session.get(User, principal.user_id)
    teams = call.channel == CallChannel.TEAMS
    meeting = call.channel in MEETING_CHANNELS
    if contact is None:
        raise MissingPhoneError("Contact not found")
    if not meeting:
        if not contact.phone:
            raise MissingPhoneError("Add the customer's phone number before starting the call")
        if user is None or not user.phone:
            raise MissingPhoneError(
                "Add your own phone number in your profile before starting a call"
            )
    elif not call.meeting_url:
        raise InvalidStateError(
            f"A {'Teams' if teams else 'Google Meet'} call needs the meeting link"
        )

    resolved = await calling.resolve_for_start(session, principal.company_id, call)
    provider = resolved.provider
    now = datetime.now(UTC)
    call.status = CallStatus.INITIATED.value
    call.user_id = call.user_id or principal.user_id
    call.provider = provider.name
    call.telephony_error = None
    call.end_requested_at = None
    call.finalized_at = None
    call.last_activity_at = now
    call.transcript_persistence = resolved.persistence.value
    live.clear_media_status(call.id)
    session.add(CallRoute(call_id=call.id, company_id=principal.company_id, provider=provider.name))
    await conversations.open_call_session(session, call, integration_id=resolved.integration_id)
    audit.record(
        session,
        "call.started",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="call",
        entity_id=call.id,
        ip=ip,
    )
    try:
        await session.commit()  # releases the row lock: from here the call is INITIATED
    except IntegrityError:
        await session.rollback()
        raise InvalidStateError(
            "This call is already being started", code="invalid_state"
        ) from None

    status_url, media_url = _callback_urls(provider.name, call.id)
    try:
        provider_call_id = await provider.create_call(
            OutboundCallRequest(
                call_id=call.id,
                agent_number=(user.phone if user and user.phone else ""),
                customer_number=contact.phone or "",
                status_callback_url=status_url,
                media_stream_url=media_url,
                meeting_url=call.meeting_url,
                agent_external_id=await _teams_user_id(session, principal) if teams else None,
            )
        )
    except Exception as exc:
        if not isinstance(exc, TelephonyError):
            logger.exception("telephony_start_crashed", extra={"call_id": str(call.id)})
        await _fail_start(session, principal, call, _telephony_code(exc))
        raise TelephonyUnavailableError() from None
    call.provider_call_id = provider_call_id
    route = await session.get(CallRoute, call.id)
    if route is not None:
        route.provider_call_id = provider_call_id
    await session.commit()
    hub.publish(call.id, "call.status", {"status": call.status})
    return await get_call(session, principal, call.id)


async def _unlocked(session: AsyncSession, principal: Principal, call: Call) -> Call:
    call_id = call.id
    await session.rollback()  # release the FOR UPDATE lock
    return await get_call(session, principal, call_id)


async def _fail_start(session: AsyncSession, principal: Principal, call: Call, code: str) -> None:
    call.status = CallStatus.FAILED.value
    call.telephony_error = code
    call.ended_at = datetime.now(UTC)
    call.finalized_at = call.ended_at  # nothing was started: nothing to finalise
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=call.contact_id,
        category=TimelineCategory.CALL,
        event_type=TimelineEventType.CALL_STATUS_CHANGED,
        summary="Call could not be started (calling provider error)",
        actor_user_id=principal.user_id,
        from_status=CallStatus.INITIATED.value,
        to_status=CallStatus.FAILED.value,
        call_id=call.id,
        channel=call.channel,
    )
    await conversations.record_call_event(
        session,
        principal.company_id,
        call.id,
        ConversationEventType.CALL_ENDED,
        status=CallStatus.FAILED.value,
        ended=True,
    )
    await session.commit()
    hub.publish(call.id, "call.status", {"status": call.status})


REOPENABLE = frozenset({CallStatus.FAILED, CallStatus.CANCELLED, CallStatus.NO_ANSWER})


async def reopen_call(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, *, ip: str | None
) -> Call:
    """Turn a call that never connected (failed to start, cancelled, no answer) back into a
    PLANNED call so its details can be corrected and it can be started again. Idempotent on a
    call that is already PLANNED. A call that connected is history: plan a new one."""
    call = await _get_locked(session, principal, call_id)
    _require_owner(principal, call, "re-open")
    current = CallStatus(call.status)
    if current == CallStatus.PLANNED:
        return await _unlocked(session, principal, call)
    if current not in REOPENABLE or call.started_at is not None:
        raise InvalidStateError(
            "Only a call that never connected can be re-opened; plan a new call instead",
            code="invalid_state",
        )
    if call_id in _finalizers:
        raise InvalidStateError("This call is still being finalised; try again in a moment")
    await session.execute(delete(CallRoute).where(CallRoute.call_id == call.id))
    old = call.status
    call.status = CallStatus.PLANNED.value
    call.provider = None
    call.provider_call_id = None
    call.telephony_error = None
    call.ended_at = None
    call.duration_seconds = None
    call.end_requested_at = None
    call.last_activity_at = None
    call.finalized_at = None
    call.transcript_persistence = TranscriptPersistence.PERSISTED.value
    live.clear_media_status(call.id)
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=call.contact_id,
        category=TimelineCategory.CALL,
        event_type=TimelineEventType.CALL_STATUS_CHANGED,
        summary="Call re-opened for another attempt",
        actor_user_id=principal.user_id,
        from_status=old,
        to_status=CallStatus.PLANNED.value,
        call_id=call.id,
        channel=call.channel,
    )
    audit.record(
        session,
        "call.reopened",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="call",
        entity_id=call.id,
        ip=ip,
    )
    await session.commit()
    hub.publish(call.id, "call.status", {"status": CallStatus.PLANNED.value})
    return await get_call(session, principal, call_id)


async def _teams_user_id(session: AsyncSession, principal: Principal) -> str | None:
    from app.integrations.models import IntegrationUserConnection

    value: str | None = await session.scalar(
        select(IntegrationUserConnection.external_user_id).where(
            IntegrationUserConnection.company_id == principal.company_id,
            IntegrationUserConnection.user_id == principal.user_id,
            IntegrationUserConnection.provider == "MICROSOFT_TEAMS",
        )
    )
    return value


# ---- End: idempotent, provider-neutral, bounded -------------------------------------------------


async def hang_up_provider(session: AsyncSession, company_id: uuid.UUID, call: Call) -> None:
    """Best effort: ask the call's provider to end it. Never raises; a provider that cannot be
    reached is handled by the bounded wait + forced local end, not by failing the request."""
    if not call.provider_call_id:
        return
    try:
        provider = await calling.provider_for_call(session, company_id, call)
        if provider is None:
            return
        await provider.end_call(call.provider_call_id)
        if provider.emits_end_events:
            state = (
                ProviderCallState.COMPLETED
                if call.started_at is not None
                else ProviderCallState.CANCELLED
            )
            await process_event(
                provider.name,
                TelephonyEvent(
                    event_id=f"end-{call.id}-{call.provider_call_id}"[:128],
                    provider_call_id=call.provider_call_id,
                    state=state,
                    occurred_at=datetime.now(UTC),
                ),
            )
    except Exception:
        logger.warning("telephony_end_failed", extra={"call_id": str(call.id)})


async def _status_of(company_id: uuid.UUID, call_id: uuid.UUID) -> CallStatus | None:
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        value = await session.scalar(
            select(Call.status).where(Call.company_id == company_id, Call.id == call_id)
        )
    return CallStatus(value) if value else None


async def force_complete(
    company_id: uuid.UUID, call_id: uuid.UUID, *, error: str | None = None
) -> bool:
    """Move a call that is not terminal to a terminal state locally (the provider did not do it
    in time): COMPLETED if it ever connected, otherwise CANCELLED (FAILED when ``error`` says
    why). Closes media/STT and finalises through ``apply_state``. False if already terminal."""
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        row = (
            await session.execute(
                select(Call.status, Call.started_at).where(
                    Call.company_id == company_id, Call.id == call_id
                )
            )
        ).first()
    if row is None or CallStatus(row.status) in TERMINAL_CALL_STATUSES:
        return False
    if row.started_at is not None:
        final = CallStatus.COMPLETED
    else:
        final = CallStatus.FAILED if error else CallStatus.CANCELLED
    logger.info(
        "call_force_completed",
        extra={"call_id": str(call_id), "from": row.status, "to": final.value, "reason": error},
    )
    return await apply_state(company_id, call_id, final, datetime.now(UTC), error=error)


async def _await_terminal(company_id: uuid.UUID, call_id: uuid.UUID, deadline: datetime) -> bool:
    """Poll until the call is terminal or ``deadline`` passes. True if terminal."""
    while True:
        status = await _status_of(company_id, call_id)
        if status is None or status in TERMINAL_CALL_STATUSES:
            return True
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.25, remaining))


async def request_end(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> Call:
    """End a call. Idempotent and bounded:

    1. the call becomes ENDING (recorded with a timestamp, so a restart cannot lose the intent);
    2. the provider is asked to hang up / leave (phone: Plivo hangup, Teams: gateway leave);
    3. we wait for the provider's confirmation for at most CALL_END_GRACE_SECONDS;
    4. if it never comes the call is completed locally - media and STT are closed, the
       transcript is finalised and post-call processing is queued.

    A repeated End while ENDING does not contact the provider again; it joins the same bounded
    wait. Ending a finished call returns it unchanged."""
    call = await _get_locked(session, principal, call_id)
    _require_owner(principal, call, "end")
    current = CallStatus(call.status)
    if current in TERMINAL_CALL_STATUSES:
        return await _unlocked(session, principal, call)
    if current == CallStatus.PLANNED:
        raise InvalidStateError("This call has not been started")
    first = current != CallStatus.ENDING
    if first:
        call.status = CallStatus.ENDING.value
        call.end_requested_at = datetime.now(UTC)
    end_requested_at = call.end_requested_at or datetime.now(UTC)
    has_provider_call = bool(call.provider_call_id)
    # Release the row lock BEFORE any waiting: the provider callback / forced end must be able
    # to take it (a repeated End only joins the wait).
    await session.commit()
    if first:
        hub.publish(call_id, "call.status", {"status": CallStatus.ENDING.value})
        await hang_up_provider(session, principal.company_id, call)
        await session.commit()
    grace = timedelta(seconds=get_settings().call_end_grace_seconds)
    if has_provider_call:
        done = await _await_terminal(principal.company_id, call_id, end_requested_at + grace)
    else:
        done = False  # nothing exists at a provider that could confirm: end locally at once
    if not done:
        await force_complete(principal.company_id, call_id, error=None)
    session.expire_all()
    return await get_call(session, principal, call_id)


async def handle_webhook_events(provider_name: str, events: list[TelephonyEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        outcome = await process_event(provider_name, event)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


async def process_event(provider_name: str, event: TelephonyEvent) -> str:
    """Apply one provider event exactly once. Returns applied|duplicate|ignored|unknown_call."""
    async with get_session_factory()() as session:
        inserted = await session.scalar(
            insert(TelephonyWebhookEvent)
            .values(
                id=uuid.uuid4(),
                provider=provider_name,
                event_id=event.event_id,
                provider_call_id=event.provider_call_id,
                state=event.state.value,
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
            .returning(TelephonyWebhookEvent.id)
        )
        if inserted is None:
            await session.rollback()
            return "duplicate"
        route = await session.scalar(
            select(CallRoute).where(
                CallRoute.provider == provider_name,
                CallRoute.provider_call_id == event.provider_call_id,
            )
        )
        if route is None:
            await session.execute(
                update(TelephonyWebhookEvent)
                .where(TelephonyWebhookEvent.id == inserted)
                .values(applied="unknown_call")
            )
            await session.commit()
            logger.warning("telephony_event_unknown_call", extra={"provider": provider_name})
            return "unknown_call"
        await session.execute(
            update(TelephonyWebhookEvent)
            .where(TelephonyWebhookEvent.id == inserted)
            .values(call_id=route.call_id)
        )
        await session.commit()
    changed = await apply_state(
        route.company_id,
        route.call_id,
        CallStatus(event.state.value),
        event.occurred_at,
        duration=event.duration_seconds,
        error=event.error_code,
    )
    if not changed:
        async with get_session_factory()() as session:
            await session.execute(
                update(TelephonyWebhookEvent)
                .where(TelephonyWebhookEvent.id == inserted)
                .values(applied="ignored")
            )
            await session.commit()
        return "ignored"
    return "applied"


async def process_event_for_call(
    provider_name: str, company_id: uuid.UUID, call_id: uuid.UUID, event: TelephonyEvent
) -> str:
    """Like ``process_event`` for providers whose callbacks carry our call id in a signed URL
    (Plivo). The route must belong to the same tenant and provider; the provider's call id is
    recorded on first sight so hang-up requests can address the live call."""
    async with get_session_factory()() as session:
        route = await session.get(CallRoute, call_id)
        if route is None or route.company_id != company_id or route.provider != provider_name:
            return "unknown_call"
        inserted = await session.scalar(
            insert(TelephonyWebhookEvent)
            .values(
                id=uuid.uuid4(),
                provider=provider_name,
                event_id=event.event_id,
                provider_call_id=event.provider_call_id,
                state=event.state.value,
                call_id=call_id,
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
            .returning(TelephonyWebhookEvent.id)
        )
        if inserted is None:
            await session.rollback()
            return "duplicate"
        route.provider_call_id = route.provider_call_id or event.provider_call_id
        await session.commit()
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        call = await session.scalar(
            select(Call).where(Call.company_id == company_id, Call.id == call_id)
        )
        if call is not None and call.provider_call_id != event.provider_call_id:
            call.provider_call_id = event.provider_call_id
            await session.commit()
    changed = await apply_state(
        company_id,
        call_id,
        CallStatus(event.state.value),
        event.occurred_at,
        duration=event.duration_seconds,
        error=event.error_code,
    )
    if not changed:
        async with get_session_factory()() as session:
            await session.execute(
                update(TelephonyWebhookEvent)
                .where(TelephonyWebhookEvent.id == inserted)
                .values(applied="ignored")
            )
            await session.commit()
        return "ignored"
    return "applied"


async def apply_state(
    company_id: uuid.UUID,
    call_id: uuid.UUID,
    new: CallStatus,
    occurred_at: datetime,
    *,
    duration: int | None = None,
    error: str | None = None,
) -> bool:
    """Move a call forward in its lifecycle. Returns False if the change was stale."""
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        call = await session.scalar(
            select(Call).where(Call.company_id == company_id, Call.id == call_id).with_for_update()
        )
        if call is None:
            return False
        current = CallStatus(call.status)
        if current in TERMINAL_CALL_STATUSES or RANK[new] <= RANK[current]:
            return False  # duplicate-in-effect, out of order, or after the call ended
        old = call.status
        call.status = new.value
        call.last_activity_at = datetime.now(UTC)
        if new in (CallStatus.CONNECTED, CallStatus.ACTIVE) and call.started_at is None:
            call.started_at = occurred_at
        if new in TERMINAL_CALL_STATUSES:
            call.ended_at = max(occurred_at, call.started_at) if call.started_at else occurred_at
            if duration is not None:
                call.duration_seconds = max(0, duration)
            elif call.started_at is not None:
                call.duration_seconds = max(
                    0, int((call.ended_at - call.started_at).total_seconds())
                )
            if error:
                call.telephony_error = error[:64]
        if new in TIMELINE_STATES:
            label = new.value.replace("_", " ").lower()
            prefix = "Teams call" if call.channel == CallChannel.TEAMS else "Call"
            timeline.record(
                session,
                company_id=company_id,
                contact_id=call.contact_id,
                category=TimelineCategory.CALL,
                event_type=TimelineEventType.CALL_STATUS_CHANGED,
                summary=f"{prefix} {label}",
                actor_user_id=None,
                from_status=old,
                to_status=new.value,
                call_id=call.id,
                channel=call.channel,
            )
        if new == CallStatus.CONNECTED:
            await conversations.record_call_event(
                session, company_id, call.id, ConversationEventType.CALL_CONNECTED
            )
        if new in TERMINAL_CALL_STATUSES:
            await conversations.record_call_event(
                session,
                company_id,
                call.id,
                ConversationEventType.CALL_ENDED,
                status=new.value,
                ended=True,
            )
        await session.commit()
        payload = {
            "status": call.status,
            "started_at": _iso(call.started_at),
            "ended_at": _iso(call.ended_at),
        }
    hub.publish(call_id, "call.status", payload)
    if new in (CallStatus.CONNECTED, CallStatus.ACTIVE):
        _connected_calls.add(call_id)
    if new in TERMINAL_CALL_STATUSES:
        _connected_calls.discard(call_id)
        schedule_finalize(company_id, call_id)
    return True


# Calls this process saw connect: lets a media stream that started before the customer answered
# begin forwarding immediately (the periodic DB check covers other processes / restarts).
_connected_calls: set[uuid.UUID] = set()


# ---- End of call: drain the live pipeline, THEN post-call processing ----------------------------

_finalizers: dict[uuid.UUID, asyncio.Task[None]] = {}


def schedule_finalize(company_id: uuid.UUID, call_id: uuid.UUID) -> asyncio.Task[None]:
    """Run the end-of-call sequence in the background (provider webhooks must answer fast):
    live session drain (final transcript + copilot) -> post-call job. Idempotent per call."""
    existing = _finalizers.get(call_id)
    if existing is not None:
        return existing
    task = asyncio.create_task(_finalize(company_id, call_id), name=f"finalize-{call_id}")
    _finalizers[call_id] = task
    return task


async def _finalize(company_id: uuid.UUID, call_id: uuid.UUID) -> None:
    try:
        await live.close_session(call_id)
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=company_id))
            persistence = await session.scalar(
                select(Call.transcript_persistence).where(
                    Call.company_id == company_id, Call.id == call_id
                )
            )
            # Nothing derived from media may be stored for a transient (e.g. Teams) call.
            if persistence == TranscriptPersistence.PERSISTED and jobs.has_handler(
                "postcall.process"
            ):
                await jobs.enqueue(
                    session,
                    "postcall.process",
                    company_id=company_id,
                    payload={"call_id": str(call_id)},
                    dedupe_key=f"postcall:{call_id}",
                )
            await session.execute(
                update(Call)
                .where(Call.company_id == company_id, Call.id == call_id)
                .values(finalized_at=datetime.now(UTC))
            )
            await session.commit()
    except Exception:
        logger.exception("call_finalize_failed", extra={"call_id": str(call_id)})
    finally:
        _finalizers.pop(call_id, None)


async def wait_finalized(call_id: uuid.UUID) -> None:
    task = _finalizers.get(call_id)
    if task is not None:
        await asyncio.shield(task)


async def wait_all_finalized() -> None:
    tasks = list(_finalizers.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class MediaStreamEnded(Exception):
    """The call is over (or ending): the media WebSocket should be closed."""


class MediaIngest:
    """Consumes normalised media messages for one call (from the provider's WebSocket or the
    simulator). Audio goes straight to the live session's STT streams and is never stored.

    Every second or so it re-reads the call's state (one indexed query): it raises
    ``MediaStreamEnded`` once the call is ENDING/terminal so the socket is closed server-side,
    and - for providers whose stream can start before the customer has answered
    (``require_connected``) - it drops frames (ringback tone) until the provider reported the
    call CONNECTED. It also records a throttled ``last_activity_at`` so stuck calls can be told
    apart from quiet ones."""

    CHECK_EVERY_S = 1.0
    ACTIVITY_EVERY_S = 15.0

    def __init__(self, call_id: uuid.UUID, *, require_connected: bool = False) -> None:
        self.call_id = call_id
        self.require_connected = require_connected
        self.started = False
        self.encoding = "mulaw"
        self.sample_rate = 8000
        self._connected = not require_connected
        self._checked_at = 0.0
        self._activity_at = 0.0
        self._company_id: uuid.UUID | None = None

    async def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked_at < self.CHECK_EVERY_S and self._checked_at:
            return
        self._checked_at = now
        if self._company_id is None:
            async with get_session_factory()() as db:
                route = await db.get(CallRoute, self.call_id)
            if route is None:
                raise MediaStreamEnded()
            self._company_id = route.company_id
        touch = now - self._activity_at >= self.ACTIVITY_EVERY_S
        async with get_session_factory()() as db:
            await set_tenant_context(db, TenantContext(company_id=self._company_id))
            status = await db.scalar(
                select(Call.status).where(
                    Call.company_id == self._company_id, Call.id == self.call_id
                )
            )
            if status is not None and touch and status not in TERMINAL_STATUS_VALUES:
                self._activity_at = now
                await db.execute(
                    update(Call)
                    .where(Call.company_id == self._company_id, Call.id == self.call_id)
                    .values(last_activity_at=datetime.now(UTC))
                )
                await db.commit()
        if status is None or status in TERMINAL_STATUS_VALUES or status == CallStatus.ENDING:
            raise MediaStreamEnded()
        if status in (CallStatus.CONNECTED, CallStatus.ACTIVE):
            self._connected = True

    async def handle(self, message: MediaFrame | MediaControl | None) -> None:
        if message is None:
            return
        if isinstance(message, MediaControl):
            if message.kind == "start":
                self.encoding = message.encoding or self.encoding
                self.sample_rate = message.sample_rate or self.sample_rate
                await self._refresh()
                await self._start()
            elif message.kind == "stop":
                await self._stopped()
            return
        await self._refresh()
        if not self._connected and self.call_id in _connected_calls:
            self._connected = True
        if not self._connected:
            return  # the customer has not answered yet (ringback/early media): not call audio
        if not self.started:
            await self._start()
        session = live.get_session(self.call_id)
        if session is not None:
            await session.on_audio(message.track, message.audio)

    async def _start(self) -> None:
        if self.started or not self._connected:
            return
        self.started = True
        async with get_session_factory()() as db:
            route = await db.get(CallRoute, self.call_id)
        if route is None:
            return
        await apply_state(route.company_id, self.call_id, CallStatus.ACTIVE, datetime.now(UTC))
        status = await _status_of(route.company_id, self.call_id)
        if status is None or status in TERMINAL_CALL_STATUSES or status == CallStatus.ENDING:
            return  # never process audio for a call that has ended or is ending
        async with get_session_factory()() as db:
            await set_tenant_context(db, TenantContext(company_id=route.company_id))
            await conversations.record_call_event(
                db, route.company_id, self.call_id, ConversationEventType.AUDIO_STREAM_STARTED
            )
            await db.commit()
        try:
            await live.get_or_open(
                self.call_id, encoding=self.encoding, sample_rate=self.sample_rate
            )
        except LookupError:
            logger.warning("media_for_unknown_call")

    async def _stopped(self) -> None:
        """The provider closed the audio stream (AUDIO_INPUT_FINISHED). The call itself may
        continue; outstanding transcription results keep draining into the pipeline."""
        session = live.get_session(self.call_id)
        if session is not None:
            await session.finish_input()
        async with get_session_factory()() as db:
            route = await db.get(CallRoute, self.call_id)
        if route is None:
            return
        async with get_session_factory()() as db:
            await set_tenant_context(db, TenantContext(company_id=route.company_id))
            await conversations.record_call_event(
                db, route.company_id, self.call_id, ConversationEventType.AUDIO_STREAM_ENDED
            )
            await db.commit()
