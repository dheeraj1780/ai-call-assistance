import uuid
from datetime import datetime

from pydantic import BaseModel

from app.common.schemas import APIModel
from app.timeline.models import TimelineCategory, TimelineEventType


class TimelineEventOut(APIModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    category: TimelineCategory
    event_type: TimelineEventType
    occurred_at: datetime
    actor_user_id: uuid.UUID | None
    summary: str
    from_status: str | None
    to_status: str | None
    note_id: uuid.UUID | None
    call_id: uuid.UUID | None
    action_item_id: uuid.UUID | None
    channel: str | None = None
    session_id: uuid.UUID | None = None


class TimelinePage(BaseModel):
    items: list[TimelineEventOut]
    next_cursor: str | None
