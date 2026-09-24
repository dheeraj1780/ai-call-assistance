"""Post-call processing (background job) and follow-up draft review.

Grounding rules enforced in code (not just in the prompt):
- A scalar field may be CONFIRMED only if its ``evidence`` is a verbatim quote found in the
  transcript; otherwise it is downgraded to INFERRED. Empty values are NOT_DISCUSSED.
- AI action items are created unconfirmed (source AI) - a human must confirm them.
- Follow-up drafts are only drafts. Any figure (amount, percentage, number) that does not
  appear in the call transcript/notes is flagged in ``warnings`` for the human reviewer.
- Nothing is ever sent to a customer by the system.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items.models import ActionItem, ActionItemKind, ActionItemSource, ActionItemStatus
from app.agendas.models import AgendaItem
from app.ai.gateway import get_ai_gateway
from app.ai.mock_provider import register_mock
from app.ai.provider import AIError, AIInvalidOutputError, AITask
from app.ai.safety import UNTRUSTED_DATA_RULES, data_block
from app.auth.dependencies import Principal
from app.calls.models import TERMINAL_CALL_STATUSES, Call, CallOutcome, CallStatus
from app.calls.service import get_call
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import ForbiddenError, InvalidStateError, NotFoundError
from app.contacts.models import Contact
from app.intel.models import CallNote, IntelSource, NoteKind, NoteStatus, ObjectionCategory
from app.intel.service import short_hash, suggest_note
from app.jobs.service import enqueue, register_job
from app.live.models import TranscriptSegment
from app.postcall.models import (
    SCALAR_FIELDS,
    CallSummary,
    DraftChannel,
    DraftStatus,
    FieldStatus,
    FollowUpDraft,
    SummaryStatus,
)
from app.tenants.models import Company
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 40_000
NO_TRANSCRIPT = "No transcript is available for this call."

# ---- AI schema ---------------------------------------------------------------------------------


class GroundedField(BaseModel):
    value: Annotated[str, Field(max_length=500)] | None = None
    status: FieldStatus = FieldStatus.NOT_DISCUSSED
    evidence: Annotated[str, Field(max_length=500)] | None = None


class GroundedItem(BaseModel):
    text: Annotated[str, Field(max_length=300)]
    status: Literal["CONFIRMED", "INFERRED"] = "INFERRED"
    evidence: Annotated[str, Field(max_length=500)] | None = None
    category: ObjectionCategory | None = None


class SuggestedAction(BaseModel):
    title: Annotated[str, Field(max_length=300)]
    kind: Literal["TASK", "FOLLOW_UP", "APPOINTMENT"] = "TASK"
    due_in_days: Annotated[int, Field(ge=0, le=90)] | None = None


class FollowUpText(BaseModel):
    email_subject: Annotated[str, Field(max_length=300)]
    email_body: Annotated[str, Field(max_length=5000)]
    whatsapp_body: Annotated[str, Field(max_length=2000)]
    general_body: Annotated[str, Field(max_length=3000)]


class PostCallAnalysis(BaseModel):
    summary: Annotated[str, Field(max_length=1500)]
    current_solution: GroundedField = GroundedField()
    budget: GroundedField = GroundedField()
    timeline: GroundedField = GroundedField()
    decision_maker: GroundedField = GroundedField()
    next_step: GroundedField = GroundedField()
    requirements: Annotated[list[GroundedItem], Field(max_length=15)] = []
    pain_points: Annotated[list[GroundedItem], Field(max_length=15)] = []
    objections: Annotated[list[GroundedItem], Field(max_length=15)] = []
    competitors: Annotated[list[GroundedItem], Field(max_length=10)] = []
    action_items: Annotated[list[SuggestedAction], Field(max_length=10)] = []
    suggested_outcome: CallOutcome | None = None
    follow_up: FollowUpText


class PostCallContext(BaseModel):
    contact_name: str
    company_name: str
    rep_name: str | None
    objective: str | None
    agenda: list[str]
    notes: list[dict[str, str]]
    transcript: list[dict[str, str]]


POST_CALL_SYSTEM = (
    "You write the after-call record for an Indian MSME salesperson. Using ONLY the transcript "
    "and notes: summarise the call; fill current solution, budget, timeline, decision maker and "
    "next step - each with status CONFIRMED (only if you quote the customer's exact words as "
    "evidence), INFERRED (reasonable reading, no direct quote) or NOT_DISCUSSED (value null); "
    "list requirements, pain points, objections (with category) and competitors; propose action "
    "items; suggest an outcome; and draft a short follow-up email, WhatsApp message and general "
    "message from the salesperson. Drafts must not promise prices, discounts, deadlines, features "
    "or commitments that were not stated in the call; if unsure, say you will share details. "
    + UNTRUSTED_DATA_RULES
)


def _normalise(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9₹%]+", s.lower()))


def _grounded(evidence: str | None, haystack: str) -> bool:
    if not evidence:
        return False
    needle = _normalise(evidence)
    return len(needle) >= 6 and needle in haystack


_FIGURE = re.compile(
    r"(?:₹\s?\d[\d,.]*|\brs\.?\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:%|lakh|lakhs|crore|k)\b|\b\d{2,}[\d,.]*\b)",
    re.IGNORECASE,
)


def figure_warnings(text: str, source_text: str) -> list[str]:
    """Flag figures in a draft that never appeared in the call (possible invented promises)."""
    src_digits = set(re.findall(r"\d+", source_text))
    warnings = []
    for m in _FIGURE.finditer(text):
        digits = re.findall(r"\d+", m.group(0))
        if digits and not all(d in src_digits for d in digits):
            warnings.append(f"Contains a figure not mentioned in the call: {m.group(0).strip()}")
    return sorted(set(warnings))


# ---- Deterministic mock ----------------------------------------------------------------------


@register_mock(AITask.POST_CALL)
def _mock_post_call(context: BaseModel | None) -> PostCallAnalysis:
    """MOCKED analysis built only from notes/transcript in the context (no invented facts)."""
    if not isinstance(context, PostCallContext):
        raise AIInvalidOutputError("post-call mock needs a PostCallContext")
    by_kind: dict[str, list[str]] = {}
    for n in context.notes:
        by_kind.setdefault(n["kind"], []).append(n["text"])

    def field(kind: str) -> GroundedField:
        values = by_kind.get(kind)
        if not values:
            return GroundedField()
        return GroundedField(
            value=values[0][:500], status=FieldStatus.CONFIRMED, evidence=values[0][:500]
        )

    def items(kind: str) -> list[GroundedItem]:
        return [
            GroundedItem(text=t[:300], status="CONFIRMED", evidence=t[:500])
            for t in by_kind.get(kind, [])
        ][:10]

    reqs = by_kind.get("REQUIREMENT", [])
    parts = [f"Call with {context.contact_name}."]
    if context.objective:
        parts.append(f"Objective: {context.objective}.")
    if reqs:
        parts.append("Customer requirements: " + "; ".join(r[:120] for r in reqs[:3]) + ".")
    if by_kind.get("OBJECTION"):
        parts.append(f"{len(by_kind['OBJECTION'])} objection(s) raised.")
    if by_kind.get("NEXT_STEP"):
        parts.append("Next step: " + by_kind["NEXT_STEP"][0][:160])
    actions = [
        SuggestedAction(title=f"Follow up: {t[:120]}", kind="FOLLOW_UP", due_in_days=2)
        for t in by_kind.get("NEXT_STEP", [])[:3]
    ]
    greeting = f"Dear {context.contact_name},"
    points = "\n".join(f"- {r[:150]}" for r in reqs[:3]) or "- the points we discussed"
    sign = context.rep_name or "Team"
    body = (
        f"{greeting}\n\nThank you for your time on the call today. To summarise what you shared:\n"
        f"{points}\n\nI will share the relevant details with you shortly. Please let me know if I "
        f"missed anything.\n\nRegards,\n{sign}\n{context.company_name}"
    )
    return PostCallAnalysis(
        summary=" ".join(parts)[:1500],
        current_solution=field("CURRENT_SOLUTION"),
        budget=field("BUDGET"),
        timeline=field("TIMELINE"),
        decision_maker=field("DECISION_MAKER"),
        next_step=field("NEXT_STEP"),
        requirements=items("REQUIREMENT"),
        pain_points=items("PAIN_POINT"),
        objections=[
            GroundedItem(text=t[:300], status="CONFIRMED", evidence=t[:500], category=None)
            for t in by_kind.get("OBJECTION", [])
        ][:10],
        competitors=items("COMPETITOR"),
        action_items=actions,
        suggested_outcome=CallOutcome.FOLLOW_UP_REQUIRED if by_kind.get("NEXT_STEP") else None,
        follow_up=FollowUpText(
            email_subject="Following up on our discussion",
            email_body=body,
            whatsapp_body=(
                f"Hi {context.contact_name}, thank you for your time today. I'll share the "
                f"details we discussed shortly. - {sign}, {context.company_name}"
            ),
            general_body=f"Follow-up with {context.contact_name}: "
            + ("; ".join(r[:100] for r in reqs[:3]) or "see call summary"),
        ),
    )


# ---- Job ----------------------------------------------------------------------------------------


async def _build_context(
    session: AsyncSession, company_id: uuid.UUID, call: Call
) -> tuple[PostCallContext, str]:
    contact = await session.scalar(
        select(Contact).where(Contact.company_id == company_id, Contact.id == call.contact_id)
    )
    company = await session.scalar(select(Company).where(Company.id == company_id))
    from app.users.models import User

    rep = await session.get(User, call.user_id) if call.user_id else None
    segments = list(
        (
            await session.scalars(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.company_id == company_id, TranscriptSegment.call_id == call.id
                )
                .order_by(TranscriptSegment.seq)
            )
        ).all()
    )
    notes = list(
        (
            await session.scalars(
                select(CallNote).where(
                    CallNote.company_id == company_id,
                    CallNote.call_id == call.id,
                    CallNote.status != NoteStatus.REJECTED.value,
                )
            )
        ).all()
    )
    agenda = list(
        (
            await session.scalars(
                select(AgendaItem.title).where(
                    AgendaItem.company_id == company_id, AgendaItem.call_id == call.id
                )
            )
        ).all()
    )
    transcript: list[dict[str, str]] = []
    used = 0
    for s in segments:
        used += len(s.text)
        if used > MAX_TRANSCRIPT_CHARS:
            transcript.append({"speaker": "SYSTEM", "text": "[transcript truncated]"})
            break
        transcript.append({"speaker": s.speaker, "text": s.text})
    ctx = PostCallContext(
        contact_name=contact.name if contact else "the customer",
        company_name=company.name if company else "",
        rep_name=rep.full_name if rep else None,
        objective=call.objective,
        agenda=agenda,
        notes=[{"kind": n.kind, "text": n.text, "status": n.status} for n in notes],
        transcript=transcript,
    )
    source_text = "\n".join(s.text for s in segments) + "\n" + "\n".join(n.text for n in notes)
    return ctx, source_text


def _prompt(ctx: PostCallContext) -> str:
    return "\n\n".join(
        [
            data_block(
                "call_facts",
                (
                    f"customer: {ctx.contact_name}\nour company: {ctx.company_name}\n"
                    f"salesperson: {ctx.rep_name or ''}\nobjective: {ctx.objective or ''}"
                ),
            ),
            data_block("agenda", "\n".join(ctx.agenda) or "(none)"),
            data_block(
                "notes",
                "\n".join(f"{n['kind']} ({n['status']}): {n['text']}" for n in ctx.notes)
                or "(none)",
            ),
            data_block(
                "transcript",
                "\n".join(f"{t['speaker']}: {t['text']}" for t in ctx.transcript),
                max_chars=MAX_TRANSCRIPT_CHARS + 1000,
            ),
        ]
    )


async def process_call(company_id: uuid.UUID, call_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        call = await session.scalar(
            select(Call).where(Call.company_id == company_id, Call.id == call_id)
        )
        if call is None or CallStatus(call.status) not in TERMINAL_CALL_STATUSES:
            return
        summary = await session.scalar(
            select(CallSummary).where(
                CallSummary.company_id == company_id, CallSummary.call_id == call_id
            )
        )
        if summary is not None and summary.status == SummaryStatus.READY:
            return  # idempotent: never duplicate drafts/action items
        if summary is None:
            summary = CallSummary(
                company_id=company_id, call_id=call_id, status=SummaryStatus.PENDING.value
            )
            session.add(summary)
            await session.flush()
        ctx, source_text = await _build_context(session, company_id, call)
        if not ctx.transcript:
            summary.status = SummaryStatus.READY.value
            summary.summary = NO_TRANSCRIPT
            summary.generated_at = datetime.now(UTC)
            await session.commit()
            return
        try:
            result = await get_ai_gateway().run(
                company_id=company_id,
                call_id=call_id,
                task=AITask.POST_CALL,
                system=POST_CALL_SYSTEM,
                prompt=_prompt(ctx),
                schema=PostCallAnalysis,
                context=ctx,
                max_tokens=6000,
            )
        except AIError as exc:
            summary.status = SummaryStatus.FAILED.value
            summary.error_code = exc.code
            await session.commit()
            logger.warning("postcall_ai_failed", extra={"call_id": str(call_id), "error": exc.code})
            return
        await _persist(
            session, company_id, call, summary, result.output, result.usage.provider, source_text
        )


async def _persist(
    session: AsyncSession,
    company_id: uuid.UUID,
    call: Call,
    summary: CallSummary,
    analysis: PostCallAnalysis,
    provider: str,
    source_text: str,
) -> None:
    haystack = _normalise(source_text)
    summary.summary = analysis.summary
    summary.provider = provider
    summary.suggested_outcome = (
        analysis.suggested_outcome.value if analysis.suggested_outcome else None
    )
    for name in SCALAR_FIELDS:
        f: GroundedField = getattr(analysis, name)
        value = (f.value or "").strip() or None
        status = FieldStatus.NOT_DISCUSSED if value is None else f.status
        if status == FieldStatus.CONFIRMED and not _grounded(f.evidence, haystack):
            status = FieldStatus.INFERRED  # no verbatim evidence -> cannot claim confirmed
        if status == FieldStatus.NOT_DISCUSSED:
            value = None
        setattr(summary, name, value[:500] if value else None)
        setattr(summary, f"{name}_status", status.value)

    lists = [
        (NoteKind.REQUIREMENT, analysis.requirements),
        (NoteKind.PAIN_POINT, analysis.pain_points),
        (NoteKind.OBJECTION, analysis.objections),
        (NoteKind.COMPETITOR, analysis.competitors),
    ]
    existing = {
        (n.kind, _normalise(n.text))
        for n in await session.scalars(
            select(CallNote).where(CallNote.company_id == company_id, CallNote.call_id == call.id)
        )
    }
    for kind, items in lists:
        for item in items:
            if (kind.value, _normalise(item.text)) in existing:
                continue  # already captured live (or rejected by the salesperson)
            grounded = item.status == "CONFIRMED" and _grounded(item.evidence, haystack)
            await suggest_note(
                session,
                company_id=company_id,
                call_id=call.id,
                contact_id=call.contact_id,
                kind=kind,
                text=item.text,
                source=IntelSource.AI,
                dedupe_key=f"post:{kind}:{short_hash(item.text)}",
                confidence=0.8 if grounded else 0.5,
                category=item.category,
            )

    now = datetime.now(UTC)
    for action in analysis.action_items:
        session.add(
            ActionItem(
                company_id=company_id,
                contact_id=call.contact_id,
                call_id=call.id,
                kind=ActionItemKind(action.kind).value,
                title=action.title,
                assignee_user_id=call.user_id,
                due_at=now + timedelta(days=action.due_in_days)
                if action.due_in_days is not None
                else None,
                status=ActionItemStatus.OPEN.value,
                source=ActionItemSource.AI.value,
            )
        )

    fu = analysis.follow_up
    drafts = [
        (DraftChannel.EMAIL, fu.email_subject, fu.email_body),
        (DraftChannel.WHATSAPP, None, fu.whatsapp_body),
        (DraftChannel.GENERAL, None, fu.general_body),
    ]
    for channel, subject, body in drafts:
        session.add(
            FollowUpDraft(
                company_id=company_id,
                call_id=call.id,
                contact_id=call.contact_id,
                channel=channel.value,
                subject=subject,
                body=body,
                status=DraftStatus.DRAFT.value,
                source="AI",
                warnings=figure_warnings(f"{subject or ''}\n{body}", source_text),
            )
        )

    summary.status = SummaryStatus.READY.value
    summary.error_code = None
    summary.generated_at = now
    timeline.record(
        session,
        company_id=company_id,
        contact_id=call.contact_id,
        category=TimelineCategory.SUMMARY,
        event_type=TimelineEventType.CALL_SUMMARY,
        summary=analysis.summary,
        actor_user_id=None,
        call_id=call.id,
    )
    timeline.record(
        session,
        company_id=company_id,
        contact_id=call.contact_id,
        category=TimelineCategory.FOLLOW_UP,
        event_type=TimelineEventType.FOLLOW_UP_DRAFTED,
        summary="Follow-up drafts prepared (email, WhatsApp, general) - not sent",
        actor_user_id=None,
        call_id=call.id,
    )
    await session.commit()


@register_job("postcall.process")
async def _postcall_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    if company_id is not None:
        await process_call(company_id, uuid.UUID(payload["call_id"]))


# ---- API-facing ---------------------------------------------------------------------------------


def _require_editor(principal: Principal, call: Call) -> None:
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can change this call's follow-up")


async def get_bundle(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID
) -> dict[str, Any]:
    call = await get_call(session, principal, call_id)
    summary = await session.scalar(
        select(CallSummary).where(
            CallSummary.company_id == principal.company_id, CallSummary.call_id == call.id
        )
    )
    drafts = list(
        (
            await session.scalars(
                select(FollowUpDraft)
                .where(
                    FollowUpDraft.company_id == principal.company_id,
                    FollowUpDraft.call_id == call.id,
                    FollowUpDraft.status != DraftStatus.DISCARDED.value,
                )
                .order_by(FollowUpDraft.channel)
            )
        ).all()
    )
    return {"call": call, "summary": summary, "drafts": drafts}


async def retry(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> None:
    call = await get_call(session, principal, call_id)
    _require_editor(principal, call)
    if CallStatus(call.status) not in TERMINAL_CALL_STATUSES:
        raise InvalidStateError("Post-call processing runs after the call has ended")
    summary = await session.scalar(
        select(CallSummary).where(
            CallSummary.company_id == principal.company_id, CallSummary.call_id == call.id
        )
    )
    if summary is not None and summary.status == SummaryStatus.READY:
        raise InvalidStateError("This call has already been processed")
    await enqueue(
        session,
        "postcall.process",
        company_id=principal.company_id,
        payload={"call_id": str(call.id)},
        dedupe_key=f"postcall:{call.id}:{uuid.uuid4().hex[:8]}",
    )
    await session.commit()


async def review_draft(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    subject: str | None,
    body: str | None,
    status: DraftStatus | None,
) -> FollowUpDraft:
    call = await get_call(session, principal, call_id)
    _require_editor(principal, call)
    draft = await session.scalar(
        select(FollowUpDraft).where(
            FollowUpDraft.company_id == principal.company_id,
            FollowUpDraft.call_id == call.id,
            FollowUpDraft.id == draft_id,
        )
    )
    if draft is None:
        raise NotFoundError("Draft not found")
    if subject is not None:
        draft.subject = subject
    if body is not None:
        draft.body = body
        draft.status = DraftStatus.DRAFT.value  # edited text needs a fresh approval
    if status is not None:
        draft.status = status.value
        draft.reviewed_by_user_id = principal.user_id
        draft.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(draft)
    return draft
