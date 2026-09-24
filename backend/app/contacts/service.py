"""Contact and contact-note use cases.

Authorization (MVP):
- Every member can read all of the company's contacts and notes, create contacts and add notes.
- Editing a contact (including reassigning its owner): OWNER/ADMIN, the contact's owner, or
  anyone if the contact is unowned.
- Deleting a contact: OWNER/ADMIN or the contact's owner.
- Editing/deleting a note: OWNER/ADMIN or the note's author.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.errors import ForbiddenError, NotFoundError
from app.contacts.models import Contact, ContactNote, ContactStatus
from app.contacts.repository import ContactNoteRepository, ContactRepository
from app.contacts.schemas import ContactCreate, ContactUpdate, NoteCreate, NoteUpdate
from app.tenants.membership import ensure_member
from app.timeline import service as timeline
from app.timeline.models import TimelineCategory, TimelineEventType

# Fields that may not be cleared with an explicit null.
_NON_NULLABLE = {"name", "status", "tags"}


def can_edit_contact(principal: Principal, contact: Contact) -> bool:
    return (
        principal.is_admin
        or contact.owner_user_id is None
        or contact.owner_user_id == principal.user_id
    )


async def get_contact(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID
) -> Contact:
    contact = await ContactRepository(session).get(principal.company_id, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    return contact


async def create_contact(
    session: AsyncSession, principal: Principal, data: ContactCreate, *, ip: str | None
) -> Contact:
    owner = data.owner_user_id if "owner_user_id" in data.model_fields_set else principal.user_id
    await ensure_member(session, principal.company_id, owner, field="owner_user_id")
    contact = Contact(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        **data.model_dump(exclude={"owner_user_id"}),
        owner_user_id=owner,
    )
    session.add(contact)
    await session.flush()
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=contact.id,
        category=TimelineCategory.CONTACT,
        event_type=TimelineEventType.CONTACT_CREATED,
        summary=f"Contact created with status {contact.status}",
        actor_user_id=principal.user_id,
        to_status=contact.status,
    )
    audit.record(
        session,
        "contact.created",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="contact",
        entity_id=contact.id,
        ip=ip,
    )
    await session.commit()
    return contact


async def update_contact(
    session: AsyncSession,
    principal: Principal,
    contact_id: uuid.UUID,
    data: ContactUpdate,
    *,
    ip: str | None,
) -> Contact:
    contact = await get_contact(session, principal, contact_id)
    if not can_edit_contact(principal, contact):
        raise ForbiddenError("Only the contact's owner or an admin can edit this contact")

    changes: dict[str, Any] = {
        field: value
        for field, value in data.model_dump(exclude_unset=True).items()
        if not (value is None and field in _NON_NULLABLE)
    }
    if "owner_user_id" in changes:
        await ensure_member(
            session, principal.company_id, changes["owner_user_id"], field="owner_user_id"
        )

    old_status = contact.status
    for field, value in changes.items():
        setattr(contact, field, value.value if isinstance(value, ContactStatus) else value)

    if "status" in changes and contact.status != old_status:
        timeline.record(
            session,
            company_id=principal.company_id,
            contact_id=contact.id,
            category=TimelineCategory.STATUS_CHANGE,
            event_type=TimelineEventType.STATUS_CHANGED,
            summary=f"Status changed from {old_status} to {contact.status}",
            actor_user_id=principal.user_id,
            from_status=old_status,
            to_status=contact.status,
        )
    if changes:
        audit.record(
            session,
            "contact.updated",
            company_id=principal.company_id,
            actor_user_id=principal.user_id,
            entity_type="contact",
            entity_id=contact.id,
            ip=ip,
            details={"fields": sorted(changes)},
        )
    await session.commit()
    await session.refresh(contact)
    return contact


async def delete_contact(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID, *, ip: str | None
) -> None:
    """Hard delete; notes, calls, action items and timeline events cascade."""
    contact = await get_contact(session, principal, contact_id)
    if not (principal.is_admin or contact.owner_user_id == principal.user_id):
        raise ForbiddenError("Only the contact's owner or an admin can delete this contact")
    await session.delete(contact)
    audit.record(
        session,
        "contact.deleted",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="contact",
        entity_id=contact_id,
        ip=ip,
    )
    await session.commit()


# ---- Notes -----------------------------------------------------------------------------


async def _get_note(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID, note_id: uuid.UUID
) -> ContactNote:
    note = await ContactNoteRepository(session).get(principal.company_id, note_id)
    if note is None or note.contact_id != contact_id:
        raise NotFoundError("Note not found")
    return note


def _require_note_author(principal: Principal, note: ContactNote) -> None:
    if not (principal.is_admin or note.author_user_id == principal.user_id):
        raise ForbiddenError("Only the note's author or an admin can change this note")


async def add_note(
    session: AsyncSession, principal: Principal, contact_id: uuid.UUID, data: NoteCreate
) -> ContactNote:
    contact = await get_contact(session, principal, contact_id)
    note = ContactNote(
        id=uuid.uuid4(),
        company_id=principal.company_id,
        contact_id=contact.id,
        author_user_id=principal.user_id,
        body=data.body,
    )
    session.add(note)
    await session.flush()
    timeline.record(
        session,
        company_id=principal.company_id,
        contact_id=contact.id,
        category=TimelineCategory.NOTE,
        event_type=TimelineEventType.NOTE_ADDED,
        summary=note.body,
        actor_user_id=principal.user_id,
        note_id=note.id,
    )
    await session.commit()
    await session.refresh(note)
    return note


async def update_note(
    session: AsyncSession,
    principal: Principal,
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    data: NoteUpdate,
) -> ContactNote:
    note = await _get_note(session, principal, contact_id, note_id)
    _require_note_author(principal, note)
    note.body = data.body
    await timeline.update_note_summary(session, principal.company_id, note.id, note.body)
    await session.commit()
    await session.refresh(note)
    return note


async def delete_note(
    session: AsyncSession,
    principal: Principal,
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    *,
    ip: str | None,
) -> None:
    note = await _get_note(session, principal, contact_id, note_id)
    _require_note_author(principal, note)
    await session.delete(note)
    audit.record(
        session,
        "contact_note.deleted",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="contact_note",
        entity_id=note_id,
        ip=ip,
    )
    await session.commit()
