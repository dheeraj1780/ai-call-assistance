import uuid
from datetime import datetime

from pydantic import BaseModel

from app.action_items.schemas import ActionItemOut
from app.agendas.schemas import AgendaItemOut
from app.calls.schemas import CallOut
from app.contacts.schemas import ContactOut, NoteOut
from app.timeline.schemas import TimelineEventOut


class PreviousCall(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    objective: str | None
    outcome: str | None
    next_step: str | None
    summary: str | None = None


class KnownItem(BaseModel):
    """A requirement/objection recorded on an earlier call (confirmed or corrected by a human
    where possible). ``status`` shows how much to trust it."""

    kind: str
    text: str
    status: str
    call_id: uuid.UUID | None


class CallPrepOut(BaseModel):
    call: CallOut
    contact: ContactOut
    agenda: list[AgendaItemOut]
    previous_calls: list[PreviousCall]
    recent_notes: list[NoteOut]
    open_action_items: list[ActionItemOut]
    known_requirements: list[KnownItem]
    known_objections: list[KnownItem]
    recent_timeline: list[TimelineEventOut]
