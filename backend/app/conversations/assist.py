"""AI assistance for incoming customer messages - the SAME intelligence as calls, with the
channel as metadata (no per-provider copilot).

For each new customer message (background job ``conversation.assist``):
1. Deterministic detectors (app/copilot/detectors.py) -> suggested structured notes.
2. Tenant-scoped knowledge retrieval (app/knowledge) for questions.
3. One AI pass (AITask.MESSAGE_ASSIST via the AI gateway: budget, timeout, usage records)
   -> a reply suggestion, extra notes, suggested action items.
4. Persisted as SUGGESTIONS only: a draft reply (never sent automatically), SUGGESTED notes,
   unconfirmed AI action items. Figures in the draft that do not appear in the conversation or
   the retrieved knowledge are flagged for the human reviewer.

If the AI is unavailable, the message is already stored; deterministic notes still apply and
the job retries (bounded). Nothing is ever sent to the customer from here.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items.models import ActionItem, ActionItemKind, ActionItemSource, ActionItemStatus
from app.ai.gateway import get_ai_gateway
from app.ai.mock_provider import register_mock
from app.ai.provider import AITask
from app.ai.safety import UNTRUSTED_DATA_RULES, data_block
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.contacts.models import Contact
from app.conversations.models import (
    CommunicationMessage,
    CommunicationSession,
    DraftSource,
    MessageDraft,
    MessageDraftStatus,
    SenderType,
)
from app.copilot.detectors import detect, is_question
from app.intel.models import IntelSource, NoteKind, ObjectionCategory
from app.intel.service import short_hash, suggest_note
from app.jobs.service import register_job
from app.postcall.service import figure_warnings
from app.tenants.models import Company

logger = logging.getLogger(__name__)

HISTORY = 15
NOTE_MIN_CONFIDENCE = 0.6
MAX_AI_ACTIONS = 2


class AssistNote(BaseModel):
    kind: NoteKind
    text: Annotated[str, Field(max_length=300)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    category: ObjectionCategory | None = None


class AssistAction(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Literal["TASK", "FOLLOW_UP", "APPOINTMENT"] = "FOLLOW_UP"
    due_in_days: Annotated[int, Field(ge=0, le=60)] | None = None


class MessageAssist(BaseModel):
    reply: Annotated[str, Field(max_length=1000)]
    notes: Annotated[list[AssistNote], Field(max_length=8)] = []
    actions: Annotated[list[AssistAction], Field(max_length=3)] = []


class MessageAssistContext(BaseModel):
    channel: str
    customer_name: str | None
    company_products: str | None
    history: list[dict[str, str]]
    latest: str
    knowledge: list[dict[str, str]]


ASSIST_SYSTEM = (
    "You help a salesperson reply to a customer message (channel given in the data). Draft ONE "
    "short, polite reply the salesperson can edit before sending; answer only with facts from "
    "the conversation and the company knowledge excerpts; if information is missing, say the "
    "salesperson will confirm. Never promise prices, discounts, delivery dates or features "
    "that are not in the data. Also extract structured notes (requirement, budget, timeline, "
    "objection with category, next step...) from the CUSTOMER's messages and suggest at most "
    "two concrete follow-up actions. " + UNTRUSTED_DATA_RULES
)


@register_mock(AITask.MESSAGE_ASSIST)
def _mock_assist(context: BaseModel | None) -> MessageAssist:
    """Deterministic stand-in (MOCKED): acknowledges the request using only given facts."""
    if not isinstance(context, MessageAssistContext):
        return MessageAssist(reply="Thank you for your message. I will get back to you shortly.")
    latest = context.latest.lower()
    name = f" {context.customer_name.split()[0]}" if context.customer_name else ""
    if "quotation" in latest or "quote" in latest:
        reply = f"Hi{name}, thank you. I will prepare the quotation and share it with you shortly."
        actions = [AssistAction(title="Send quotation", kind="FOLLOW_UP", due_in_days=1)]
    elif context.knowledge:
        snippet = " ".join(context.knowledge[0]["content"].split())[:300]
        reply = f"Hi{name}, thanks for asking. {snippet}"
        actions = []
    elif is_question(context.latest):
        reply = f"Hi{name}, thanks for your question. Let me check and confirm the details shortly."
        actions = []
    else:
        reply = f"Hi{name}, thank you for your message. I will get back to you shortly."
        actions = []
    notes = [
        AssistNote(kind=d.kind, text=d.text[:300], confidence=0.65, category=d.category)
        for d in detect(context.latest, speaker_is_customer=True)
    ]
    return MessageAssist(reply=reply, notes=notes[:8], actions=actions)


async def _context(
    session: AsyncSession, comm: CommunicationSession, message: CommunicationMessage
) -> tuple[MessageAssistContext, Contact | None, str]:
    company = await session.scalar(select(Company).where(Company.id == comm.company_id))
    contact = (
        await session.scalar(
            select(Contact).where(
                Contact.company_id == comm.company_id, Contact.id == comm.contact_id
            )
        )
        if comm.contact_id
        else None
    )
    rows = await session.scalars(
        select(CommunicationMessage)
        .where(
            CommunicationMessage.company_id == comm.company_id,
            CommunicationMessage.session_id == comm.id,
            CommunicationMessage.occurred_at <= message.occurred_at,
        )
        .order_by(CommunicationMessage.occurred_at.desc())
        .limit(HISTORY)
    )
    history = [
        {
            "from": "customer" if m.sender_type == SenderType.CUSTOMER else "salesperson",
            "text": m.content,
        }
        for m in reversed(rows.all())
    ]
    knowledge: list[dict[str, str]] = []
    if is_question(message.content) or len(message.content.split()) >= 4:
        try:
            from app.knowledge.service import retrieve

            threshold = get_settings().knowledge_score_threshold
            hits = await retrieve(session, comm.company_id, message.content, k=2)
            knowledge = [
                {"title": h.title, "content": h.content[:1200]}
                for h in hits
                if h.score >= threshold
            ]
        except Exception:
            logger.warning("assist_knowledge_failed")
    ctx = MessageAssistContext(
        channel=comm.channel,
        customer_name=contact.name if contact else None,
        company_products=company.products_services if company else None,
        history=history,
        latest=message.content,
        knowledge=knowledge,
    )
    source_text = "\n".join(
        [h["text"] for h in history]
        + [k["content"] for k in knowledge]
        + [ctx.company_products or ""]
    )
    return ctx, contact, source_text


def _prompt(ctx: MessageAssistContext, company_instructions: str | None) -> str:
    return "\n\n".join(
        [
            data_block("channel", ctx.channel),
            data_block("company_products", ctx.company_products or "(none)"),
            data_block("company_instructions", company_instructions or "(none)"),
            data_block("customer_name", ctx.customer_name or "(unknown)"),
            data_block(
                "conversation",
                "\n".join(f"{h['from']}: {h['text']}" for h in ctx.history) or "(none)",
            ),
            data_block("latest_customer_message", ctx.latest),
            data_block(
                "company_knowledge",
                "\n\n".join(f"[{k['title']}]\n{k['content']}" for k in ctx.knowledge) or "(none)",
            ),
        ]
    )


async def assist_message(
    company_id: uuid.UUID, session_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        comm = await session.scalar(
            select(CommunicationSession).where(
                CommunicationSession.company_id == company_id, CommunicationSession.id == session_id
            )
        )
        message = await session.scalar(
            select(CommunicationMessage).where(
                CommunicationMessage.company_id == company_id, CommunicationMessage.id == message_id
            )
        )
        if comm is None or message is None:
            return  # deleted or expired meanwhile
        ctx, contact, source_text = await _context(session, comm, message)

        # 1. Deterministic notes first: they survive an AI outage.
        if contact is not None:
            for d in detect(message.content, speaker_is_customer=True):
                await suggest_note(
                    session,
                    company_id=company_id,
                    call_id=None,
                    session_id=comm.id,
                    contact_id=contact.id,
                    kind=d.kind,
                    text=d.text,
                    source=IntelSource.DETERMINISTIC,
                    dedupe_key=f"{d.kind}:{d.key or short_hash(d.text)}",
                    confidence=d.confidence,
                    category=d.category,
                )

        company = await session.scalar(select(Company).where(Company.id == company_id))
        result = await get_ai_gateway().run(
            company_id=company_id,
            task=AITask.MESSAGE_ASSIST,
            system=ASSIST_SYSTEM,
            prompt=_prompt(ctx, company.ai_instructions if company else None),
            schema=MessageAssist,
            context=ctx,
            max_tokens=1200,
        )
        out = result.output
        days = company.transcript_retention_days if company else 30
        reply = out.reply.strip()
        if reply:
            await session.execute(
                insert(MessageDraft)
                .values(
                    id=uuid.uuid4(),
                    company_id=company_id,
                    session_id=comm.id,
                    reply_to_message_id=message.id,
                    body=reply,
                    source=DraftSource.AI.value,
                    status=MessageDraftStatus.SUGGESTED.value,
                    warnings=figure_warnings(reply, source_text),
                    ai_provider=result.usage.provider,
                    expires_at=datetime.now(UTC) + timedelta(days=days),
                )
                .on_conflict_do_nothing(index_elements=["session_id", "reply_to_message_id"])
            )
        if contact is not None:
            for n in out.notes:
                if n.confidence < NOTE_MIN_CONFIDENCE:
                    continue
                await suggest_note(
                    session,
                    company_id=company_id,
                    call_id=None,
                    session_id=comm.id,
                    contact_id=contact.id,
                    kind=n.kind,
                    text=n.text,
                    source=IntelSource.AI,
                    dedupe_key=f"{n.kind}:{short_hash(n.text)}",
                    confidence=n.confidence,
                    category=n.category,
                )
            await _suggest_actions(session, company_id, contact.id, comm.owner_user_id, out.actions)
        await session.commit()


async def _suggest_actions(
    session: AsyncSession,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    actions: list[AssistAction],
) -> None:
    open_titles = set(
        (
            await session.scalars(
                select(ActionItem.title).where(
                    ActionItem.company_id == company_id,
                    ActionItem.contact_id == contact_id,
                    ActionItem.source == ActionItemSource.AI.value,
                    ActionItem.status == ActionItemStatus.OPEN.value,
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for action in actions[:MAX_AI_ACTIONS]:
        if action.title in open_titles:
            continue  # idempotent across job retries / repeated requests
        session.add(
            ActionItem(
                company_id=company_id,
                contact_id=contact_id,
                kind=ActionItemKind(action.kind).value,
                title=action.title,
                assignee_user_id=owner_user_id,
                due_at=now + timedelta(days=action.due_in_days)
                if action.due_in_days is not None
                else None,
                status=ActionItemStatus.OPEN.value,
                source=ActionItemSource.AI.value,
            )
        )
        open_titles.add(action.title)


@register_job("conversation.assist")
async def _assist_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    if company_id is None:
        return
    await assist_message(
        company_id, uuid.UUID(str(payload["session_id"])), uuid.UUID(str(payload["message_id"]))
    )
