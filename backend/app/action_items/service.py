"""Action item use cases.

Authorization: any member can create and read action items. Updating/confirming requires
OWNER/ADMIN, the assignee, or the creator. Deleting requires OWNER/ADMIN or the creator.

Rules:
- A call link implies its contact; if both are given they must match.
- Items created through the API are MANUAL and confirmed on creation. Unconfirmed items
  (future AI suggestions) must be confirmed before their status can change.
- ``completed_at`` is maintained by the server (set on DONE, cleared when reopened).
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.action_items.models import ActionItem, ActionItemKind, ActionItemSource, ActionItemStatus
from app.action_items.repository import ActionItemRepository
from app.action_items.schemas import ActionItemCreate, ActionItemUpdate
from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.repository import CallRepository
from app.common.errors import (
    ForbiddenError,
    InvalidReferenceError,
    InvalidStateError,
    NotFoundError,
)
from app.contacts.repository import ContactRepository
from app.tenants.membership import ensure_member
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType

_NON_NULLABLE = {"title", "kind", "status"}
_KIND_LABEL = {
    ActionItemKind.TASK: "Task",
    ActionItemKind.FOLLOW_UP: "Follow-up",
    ActionItemKind.APPOINTMENT: "Appointment",
}


def _category(kind: str) -> TimelineCategory:
    return TimelineCategory(kind)  # TASK / FOLLOW_UP / APPOINTMENT share names


def _can_modify(principal: Principal, item: ActionItem) -> bool:
    return principal.is_admin or principal.user_id in (
        item.assignee_user_id,
        item.created_by_user_id,
    )


async def get_item(session: AsyncSession, principal: Principal, item_id: uuid.UUID) -> ActionItem:
    item = await ActionItemRepository(session).get(principal.company_id, item_id)
    if item is None:
        raise NotFoundError("Action item not found")
    return item


async def _resolve_links(
    session: AsyncSession, principal: Principal, data: ActionItemCreate
) -> uuid.UUID | None:
    contact_id = data.contact_id
    if data.call_id is not None:
        call = await CallRepository(session).get(principal.company_id, data.call_id)
        if call is None:
            raise InvalidReferenceError("Call not found", details=[{"field": "call_id"}])
        if contact_id is not None and contact_id != call.contact_id:
            raise InvalidReferenceError(
                "contact_id does not match the call's contact", details=[{"field": "contact_id"}]
            )
        contact_id = call.contact_id
    if contact_id is not None:
        contact = await ContactRepository(session).get(principal.company_id, contact_id)
        if contact is None:
            raise InvalidReferenceError("Contact not found", details=[{"field": "contact_id"}])
    return contact_id


async def create_item(
    session: AsyncSession, principal: Principal, data: ActionItemCreate, *, ip: str | None
) -> ActionItem:
    contact_id = await _resolve_links(session, principal, data)
    assignee = (
        data.assignee_user_id if "assignee_user_id" in data.model_fields_set else principal.user_id
    )
    await ensure_member(session, principal.company_id, assignee, field="assignee_user_id")
    now = datetime.now(UTC)
    item = ActionItem(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        contact_id=contact_id,
        call_id=data.call_id,
        kind=data.kind.value,
        title=data.title,
        description=data.description,
        assignee_user_id=assignee,
        created_by_user_id=principal.user_id,
        due_at=data.due_at,
        status=ActionItemStatus.OPEN.value,
        source=ActionItemSource.MANUAL.value,
        confirmed_at=now,
        confirmed_by_user_id=principal.user_id,
    )
    session.add(item)
    await session.flush()
    if contact_id is not None:
        due = f" (due {data.due_at:%d %b %Y %H:%M %Z})" if data.due_at else ""
        timeline.record(
            session,
            company_id=principal.company_id,
            contact_id=contact_id,
            category=_category(item.kind),
            event_type=TimelineEventType.ACTION_ITEM_CREATED,
            summary=f"{_KIND_LABEL[data.kind]}: {data.title}{due}",
            actor_user_id=principal.user_id,
            action_item_id=item.id,
            call_id=data.call_id,
        )
    audit.record(
        session,
        "action_item.created",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="action_item",
        entity_id=item.id,
        ip=ip,
    )
    await session.commit()
    return await get_item(session, principal, item.id)


async def update_item(
    session: AsyncSession,
    principal: Principal,
    item_id: uuid.UUID,
    data: ActionItemUpdate,
    *,
    ip: str | None,
) -> ActionItem:
    item = await get_item(session, principal, item_id)
    if not _can_modify(principal, item):
        raise ForbiddenError("Only the assignee, the creator or an admin can update this item")

    changes: dict[str, Any] = {
        k: v
        for k, v in data.model_dump(exclude_unset=True).items()
        if not (v is None and k in _NON_NULLABLE)
    }
    if "assignee_user_id" in changes:
        await ensure_member(
            session, principal.company_id, changes["assignee_user_id"], field="assignee_user_id"
        )

    new_status = changes.pop("status", None)
    old_status = item.status
    if new_status is not None and new_status.value != old_status:
        if item.confirmed_at is None:
            raise InvalidStateError("Confirm this suggested item before changing its status")
        item.status = new_status.value
        item.completed_at = datetime.now(UTC) if new_status == ActionItemStatus.DONE else None

    for field, value in changes.items():
        setattr(item, field, value.value if isinstance(value, enum.Enum) else value)

    if item.status == ActionItemStatus.DONE and old_status != item.status and item.contact_id:
        timeline.record(
            session,
            company_id=principal.company_id,
            contact_id=item.contact_id,
            category=_category(item.kind),
            event_type=TimelineEventType.ACTION_ITEM_COMPLETED,
            summary=f"{_KIND_LABEL[ActionItemKind(item.kind)]} completed: {item.title}",
            actor_user_id=principal.user_id,
            action_item_id=item.id,
        )
    audit.record(
        session,
        "action_item.updated",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="action_item",
        entity_id=item.id,
        ip=ip,
        details={"fields": sorted(data.model_fields_set)},
    )
    await session.commit()
    session.expire(item)
    return await get_item(session, principal, item.id)


async def confirm_item(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, *, ip: str | None
) -> ActionItem:
    item = await get_item(session, principal, item_id)
    if not _can_modify(principal, item):
        raise ForbiddenError("Only the assignee, the creator or an admin can confirm this item")
    if item.confirmed_at is None:  # idempotent
        item.confirmed_at = datetime.now(UTC)
        item.confirmed_by_user_id = principal.user_id
        if item.contact_id:
            timeline.record(
                session,
                company_id=principal.company_id,
                contact_id=item.contact_id,
                category=_category(item.kind),
                event_type=TimelineEventType.ACTION_ITEM_CREATED,
                summary=(
                    f"{_KIND_LABEL[ActionItemKind(item.kind)]} (AI suggestion, confirmed): "
                    f"{item.title}"
                ),
                actor_user_id=principal.user_id,
                action_item_id=item.id,
                call_id=item.call_id,
            )
        audit.record(
            session,
            "action_item.confirmed",
            company_id=principal.company_id,
            actor_user_id=principal.user_id,
            entity_type="action_item",
            entity_id=item.id,
            ip=ip,
        )
        await session.commit()
        session.expire(item)
    return await get_item(session, principal, item.id)


async def delete_item(
    session: AsyncSession, principal: Principal, item_id: uuid.UUID, *, ip: str | None
) -> None:
    item = await get_item(session, principal, item_id)
    if not (principal.is_admin or item.created_by_user_id == principal.user_id):
        raise ForbiddenError("Only the creator or an admin can delete this item")
    await session.delete(item)
    audit.record(
        session,
        "action_item.deleted",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="action_item",
        entity_id=item_id,
        ip=ip,
    )
    await session.commit()
