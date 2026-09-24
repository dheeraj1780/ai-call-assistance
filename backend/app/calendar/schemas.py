import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.common.validation import Text2k, Title300


class ConnectionOut(BaseModel):
    connected: bool
    provider: str | None = None
    account_email: str | None = None
    status: str | None = None


class ConnectStart(BaseModel):
    authorization_url: str


class ExternalEventOut(BaseModel):
    external_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    html_link: str | None


class EventCreate(BaseModel):
    """Creating a calendar event always needs ``confirm: true`` - the UI shows the user the
    exact event first. Nothing is ever written to a calendar automatically."""

    model_config = ConfigDict(extra="forbid")

    title: Title300
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    description: Text2k | None = None
    contact_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    action_item_id: uuid.UUID | None = None
    confirm: Literal[True]

    @model_validator(mode="after")
    def _check_times(self) -> "EventCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.ends_at - self.starts_at > timedelta(hours=12):
            raise ValueError("events longer than 12 hours are not supported")
        return self


class EventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title300
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    description: Text2k | None = None
    confirm: Literal[True]

    @model_validator(mode="after")
    def _check_times(self) -> "EventUpdate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    external_event_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    html_link: str | None
    contact_id: uuid.UUID | None
    call_id: uuid.UUID | None
    action_item_id: uuid.UUID | None


DaysAhead = Annotated[int, Field(ge=1, le=60)]
