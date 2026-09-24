import uuid

from sqlalchemy import Select, or_

from app.common.repository import TenantRepository, like_pattern
from app.contacts.models import Contact, ContactNote, ContactStatus

CONTACT_SORTS = {
    "-updated_at": (Contact.updated_at.desc(), Contact.id.desc()),
    "-created_at": (Contact.created_at.desc(), Contact.id.desc()),
    "name": (Contact.name.asc(), Contact.id.asc()),
}


class ContactRepository(TenantRepository[Contact]):
    model = Contact

    def search(
        self,
        company_id: uuid.UUID,
        *,
        q: str | None,
        status: ContactStatus | None,
        tag: str | None,
        owner_user_id: uuid.UUID | None,
        sort: str,
    ) -> Select[tuple[Contact]]:
        stmt = self.scoped(company_id)
        if q:
            pattern = like_pattern(q.strip())
            stmt = stmt.where(
                or_(
                    Contact.name.ilike(pattern, escape="\\"),
                    Contact.organization.ilike(pattern, escape="\\"),
                    Contact.email.ilike(pattern, escape="\\"),
                    Contact.phone.ilike(pattern, escape="\\"),
                )
            )
        if status:
            stmt = stmt.where(Contact.status == status.value)
        if tag:
            stmt = stmt.where(Contact.tags.contains([tag.strip().lower()]))
        if owner_user_id:
            stmt = stmt.where(Contact.owner_user_id == owner_user_id)
        return stmt.order_by(*CONTACT_SORTS[sort])


class ContactNoteRepository(TenantRepository[ContactNote]):
    model = ContactNote

    def for_contact(self, company_id: uuid.UUID, contact_id: uuid.UUID) -> Select[tuple[ContactNote]]:
        return (
            self.scoped(company_id)
            .where(ContactNote.contact_id == contact_id)
            .order_by(ContactNote.created_at.desc(), ContactNote.id.desc())
        )
