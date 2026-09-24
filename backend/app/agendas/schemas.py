import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agendas.models import MAX_AGENDA_ITEMS, AgendaItemSource, AgendaItemStatus, StatusSource
from app.common.schemas import APIModel

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class AgendaItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    question: Question | None = None
    description: Description | None = None
    source: AgendaItemSource = AgendaItemSource.MANUAL


class AgendaReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: Annotated[list[AgendaItemIn], Field(max_length=MAX_AGENDA_ITEMS)]


class AgendaItemStatusUpdate(BaseModel):
    """Manual override by the salesperson; always wins over automatic inference."""

    model_config = ConfigDict(extra="forbid")

    status: AgendaItemStatus


class AgendaItemOut(APIModel):
    id: uuid.UUID
    call_id: uuid.UUID
    position: int
    title: str
    question: str | None
    description: str | None
    source: AgendaItemSource
    status: AgendaItemStatus
    status_source: StatusSource
    status_confidence: float | None
    status_reason: str | None
    completed_at: datetime | None


# ---- AI agenda generation ------------------------------------------------------------------


class SuggestedAgendaItem(BaseModel):
    title: Annotated[str, Field(max_length=200)]
    question: Annotated[str, Field(max_length=500)] | None = None
    why: Annotated[str, Field(max_length=500)] | None = None


class CustomerFact(BaseModel):
    """A fact taken from the customer's existing records. ``source_ref`` must be one of the
    reference ids given in the prompt; facts with unknown refs are discarded server-side."""

    text: Annotated[str, Field(max_length=500)]
    source_ref: Annotated[str, Field(max_length=64)]


class AgendaSuggestion(BaseModel):
    customer_facts: Annotated[list[CustomerFact], Field(max_length=15)] = []
    agenda: Annotated[list[SuggestedAgendaItem], Field(max_length=MAX_AGENDA_ITEMS)]
    open_questions: Annotated[
        list[Annotated[str, Field(max_length=300)]], Field(max_length=10)
    ] = []


class AgendaSuggestionOut(BaseModel):
    """What the UI shows: facts (from records) are kept separate from AI suggestions."""

    customer_facts: list[CustomerFact]
    ai_suggested_agenda: list[SuggestedAgendaItem]
    ai_open_questions: list[str]
    provider: str
    discarded_ungrounded_facts: int
