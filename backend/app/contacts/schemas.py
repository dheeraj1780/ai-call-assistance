import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.common.schemas import APIModel
from app.common.validation import Body10k, Email, Name200, Phone, Text120, Text200, Text10k
from app.contacts.models import MAX_TAGS, ContactStatus

Tag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40),
]


def _dedupe(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tags))


Tags = Annotated[list[Tag], Field(max_length=MAX_TAGS), AfterValidator(_dedupe)]


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name200
    organization: Text200 | None = None
    phone: Phone | None = None
    email: Email | None = None
    designation: Text120 | None = None
    status: ContactStatus = ContactStatus.NEW
    tags: Tags = []
    notes: Text10k | None = None
    # Defaults to the creating user when omitted; explicit null = unassigned.
    owner_user_id: uuid.UUID | None = None


class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name200 | None = None
    organization: Text200 | None = None
    phone: Phone | None = None
    email: Email | None = None
    designation: Text120 | None = None
    status: ContactStatus | None = None
    tags: Tags | None = None
    notes: Text10k | None = None
    owner_user_id: uuid.UUID | None = None


class ContactOut(APIModel):
    id: uuid.UUID
    name: str
    organization: str | None
    phone: str | None
    email: str | None
    designation: str | None
    status: ContactStatus
    tags: list[str]
    notes: str | None
    owner_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class ContactSummary(APIModel):
    id: uuid.UUID
    name: str
    organization: str | None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Body10k


class NoteUpdate(NoteCreate):
    pass


class NoteOut(APIModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    author_user_id: uuid.UUID | None
    body: str
    created_at: datetime
    updated_at: datetime
