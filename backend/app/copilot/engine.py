"""Live copilot engine (one per active call).

Pipeline for every FINAL transcript segment (partials are ignored):
1. Deterministic detectors -> suggested structured notes + a few high-value cards.
2. Agenda tracking: an item moves to IN_PROGRESS when its topic is raised, and to COMPLETED
   only when the customer gives a substantive answer after the topic was raised (never on a
   keyword alone). Manual statuses always win (see agendas.service.apply_status).
3. Customer questions -> tenant-scoped knowledge retrieval (extractive, no LLM on the hot path).
4. Every N segments: one incremental LLM pass over ONLY the new segments plus compact state
   (never the whole transcript), bounded by a per-call budget, one request in flight.
5. Missing agenda items -> gentle reminder + suggested question (deduplicated, cooled down).

Every failure is caught: the copilot degrades to "unavailable" but the call, the transcript
and the media pipeline continue.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agendas.models import AgendaItem, AgendaItemStatus, StatusSource
from app.agendas.service import apply_status
from app.ai.gateway import get_ai_gateway
from app.ai.mock_provider import register_mock
from app.ai.provider import AIError, AITask
from app.ai.safety import UNTRUSTED_DATA_RULES, data_block
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.copilot.detectors import (
    Detection,
    detect,
    expand_keywords,
    is_question,
    topic_matches,
    word_count,
)
from app.intel.models import (
    InsightPriority,
    InsightType,
    IntelSource,
    NoteKind,
    ObjectionCategory,
)
from app.intel.service import add_insight, short_hash, suggest_note
from app.live.hub import hub
from app.live.models import Speaker

logger = logging.getLogger(__name__)

ANSWER_WINDOW = 3  # customer must answer within this many segments after a topic is raised
MIN_ANSWER_WORDS = 5
MISSING_CHECK_EVERY = 8
QUESTION_COOLDOWN_S = 20.0
NOTE_MIN_CONFIDENCE = 0.6
AGENDA_AI_MIN_CONFIDENCE = 0.7


@dataclass
class SegmentView:
    id: uuid.UUID
    seq: int
    speaker: Speaker
    text: str


@dataclass
class _AgendaState:
    id: uuid.UUID
    title: str
    question: str | None
    keywords: set[str]
    status: AgendaItemStatus
    manual: bool
    raised_at: int | None = None  # segment index when the topic was raised
    reminded: bool = False


# ---- LLM incremental pass --------------------------------------------------------------------


class ExtractedNote(BaseModel):
    kind: NoteKind
    text: Annotated[str, Field(max_length=300)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    category: ObjectionCategory | None = None


class AgendaUpdate(BaseModel):
    item_id: str
    status: Literal["IN_PROGRESS", "COMPLETED"]
    confidence: Annotated[float, Field(ge=0, le=1)]
    reason: Annotated[str, Field(max_length=200)]


class CopilotDelta(BaseModel):
    notes: Annotated[list[ExtractedNote], Field(max_length=10)] = []
    agenda_updates: Annotated[list[AgendaUpdate], Field(max_length=15)] = []
    suggested_question: Annotated[str, Field(max_length=300)] | None = None
    objective_satisfied: bool | None = None


class CopilotContext(BaseModel):
    objective: str | None
    agenda: list[dict[str, str]]
    known_notes: list[str]
    new_segments: list[dict[str, str]]


COPILOT_SYSTEM = (
    "You assist a salesperson DURING a live phone call. From the NEW transcript lines only, "
    "extract short structured notes (requirements, pain points, current solution, budget, "
    "timeline, decision maker, competitor, objection with category, preference, next step), "
    "decide whether any agenda item was genuinely discussed (COMPLETED only if the customer "
    "actually answered it), and optionally suggest ONE short next question. Be conservative "
    "and concise; confidence must reflect uncertainty; do not repeat known notes. "
    + UNTRUSTED_DATA_RULES
)


@register_mock(AITask.COPILOT)
def _mock_copilot(context: BaseModel | None) -> CopilotDelta:
    """Deterministic stand-in: detector output for new customer lines + next pending question."""
    if not isinstance(context, CopilotContext):
        return CopilotDelta()
    notes: list[ExtractedNote] = []
    for seg in context.new_segments:
        if seg["speaker"] != Speaker.CUSTOMER:
            continue
        for d in detect(seg["text"], speaker_is_customer=True):
            notes.append(
                ExtractedNote(kind=d.kind, text=d.text[:300], confidence=0.65, category=d.category)
            )
    pending = [a for a in context.agenda if a["status"] == AgendaItemStatus.NOT_STARTED]
    question = (
        pending[0].get("question") or f"Could we talk about {pending[0]['title'].lower()}?"
        if pending
        else None
    )
    return CopilotDelta(notes=notes[:10], suggested_question=question)


@dataclass
class CopilotEngine:
    company_id: uuid.UUID
    call_id: uuid.UUID
    contact_id: uuid.UUID
    objective: str | None
    retention_days: int
    agenda: list[_AgendaState] = field(default_factory=list)
    segments: list[SegmentView] = field(default_factory=list)
    llm_calls: int = 0
    llm_cursor: int = 0  # index of the first segment not yet sent to the LLM
    llm_task: asyncio.Task[None] | None = None
    degraded: bool = False
    last_question_at: float = 0.0
    known_notes: list[str] = field(default_factory=list)

    @classmethod
    async def create(
        cls,
        company_id: uuid.UUID,
        call_id: uuid.UUID,
        contact_id: uuid.UUID,
        objective: str | None,
        retention_days: int,
    ) -> "CopilotEngine":
        engine = cls(company_id, call_id, contact_id, objective, retention_days)
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=company_id))
            items = await session.scalars(
                select(AgendaItem)
                .where(AgendaItem.company_id == company_id, AgendaItem.call_id == call_id)
                .order_by(AgendaItem.position)
            )
            for item in items:
                engine.agenda.append(
                    _AgendaState(
                        id=item.id,
                        title=item.title,
                        question=item.question,
                        keywords=expand_keywords(item.keywords),
                        status=AgendaItemStatus(item.status),
                        manual=item.status_source == StatusSource.MANUAL,
                    )
                )
        return engine

    # -- entry point ----------------------------------------------------------------------

    async def on_segment(self, seg: SegmentView) -> None:
        self.segments.append(seg)
        try:
            await self._deterministic(seg)
            await self._track_agenda(seg)
            if seg.speaker == Speaker.CUSTOMER and is_question(seg.text):
                await self._knowledge_lookup(seg)
            if len(self.segments) % MISSING_CHECK_EVERY == 0:
                await self._missing_agenda(seg)
            self._maybe_schedule_llm()
        except Exception:
            logger.exception("copilot_segment_failed", extra={"call_id": str(self.call_id)})
            self._set_degraded("internal_error")

    async def close(self) -> None:
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()

    # -- deterministic --------------------------------------------------------------------

    async def _deterministic(self, seg: SegmentView) -> None:
        detections = detect(seg.text, speaker_is_customer=seg.speaker == Speaker.CUSTOMER)
        if not detections:
            return
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=self.company_id))
            for d in detections:
                await self._store_detection(session, d, seg.id, IntelSource.DETERMINISTIC)

    async def _store_detection(
        self, session: AsyncSession, d: Detection, segment_id: uuid.UUID | None, source: IntelSource
    ) -> None:
        key = f"{d.kind}:{d.key or short_hash(d.text)}"
        note = await suggest_note(
            session,
            company_id=self.company_id,
            call_id=self.call_id,
            contact_id=self.contact_id,
            kind=d.kind,
            text=d.text,
            source=source,
            dedupe_key=key,
            confidence=d.confidence,
            category=d.category,
            segment_id=segment_id,
        )
        if note is None:
            return
        self.known_notes.append(f"{d.kind}: {d.text}")
        card: tuple[InsightType, InsightPriority, str] | None = None
        if d.kind == NoteKind.OBJECTION:
            label = (d.category or ObjectionCategory.OTHER).value.replace("_", " ").lower()
            card = (InsightType.OBJECTION, InsightPriority.HIGH, f"Objection ({label}): {d.text}")
        elif d.kind == NoteKind.CURRENT_SOLUTION:
            card = (
                InsightType.IMPORTANT_FACT,
                InsightPriority.MEDIUM,
                f"Customer mentioned: {d.text}",
            )
        elif d.kind == NoteKind.REQUIREMENT:
            card = (
                InsightType.CUSTOMER_REQUIREMENT,
                InsightPriority.MEDIUM,
                f"Requirement: {d.text}",
            )
        elif d.kind == NoteKind.NEXT_STEP:
            card = (InsightType.NEXT_STEP, InsightPriority.MEDIUM, f"Next step: {d.text}")
        elif d.kind in (NoteKind.BUDGET, NoteKind.TIMELINE, NoteKind.DECISION_MAKER):
            label = d.kind.value.replace("_", " ").lower()
            card = (
                InsightType.IMPORTANT_FACT,
                InsightPriority.LOW,
                f"{label.capitalize()}: {d.text}",
            )
        if card:
            await add_insight(
                session,
                company_id=self.company_id,
                call_id=self.call_id,
                type_=card[0],
                content=card[2],
                priority=card[1],
                source=source,
                dedupe_key=f"card:{key}",
                retention_days=self.retention_days,
                confidence=d.confidence,
                segment_id=segment_id,
            )

    # -- agenda ---------------------------------------------------------------------------

    async def _track_agenda(self, seg: SegmentView) -> None:
        index = len(self.segments) - 1
        changes: list[tuple[_AgendaState, AgendaItemStatus, str, float]] = []
        for item in self.agenda:
            if item.manual or item.status in (AgendaItemStatus.COMPLETED, AgendaItemStatus.SKIPPED):
                continue
            matched = topic_matches(item.keywords, seg.text)
            if item.status == AgendaItemStatus.NOT_STARTED and matched:
                item.status = AgendaItemStatus.IN_PROGRESS
                item.raised_at = index
                changes.append(
                    (item, AgendaItemStatus.IN_PROGRESS, "Topic raised in conversation", 0.5)
                )
                # A customer who raises the topic with a full answer also completes it.
                if seg.speaker == Speaker.CUSTOMER and word_count(seg.text) >= MIN_ANSWER_WORDS + 3:
                    item.status = AgendaItemStatus.COMPLETED
                    changes.append(
                        (
                            item,
                            AgendaItemStatus.COMPLETED,
                            "Customer discussed the topic in detail",
                            0.6,
                        )
                    )
            elif (
                item.status == AgendaItemStatus.IN_PROGRESS
                and item.raised_at is not None
                and index > item.raised_at
                and index - item.raised_at <= ANSWER_WINDOW
                and seg.speaker == Speaker.CUSTOMER
                and word_count(seg.text) >= MIN_ANSWER_WORDS
            ):
                item.status = AgendaItemStatus.COMPLETED
                changes.append(
                    (
                        item,
                        AgendaItemStatus.COMPLETED,
                        "Customer answered after the topic was raised",
                        0.6,
                    )
                )
        if changes:
            await self._persist_agenda(changes, StatusSource.DETERMINISTIC)

    async def _persist_agenda(
        self, changes: list[tuple[_AgendaState, AgendaItemStatus, str, float]], source: StatusSource
    ) -> None:
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=self.company_id))
            for state, status, reason, confidence in changes:
                row = await session.scalar(
                    select(AgendaItem).where(
                        AgendaItem.company_id == self.company_id, AgendaItem.id == state.id
                    )
                )
                if row is None:
                    continue
                if row.status_source == StatusSource.MANUAL:
                    state.manual = True
                    state.status = AgendaItemStatus(row.status)
                    continue
                if apply_status(row, status, source, confidence=confidence, reason=reason):
                    await session.flush()
                    hub.publish(
                        self.call_id,
                        "agenda.updated",
                        {
                            "id": str(row.id),
                            "status": row.status,
                            "status_source": row.status_source,
                            "status_confidence": row.status_confidence,
                            "status_reason": row.status_reason,
                        },
                    )
            await session.commit()

    async def _missing_agenda(self, seg: SegmentView) -> None:
        started = [a for a in self.agenda if a.status != AgendaItemStatus.NOT_STARTED]
        pending = [
            a for a in self.agenda if a.status == AgendaItemStatus.NOT_STARTED and not a.reminded
        ]
        if not started or not pending:
            return
        item = pending[0]
        item.reminded = True
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=self.company_id))
            await add_insight(
                session,
                company_id=self.company_id,
                call_id=self.call_id,
                type_=InsightType.MISSING_AGENDA_ITEM,
                priority=InsightPriority.MEDIUM,
                content=f"{item.title} hasn't been discussed yet.",
                source=IntelSource.DETERMINISTIC,
                dedupe_key=f"missing:{item.id}",
                retention_days=self.retention_days,
            )
            if item.question and time.monotonic() - self.last_question_at > QUESTION_COOLDOWN_S:
                self.last_question_at = time.monotonic()
                await add_insight(
                    session,
                    company_id=self.company_id,
                    call_id=self.call_id,
                    type_=InsightType.QUESTION_SUGGESTION,
                    priority=InsightPriority.MEDIUM,
                    content=item.question,
                    source=IntelSource.DETERMINISTIC,
                    dedupe_key=f"question:agenda:{item.id}",
                    retention_days=self.retention_days,
                    context=f"Agenda: {item.title}",
                )

    # -- knowledge ------------------------------------------------------------------------

    async def _knowledge_lookup(self, seg: SegmentView) -> None:
        from app.knowledge.service import retrieve

        settings = get_settings()
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=self.company_id))
            hits = await retrieve(session, self.company_id, seg.text, k=2)
            good = [h for h in hits if h.score >= settings.knowledge_score_threshold]
            key = f"knowledge:{short_hash(seg.text)}"
            if good:
                best = good[0]
                snippet = " ".join(best.content.split())[:400]
                await add_insight(
                    session,
                    company_id=self.company_id,
                    call_id=self.call_id,
                    type_=InsightType.KNOWLEDGE_RESULT,
                    priority=InsightPriority.HIGH,
                    content=snippet,
                    source=IntelSource.KNOWLEDGE,
                    dedupe_key=key,
                    retention_days=self.retention_days,
                    confidence=round(best.score, 3),
                    context=f"Company knowledge: {best.title}",
                    segment_id=seg.id,
                )
            else:
                await add_insight(
                    session,
                    company_id=self.company_id,
                    call_id=self.call_id,
                    type_=InsightType.KNOWLEDGE_RESULT,
                    priority=InsightPriority.LOW,
                    content="No relevant company information found for the customer's question.",
                    source=IntelSource.KNOWLEDGE,
                    dedupe_key=key,
                    retention_days=self.retention_days,
                    context="Company knowledge",
                    segment_id=seg.id,
                )

    # -- LLM ------------------------------------------------------------------------------

    def _maybe_schedule_llm(self) -> None:
        settings = get_settings()
        new = len(self.segments) - self.llm_cursor
        if (
            new < settings.copilot_llm_every_n_segments
            or self.llm_calls >= settings.copilot_max_llm_calls_per_call
            or (self.llm_task is not None and not self.llm_task.done())
        ):
            return
        window = self.segments[self.llm_cursor :]
        self.llm_cursor = len(self.segments)
        self.llm_calls += 1
        self.llm_task = asyncio.create_task(
            self._llm_pass(window), name=f"copilot-llm-{self.call_id}"
        )

    async def _llm_pass(self, window: list[SegmentView]) -> None:
        ctx = CopilotContext(
            objective=self.objective,
            agenda=[
                {
                    "id": str(a.id),
                    "title": a.title,
                    "status": a.status.value,
                    "question": a.question or "",
                }
                for a in self.agenda
            ],
            known_notes=self.known_notes[-20:],
            new_segments=[{"speaker": s.speaker.value, "text": s.text} for s in window],
        )
        prompt = "\n\n".join(
            [
                data_block("call_objective", ctx.objective or "(none)"),
                data_block(
                    "agenda",
                    "\n".join(f"[id={a['id']}] ({a['status']}) {a['title']}" for a in ctx.agenda)
                    or "(none)",
                ),
                data_block("known_notes", "\n".join(ctx.known_notes) or "(none)"),
                data_block(
                    "new_transcript",
                    "\n".join(f"{s['speaker']}: {s['text']}" for s in ctx.new_segments),
                ),
            ]
        )
        try:
            result = await get_ai_gateway().run(
                company_id=self.company_id,
                call_id=self.call_id,
                task=AITask.COPILOT,
                system=COPILOT_SYSTEM,
                prompt=prompt,
                schema=CopilotDelta,
                context=ctx,
                max_tokens=1500,
            )
        except AIError as exc:
            self._set_degraded(exc.code)
            return
        except Exception:
            logger.exception("copilot_llm_crashed")
            self._set_degraded("ai_error")
            return
        if self.degraded:
            self.degraded = False
            hub.publish(self.call_id, "copilot.status", {"state": "ok"})
        try:
            await self._apply_delta(result.output, window[-1].id if window else None)
        except Exception:
            logger.exception("copilot_apply_failed")

    async def _apply_delta(self, delta: CopilotDelta, segment_id: uuid.UUID | None) -> None:
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=self.company_id))
            for n in delta.notes:
                if n.confidence < NOTE_MIN_CONFIDENCE:
                    continue
                d = Detection(n.kind, n.text, n.confidence, n.category, key=short_hash(n.text))
                await self._store_detection(session, d, segment_id, IntelSource.AI)
            if (
                delta.suggested_question
                and time.monotonic() - self.last_question_at > QUESTION_COOLDOWN_S
            ):
                self.last_question_at = time.monotonic()
                await add_insight(
                    session,
                    company_id=self.company_id,
                    call_id=self.call_id,
                    type_=InsightType.QUESTION_SUGGESTION,
                    priority=InsightPriority.MEDIUM,
                    content=delta.suggested_question,
                    source=IntelSource.AI,
                    dedupe_key=f"question:{short_hash(delta.suggested_question)}",
                    retention_days=self.retention_days,
                )
        by_id = {str(a.id): a for a in self.agenda}
        changes = []
        for u in delta.agenda_updates:
            state = by_id.get(u.item_id)  # ignore ids the model made up
            if state is None or state.manual or u.confidence < AGENDA_AI_MIN_CONFIDENCE:
                continue
            new_status = AgendaItemStatus(u.status)
            state.status = new_status
            changes.append((state, new_status, u.reason, u.confidence))
        if changes:
            await self._persist_agenda(changes, StatusSource.AI)

    def _set_degraded(self, reason: str) -> None:
        if not self.degraded:
            self.degraded = True
            hub.publish(self.call_id, "copilot.status", {"state": "degraded", "reason": reason})

    def mark_manual(self, item_id: uuid.UUID, status: AgendaItemStatus) -> None:
        for a in self.agenda:
            if a.id == item_id:
                a.manual = True
                a.status = status
