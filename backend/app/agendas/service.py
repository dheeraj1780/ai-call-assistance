"""Agenda use cases: manual agenda, AI suggestions (grounded), manual status override."""

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agendas.models import AgendaItem, AgendaItemStatus, StatusSource
from app.agendas.schemas import (
    AgendaItemIn,
    AgendaSuggestion,
    AgendaSuggestionOut,
    CustomerFact,
    SuggestedAgendaItem,
)
from app.ai.gateway import get_ai_gateway
from app.ai.mock_provider import register_mock
from app.ai.provider import AITask
from app.ai.safety import UNTRUSTED_DATA_RULES, data_block
from app.auth.dependencies import Principal
from app.calls.models import Call, CallStatus
from app.calls.service import get_call
from app.common.errors import ForbiddenError, InvalidStateError, NotFoundError
from app.tenants.repository import CompanyRepository

_STOPWORDS = frozenset(
    re.findall(
        r"[a-z]+",
        """a an and are as at be by current currently do does for from how in is it its of on or
    our the their them they this to we what when where which who why will with you your ask
    discuss check understand about any have has had set aside get got would could should can
        like make much more some there here that these those been being into also just very
        really please tell let know""",
    )
)


def derive_keywords(title: str, question: str | None) -> str:
    words = re.findall(r"[a-z0-9]+", f"{title} {question or ''}".lower())
    unique = dict.fromkeys(w for w in words if w not in _STOPWORDS and len(w) > 2)
    return " ".join(list(unique)[:20])


def _require_call_editor(principal: Principal, call: Call) -> None:
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can change this call's agenda")


async def list_items(
    session: AsyncSession, company_id: uuid.UUID, call_id: uuid.UUID
) -> list[AgendaItem]:
    rows = await session.scalars(
        select(AgendaItem)
        .where(AgendaItem.company_id == company_id, AgendaItem.call_id == call_id)
        .order_by(AgendaItem.position)
    )
    return list(rows.all())


async def replace_agenda(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, items: list[AgendaItemIn]
) -> list[AgendaItem]:
    call = await get_call(session, principal, call_id)
    _require_call_editor(principal, call)
    if call.status != CallStatus.PLANNED:
        raise InvalidStateError("The agenda can only be replaced before the call starts")
    await session.execute(
        delete(AgendaItem).where(
            AgendaItem.company_id == principal.company_id, AgendaItem.call_id == call.id
        )
    )
    for position, item in enumerate(items):
        session.add(
            AgendaItem(
                company_id=principal.company_id,
                call_id=call.id,
                position=position,
                title=item.title,
                question=item.question,
                description=item.description,
                keywords=derive_keywords(item.title, item.question),
                source=item.source.value,
                status=AgendaItemStatus.NOT_STARTED.value,
                status_source=StatusSource.DEFAULT.value,
            )
        )
    await session.commit()
    return await list_items(session, principal.company_id, call.id)


async def override_status(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    item_id: uuid.UUID,
    status: AgendaItemStatus,
) -> AgendaItem:
    call = await get_call(session, principal, call_id)
    _require_call_editor(principal, call)
    item = await session.scalar(
        select(AgendaItem).where(
            AgendaItem.company_id == principal.company_id,
            AgendaItem.call_id == call.id,
            AgendaItem.id == item_id,
        )
    )
    if item is None:
        raise NotFoundError("Agenda item not found")
    apply_status(item, status, StatusSource.MANUAL, confidence=None, reason="Set by salesperson")
    await session.commit()
    await session.refresh(item)
    return item


def apply_status(
    item: AgendaItem,
    status: AgendaItemStatus,
    source: StatusSource,
    *,
    confidence: float | None,
    reason: str | None,
) -> bool:
    """Apply a status change respecting precedence. Returns True if anything changed.

    A MANUAL status is never overwritten by automatic inference, and automatic inference
    never moves an item backwards (e.g. COMPLETED -> IN_PROGRESS).
    """
    if source != StatusSource.MANUAL:
        if item.status_source == StatusSource.MANUAL:
            return False
        order = [
            AgendaItemStatus.NOT_STARTED,
            AgendaItemStatus.IN_PROGRESS,
            AgendaItemStatus.COMPLETED,
        ]
        if item.status == AgendaItemStatus.SKIPPED or status not in order:
            return False
        if order.index(status) <= order.index(AgendaItemStatus(item.status)):
            return False
    if item.status == status and item.status_source == source:
        return False
    item.status = status.value
    item.status_source = source.value
    item.status_confidence = confidence
    item.status_reason = reason[:300] if reason else None
    item.completed_at = datetime.now(UTC) if status == AgendaItemStatus.COMPLETED else None
    return True


