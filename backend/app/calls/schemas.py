import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from app.calls.models import CallChannel, CallOutcome, CallStatus
from app.calls.targets import TargetError, normalize_target
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
    channel: CallChannel = CallChannel.PHONE
    # Meeting link (required for channels TEAMS and GOOGLE_MEET).
    meeting_url: str | None = None
    # Recognition language; defaults to the server's STT_LANGUAGE.
    language: Literal["en-IN", "en-US", "hi-IN", "de-DE"] | None = None

    @model_validator(mode="after")
    def _meeting(self) -> "CallCreate":
        try:
            self.meeting_url = normalize_target(self.channel, self.meeting_url)
        except TargetError as exc:
            raise ValueError(str(exc)) from None
        return self


class CallUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: Text2k | None = None
    desired_outcome: Text2k | None = None
    scheduled_at: AwareDatetime | None = None
    # Pre-start edits (only while PLANNED): the meeting link is re-validated for the call's
    # channel; the channel and contact never change (plan a new call instead).
    meeting_url: str | None = None
    language: Literal["en-IN", "en-US", "hi-IN", "de-DE"] | None = None
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
    provider: str | None = None
    telephony_error: str | None = None
    channel: CallChannel = CallChannel.PHONE
    meeting_url: str | None = None
    transcript_persistence: str = "PERSISTED"
    language: str | None = None
    end_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
