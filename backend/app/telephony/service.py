"""Telephony use cases: start/end calls, provider webhooks (verified, idempotent, order-safe),
media stream ingestion.

The provider is authoritative for call state: the browser never sets telephony states.
Duplicate webhooks are ignored via a unique (provider, event_id); out-of-order webhooks never
move a call backwards and nothing changes a call after it reaches a terminal state.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.models import (
    TERMINAL_CALL_STATUSES,
    Call,
    CallChannel,
    CallStatus,
    TranscriptPersistence,
)
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import AppError, ForbiddenError, InvalidStateError
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
    CallStatus.COMPLETED: 5,
    CallStatus.NO_ANSWER: 5,
    CallStatus.FAILED: 5,
    CallStatus.CANCELLED: 5,
}
TIMELINE_STATES = {CallStatus.CONNECTED, *TERMINAL_CALL_STATUSES}


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


async def start_call(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, *, ip: str | None
) -> Call:
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can start this call")
    if call.status != CallStatus.PLANNED:
        raise InvalidStateError("Only a planned call can be started")
    contact = await ContactRepository(session).get(principal.company_id, call.contact_id)
    user = await session.get(User, principal.user_id)
    teams = call.channel == CallChannel.TEAMS
    if contact is None:
        raise MissingPhoneError("Contact not found")
    if not teams:
        if not contact.phone:
            raise MissingPhoneError("Add the customer's phone number before starting the call")
        if user is None or not user.phone:
            raise MissingPhoneError(
                "Add your own phone number in your profile before starting a call"
            )
    elif not call.meeting_url:
        raise InvalidStateError("A Teams call needs the meeting link")

    resolved = await calling.resolve_for_start(session, principal.company_id, call)
    provider = resolved.provider
    call.status = CallStatus.INITIATED.value
    call.user_id = call.user_id or principal.user_id
    call.provider = provider.name
    call.telephony_error = None
    call.transcript_persistence = resolved.persistence.value
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
    await session.commit()

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
    except TelephonyError as exc:
        call.status = CallStatus.FAILED.value
        call.telephony_error = exc.code
        call.ended_at = datetime.now(UTC)
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
        raise TelephonyUnavailableError() from None
    call.provider_call_id = provider_call_id
    route = await session.get(CallRoute, call.id)
    if route is not None:
        route.provider_call_id = provider_call_id
    await session.commit()
    hub.publish(call.id, "call.status", {"status": call.status})
    return await get_call(session, principal, call.id)


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


async def request_end(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> Call:
    """Ask the provider to hang up. The final state arrives via webhook (provider is the
    source of truth); the mock provider emits that webhook itself."""
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can end this call")
    if CallStatus(call.status) in TERMINAL_CALL_STATUSES:
        return call
    if call.status == CallStatus.PLANNED:
        raise InvalidStateError("This call has not been started")
    provider = await calling.provider_for_call(session, principal.company_id, call)
    if call.provider_call_id and provider is not None:
        try:
            await provider.end_call(call.provider_call_id)
        except TelephonyError:
            logger.warning("telephony_end_failed", extra={"call_id": str(call.id)})
        if provider.emits_end_events:
            state = (
                ProviderCallState.COMPLETED
                if call.status in (CallStatus.CONNECTED, CallStatus.ACTIVE)
                else ProviderCallState.CANCELLED
            )
            await process_event(
                provider.name,
                TelephonyEvent(
                    event_id=f"mock-end-{call.id}",
                    provider_call_id=call.provider_call_id,
                    state=state,
                    occurred_at=datetime.now(UTC),
                ),
            )
    await session.refresh(call)
    return await get_call(session, principal, call.id)


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
    if new in TERMINAL_CALL_STATUSES:
        schedule_finalize(company_id, call_id)
    return True


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


class MediaIngest:
    """Consumes normalised media messages for one call (from the provider's WebSocket or the
    simulator). Audio goes straight to the live session's STT streams."""

    def __init__(self, call_id: uuid.UUID) -> None:
        self.call_id = call_id
        self.started = False
        self.encoding = "mulaw"
        self.sample_rate = 8000

    async def handle(self, message: MediaFrame | MediaControl | None) -> None:
        if message is None:
            return
        if isinstance(message, MediaControl):
            if message.kind == "start":
                self.encoding = message.encoding or self.encoding
                self.sample_rate = message.sample_rate or self.sample_rate
                await self._start()
            elif message.kind == "stop":
                await self._stopped()
            return
        if not self.started:
            await self._start()
        session = live.get_session(self.call_id)
        if session is not None:
            await session.on_audio(message.track, message.audio)

    async def _start(self) -> None:
        if self.started:
            return
        self.started = True
        async with get_session_factory()() as db:
            route = await db.get(CallRoute, self.call_id)
        if route is None:
            return
        await apply_state(route.company_id, self.call_id, CallStatus.ACTIVE, datetime.now(UTC))
        async with get_session_factory()() as db:
            await set_tenant_context(db, TenantContext(company_id=route.company_id))
            status = await db.scalar(
                select(Call.status).where(
                    Call.company_id == route.company_id, Call.id == self.call_id
                )
            )
        if status is None or CallStatus(status) in TERMINAL_CALL_STATUSES:
            return  # never process audio for a call that has already ended
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
