"""Lightweight dashboard (no analytics): today's work at a glance for the signed-in user."""

import zoneinfo
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.action_items.models import ActionItem, ActionItemKind, ActionItemStatus
from app.action_items.schemas import ActionItemOut
from app.auth.dependencies import Principal, get_principal
from app.calls.models import Call, CallOutcome, CallStatus
from app.calls.schemas import CallOut
from app.common.db import get_db_session
from app.common.errors import AppError
from app.contacts.models import Contact
from app.contacts.schemas import ContactSummary
from app.postcall.models import DraftStatus, FollowUpDraft
from app.timeline.models import TimelineEvent
from app.timeline.schemas import TimelineEventOut

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class DashboardOut(BaseModel):
    calls_today: int
    calls_this_week: int
    open_action_items: int
    upcoming_follow_ups: list[ActionItemOut]
    recent_customers: list[ContactSummary]
    calls_needing_follow_up: list[CallOut]
    recent_activity: list[dict[str, Any]]


@router.get("", response_model=DashboardOut)
async def dashboard(
    tz: str = Query(default="Asia/Kolkata", max_length=64),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> DashboardOut:
    try:
        zone = zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise AppError("Unknown time zone", code="invalid_timezone") from None
    now = datetime.now(zone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    week_start = (
        (now - timedelta(days=now.weekday()))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    cid, uid = principal.company_id, principal.user_id
    started = func.coalesce(Call.started_at, Call.created_at)
    mine_calls = and_(
        Call.company_id == cid, Call.user_id == uid, Call.status != CallStatus.PLANNED.value
    )

    calls_today = (
        await session.scalar(select(func.count()).where(mine_calls, started >= day_start)) or 0
    )
    calls_week = (
        await session.scalar(select(func.count()).where(mine_calls, started >= week_start)) or 0
    )
    open_items = (
        await session.scalar(
            select(func.count()).where(
                ActionItem.company_id == cid,
                ActionItem.assignee_user_id == uid,
                ActionItem.status.in_(
                    [ActionItemStatus.OPEN.value, ActionItemStatus.IN_PROGRESS.value]
                ),
            )
        )
        or 0
    )
    upcoming = (
        (
            await session.scalars(
                select(ActionItem)
                .options(joinedload(ActionItem.contact))
                .where(
                    ActionItem.company_id == cid,
                    ActionItem.assignee_user_id == uid,
                    ActionItem.kind.in_(
                        [ActionItemKind.FOLLOW_UP.value, ActionItemKind.APPOINTMENT.value]
                    ),
                    ActionItem.status.in_(
                        [ActionItemStatus.OPEN.value, ActionItemStatus.IN_PROGRESS.value]
                    ),
                    ActionItem.due_at.is_not(None),
                    ActionItem.due_at <= datetime.now(UTC) + timedelta(days=7),
                )
                .order_by(ActionItem.due_at)
                .limit(10)
            )
        )
        .unique()
        .all()
    )
    recent_customers = (
        await session.scalars(
            select(Contact)
            .where(Contact.company_id == cid)
            .order_by(Contact.updated_at.desc())
            .limit(5)
        )
    ).all()
    # Completed calls in the last 14 days that still need attention: follow-up outcome, or a
    # drafted follow-up nobody has approved/copied yet.
    pending_draft = (
        select(FollowUpDraft.id)
        .where(
            FollowUpDraft.company_id == cid,
            FollowUpDraft.call_id == Call.id,
            FollowUpDraft.status == DraftStatus.DRAFT.value,
        )
        .exists()
    )
    needing = (
        (
            await session.scalars(
                select(Call)
                .options(joinedload(Call.contact))
                .where(
                    Call.company_id == cid,
                    Call.user_id == uid,
                    Call.status == CallStatus.COMPLETED.value,
                    Call.ended_at >= datetime.now(UTC) - timedelta(days=14),
                    or_(Call.outcome == CallOutcome.FOLLOW_UP_REQUIRED.value, pending_draft),
                )
                .order_by(Call.ended_at.desc())
                .limit(10)
            )
        )
        .unique()
        .all()
    )
    activity = (
        await session.execute(
            select(TimelineEvent, Contact.name)
            .join(
                Contact,
                and_(
                    Contact.company_id == TimelineEvent.company_id,
                    Contact.id == TimelineEvent.contact_id,
                ),
            )
            .where(TimelineEvent.company_id == cid)
            .order_by(TimelineEvent.occurred_at.desc())
            .limit(10)
        )
    ).all()
    return DashboardOut(
        calls_today=calls_today,
        calls_this_week=calls_week,
        open_action_items=open_items,
        upcoming_follow_ups=[ActionItemOut.model_validate(i) for i in upcoming],
        recent_customers=[ContactSummary.model_validate(c) for c in recent_customers],
        calls_needing_follow_up=[CallOut.model_validate(c) for c in needing],
        recent_activity=[
            {**TimelineEventOut.model_validate(e).model_dump(mode="json"), "contact_name": name}
            for e, name in activity
        ],
    )
