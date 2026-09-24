"""Membership checks used by other modules when assigning owners/assignees."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import InvalidReferenceError
from app.tenants.models import CompanyMember


async def ensure_member(
    session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID | None, *, field: str
) -> None:
    """Raise 422 unless user_id is None or a member of company_id."""
    if user_id is None:
        return
    exists = await session.scalar(
        select(CompanyMember.id).where(
            CompanyMember.company_id == company_id, CompanyMember.user_id == user_id
        )
    )
    if exists is None:
        raise InvalidReferenceError(
            f"{field} must be a member of your company", details=[{"field": field}]
        )
