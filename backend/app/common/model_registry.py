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
from app.intel.models import CallNote, CopilotInsight
from app.jobs.models import Job
from app.knowledge.models import KnowledgeChunk, KnowledgeDocument
from app.live.models import TranscriptSegment
from app.postcall.models import CallSummary, FollowUpDraft
from app.telephony.models import CallRoute, TelephonyWebhookEvent
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
    "CallNote",
    "CallRoute",
    "CallSummary",
    "Company",
    "CompanyMember",
    "Contact",
    "ContactNote",
    "CopilotInsight",
    "FollowUpDraft",
    "Job",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "RefreshToken",
    "TelephonyWebhookEvent",
    "TimelineEvent",
    "TranscriptSegment",
    "User",
]