# ---- AI agenda suggestion ----------------------------------------------------------------


class ContextRecord(BaseModel):
    ref: str
    kind: str
    text: str


class AgendaContext(BaseModel):
    objective: str | None
    desired_outcome: str | None
    company_offering: str | None
    records: list[ContextRecord]


AGENDA_SYSTEM = (
    "You help an Indian MSME salesperson prepare for a phone call. Produce a short, practical "
    "call agenda (4-8 items) that serves the call objective, each with one natural question "
    "to ask. Separately list customer facts that appear in the provided records, each citing "
    "the exact ref of the record it came from. Do not invent customer history: if the "
    "records say nothing about something, do not state it as a fact - put it in "
    "open_questions instead. " + UNTRUSTED_DATA_RULES
)


def build_prompt(ctx: AgendaContext) -> str:
    parts = [
        data_block("call_objective", ctx.objective or "(not provided)"),
        data_block("desired_outcome", ctx.desired_outcome or "(not provided)"),
        data_block("our_company_offering", ctx.company_offering or "(not provided)"),
        data_block(
            "customer_records",
            "\n".join(f"[ref={r.ref}] ({r.kind}) {r.text}" for r in ctx.records) or "(none)",
        ),
    ]
    return "\n\n".join(parts) + "\n\nReturn the agenda suggestion."


DEFAULT_DISCOVERY = [
    ("Current process", "How do you handle this today?"),
    ("Pain points", "What is the biggest problem with the current way of working?"),
    ("Scale", "How many locations, users or orders are involved?"),
    ("Budget", "Have you set aside a budget for solving this?"),
    ("Timeline", "By when would you like a solution in place?"),
    ("Decision maker", "Who else is involved in making this decision?"),
    ("Next step", "What would be a good next step from your side?"),
]


@register_mock(AITask.AGENDA)
def _mock_agenda(context: BaseModel | None) -> AgendaSuggestion:
    """Deterministic agenda from the objective + standard discovery; facts only from records."""
    ctx = (
        context
        if isinstance(context, AgendaContext)
        else AgendaContext(objective=None, desired_outcome=None, company_offering=None, records=[])
    )
    agenda: list[SuggestedAgendaItem] = []
    if ctx.objective:
        agenda.append(
            SuggestedAgendaItem(
                title="Confirm the purpose of the call",
                question=f"I wanted to understand: {ctx.objective[:200]}. Is that right?",
                why="Sets context around the call objective.",
            )
        )
    agenda += [SuggestedAgendaItem(title=t, question=q) for t, q in DEFAULT_DISCOVERY]
    facts = [
        CustomerFact(text=r.text[:300], source_ref=r.ref)
        for r in ctx.records
        if r.kind in {"contact", "note", "previous_call"}
    ][:8]
    return AgendaSuggestion(
        customer_facts=facts,
        agenda=agenda[:8],
        open_questions=[] if ctx.records else ["No earlier interactions are recorded."],
    )


async def build_context(session: AsyncSession, principal: Principal, call: Call) -> AgendaContext:
    from app.call_prep.service import gather_records  # local import avoids a cycle

    records = await gather_records(session, principal, call)
    company = await CompanyRepository(session).get(principal.company_id)
    offering = None
    if company is not None:
        offering = (
            " ".join(
                p
                for p in (company.description, company.products_services, company.target_customer)
                if p
            )
            or None
        )
    return AgendaContext(
        objective=call.objective,
        desired_outcome=call.desired_outcome,
        company_offering=offering,
        records=records,
    )


async def suggest_agenda(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID
) -> AgendaSuggestionOut:
    call = await get_call(session, principal, call_id)
    ctx = await build_context(session, principal, call)
    gateway = get_ai_gateway()
    result = await gateway.run(
        company_id=principal.company_id,
        call_id=call.id,
        task=AITask.AGENDA,
        system=AGENDA_SYSTEM,
        prompt=build_prompt(ctx),
        schema=AgendaSuggestion,
        context=ctx,
        max_tokens=3000,
    )
    valid_refs = {r.ref for r in ctx.records}
    grounded = [f for f in result.output.customer_facts if f.source_ref in valid_refs]
    return AgendaSuggestionOut(
        customer_facts=grounded,
        ai_suggested_agenda=result.output.agenda,
        ai_open_questions=result.output.open_questions,
        provider=result.usage.provider,
        discarded_ungrounded_facts=len(result.output.customer_facts) - len(grounded),
    )
