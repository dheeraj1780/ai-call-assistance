"""Tenant repositories.

Every method takes the company id explicitly and filters on it. The company id must come
from the authenticated principal, never from request input. RLS is the backstop.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenants.models import Company, CompanyMember
from app.users.models import User


class CompanyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, company_id: uuid.UUID) -> Company | None:
        company: Company | None = await self.session.scalar(
            select(Company).where(Company.id == company_id)
        )
        return company


class MemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_user(self, user_id: uuid.UUID) -> CompanyMember | None:
        member: CompanyMember | None = await self.session.scalar(
            select(CompanyMember).where(CompanyMember.user_id == user_id)
        )
        return member

    async def list_for_company(
        self, company_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[Sequence[Row[tuple[CompanyMember, User]]], int]:
        total = await self.session.scalar(
            select(func.count())
            .select_from(CompanyMember)
            .where(CompanyMember.company_id == company_id)
        )
        rows = (
            await self.session.execute(
                select(CompanyMember, User)
                .join(User, User.id == CompanyMember.user_id)
                .where(CompanyMember.company_id == company_id)
                .order_by(CompanyMember.created_at, CompanyMember.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return rows, total or 0
