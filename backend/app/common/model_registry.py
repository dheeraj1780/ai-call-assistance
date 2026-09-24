"""Imports every ORM model so Base.metadata is complete (used by Alembic and tests)."""

from app.action_items.models import ActionItem
from app.agendas.models import AgendaItem
from app.ai.models import AIUsageRecord
from app.audit.models import AuditLog
from app.auth.models import AuthSession, RefreshToken
from app.calendar.models import CalendarConnection, CalendarEvent
from app.calls.models import Call
from app.common.models import Base
from app.contacts.models import Contact, ContactNote
from app.jobs.models import Job
from app.tenants.models import Company, CompanyMember
from app.timeline.models import TimelineEvent
from app.users.models import User

__all__ = [
    "AIUsageRecord",
    "ActionItem",
    "AgendaItem",
    "AuditLog",
    "AuthSession",
    "Base",
    "CalendarConnection",
    "CalendarEvent",
    "Call",
    "Company",
    "CompanyMember",
    "Contact",
    "ContactNote",
    "Job",
    "RefreshToken",
    "TimelineEvent",
    "User",
]
