"""Imports every ORM model so Base.metadata is complete (used by Alembic and tests)."""

from app.action_items.models import ActionItem
from app.audit.models import AuditLog
from app.auth.models import AuthSession, RefreshToken
from app.calls.models import Call
from app.common.models import Base
from app.contacts.models import Contact, ContactNote
from app.tenants.models import Company, CompanyMember
from app.timeline.models import TimelineEvent
from app.users.models import User

__all__ = [
    "ActionItem",
    "AuditLog",
    "AuthSession",
    "Base",
    "Call",
    "Company",
    "CompanyMember",
    "Contact",
    "ContactNote",
    "RefreshToken",
    "TimelineEvent",
    "User",
]
