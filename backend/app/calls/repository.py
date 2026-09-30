import uuid
from datetime import datetime

from sqlalchemy import Select, func
from sqlalchemy.orm import joinedload

from app.calls.models import Call, CallStatus
from app.common.repository import TenantRepository


class CallRepository(TenantRepository[Call]):
    model = Call

    def scoped(self, company_id: uuid.UUID) -> Select[tuple[Call]]:
        return super().scoped(company_id).options(joinedload(Call.contact))

    async def get_locked(self, company_id: uuid.UUID, entity_id: uuid.UUID) -> Call | None:
        """The call under ``SELECT ... FOR UPDATE`` (lifecycle commands serialise per call)."""
        result: Call | None = await self.session.scalar(
            self.scoped(company_id).where(Call.id == entity_id).with_for_update(of=Call)
        )
        return result

    def search(
        self,
        company_id: uuid.UUID,
        *,
        contact_id: uuid.UUID | None,
        status: CallStatus | None,
        user_id: uuid.UUID | None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Select[tuple[Call]]:
        stmt = self.scoped(company_id)
        when = func.coalesce(Call.started_at, Call.scheduled_at, Call.created_at)
        if since:
            stmt = stmt.where(when >= since)
        if until:
            stmt = stmt.where(when < until)
        if contact_id:
            stmt = stmt.where(Call.contact_id == contact_id)
        if status:
            stmt = stmt.where(Call.status == status.value)
        if user_id:
            stmt = stmt.where(Call.user_id == user_id)
        return stmt.order_by(Call.created_at.desc(), Call.id.desc())
