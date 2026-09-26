"""Conversation use cases, shared by every channel.

- ``ingest``: apply one normalised ConversationEvent exactly once (tenant already bound by the
  caller from a verified webhook route - never from an identifier inside the payload).
- contact matching: exact identifiers only (confirmed identities, normalised phone, email).
  Two candidates -> AMBIGUOUS, none -> UNMATCHED; a human links the conversation. Names are
  never used for matching and contacts are never merged automatically.
- ``send_message``: the ONLY way a message leaves the system; always an explicit user action
  (AI drafts are suggestions). WhatsApp free-form messages respect the 24-hour window.
- calls (Teams/phone) get a communication session too, so every channel shares one model.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.models import Call
from app.common.config import get_settings
from app.common.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.contacts.models import Contact
from app.contacts.repository import ContactRepository
from app.conversations.identity import normalize_email, normalize_phone, phone_variants
from app.conversations.models import (
    MESSAGE_STATUS_RANK,
    CommunicationEvent,
    CommunicationMessage,
    CommunicationParticipant,
    CommunicationSession,
    ContactIdentity,
    Direction,
    IdentityKind,
    MatchStatus,
    MessageDraft,
    MessageDraftStatus,
    MessageStatus,
    MessageType,
    ParticipantRole,
    SenderType,
    SessionStatus,
)
from app.integrations import service as integrations
from app.integrations.domain import (
    Capability,
    Channel,
    ConversationEventType,
    IntegrationMode,
    Provider,
)
from app.integrations.normalizer import ConversationEvent, ParticipantRef
from app.integrations.providers import factory
from app.integrations.providers.base import (
    MessageProvider,
    OutboundMessage,
    ProviderAuthError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
)
from app.integrations.providers.whatsapp import META_ERRORS
from app.jobs import service as jobs
from app.tenants.models import Company
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType

logger = logging.getLogger(__name__)

WHATSAPP_WINDOW = timedelta(hours=24)
CHANNEL_LABEL = {Channel.WHATSAPP: "WhatsApp", Channel.TEAMS: "Teams", Channel.PHONE: "Phone"}


class WhatsAppWindowClosedError(AppError):
    status_code = 409
    code = "whatsapp_window_closed"
    message = (
        "WhatsApp only allows free-form messages within 24 hours of the customer's last "
        "message. Ask the customer to message you first (template messages are not supported "
        "in this version)."
    )


class MessageSendFailedError(AppError):
    status_code = 502
    code = "message_send_failed"
    message = "The message could not be sent. It is saved as failed; you can try again."


# ---- matching --------------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    contact_id: uuid.UUID | None
    candidates: int


async def match_contact(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    identities: list[tuple[IdentityKind, str]],
    phone: str | None = None,
    email: str | None = None,
) -> MatchResult:
    candidates: set[uuid.UUID] = set()
    if identities:
        rows = await session.scalars(
            select(ContactIdentity.contact_id).where(
                ContactIdentity.company_id == company_id,
                or_(
                    *[
                        (ContactIdentity.kind == k.value) & (ContactIdentity.value == v)
                        for k, v in identities
                    ]
                ),
            )
        )
        confirmed = set(rows.all())
        if len(confirmed) == 1:
            # A human-confirmed identity always wins over heuristic matches.
            return MatchResult(MatchStatus.MATCHED, confirmed.pop(), 1)
        candidates |= confirmed
    if phone:
        rows = await session.scalars(
            select(Contact.id).where(
                Contact.company_id == company_id,
                func.regexp_replace(Contact.phone, r"\D", "", "g").in_(phone_variants(phone)),
            )
        )
        candidates |= set(rows.all())
    if email:
        rows = await session.scalars(
            select(Contact.id).where(Contact.company_id == company_id, Contact.email == email)
        )
        candidates |= set(rows.all())
    if len(candidates) == 1:
        return MatchResult(MatchStatus.MATCHED, next(iter(candidates)), 1)
    if len(candidates) > 1:
        return MatchResult(MatchStatus.AMBIGUOUS, None, len(candidates))
    return MatchResult(MatchStatus.UNMATCHED, None, 0)


def _identities_for(
    channel: Channel, participant: ParticipantRef | None
) -> list[tuple[IdentityKind, str]]:
    if participant is None:
        return []
    if channel == Channel.WHATSAPP:
        return [(IdentityKind.WHATSAPP, participant.external_id)]
    if channel == Channel.TEAMS:
        return [(IdentityKind.TEAMS_USER, participant.external_id)]
    return []


# ---- sessions --------------------------------------------------------------------------------


async def _retention_days(session: AsyncSession, company_id: uuid.UUID) -> int:
    days = await session.scalar(
        select(Company.transcript_retention_days).where(Company.id == company_id)
    )
    return int(days or 30)


async def get_or_create_session(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    provider: str,
    channel: Channel,
    capability: Capability,
    external_session_id: str,
    integration_id: uuid.UUID | None,
    participant: ParticipantRef | None = None,
    owner_user_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
) -> CommunicationSession:
    stmt = select(CommunicationSession).where(
        CommunicationSession.company_id == company_id,
        CommunicationSession.provider == provider,
        CommunicationSession.capability == capability.value,
        CommunicationSession.external_session_id == external_session_id,
    )
    existing: CommunicationSession | None = await session.scalar(stmt)
    if existing is not None:
        if existing.match_status != MatchStatus.MATCHED and (participant or contact_id):
            await _apply_match(session, existing, channel, participant, contact_id)
        return existing
    comm = CommunicationSession(
        id=uuid.uuid4(),
        company_id=company_id,
        integration_id=integration_id,
        provider=provider,
        channel=channel.value,
        capability=capability.value,
        external_session_id=external_session_id,
        external_participant_id=participant.external_id if participant else None,
        owner_user_id=owner_user_id,
        status=SessionStatus.OPEN.value,
        match_status=MatchStatus.UNMATCHED.value,
        started_at=datetime.now(UTC),
        metadata_={},
    )
    await _apply_match(session, comm, channel, participant, contact_id)
    await session.execute(
        insert(CommunicationSession)
        .values(
            id=comm.id,
            company_id=company_id,
            integration_id=integration_id,
            contact_id=comm.contact_id,
            provider=provider,
            channel=channel.value,
            capability=capability.value,
            external_session_id=external_session_id,
            external_participant_id=comm.external_participant_id,
            owner_user_id=owner_user_id,
            status=comm.status,
            match_status=comm.match_status,
            started_at=comm.started_at,
            metadata_={"candidates": comm.metadata_.get("candidates", 0)},
        )
        .on_conflict_do_nothing(constraint="uq_communication_sessions_external_session")
    )
    row: CommunicationSession | None = await session.scalar(
        stmt.execution_options(populate_existing=True)
    )
    if row is None:  # pragma: no cover - insert or conflict always leaves a row
        raise RuntimeError("session upsert failed")
    return row


async def _apply_match(
    session: AsyncSession,
    comm: CommunicationSession,
    channel: Channel,
    participant: ParticipantRef | None,
    contact_id: uuid.UUID | None,
) -> None:
    if contact_id is not None:
        comm.contact_id = contact_id
        comm.match_status = MatchStatus.MATCHED.value
        return
    result = await match_contact(
        session,
        comm.company_id,
        identities=_identities_for(channel, participant),
        phone=participant.phone if participant else None,
        email=normalize_email(participant.email) if participant else None,
    )
    comm.contact_id = result.contact_id
    comm.match_status = result.status.value
    comm.metadata_ = {**(comm.metadata_ or {}), "candidates": result.candidates}


# ---- ingest ----------------------------------------------------------------------------------


async def ingest(
    session: AsyncSession,
    company_id: uuid.UUID,
    event: ConversationEvent,
    *,
    integration_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None = None,
) -> str:
    """Apply one message event. Returns applied | duplicate | ignored | unknown_message."""
    comm = await get_or_create_session(
        session,
        company_id,
        provider=event.provider.value,
        channel=event.channel,
        capability=event.capability,
        external_session_id=event.external_session_id,
        integration_id=integration_id,
        participant=event.participant,
        owner_user_id=owner_user_id,
    )
    if event.type == ConversationEventType.MESSAGE_STATUS_UPDATED:
        outcome = await _apply_status(session, comm, event)
        await session.commit()
        return outcome
    if event.message is None:
        return "ignored"
    msg = event.message
    days = await _retention_days(session, company_id)
    message_id = await session.scalar(
        insert(CommunicationMessage)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            session_id=comm.id,
            direction=msg.direction,
            sender_type=msg.sender_type,
            sender_external_id=event.participant.external_id if event.participant else None,
            sender_user_id=owner_user_id if msg.direction == Direction.OUTBOUND else None,
            message_type=msg.message_type,
            content=msg.text[:10_000],
            external_message_id=msg.external_message_id,
            status=msg.status,
            occurred_at=event.occurred_at,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            metadata_={},
        )
        .on_conflict_do_nothing(index_elements=["session_id", "external_message_id"])
        .returning(CommunicationMessage.id)
    )
    if message_id is None:
        await session.rollback()
        return "duplicate"
    inbound = msg.direction == Direction.INBOUND
    comm.last_message_at = max(comm.last_message_at or event.occurred_at, event.occurred_at)
    if inbound:
        comm.last_inbound_at = max(comm.last_inbound_at or event.occurred_at, event.occurred_at)
    if event.participant is not None:
        await _add_participant(session, comm, event.participant)
        if event.participant.display_name and not comm.metadata_.get("display_name"):
            comm.metadata_ = {**comm.metadata_, "display_name": event.participant.display_name}
    await _record_event(
        session,
        comm,
        event.type,
        event.external_event_id,
        event.occurred_at,
        {"message_id": str(message_id)},
    )
    if comm.contact_id is not None:
        _timeline_message(
            session, comm, inbound=inbound, actor=owner_user_id if not inbound else None
        )
    settings = get_settings()
    if (
        inbound
        and msg.sender_type == SenderType.CUSTOMER
        and msg.message_type != MessageType.UNSUPPORTED
        and settings.conversation_ai_assist_enabled
        and jobs.has_handler("conversation.assist")
    ):
        await jobs.enqueue(
            session,
            "conversation.assist",
            company_id=company_id,
            payload={"session_id": str(comm.id), "message_id": str(message_id)},
            dedupe_key=f"assist:{message_id}",
        )
    await session.commit()
    return "applied"


async def _apply_status(
    session: AsyncSession, comm: CommunicationSession, event: ConversationEvent
) -> str:
    external_id = str(event.data.get("external_message_id") or "")
    new = MessageStatus(str(event.data.get("status")))
    msg = await session.scalar(
        select(CommunicationMessage).where(
            CommunicationMessage.company_id == comm.company_id,
            CommunicationMessage.session_id == comm.id,
            CommunicationMessage.external_message_id == external_id,
        )
    )
    if msg is None:
        return "unknown_message"
    current = MessageStatus(msg.status)
    if current == MessageStatus.FAILED or MESSAGE_STATUS_RANK[new] <= MESSAGE_STATUS_RANK[current]:
        return "ignored"
    msg.status = new.value
    if new == MessageStatus.FAILED:
        msg.error_code = str(event.data.get("error_code") or "provider_failed")[:64]
    await _record_event(
        session,
        comm,
        event.type,
        event.external_event_id,
        event.occurred_at,
        {"message_id": str(msg.id), "status": new.value},
    )
    return "applied"


async def _add_participant(
    session: AsyncSession, comm: CommunicationSession, p: ParticipantRef
) -> None:
    await session.execute(
        insert(CommunicationParticipant)
        .values(
            id=uuid.uuid4(),
            company_id=comm.company_id,
            session_id=comm.id,
            role=p.role,
            contact_id=comm.contact_id if p.role == ParticipantRole.CUSTOMER else None,
            external_id=p.external_id[:256],
            display_name=p.display_name,
            joined_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["session_id", "role", "external_id"])
    )


async def _record_event(
    session: AsyncSession,
    comm: CommunicationSession,
    type_: ConversationEventType,
    external_event_id: str | None,
    occurred_at: datetime,
    data: dict[str, str | int | None],
) -> None:
    await session.execute(
        insert(CommunicationEvent)
        .values(
            id=uuid.uuid4(),
            company_id=comm.company_id,
            session_id=comm.id,
            type=type_.value,
            occurred_at=occurred_at,
            external_event_id=external_event_id[:300] if external_event_id else None,
            data=data,
        )
        .on_conflict_do_nothing(index_elements=["session_id", "external_event_id"])
    )


def _timeline_message(
    session: AsyncSession, comm: CommunicationSession, *, inbound: bool, actor: uuid.UUID | None
) -> None:
    if comm.contact_id is None:
        return
    label = CHANNEL_LABEL[Channel(comm.channel)]
    # No message content in the timeline: messages are retention-controlled, timeline events
    # are kept with the CRM record.
    timeline.record(
        session,
        company_id=comm.company_id,
        contact_id=comm.contact_id,
        category=TimelineCategory.MESSAGE,
        event_type=TimelineEventType.MESSAGE_RECEIVED
        if inbound
        else TimelineEventType.MESSAGE_SENT,
        summary=f"{label} message {'received from customer' if inbound else 'sent'}",
        actor_user_id=actor,
        channel=comm.channel,
        session_id=comm.id,
    )


# ---- reads -----------------------------------------------------------------------------------


async def get_session(
    session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> CommunicationSession:
    row: CommunicationSession | None = await session.scalar(
        select(CommunicationSession).where(
            CommunicationSession.company_id == principal.company_id,
            CommunicationSession.id == session_id,
        )
    )
    if row is None:
        raise NotFoundError("Conversation not found")
    return row


async def list_sessions(
    session: AsyncSession,
    principal: Principal,
    *,
    contact_id: uuid.UUID | None,
    match_status: MatchStatus | None,
    channel: Channel | None,
    limit: int,
) -> list[CommunicationSession]:
    stmt = select(CommunicationSession).where(
        CommunicationSession.company_id == principal.company_id,
        CommunicationSession.capability == Capability.MESSAGE.value,
    )
    if contact_id:
        stmt = stmt.where(CommunicationSession.contact_id == contact_id)
    if match_status:
        stmt = stmt.where(CommunicationSession.match_status == match_status.value)
    if channel:
        stmt = stmt.where(CommunicationSession.channel == channel.value)
    stmt = stmt.order_by(
        CommunicationSession.last_message_at.desc().nulls_last(), CommunicationSession.id
    ).limit(limit)
    return list((await session.scalars(stmt)).all())


async def list_messages(
    session: AsyncSession, company_id: uuid.UUID, session_id: uuid.UUID, limit: int = 200
) -> list[CommunicationMessage]:
    rows = await session.scalars(
        select(CommunicationMessage)
        .where(
            CommunicationMessage.company_id == company_id,
            CommunicationMessage.session_id == session_id,
        )
        .order_by(CommunicationMessage.occurred_at.desc(), CommunicationMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))


async def list_drafts(
    session: AsyncSession, company_id: uuid.UUID, session_id: uuid.UUID
) -> list[MessageDraft]:
    rows = await session.scalars(
        select(MessageDraft)
        .where(
            MessageDraft.company_id == company_id,
            MessageDraft.session_id == session_id,
            MessageDraft.status == MessageDraftStatus.SUGGESTED.value,
        )
        .order_by(MessageDraft.created_at.desc())
        .limit(5)
    )
    return list(rows.all())


# ---- linking ---------------------------------------------------------------------------------


async def link_contact(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    ip: str | None,
) -> CommunicationSession:
    """A human confirms which contact a conversation belongs to. The provider identity is
    remembered so future messages match automatically. Never merges contacts."""
    comm = await get_session(session, principal, session_id)
    contact = await ContactRepository(session).get(principal.company_id, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    identity_kind = {
        Channel.WHATSAPP: IdentityKind.WHATSAPP,
        Channel.TEAMS: IdentityKind.TEAMS_USER,
    }.get(Channel(comm.channel))
    if identity_kind and comm.external_participant_id:
        existing = await session.scalar(
            select(ContactIdentity).where(
                ContactIdentity.company_id == principal.company_id,
                ContactIdentity.kind == identity_kind.value,
                ContactIdentity.value == comm.external_participant_id,
            )
        )
        if existing is not None and existing.contact_id != contact.id:
            raise ConflictError(
                "This conversation's sender is already linked to another contact.",
                code="identity_linked_elsewhere",
            )
        if existing is None:
            session.add(
                ContactIdentity(
                    company_id=principal.company_id,
                    contact_id=contact.id,
                    kind=identity_kind.value,
                    value=comm.external_participant_id,
                    created_by_user_id=principal.user_id,
                )
            )
    comm.contact_id = contact.id
    comm.match_status = MatchStatus.MATCHED.value
    await session.execute(
        update(CommunicationParticipant)
        .where(
            CommunicationParticipant.company_id == principal.company_id,
            CommunicationParticipant.session_id == comm.id,
            CommunicationParticipant.role == ParticipantRole.CUSTOMER.value,
        )
        .values(contact_id=contact.id)
    )
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=contact.id,
        category=TimelineCategory.MESSAGE,
        event_type=TimelineEventType.CONVERSATION_LINKED,
        summary=f"{CHANNEL_LABEL[Channel(comm.channel)]} conversation linked to this contact",
        actor_user_id=principal.user_id,
        channel=comm.channel,
        session_id=comm.id,
    )
    audit.record(
        session,
        "conversation.linked",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="communication_session",
        entity_id=comm.id,
        ip=ip,
    )
    await session.commit()
    return await get_session(session, principal, session_id)


async def create_contact_for_session(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    *,
    name: str | None,
    ip: str | None,
) -> CommunicationSession:
    """A human creates a contact for an unknown WhatsApp sender (never done automatically) and
    links the conversation to it; later messages from that number then match directly."""
    from app.contacts import service as contacts
    from app.contacts.models import ContactSource
    from app.contacts.schemas import ContactCreate

    comm = await get_session(session, principal, session_id)
    if comm.match_status != MatchStatus.UNMATCHED:
        raise ConflictError(
            "Link this conversation to one of the matching contacts instead.",
            code="conversation_not_unmatched",
        )
    wa_id = normalize_phone("+" + (comm.external_participant_id or ""))
    if Channel(comm.channel) != Channel.WHATSAPP or not wa_id:
        raise AppError("Only a WhatsApp sender can be turned into a contact.").with_status(422)
    display = (name or "").strip() or str(comm.metadata_.get("display_name") or "") or f"+{wa_id}"
    contact = await contacts.create_contact(
        session,
        principal,
        ContactCreate(name=display[:200], phone=f"+{wa_id}", source=ContactSource.OTHER),
        ip=ip,
    )
    return await link_contact(session, principal, session_id, contact.id, ip=ip)


async def open_conversation(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID, channel: Channel
) -> CommunicationSession:
    """Find or start the message conversation with a contact on a channel."""
    contact = await ContactRepository(session).get(principal.company_id, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    if channel == Channel.WHATSAPP:
        record = await integrations.require_capability(
            session, principal.company_id, Provider.WHATSAPP, Capability.MESSAGE
        )
        wa_id = normalize_phone(contact.phone)
        if not wa_id:
            raise AppError(
                "Add the customer's mobile number (with country code) to message them on WhatsApp.",
                code="missing_phone",
            ).with_status(422)
        comm = await get_or_create_session(
            session,
            principal.company_id,
            provider=Provider.WHATSAPP.value,
            channel=Channel.WHATSAPP,
            capability=Capability.MESSAGE,
            external_session_id=wa_id,
            integration_id=record.id,
            participant=ParticipantRef(role="CUSTOMER", external_id=wa_id, phone=wa_id),
            owner_user_id=principal.user_id,
            contact_id=None,
        )
        if comm.match_status != MatchStatus.MATCHED:
            comm.contact_id = contact.id
            comm.match_status = MatchStatus.MATCHED.value
        elif comm.contact_id != contact.id:
            raise ConflictError(
                "This WhatsApp number's conversation belongs to another contact.",
                code="conversation_linked_elsewhere",
            )
        await session.commit()
        return comm
    if channel == Channel.TEAMS:
        await integrations.require_capability(
            session, principal.company_id, Provider.MICROSOFT_TEAMS, Capability.MESSAGE
        )
        linked = await session.scalar(
            select(CommunicationSession)
            .where(
                CommunicationSession.company_id == principal.company_id,
                CommunicationSession.contact_id == contact.id,
                CommunicationSession.channel == Channel.TEAMS.value,
                CommunicationSession.capability == Capability.MESSAGE.value,
            )
            .order_by(CommunicationSession.last_message_at.desc().nulls_last())
            .limit(1)
        )
        if linked is None:
            raise AppError(
                "Link one of your Teams chats to this customer first.",
                code="teams_chat_not_linked",
            ).with_status(409)
        return linked
    raise AppError("Messaging is not available on this channel").with_status(422)


# ---- sending (human-approved only) -----------------------------------------------------------


async def _message_provider(
    session: AsyncSession, principal: Principal, comm: CommunicationSession
) -> tuple[MessageProvider, uuid.UUID]:
    channel = Channel(comm.channel)
    if channel == Channel.WHATSAPP:
        record = await integrations.require_capability(
            session, principal.company_id, Provider.WHATSAPP, Capability.MESSAGE
        )
        return factory.whatsapp_client(record, integrations.secrets_of(record)), record.id
    if channel == Channel.TEAMS:
        record = await integrations.require_capability(
            session, principal.company_id, Provider.MICROSOFT_TEAMS, Capability.MESSAGE
        )
        from app.integrations import teams_service

        return await teams_service.message_provider_for(session, principal, record), record.id
    raise AppError("Messaging is not available on this channel").with_status(422)


async def send_message(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    *,
    text: str,
    client_message_id: uuid.UUID,
    draft_id: uuid.UUID | None,
    ip: str | None,
) -> CommunicationMessage:
    comm = await get_session(session, principal, session_id)
    if comm.capability != Capability.MESSAGE.value:
        raise AppError("This conversation does not support messages").with_status(422)
    if (
        comm.owner_user_id not in (None, principal.user_id)
        and not principal.is_admin
        and Channel(comm.channel) == Channel.TEAMS
    ):
        raise ForbiddenError("Only the salesperson who linked this Teams chat can reply in it")
    existing = await session.scalar(
        select(CommunicationMessage).where(
            CommunicationMessage.company_id == principal.company_id,
            CommunicationMessage.session_id == comm.id,
            CommunicationMessage.client_message_id == client_message_id,
        )
    )
    if existing is not None:
        return existing  # double-click / retry of the same send
    if Channel(comm.channel) == Channel.WHATSAPP:
        last_in = comm.last_inbound_at
        if last_in is None or datetime.now(UTC) - last_in > WHATSAPP_WINDOW:
            raise WhatsAppWindowClosedError()
    provider, integration_id = await _message_provider(session, principal, comm)
    days = await _retention_days(session, principal.company_id)
    now = datetime.now(UTC)
    msg = CommunicationMessage(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        session_id=comm.id,
        direction=Direction.OUTBOUND.value,
        sender_type=SenderType.SALESPERSON.value,
        sender_user_id=principal.user_id,
        message_type=MessageType.TEXT.value,
        content=text,
        client_message_id=client_message_id,
        status=MessageStatus.SENDING.value,
        occurred_at=now,
        expires_at=now + timedelta(days=days),
        metadata_={"draft_id": str(draft_id)} if draft_id else {},
    )
    session.add(msg)
    await session.commit()  # the attempt is visible even if the provider call hangs

    error: ProviderError | None = None
    try:
        sent = await provider.send_text(
            OutboundMessage(
                to=comm.external_session_id or "",
                text=text,
                client_message_id=str(client_message_id),
            )
        )
    except ProviderError as exc:
        error = exc
    if error is not None:
        provider_code = getattr(error, "provider_code", None)
        msg.status = MessageStatus.FAILED.value
        msg.error_code = (f"meta:{provider_code}" if provider_code else error.code)[:64]
        await session.commit()
        if isinstance(error, ProviderAuthError | ProviderPermissionError):
            await integrations.flag_runtime_error(principal.company_id, integration_id, error.code)
        logger.warning(
            "message_send_failed",
            extra={"session_id": str(comm.id), "error": msg.error_code, "channel": comm.channel},
        )
        raise _send_failed(comm, error, msg)
    msg.status = MessageStatus.SENT.value
    msg.external_message_id = sent.external_message_id
    comm.last_message_at = now
    if draft_id is not None:
        draft = await session.scalar(
            select(MessageDraft).where(
                MessageDraft.company_id == principal.company_id,
                MessageDraft.session_id == comm.id,
                MessageDraft.id == draft_id,
            )
        )
        if draft is not None:
            draft.status = MessageDraftStatus.SENT.value
            draft.sent_message_id = msg.id
            draft.reviewed_by_user_id = principal.user_id
            draft.reviewed_at = now
    await _record_event(
        session,
        comm,
        ConversationEventType.MESSAGE_SENT,
        f"msg:{sent.external_message_id}",
        now,
        {"message_id": str(msg.id)},
    )
    if comm.contact_id is not None:
        _timeline_message(session, comm, inbound=False, actor=principal.user_id)
    audit.record(
        session,
        "message.sent",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="communication_message",
        entity_id=msg.id,
        ip=ip,
        details={"channel": comm.channel, "from_ai_draft": draft_id is not None},
    )
    await session.commit()
    return msg


def _send_failed(
    comm: CommunicationSession, error: ProviderError, msg: CommunicationMessage
) -> AppError:
    """A clear reason for the salesperson; the message stays FAILED and can be retried."""
    details = {"message_id": str(msg.id), "error": msg.error_code}
    whatsapp = Channel(comm.channel) == Channel.WHATSAPP
    provider_code = getattr(error, "provider_code", None)
    if isinstance(error, ProviderRateLimitError):
        return MessageSendFailedError(
            "The messaging provider's rate limit was reached. Wait a minute and try again.",
            code="message_rate_limited",
            details={**details, "retry_after": error.retry_after},
        ).with_status(429)
    if isinstance(error, ProviderAuthError):
        return MessageSendFailedError(
            "The provider rejected the stored access token (expired or revoked). An admin must "
            "update it in Settings > Integrations.",
            details=details,
        )
    if whatsapp and provider_code in META_ERRORS:
        code = "whatsapp_window_closed" if provider_code == "131047" else "message_send_failed"
        return MessageSendFailedError(META_ERRORS[provider_code], code=code, details=details)
    return MessageSendFailedError(details=details)


async def update_draft(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    status: MessageDraftStatus | None,
    body: str | None,
) -> MessageDraft:
    comm = await get_session(session, principal, session_id)
    draft = await session.scalar(
        select(MessageDraft).where(
            MessageDraft.company_id == principal.company_id,
            MessageDraft.session_id == comm.id,
            MessageDraft.id == draft_id,
        )
    )
    if draft is None:
        raise NotFoundError("Draft not found")
    if body is not None:
        draft.body = body
    if status is not None:
        if status == MessageDraftStatus.SENT:
            raise AppError("Send a draft with the send endpoint").with_status(422)
        draft.status = status.value
        draft.reviewed_by_user_id = principal.user_id
        draft.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(draft)
    return draft


# ---- calls -----------------------------------------------------------------------------------


def _call_provider_label(provider_name: str | None) -> str:
    return {
        "plivo": Provider.PLIVO.value,
        "teams": Provider.MICROSOFT_TEAMS.value,
        "google-meet": Provider.GOOGLE_MEET.value,
    }.get(provider_name or "", "MOCK")


async def open_call_session(
    session: AsyncSession, call: Call, *, integration_id: uuid.UUID | None
) -> CommunicationSession:
    """Every started call also gets a communication session (common model)."""
    channel = {"TEAMS": Channel.TEAMS, "GOOGLE_MEET": Channel.GOOGLE_MEET}.get(
        call.channel, Channel.PHONE
    )
    capability = Capability.PHONE_CALL if channel == Channel.PHONE else Capability.REAL_TIME_CALL
    comm = await get_or_create_session(
        session,
        call.company_id,
        provider=_call_provider_label(call.provider),
        channel=channel,
        capability=capability,
        external_session_id=f"call:{call.id}",
        integration_id=integration_id,
        owner_user_id=call.user_id,
        contact_id=call.contact_id,
    )
    comm.call_id = call.id
    comm.status = SessionStatus.ACTIVE.value
    if call.user_id:
        await _add_participant(
            session, comm, ParticipantRef(role="SALESPERSON", external_id=f"user:{call.user_id}")
        )
    await _add_participant(
        session, comm, ParticipantRef(role="CUSTOMER", external_id=f"contact:{call.contact_id}")
    )
    await _record_event(
        session,
        comm,
        ConversationEventType.CALL_STARTED,
        f"call-started:{call.id}",
        datetime.now(UTC),
        {"call_id": str(call.id)},
    )
    return comm


async def record_call_event(
    session: AsyncSession,
    company_id: uuid.UUID,
    call_id: uuid.UUID,
    type_: ConversationEventType,
    *,
    status: str | None = None,
    ended: bool = False,
) -> None:
    comm = await session.scalar(
        select(CommunicationSession).where(
            CommunicationSession.company_id == company_id, CommunicationSession.call_id == call_id
        )
    )
    if comm is None:
        return
    now = datetime.now(UTC)
    await _record_event(
        session,
        comm,
        type_,
        f"{type_.value.lower()}:{call_id}:{status or ''}",
        now,
        {"status": status},
    )
    if ended:
        comm.status = SessionStatus.ENDED.value
        comm.ended_at = now


def mock_mode(record_mode: str | None) -> bool:
    return record_mode == IntegrationMode.MOCK.value
