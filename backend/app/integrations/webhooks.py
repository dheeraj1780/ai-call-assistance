"""Provider webhook endpoints (public, unauthenticated by user tokens).

Every handler:
1. resolves the tenant ONLY from the integration route in the URL (system table) - never from an
   identifier inside the payload;
2. verifies the provider signature with THAT integration's secret (WhatsApp X-Hub-Signature-256,
   Plivo V3 signature, Teams clientState, gateway HMAC) - so a URL for tenant A cannot be used
   with tenant B's (or an attacker's) payloads;
3. enforces idempotency (integration_webhook_receipts + unique message/event ids);
4. normalises and stores quickly; AI work runs in background jobs;
5. answers in the format the provider expects. Rejections are logged without payloads.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.calls.models import Call, CallChannel, CallStatus, TranscriptPersistence
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import error_response
from app.contacts.models import Contact, ContactSource
from app.conversations import service as conversations
from app.conversations.identity import normalize_phone
from app.integrations import service as integrations
from app.integrations.domain import Capability, ConversationEventType, IntegrationMode, Provider
from app.integrations.models import Integration, WebhookReceipt
from app.integrations.providers import plivo as plivo_adapter
from app.integrations.providers import teams as teams_adapter
from app.integrations.providers import whatsapp as whatsapp_adapter
from app.integrations.providers.base import WebhookRejectedError
from app.jobs import service as jobs
from app.telephony import service as telephony
from app.telephony.models import CallRoute
from app.telephony.provider import ProviderCallState, TelephonyEvent, media_token
from app.tenants.models import CompanyMember, MemberRole
from app.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", include_in_schema=False)

MAX_BODY = 256 * 1024


async def _receipt(provider: Provider, integration_id: uuid.UUID | None, key: str) -> bool:
    """True if this webhook item is new (first delivery)."""
    async with get_session_factory()() as session:
        inserted = await session.scalar(
            insert(WebhookReceipt)
            .values(
                id=uuid.uuid4(),
                provider=provider.value,
                integration_id=integration_id,
                dedupe_key=key[:300],
                outcome="received",
            )
            .on_conflict_do_nothing(index_elements=["provider", "dedupe_key"])
            .returning(WebhookReceipt.id)
        )
        await session.commit()
    return inserted is not None


async def _release_receipt(provider: Provider, key: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            delete(WebhookReceipt).where(
                WebhookReceipt.provider == provider.value, WebhookReceipt.dedupe_key == key[:300]
            )
        )
        await session.commit()


def _reject(provider: str, reason: str, status: int = 401) -> Response:
    logger.warning("webhook_rejected", extra={"provider": provider, "reason": reason})
    return error_response(status, "invalid_signature" if status == 401 else "not_found", "Rejected")


def _public_url(request: Request) -> str:
    """The URL as the provider called it (we gave providers PUBLIC_BASE_URL-based URLs; a proxy
    may change scheme/host seen by the app)."""
    base = get_settings().public_base_url.rstrip("/")
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


# ---- WhatsApp --------------------------------------------------------------------------------


@router.get("/whatsapp/webhooks/{integration_id}")
async def whatsapp_verify(integration_id: uuid.UUID, request: Request) -> Response:
    async with get_session_factory()() as session:
        record = await integrations.load_for_webhook(session, integration_id, Provider.WHATSAPP)
        if record is None or record.mode != IntegrationMode.LIVE:
            return _reject("whatsapp", "unknown integration", 404)
        secrets = integrations.secrets_of(record)
        try:
            challenge = whatsapp_adapter.verify_challenge(
                secrets.get("verify_token", ""), dict(request.query_params)
            )
        except WebhookRejectedError as exc:
            return _reject("whatsapp", str(exc), 403)
        record.config = {**record.config, "webhook_verified_at": datetime.now(UTC).isoformat()}
        await session.commit()
    return PlainTextResponse(challenge)


@router.post("/whatsapp/webhooks/{integration_id}")
async def whatsapp_webhook(integration_id: uuid.UUID, request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY:
        return error_response(413, "payload_too_large", "Payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    async with get_session_factory()() as session:
        record = await integrations.load_for_webhook(session, integration_id, Provider.WHATSAPP)
        if record is None or record.mode != IntegrationMode.LIVE:
            return _reject("whatsapp", "unknown integration", 404)
        secrets = integrations.secrets_of(record)
        try:
            whatsapp_adapter.verify_signature(secrets.get("app_secret", ""), headers, body)
            events = whatsapp_adapter.parse_webhook(
                body, expected_phone_number_id=str(record.config.get("phone_number_id", ""))
            )
        except WebhookRejectedError as exc:
            return _reject("whatsapp", str(exc))
        counts = await process_message_events(session, record, events)
    return JSONResponse({"received": len(events), **counts})


async def process_message_events(
    session: AsyncSession,
    record: Integration,
    events: list[Any],
    *,
    owner_user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Shared by live webhooks and the developer simulator (same code path)."""
    counts: dict[str, int] = {}
    provider = Provider(record.provider)
    if Capability.MESSAGE.value not in record.enabled_capabilities:
        counts["ignored_disabled"] = len(events)
        return counts
    for event in events:
        key = f"{record.id}:{event.external_event_id}"
        if not await _receipt(provider, record.id, key):
            outcome = "duplicate"
        else:
            await set_tenant_context(session, TenantContext(company_id=record.company_id))
            try:
                outcome = await conversations.ingest(
                    session,
                    record.company_id,
                    event,
                    integration_id=record.id,
                    owner_user_id=owner_user_id,
                )
            except Exception:
                # Not processed: forget the receipt so the provider's retry (after our 5xx) is
                # applied instead of being dropped as a duplicate.
                await session.rollback()
                await _release_receipt(provider, key)
                raise
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# ---- Microsoft Teams (Graph change notifications) ---------------------------------------------


