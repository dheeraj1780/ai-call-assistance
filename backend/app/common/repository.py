"""Base repository for tenant-owned models."""

import uuid
from typing import Any, ClassVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards in user input and wrap it for a substring match."""
    escaped = term.replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
    return f"%{escaped}%"


class TenantRepository[M]:
    """Every query is filtered by company_id explicitly (RLS is the backstop)."""

    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def scoped(self, company_id: uuid.UUID) -> Select[tuple[M]]:
        return select(self.model).where(self.model.company_id == company_id)

    async def get(self, company_id: uuid.UUID, entity_id: uuid.UUID) -> M | None:
        result: M | None = await self.session.scalar(
            self.scoped(company_id).where(self.model.id == entity_id)
        )
        return result

    async def page(
        self, stmt: Select[tuple[M]], *, limit: int, offset: int
    ) -> tuple[list[M], int]:
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (await self.session.scalars(stmt.limit(limit).offset(offset))).unique().all()
        return list(rows), total or 0
