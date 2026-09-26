"""Conversations API: unified inbox, conversation view (messages + AI suggestions + notes),
human-approved sending, linking unmatched conversations, per-contact communication options."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.db import get_db_session
from app.common.errors import ErrorResponse, NotFoundError
from app.common.rate_limit import client_ip
from app.contacts.models import Contact
from app.contacts.repository import ContactRepository
from app.conversations import service
from app.conversations.identity import normalize_phone
from app.conversations.models import (
    CommunicationMessage,
    CommunicationSession,
    MatchStatus,
    MessageDraft,
    MessageDraftStatus,
)
from app.integrations import service as integrations
from app.integrations.domain import SLUG_OF, Capability, Channel, IntegrationMode, Provider
from app.integrations.models import Integration
from app.intel.models import CallNote, NoteStatus
from app.intel.service import note_payload

router = APIRouter(
    tags=["conversations"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


class ContactRef(BaseModel):
    id: uuid.UUID
    name: str
    organization: str | None


class SessionOut(BaseModel):
    id: uuid.UUID
    provider: str
    channel: str
    capability: str
    status: str
    match_status: str
    contact: ContactRef | None
    call_id: uuid.UUID | None
    display_name: str | None
    external_participant_id: str | None
    last_message_at: datetime | None
    last_inbound_at: datetime | None
    mock: bool
    preview: str | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: str
    sender_type: str
    sender_user_id: uuid.UUID | None
    message_type: str
    content: str
    status: str
    error_code: str | None
    occurred_at: datetime


class KnowledgeSourceOut(BaseModel):
    document_id: uuid.UUID
    title: str
    score: float | None = None


class DraftOut(BaseModel):
    id: uuid.UUID
    reply_to_message_id: uuid.UUID | None
    body: str
    source: str
    status: str
    warnings: list[str]
    ai_provider: str | None
    # "Based on company knowledge": the documents the draft relies on (empty = none used).
    knowledge_sources: list[KnowledgeSourceOut] = []
    created_at: datetime


class ConversationOut(BaseModel):
    session: SessionOut
    messages: list[MessageOut]
    drafts: list[DraftOut]
    notes: list[dict[str, Any]]
    can_send: bool
    send_blocked_reason: str | None


async def _session_out(
    db: AsyncSession, comm: CommunicationSession, *, preview: str | None = None
) -> SessionOut:
    contact = None
    if comm.contact_id:
        c = await db.scalar(
            select(Contact).where(
                Contact.company_id == comm.company_id, Contact.id == comm.contact_id
            )
        )
        if c is not None:
            contact = ContactRef(id=c.id, name=c.name, organization=c.organization)
    mock = False
    if comm.integration_id:
        record = await db.scalar(
            select(Integration).where(
                Integration.company_id == comm.company_id,
                Integration.id == comm.integration_id,
            )
        )
        mock = record is not None and record.mode == IntegrationMode.MOCK
    return SessionOut(
        id=comm.id,
        provider=comm.provider,
        channel=comm.channel,
        capability=comm.capability,
        status=comm.status,
        match_status=comm.match_status,
        contact=contact,
        call_id=comm.call_id,
        display_name=(comm.metadata_ or {}).get("display_name"),
        external_participant_id=comm.external_participant_id,
        last_message_at=comm.last_message_at,
        last_inbound_at=comm.last_inbound_at,
        mock=mock or comm.provider == "MOCK",
        preview=preview,
    )


def _message_out(m: CommunicationMessage) -> MessageOut:
    return MessageOut(
        id=m.id,
        direction=m.direction,
        sender_type=m.sender_type,
        sender_user_id=m.sender_user_id,
        message_type=m.message_type,
        content=m.content,
        status=m.status,
        error_code=m.error_code,
        occurred_at=m.occurred_at,
    )


def _draft_out(d: MessageDraft) -> DraftOut:
    return DraftOut(
        id=d.id,
        reply_to_message_id=d.reply_to_message_id,
        body=d.body,
        source=d.source,
        status=d.status,
        warnings=list(d.warnings or []),
        ai_provider=d.ai_provider,
        knowledge_sources=_sources(d.knowledge_sources or []),
        created_at=d.created_at,
    )


def _sources(raw: list[dict[str, str]]) -> list[KnowledgeSourceOut]:
    """One entry per document (best score), in citation order."""
    best: dict[str, KnowledgeSourceOut] = {}
    for item in raw:
        try:
            score = float(item.get("score") or 0)
            doc = KnowledgeSourceOut(
                document_id=uuid.UUID(item["document_id"]), title=item["title"], score=score
            )
        except (KeyError, ValueError):
            continue
        key = str(doc.document_id)
        if key not in best or score > (best[key].score or 0):
            best[key] = doc
    return list(best.values())


@router.get("/conversations", response_model=list[SessionOut])
async def list_conversations(
    contact_id: uuid.UUID | None = None,
    match_status: MatchStatus | None = None,
    channel: Channel | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[SessionOut]:
    rows = await service.list_sessions(
        session,
        principal,
        contact_id=contact_id,
        match_status=match_status,
        channel=channel,
        limit=limit,
    )
    out = []
    for comm in rows:
        last = await service.list_messages(session, principal.company_id, comm.id, limit=1)
        out.append(
            await _session_out(session, comm, preview=last[-1].content[:140] if last else None)
        )
    return out


async def _send_state(
    db: AsyncSession, principal: Principal, comm: CommunicationSession
) -> tuple[bool, str | None]:
    channel = Channel(comm.channel)
    provider = {Channel.WHATSAPP: Provider.WHATSAPP, Channel.TEAMS: Provider.MICROSOFT_TEAMS}.get(
        channel
    )
    if provider is None or comm.capability != Capability.MESSAGE.value:
        return False, "Messages cannot be sent on this channel."
    if (
        await integrations.capability_record(db, principal.company_id, provider, Capability.MESSAGE)
        is None
    ):
        return False, "Messaging on this channel is not enabled (Settings > Integrations)."
    if channel == Channel.WHATSAPP:
        from datetime import UTC
        from datetime import datetime as dt

        if (
            comm.last_inbound_at is None
            or dt.now(UTC) - comm.last_inbound_at > service.WHATSAPP_WINDOW
        ):
            return False, service.WhatsAppWindowClosedError.message
    return True, None


@router.get("/conversations/{session_id}", response_model=ConversationOut)
async def get_conversation(
    session_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationOut:
    comm = await service.get_session(session, principal, session_id)
    messages = await service.list_messages(session, principal.company_id, comm.id)
    drafts = await service.list_drafts(session, principal.company_id, comm.id)
    notes = await session.scalars(
        select(CallNote)
        .where(
            CallNote.company_id == principal.company_id,
            CallNote.session_id == comm.id,
            CallNote.status != NoteStatus.REJECTED.value,
        )
        .order_by(CallNote.created_at)
    )
    can_send, reason = await _send_state(session, principal, comm)
    return ConversationOut(
        session=await _session_out(session, comm),
        messages=[_message_out(m) for m in messages],
        drafts=[_draft_out(d) for d in drafts],
        notes=[note_payload(n) for n in notes],
        can_send=can_send,
        send_blocked_reason=reason,
    )


MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: MessageText
    client_message_id: uuid.UUID
    draft_id: uuid.UUID | None = None


@router.post(
    "/conversations/{session_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def send_message(
    session_id: uuid.UUID,
    body: SendIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> MessageOut:
    """Send a message the salesperson reviewed. This is the only way anything is sent."""
    msg = await service.send_message(
        session,
        principal,
        session_id,
        text=body.text,
        client_message_id=body.client_message_id,
        draft_id=body.draft_id,
        ip=client_ip(request),
    )
    return _message_out(msg)


class DraftUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: MessageDraftStatus | None = None
    body: MessageText | None = None


@router.patch("/conversations/{session_id}/drafts/{draft_id}", response_model=DraftOut)
async def update_draft(
    session_id: uuid.UUID,
    draft_id: uuid.UUID,
    body: DraftUpdateIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DraftOut:
    draft = await service.update_draft(
        session, principal, session_id, draft_id, status=body.status, body=body.body
    )
    return _draft_out(draft)


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID


@router.post(
    "/conversations/{session_id}/link",
    response_model=SessionOut,
    responses={409: {"model": ErrorResponse}},
)
async def link_conversation(
    session_id: uuid.UUID,
    body: LinkIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> SessionOut:
    comm = await service.link_contact(
        session, principal, session_id, body.contact_id, ip=client_ip(request)
    )
    return await _session_out(session, comm)


class CreateContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(max_length=200)] | None = None


@router.post(
    "/conversations/{session_id}/contact",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_contact_from_conversation(
    session_id: uuid.UUID,
    body: CreateContactIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> SessionOut:
    """Create a contact for an unknown sender (explicit user action) and link it."""
    comm = await service.create_contact_for_session(
        session, principal, session_id, name=body.name, ip=client_ip(request)
    )
    return await _session_out(session, comm)


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Channel


@router.post(
    "/contacts/{contact_id}/conversations",
    response_model=SessionOut,
    responses={409: {"model": ErrorResponse}},
)
async def open_conversation(
    contact_id: uuid.UUID,
    body: OpenIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> SessionOut:
    comm = await service.open_conversation(session, principal, contact_id, body.channel)
    return await _session_out(session, comm)


class ChannelAction(BaseModel):
    key: str
    channel: str
    capability: str
    label: str
    available: bool
    reason: str | None
    mock: bool
    provider: str | None


@router.get("/contacts/{contact_id}/communication", response_model=list[ChannelAction])
async def communication_options(
    contact_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[ChannelAction]:
    """Which communication actions are available for this contact right now. The frontend
    shows only ``available`` actions; the backend is authoritative."""
    contact = await ContactRepository(session).get(principal.company_id, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    has_phone = bool(normalize_phone(contact.phone))
    actions: list[ChannelAction] = []

    async def enabled(provider: Provider, cap: Capability) -> tuple[bool, bool]:
        record = await integrations.capability_record(session, principal.company_id, provider, cap)
        return record is not None, bool(record and record.mode == IntegrationMode.MOCK)

    wa, wa_mock = await enabled(Provider.WHATSAPP, Capability.MESSAGE)
    actions.append(
        ChannelAction(
            key="whatsapp_message",
            channel="WHATSAPP",
            capability="MESSAGE",
            label="WhatsApp Message",
            available=wa and has_phone,
            reason=None
            if wa and has_phone
            else ("Add a mobile number" if wa else "WhatsApp messaging is not enabled"),
            mock=wa_mock,
            provider=SLUG_OF[Provider.WHATSAPP],
        )
    )
    tm, tm_mock = await enabled(Provider.MICROSOFT_TEAMS, Capability.MESSAGE)
    user_ok = tm_mock or await integrations.user_connected(
        session, principal, Provider.MICROSOFT_TEAMS
    )
    actions.append(
        ChannelAction(
            key="teams_message",
            channel="TEAMS",
            capability="MESSAGE",
            label="Teams Message",
            available=tm and user_ok,
            reason=None
            if tm and user_ok
            else ("Connect your Teams account" if tm else "Teams messaging is not enabled"),
            mock=tm_mock,
            provider=SLUG_OF[Provider.MICROSOFT_TEAMS],
        )
    )
    tc, tc_mock = await enabled(Provider.MICROSOFT_TEAMS, Capability.REAL_TIME_CALL)
    actions.append(
        ChannelAction(
            key="teams_call",
            channel="TEAMS",
            capability="REAL_TIME_CALL",
            label="Teams Call",
            available=tc,
            reason=None if tc else "Teams call copilot is not enabled",
            mock=tc_mock,
            provider=SLUG_OF[Provider.MICROSOFT_TEAMS],
        )
    )
    gm, gm_mock = await enabled(Provider.GOOGLE_MEET, Capability.REAL_TIME_CALL)
    gm_user = gm_mock or await integrations.user_connected(session, principal, Provider.GOOGLE_MEET)
    actions.append(
        ChannelAction(
            key="google_meet_call",
            channel="GOOGLE_MEET",
            capability="REAL_TIME_CALL",
            label="Google Meet",
            available=gm and gm_user,
            reason=None
            if gm and gm_user
            else (
                "Connect your Google account for Meet"
                if gm
                else "Google Meet copilot is not enabled"
            ),
            mock=gm_mock,
            provider=SLUG_OF[Provider.GOOGLE_MEET],
        )
    )
    pc, pc_mock = await enabled(Provider.PLIVO, Capability.PHONE_CALL)
    from app.common.config import get_settings

    fallback_mock = not pc and get_settings().telephony_provider == "mock"
    phone_ok = (pc or fallback_mock) and has_phone
    actions.append(
        ChannelAction(
            key="phone_call",
            channel="PHONE",
            capability="PHONE_CALL",
            label="Phone Call",
            available=phone_ok,
            reason=None
            if phone_ok
            else ("Add a phone number" if pc or fallback_mock else "Phone calling is not enabled"),
            mock=pc_mock or fallback_mock,
            provider=SLUG_OF[Provider.PLIVO] if pc else None,
        )
    )
    return actions
