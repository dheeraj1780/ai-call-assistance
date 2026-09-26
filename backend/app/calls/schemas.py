import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from app.calls.models import CallChannel, CallOutcome, CallStatus
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
        if self.channel == CallChannel.TEAMS:
            if not self.meeting_url or not is_teams_meeting_url(self.meeting_url):
                raise ValueError(
                    "A Teams call needs a Microsoft Teams meeting link "
                    "(https://teams.microsoft.com/...)"
                )
        elif self.channel == CallChannel.GOOGLE_MEET:
            from app.integrations.providers.google_meet import meeting_link, parse_meeting_code

            code = parse_meeting_code(self.meeting_url or "")
            if code is None:
                raise ValueError(
                    "A Google Meet call needs a Google Meet link "
                    "(https://meet.google.com/abc-defg-hij) or meeting code"
                )
            self.meeting_url = meeting_link(code)
        elif self.meeting_url is not None:
            raise ValueError("meeting_url is only used for meeting calls (Teams, Google Meet)")
        return self


def is_teams_meeting_url(value: str) -> bool:
    if len(value) > 2000:
        return False
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host in ("teams.microsoft.com", "teams.live.com") or host.endswith(".teams.microsoft.com")
    )


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
    provider: str | None = None
    telephony_error: str | None = None
    channel: CallChannel = CallChannel.PHONE
    meeting_url: str | None = None
    transcript_persistence: str = "PERSISTED"
    language: str | None = None
    created_at: datetime
    updated_at: datetime