@router.post("/teams/webhooks/{integration_id}")
async def teams_notifications(integration_id: uuid.UUID, request: Request) -> Response:
    token = request.query_params.get("validationToken")
    if token is not None:
        # Subscription validation handshake: echo the token as text/plain within 10 seconds.
        if len(token) > 1024:
            return _reject("teams", "bad validation token", 400)
        route = await integrations.route_for(integration_id)
        if route is None or route.provider != Provider.MICROSOFT_TEAMS.value:
            return _reject("teams", "unknown integration", 404)
        return PlainTextResponse(token)
    body = await request.body()
    if len(body) > MAX_BODY:
        return error_response(413, "payload_too_large", "Payload too large")
    route = await integrations.route_for(integration_id)
    if route is None or route.provider != Provider.MICROSOFT_TEAMS.value:
        return _reject("teams", "unknown integration", 404)
    try:
        notes = teams_adapter.parse_notifications(body, integration_id)
    except WebhookRejectedError as exc:
        return _reject("teams", str(exc))
    queued = 0
    for n in notes:
        if n.change_type != "created":
            continue
        if not await _receipt(
            Provider.MICROSOFT_TEAMS,
            integration_id,
            f"{n.subscription_id}:{n.chat_id}:{n.message_id}",
        ):
            continue
        async with get_session_factory()() as session:
            await jobs.enqueue(
                session,
                "teams.message_notification",
                company_id=route.company_id,
                payload={
                    "subscription_id": n.subscription_id,
                    "chat_id": n.chat_id,
                    "message_id": n.message_id,
                },
                dedupe_key=f"teams-msg:{n.chat_id}:{n.message_id}"[:128],
            )
            await session.commit()
        queued += 1
    return JSONResponse({"queued": queued}, status_code=202)


# ---- Teams media gateway (internal) -------------------------------------------------------------


class GatewayEvent(BaseModel):
    event_id: str
    call_id: uuid.UUID
    gateway_call_id: str
    state: str | None = None  # ESTABLISHING | ESTABLISHED | TERMINATED | FAILED
    recording_status: str | None = None  # RECORDING_CONFIRMED | RECORDING_FAILED
    media_status: str | None = None  # AVAILABLE | UNAVAILABLE (API media socket / compliance)
    error_code: str | None = None


_GATEWAY_STATES = {
    "ESTABLISHING": ProviderCallState.RINGING,
    "ESTABLISHED": ProviderCallState.CONNECTED,
    "TERMINATED": ProviderCallState.COMPLETED,
    "FAILED": ProviderCallState.FAILED,
}


