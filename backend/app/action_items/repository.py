import uuid
from datetime import datetime

from sqlalchemy import Select
from sqlalchemy.orm import joinedload

from app.action_items.models import ActionItem, ActionItemKind, ActionItemStatus
from app.common.repository import TenantRepository


class ActionItemRepository(TenantRepository[ActionItem]):
    model = ActionItem

    def scoped(self, company_id: uuid.UUID) -> Select[tuple[ActionItem]]:
        return super().scoped(company_id).options(joinedload(ActionItem.contact))

    def search(
        self,
        company_id: uuid.UUID,
        *,
        statuses: list[ActionItemStatus] | None,
        kind: ActionItemKind | None,
        assignee_user_id: uuid.UUID | None,
        contact_id: uuid.UUID | None,
        call_id: uuid.UUID | None,
        confirmed: bool | None,
        due_before: datetime | None,
    ) -> Select[tuple[ActionItem]]:
        stmt = self.scoped(company_id)
        if statuses:
            stmt = stmt.where(ActionItem.status.in_([s.value for s in statuses]))
        if kind:
            stmt = stmt.where(ActionItem.kind == kind.value)
        if assignee_user_id:
            stmt = stmt.where(ActionItem.assignee_user_id == assignee_user_id)
        if contact_id:
            stmt = stmt.where(ActionItem.contact_id == contact_id)
        if call_id:
            stmt = stmt.where(ActionItem.call_id == call_id)
        if confirmed is not None:
            stmt = stmt.where(
                ActionItem.confirmed_at.is_not(None)
                if confirmed
                else ActionItem.confirmed_at.is_(None)
            )
        if due_before:
            stmt = stmt.where(ActionItem.due_at < due_before)
        return stmt.order_by(
            ActionItem.due_at.asc().nulls_last(), ActionItem.created_at.desc(), ActionItem.id
        )
