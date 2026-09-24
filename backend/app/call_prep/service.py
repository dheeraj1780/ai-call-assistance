"""Call preparation context: everything the salesperson (and the AI) should know before a call.

Only data already stored for this tenant is returned; nothing is inferred here.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items.models import ActionItemStatus
from app.action_items.repository import ActionItemRepository
from app.action_items.schemas import ActionItemOut
from app.agendas.schemas import AgendaItemOut
from app.agendas.service import ContextRecord, list_items
from app.auth.dependencies import Principal
from app.call_prep.schemas import CallPrepOut, KnownItem, PreviousCall
from app.calls.models import Call, CallStatus
from app.calls.repository import CallRepository
from app.calls.schemas import CallOut
from app.calls.service import get_call
from app.common.errors import NotFoundError
from app.contacts.repository import ContactNoteRepository, ContactRepository
from app.contacts.schemas import ContactOut, NoteOut
from app.intel.models import CallNote, NoteKind, NoteStatus
from app.postcall.models import CallSummary
from app.timeline import service as timeline
from app.timeline.schemas import TimelineEventOut

TERMINAL = {CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.CANCELLED, CallStatus.FAILED}


async def _previous_calls(session: AsyncSession, principal: Principal, call: Call) -> list[Call]:
    rows = await session.scalars(
        CallRepository(session)
        .search(principal.company_id, contact_id=call.contact_id, status=None, user_id=None)
        .where(Call.id != call.id, Call.status.in_([s.value for s in TERMINAL]))
        .limit(5)
    )
    return list(rows.unique().all())


async def _known_items(
    session: AsyncSession, principal: Principal, call: Call
) -> tuple[list[KnownItem], list[KnownItem]]:
    """Requirements/objections captured on earlier calls with this contact (human-reviewed
    notes first; unreviewed suggestions are labelled as such)."""
    rows = await session.scalars(
        select(CallNote)
        .where(
            CallNote.company_id == principal.company_id,
            CallNote.contact_id == call.contact_id,
            CallNote.call_id != call.id,
            CallNote.kind.in_([NoteKind.REQUIREMENT.value, NoteKind.OBJECTION.value]),
            CallNote.status != NoteStatus.REJECTED.value,
        )
        .order_by(CallNote.created_at.desc())
        .limit(40)
    )
    reqs: list[KnownItem] = []
    objections: list[KnownItem] = []
    for n in rows:
        item = KnownItem(kind=n.kind, text=n.text, status=n.status, call_id=n.call_id)
        (reqs if n.kind == NoteKind.REQUIREMENT else objections).append(item)
    return reqs[:10], objections[:10]


async def _summaries(
    session: AsyncSession, principal: Principal, calls: list[Call]
) -> dict[str, str]:
    """Post-call summaries of earlier calls."""
    if not calls:
        return {}
    rows = await session.execute(
        select(CallSummary.call_id, CallSummary.summary).where(
            CallSummary.company_id == principal.company_id,
            CallSummary.call_id.in_([c.id for c in calls]),
            CallSummary.summary.is_not(None),
        )
    )
    return {str(call_id): summary for call_id, summary in rows}


async def gather_records(
    session: AsyncSession, principal: Principal, call: Call
) -> list[ContextRecord]:
    """Stored facts about the customer, each with a stable reference id (for grounding)."""
    contact = await ContactRepository(session).get(principal.company_id, call.contact_id)
    records: list[ContextRecord] = []
    if contact is not None:
        profile = ", ".join(
            f"{k}: {v}"
            for k, v in (
                ("name", contact.name),
                ("company", contact.organization),
                ("designation", contact.designation),
                ("status", contact.status),
                ("tags", ", ".join(contact.tags)),
                ("about", contact.notes),
            )
            if v
        )
        records.append(ContextRecord(ref="contact", kind="contact", text=profile))
    notes = await session.scalars(
        ContactNoteRepository(session).for_contact(principal.company_id, call.contact_id).limit(5)
    )
    records += [ContextRecord(ref=f"note:{n.id}", kind="note", text=n.body[:1000]) for n in notes]
    previous = await _previous_calls(session, principal, call)
    summaries = await _summaries(session, principal, previous)
    for c in previous:
        text = "; ".join(
            p
            for p in (
                f"objective: {c.objective}" if c.objective else "",
                f"outcome: {c.outcome}" if c.outcome else "",
                f"next step: {c.next_step}" if c.next_step else "",
                f"summary: {summaries[str(c.id)]}" if str(c.id) in summaries else "",
            )
            if p
        )
        if text:
            records.append(ContextRecord(ref=f"call:{c.id}", kind="previous_call", text=text))
    reqs, objections = await _known_items(session, principal, call)
    records += [
        ContextRecord(ref=f"intel:{i}", kind=item.kind.lower(), text=item.text)
        for i, item in enumerate(reqs + objections)
    ]
    return records


async def build_prep(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID
) -> CallPrepOut:
    call = await get_call(session, principal, call_id)
    contact = await ContactRepository(session).get(principal.company_id, call.contact_id)
    if contact is None:  # cannot happen: composite FK guarantees the contact exists
        raise NotFoundError("Contact not found")
    agenda = await list_items(session, principal.company_id, call.id)
    previous = await _previous_calls(session, principal, call)
    summaries = await _summaries(session, principal, previous)
    notes = await session.scalars(
        ContactNoteRepository(session).for_contact(principal.company_id, call.contact_id).limit(5)
    )
    items_repo = ActionItemRepository(session)
    open_items = await session.scalars(
        items_repo.search(
            principal.company_id,
            statuses=[ActionItemStatus.OPEN, ActionItemStatus.IN_PROGRESS],
            kind=None,
            assignee_user_id=None,
            contact_id=call.contact_id,
            call_id=None,
            confirmed=True,
            due_before=None,
        ).limit(20)
    )
    reqs, objections = await _known_items(session, principal, call)
    events, _ = await timeline.list_events(
        session,
        company_id=principal.company_id,
        contact_id=call.contact_id,
        categories=None,
        limit=10,
        before=None,
    )
    return CallPrepOut(
        call=CallOut.model_validate(call),
        contact=ContactOut.model_validate(contact),
        agenda=[AgendaItemOut.model_validate(a) for a in agenda],
        previous_calls=[
            PreviousCall(
                id=c.id,
                status=c.status,
                started_at=c.started_at,
                ended_at=c.ended_at,
                objective=c.objective,
                outcome=c.outcome,
                next_step=c.next_step,
                summary=summaries.get(str(c.id)),
            )
            for c in previous
        ],
        recent_notes=[NoteOut.model_validate(n) for n in notes],
        open_action_items=[ActionItemOut.model_validate(i) for i in open_items.unique()],
        known_requirements=reqs,
        known_objections=objections,
        recent_timeline=[TimelineEventOut.model_validate(e) for e in events],
    )