@router.post("/teams/gateway/events")
async def teams_gateway_events(request: Request) -> Response:
    settings = get_settings()
    secret = (
        settings.teams_media_gateway_secret.get_secret_value()
        if settings.teams_media_gateway_secret is not None
        else ""
    )
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        teams_adapter.verify_gateway_signature(secret, headers, body)
        event = GatewayEvent.model_validate_json(body)
    except (WebhookRejectedError, ValidationError) as exc:
        return _reject("teams-gateway", type(exc).__name__)
    outcome = await handle_gateway_event("teams", event)
    return JSONResponse({"outcome": outcome})


async def handle_gateway_event(provider_name: str, event: GatewayEvent) -> str:
    """Apply one Teams media-gateway event (also used by the mock Teams simulator)."""
    async with get_session_factory()() as session:
        route = await session.get(CallRoute, event.call_id)
    if route is None or route.provider != provider_name:
        return "unknown_call"
    if event.recording_status:
        await _apply_recording_status(route.company_id, event.call_id, event.recording_status)
    if event.media_status in ("AVAILABLE", "UNAVAILABLE"):
        from app.live import session as live

        media_state = "available" if event.media_status == "AVAILABLE" else "unavailable"
        reason = (event.error_code or event.recording_status or "")[:64] or None
        live.set_media_status(event.call_id, media_state, reason)
        logger.info(
            "teams_media_status",
            extra={"call_id": str(event.call_id), "state": media_state, "reason": reason},
        )
    state = _GATEWAY_STATES.get(event.state or "")
    if state is None:
        return "recorded"
    if event.state in ("ESTABLISHED", "TERMINATED", "FAILED"):
        logger.info(
            "teams_media_session_event",
            extra={"call_id": str(event.call_id), "state": event.state, "error": event.error_code},
        )
    return await telephony.process_event_for_call(
        provider_name,
        route.company_id,
        event.call_id,
        TelephonyEvent(
            event_id=f"gw:{event.event_id}"[:128],
            provider_call_id=event.gateway_call_id[:128],
            state=state,
            occurred_at=datetime.now(UTC),
            error_code=(event.error_code or None) if state == ProviderCallState.FAILED else None,
        ),
    )


async def _apply_recording_status(company_id: uuid.UUID, call_id: uuid.UUID, status: str) -> None:
    """Microsoft policy: nothing derived from call media may be persisted unless the bot first
    set the call's recording status and Teams confirmed it. Only then does a Teams call move
    from PENDING_RECORDING_STATUS to PERSISTED."""
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        call = await session.scalar(
            select(Call).where(Call.company_id == company_id, Call.id == call_id).with_for_update()
        )
        if call is None:
            return
        if (
            status == "RECORDING_CONFIRMED"
            and call.transcript_persistence == TranscriptPersistence.PENDING_RECORDING_STATUS
        ):
            call.transcript_persistence = TranscriptPersistence.PERSISTED.value
        elif status != "RECORDING_CONFIRMED":
            call.transcript_persistence = TranscriptPersistence.TRANSIENT.value
        await conversations.record_call_event(
            session,
            company_id,
            call_id,
            ConversationEventType.RECORDING_STATUS_CHANGED,
            status=status[:32],
        )
        await session.commit()


# ---- Plivo ---------------------------------------------------------------------------------

PLIVO_KINDS = {"ring", "answer", "fallback", "dial", "dial_action", "hangup", "stream", "inbound"}


def _xml(content: str) -> Response:
    return Response(content=content, media_type="application/xml")


