import uuid

from sqlalchemy import Select
from sqlalchemy.orm import joinedload

from app.calls.models import Call, CallStatus
from app.common.repository import TenantRepository


class CallRepository(TenantRepository[Call]):
    model = Call

    def scoped(self, company_id: uuid.UUID) -> Select[tuple[Call]]:
        return super().scoped(company_id).options(joinedload(Call.contact))

    def search(
        self,
        company_id: uuid.UUID,
        *,
        contact_id: uuid.UUID | None,
        status: CallStatus | None,
        user_id: uuid.UUID | None,
    ) -> Select[tuple[Call]]:
        stmt = self.scoped(company_id)
        if contact_id:
            stmt = stmt.where(Call.contact_id == contact_id)
        if status:
            stmt = stmt.where(Call.status == status.value)
        if user_id:
            stmt = stmt.where(Call.user_id == user_id)
        return stmt.order_by(Call.created_at.desc(), Call.id.desc())
