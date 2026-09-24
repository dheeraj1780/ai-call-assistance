import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field

from app.action_items.models import ActionItemKind, ActionItemSource, ActionItemStatus
from app.common.schemas import APIModel
from app.common.validation import Text5k, Title300
from app.contacts.schemas import ContactSummary


class ActionItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title300
    description: Text5k | None = None
    kind: ActionItemKind = ActionItemKind.TASK
    contact_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    # Defaults to the creator; explicit null = unassigned.
    assignee_user_id: uuid.UUID | None = None
    due_at: AwareDatetime | None = None


class ActionItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title300 | None = None
    description: Text5k | None = None
    kind: ActionItemKind | None = None
    assignee_user_id: uuid.UUID | None = None
    due_at: AwareDatetime | None = None
    status: ActionItemStatus | None = None


class ActionItemOut(APIModel):
    id: uuid.UUID
    title: str
    description: str | None
    kind: ActionItemKind
    status: ActionItemStatus
    source: ActionItemSource
    contact_id: uuid.UUID | None
    contact: ContactSummary | None
    call_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    created_by_user_id: uuid.UUID | None
    due_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by_user_id: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None