@router.post("/plivo/webhooks/{integration_id}/{kind}")
async def plivo_webhook(integration_id: uuid.UUID, kind: str, request: Request) -> Response:
    if kind not in PLIVO_KINDS:
        return _reject("plivo", "unknown callback", 404)
    body = await request.body()
    if len(body) > MAX_BODY:
        return error_response(413, "payload_too_large", "Payload too large")
    form = await request.form()
    params: dict[str, list[str] | str] = {}
    for key in form:
        values = [str(v) for v in form.getlist(key)]
        params[key] = values if len(values) > 1 else values[0]
    headers = {k.lower(): v for k, v in request.headers.items()}
    async with get_session_factory()() as session:
        record = await integrations.load_for_webhook(session, integration_id, Provider.PLIVO)
        if record is None or record.mode != IntegrationMode.LIVE:
            return _reject("plivo", "unknown integration", 404)
        secrets = integrations.secrets_of(record)
        try:
            plivo_adapter.verify_signature_v3(
                secrets.get("auth_token", ""),
                uri=_public_url(request),
                method="POST",
                headers=headers,
                params=params,
            )
        except WebhookRejectedError as exc:
            return _reject("plivo", str(exc))
        if kind == "inbound":
            return await _plivo_inbound(session, record, params)
        try:
            cb = plivo_adapter.parse_callback(kind if kind != "fallback" else "hangup", params)
        except WebhookRejectedError as exc:
            return _reject("plivo", str(exc), 400)
        call_id = await _plivo_call_id(record, request.query_params.get("cid"), cb.call_uuid)
        if call_id is None:
            logger.warning("plivo_callback_unknown_call", extra={"kind": kind})
            return (
                _xml(plivo_adapter.hangup_xml())
                if kind in ("answer", "fallback")
                else PlainTextResponse("OK")
            )
        if kind == "fallback":
            await telephony.process_event_for_call(
                "plivo",
                record.company_id,
                call_id,
                TelephonyEvent(
                    f"fallback:{cb.call_uuid}",
                    cb.call_uuid,
                    ProviderCallState.FAILED,
                    datetime.now(UTC),
                    error_code="answer_url_failed",
                ),
            )
            return _xml(plivo_adapter.hangup_xml())
        if kind == "stream":
            await set_tenant_context(session, TenantContext(company_id=record.company_id))
            event = str(params.get("Event", ""))
            await conversations.record_call_event(
                session,
                record.company_id,
                call_id,
                ConversationEventType.AUDIO_STREAM_ENDED
                if "stop" in event.lower() or "fail" in event.lower()
                else ConversationEventType.AUDIO_STREAM_STARTED,
                status=event[:32] or None,
            )
            await session.commit()
            return PlainTextResponse("OK")
        if cb.state is not None:
            await telephony.process_event_for_call(
                "plivo",
                record.company_id,
                call_id,
                TelephonyEvent(
                    cb.event_id,
                    cb.call_uuid,
                    cb.state,
                    datetime.now(UTC),
                    duration_seconds=cb.duration,
                    error_code=cb.error,
                ),
            )
        if kind == "answer":
            return _xml(await _answer_xml(session, record, call_id, a_leg="agent"))
        if kind == "dial_action":
            return _xml(plivo_adapter.empty_xml())
    return PlainTextResponse("OK")


async def _plivo_call_id(record: Integration, cid: str | None, call_uuid: str) -> uuid.UUID | None:
    """Resolve the call for a Plivo callback: by our signed ``cid`` (outbound) or by Plivo's
    CallUUID (inbound / application-level callbacks). Must belong to this integration's tenant."""
    async with get_session_factory()() as session:
        route: CallRoute | None = None
        if cid:
            try:
                route = await session.get(CallRoute, uuid.UUID(cid))
            except ValueError:
                return None
        else:
            route = await session.scalar(
                select(CallRoute).where(
                    CallRoute.provider == "plivo", CallRoute.provider_call_id == call_uuid
                )
            )
    if route is None or route.company_id != record.company_id or route.provider != "plivo":
        return None
    return route.call_id


def _stream_url(call_id: uuid.UUID, a_leg: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    ws = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws}/api/v1/telephony/media/plivo/{call_id}?token={media_token(call_id)}&leg={a_leg}"


async def _answer_xml(
    session: AsyncSession, record: Integration, call_id: uuid.UUID, *, a_leg: str
) -> str:
    await set_tenant_context(session, TenantContext(company_id=record.company_id))
    call = await session.scalar(
        select(Call).where(Call.company_id == record.company_id, Call.id == call_id)
    )
    if call is None or CallStatus(call.status) not in (CallStatus.INITIATED, CallStatus.RINGING):
        return plivo_adapter.hangup_xml()
    if a_leg == "agent":
        contact = await session.scalar(
            select(Contact).where(
                Contact.company_id == record.company_id, Contact.id == call.contact_id
            )
        )
        target = contact.phone if contact else None
    else:
        user = await session.get(User, call.user_id) if call.user_id else None
        target = user.phone if user else None
    if not target:
        return plivo_adapter.hangup_xml()
    base = get_settings().public_base_url.rstrip("/")
    cb = f"{base}/api/v1/integrations/plivo/webhooks/{record.id}"
    stream = Capability.MEDIA_STREAM.value in record.enabled_capabilities
    return plivo_adapter.answer_xml(
        customer_number=target,
        caller_id=str(record.config.get("phone_number", "")),
        stream_url=_stream_url(call_id, a_leg) if stream else None,
        stream_status_url=f"{cb}/stream?cid={call_id}",
        dial_action_url=f"{cb}/dial_action?cid={call_id}",
        dial_callback_url=f"{cb}/dial?cid={call_id}",
    )


