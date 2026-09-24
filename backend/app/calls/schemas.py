import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.calls.models import CallOutcome, CallStatus
from app.common.schemas import APIModel
from app.common.validation import Text2k, Text5k
from app.contacts.schemas import ContactSummary


class CallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID
    objective: Text2k | None = None
    desired_outcome: Text2k | None = None
    scheduled_at: AwareDatetime | None = None
    # The responsible salesperson; defaults to the creator.
    user_id: uuid.UUID | None = None


class CallUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: Text2k | None = None
    desired_outcome: Text2k | None = None
    scheduled_at: AwareDatetime | None = None
    user_id: uuid.UUID | None = None
    status: CallStatus | None = None
    outcome: CallOutcome | None = None
    outcome_notes: Text5k | None = None
    next_step: Text2k | None = None


class CallOut(APIModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact: ContactSummary
    user_id: uuid.UUID | None
    objective: str | None
    desired_outcome: str | None
    status: CallStatus
    scheduled_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    outcome: CallOutcome | None
    outcome_notes: str | None
    next_step: str | None
    created_at: datetime
    updated_at: datetime
