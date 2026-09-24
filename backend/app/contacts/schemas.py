import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.common.schemas import APIModel
from app.common.validation import Body10k, Email, Name200, Phone, Text10k, Text120, Text200
from app.contacts.models import MAX_ATTRIBUTES, MAX_TAGS, ContactSource, ContactStatus

Tag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40),
]


def _dedupe(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tags))


Tags = Annotated[list[Tag], Field(max_length=MAX_TAGS), AfterValidator(_dedupe)]

AttrKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
AttrValue = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Attributes = Annotated[dict[AttrKey, AttrValue], Field(max_length=MAX_ATTRIBUTES)]


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name200
    organization: Text200 | None = None
    phone: Phone | None = None
    email: Email | None = None
    designation: Text120 | None = None
    status: ContactStatus = ContactStatus.NEW
    source: ContactSource = ContactSource.MANUAL
    tags: Tags = []
    attributes: Attributes = {}
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
    source: ContactSource | None = None
    tags: Tags | None = None
    attributes: Attributes | None = None
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
    source: ContactSource
    tags: list[str]
    attributes: dict[str, str]
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