async def _plivo_inbound(
    session: AsyncSession, record: Integration, params: dict[str, list[str] | str]
) -> Response:
    """A customer calls the Plivo number: find (or create) the contact, ring the responsible
    salesperson, stream audio to the copilot. Never answered by AI."""
    if Capability.PHONE_CALL.value not in record.enabled_capabilities:
        return _xml(plivo_adapter.hangup_xml())
    call_uuid = str(params.get("CallUUID", ""))[:128]
    caller = normalize_phone(str(params.get("From", "")))
    if not call_uuid or not caller:
        return _xml(plivo_adapter.hangup_xml())
    company_id = record.company_id
    await set_tenant_context(session, TenantContext(company_id=company_id))
    existing = await session.scalar(
        select(CallRoute).where(
            CallRoute.provider == "plivo", CallRoute.provider_call_id == call_uuid
        )
    )
    if existing is not None:
        return _xml(await _answer_xml(session, record, existing.call_id, a_leg="customer"))
    match = await conversations.match_contact(session, company_id, identities=[], phone=caller)
    contact_id = match.contact_id
    if contact_id is None:
        # Unknown or ambiguous caller: a new lead record (never merged with existing contacts).
        contact = Contact(
            company_id=company_id,
            name=f"Caller +{caller}",
            phone=f"+{caller}",
            source=ContactSource.INBOUND_CALL.value,
        )
        session.add(contact)
        await session.flush()
        contact_id = contact.id
    else:
        found = await session.scalar(
            select(Contact).where(Contact.company_id == company_id, Contact.id == contact_id)
        )
        if found is None:  # pragma: no cover - matched ids exist under RLS
            return _xml(plivo_adapter.hangup_xml())
        contact = found
    user_id = await _inbound_salesperson(session, company_id, contact.owner_user_id)
    if user_id is None:
        await session.commit()
        return _xml(plivo_adapter.hangup_xml())
    call = Call(
        id=uuid.uuid4(),
        company_id=company_id,
        contact_id=contact_id,
        user_id=user_id,
        objective="Inbound call",
        status=CallStatus.INITIATED.value,
        channel=CallChannel.PHONE.value,
        provider="plivo",
        provider_call_id=call_uuid,
        transcript_persistence=TranscriptPersistence.PERSISTED.value,
    )
    session.add(call)
    await session.flush()
    session.add(
        CallRoute(
            call_id=call.id, company_id=company_id, provider="plivo", provider_call_id=call_uuid
        )
    )
    await conversations.open_call_session(session, call, integration_id=record.id)
    await session.commit()
    await telephony.process_event_for_call(
        "plivo",
        company_id,
        call.id,
        TelephonyEvent(
            f"inbound:{call_uuid}", call_uuid, ProviderCallState.RINGING, datetime.now(UTC)
        ),
    )
    return _xml(await _answer_xml(session, record, call.id, a_leg="customer"))


async def _inbound_salesperson(
    session: AsyncSession, company_id: uuid.UUID, owner_user_id: uuid.UUID | None
) -> uuid.UUID | None:
    if owner_user_id is not None:
        user = await session.get(User, owner_user_id)
        if user is not None and user.phone:
            return user.id
    rows = await session.execute(
        select(User.id)
        .join(CompanyMember, CompanyMember.user_id == User.id)
        .where(
            CompanyMember.company_id == company_id,
            CompanyMember.role.in_([MemberRole.OWNER.value, MemberRole.ADMIN.value]),
            User.phone.is_not(None),
            User.is_active.is_(True),
        )
        .order_by(CompanyMember.created_at)
        .limit(1)
    )
    row = rows.first()
    return row[0] if row else None


__all__ = ["GatewayEvent", "handle_gateway_event", "process_message_events", "router", "update"]
