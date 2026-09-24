"""Call record use cases (manual tracking; no telephony in this phase).

Authorization: any member can plan a call and read calls. Updating a call requires
OWNER/ADMIN, the call's assigned user, or an unassigned call.

Rules:
- Status changes must follow ``CALL_TRANSITIONS``; timestamps are set by the server.
- ``objective``/``scheduled_at`` can only change while the call is PLANNED.
- ``outcome`` can only be set on a COMPLETED call.
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.models import CALL_TRANSITIONS, Call, CallStatus
from app.calls.repository import CallRepository
from app.calls.schemas import CallCreate, CallUpdate
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

_PLANNING_FIELDS = {"objective", "scheduled_at"}
_NON_NULLABLE = {"status"}


def _label(status: str) -> str:
    return status.replace("_", " ").lower()


async def get_call(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> Call:
    call = await CallRepository(session).get(principal.company_id, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    return call


async def create_call(
    session: AsyncSession, principal: Principal, data: CallCreate, *, ip: str | None
) -> Call:
    contact = await ContactRepository(session).get(principal.company_id, data.contact_id)
    if contact is None:
        raise InvalidReferenceError("Contact not found", details=[{"field": "contact_id"}])
    user_id = data.user_id if "user_id" in data.model_fields_set else principal.user_id
    await ensure_member(session, principal.company_id, user_id, field="user_id")

    call = Call(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        contact_id=contact.id,
        user_id=user_id,
        objective=data.objective,
        scheduled_at=data.scheduled_at,
        status=CallStatus.PLANNED.value,
    )
    session.add(call)
    await session.flush()
    when = f" for {data.scheduled_at:%d %b %Y %H:%M %Z}" if data.scheduled_at else ""
    objective = f": {data.objective}" if data.objective else ""
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=contact.id,
        category=TimelineCategory.CALL,
        event_type=TimelineEventType.CALL_PLANNED,
        summary=f"Call planned{when}{objective}",
        actor_user_id=principal.user_id,
        to_status=CallStatus.PLANNED.value,
        call_id=call.id,
    )
    audit.record(
        session,
        "call.created",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="call",
        entity_id=call.id,
        ip=ip,
    )
    await session.commit()
    return await get_call(session, principal, call.id)


def _apply_status(call: Call, new_status: CallStatus, now: datetime) -> None:
    current = CallStatus(call.status)
    if new_status == current:
        return
    if new_status not in CALL_TRANSITIONS[current]:
        raise InvalidStateError(
            f"Cannot change call status from {current.value} to {new_status.value}",
            code="invalid_status_transition",
        )
    if new_status == CallStatus.IN_PROGRESS:
        call.started_at = now
    elif current == CallStatus.IN_PROGRESS:
        call.ended_at = now
        if call.started_at is not None:
            call.duration_seconds = max(0, int((now - call.started_at).total_seconds()))
    call.status = new_status.value


async def update_call(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    data: CallUpdate,
    *,
    ip: str | None,
) -> Call:
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can update this call")

    changes: dict[str, Any] = {
        k: v
        for k, v in data.model_dump(exclude_unset=True).items()
        if not (v is None and k in _NON_NULLABLE)
    }
    old_status = call.status

    if "status" in changes:
        _apply_status(call, changes.pop("status"), datetime.now(UTC))

    if _PLANNING_FIELDS & changes.keys() and call.status != CallStatus.PLANNED:
        raise InvalidStateError("Objective and schedule can only change while the call is planned")
    if changes.get("outcome") is not None and call.status != CallStatus.COMPLETED:
        raise InvalidStateError("An outcome can only be recorded for a completed call")
    if "user_id" in changes:
        await ensure_member(session, principal.company_id, changes["user_id"], field="user_id")

    for field, value in changes.items():
        setattr(call, field, value.value if isinstance(value, enum.Enum) else value)

    if call.status != old_status:
        summary = f"Call {_label(call.status)}"
        if call.outcome and call.status == CallStatus.COMPLETED:
            summary += f" - outcome: {_label(call.outcome)}"
        timeline.record(
            session,
            company_id=principal.company_id,
            contact_id=call.contact_id,
            category=TimelineCategory.CALL,
            event_type=TimelineEventType.CALL_STATUS_CHANGED,
            summary=summary,
            actor_user_id=principal.user_id,
            from_status=old_status,
            to_status=call.status,
            call_id=call.id,
        )
    audit.record(
        session,
        "call.updated",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="call",
        entity_id=call.id,
        ip=ip,
        details={"fields": sorted(data.model_fields_set)},
    )
    await session.commit()
    session.expire(call)
    return await get_call(session, principal, call.id)
