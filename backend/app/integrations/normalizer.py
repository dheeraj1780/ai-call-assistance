"""Normalised conversation events.

Every provider payload (WhatsApp webhook, Teams change notification + Graph message, Plivo
callback, Teams media gateway event, mock simulator) is converted into ``ConversationEvent``
objects here or in the provider adapter. The conversation service, the timeline and the AI
consume ONLY these objects - never provider payloads.

Real-time audio is normalised separately into ``MediaFrame``/``MediaControl``
(app/telephony/provider.py) because it must never be stored; transcript results become
TRANSCRIPT_PARTIAL/TRANSCRIPT_FINAL events on the live hub (app/live/session.py).
"""

import html
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.integrations.domain import Capability, Channel, ConversationEventType, Provider

MAX_TEXT = 10_000


@dataclass(frozen=True)
class ParticipantRef:
    role: str  # ParticipantRole value
    external_id: str
    display_name: str | None = None
    phone: str | None = None  # normalised digits (E.164 without '+') when known
    email: str | None = None


@dataclass(frozen=True)
class NormalizedMessage:
    external_message_id: str
    direction: str  # Direction value
    sender_type: str  # SenderType value
    message_type: str  # MessageType value
    text: str
    status: str  # MessageStatus value


@dataclass(frozen=True)
class ConversationEvent:
    type: ConversationEventType
    provider: Provider
    channel: Channel
    capability: Capability
    # Stable id used for idempotency (provider message id, provider event id, ...).
    external_event_id: str
    # Provider thread id: WhatsApp customer wa_id, Teams chat id, provider call id.
    external_session_id: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    participant: ParticipantRef | None = None
    message: NormalizedMessage | None = None
    call_id: uuid.UUID | None = None
    # Small non-content attributes (status, error code). Never message text.
    data: dict[str, str | int | None] = field(default_factory=dict)


_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(br|/p|/div)\s*/?>", re.IGNORECASE)


def html_to_text(content: str) -> str:
    """Teams message bodies are HTML; keep readable text only (no markup is ever rendered)."""
    text = _BREAKS.sub("\n", content)
    text = _TAGS.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()[:MAX_TEXT]


def clip(text: str) -> str:
    return text.strip()[:MAX_TEXT]
