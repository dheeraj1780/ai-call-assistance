"""Provider / channel / capability vocabulary shared by every integration.

Separation of concerns:
- PROVIDER: the external service we talk to (Microsoft Teams, WhatsApp, Plivo, Google Meet).
- CHANNEL: how the customer experiences the conversation (PHONE, TEAMS, WHATSAPP, GOOGLE_MEET).
- CAPABILITY: what an integration can do (MESSAGE, REAL_TIME_CALL, PHONE_CALL, ...).
- CONVERSATION: the provider-independent record (app/conversations).
- AI COPILOT: one engine for every channel (app/copilot, app/conversations/assist.py).
"""

import enum


class Provider(enum.StrEnum):
    MICROSOFT_TEAMS = "MICROSOFT_TEAMS"
    WHATSAPP = "WHATSAPP"
    PLIVO = "PLIVO"
    GOOGLE_MEET = "GOOGLE_MEET"


class Channel(enum.StrEnum):
    PHONE = "PHONE"
    TEAMS = "TEAMS"
    WHATSAPP = "WHATSAPP"
    GOOGLE_MEET = "GOOGLE_MEET"


class Capability(enum.StrEnum):
    MESSAGE = "MESSAGE"
    REAL_TIME_CALL = "REAL_TIME_CALL"
    PHONE_CALL = "PHONE_CALL"
    MEDIA_STREAM = "MEDIA_STREAM"
    WHATSAPP_VOICE_CALL = "WHATSAPP_VOICE_CALL"
    # Google Meet: read meeting-space metadata (is a conference active?) by meeting link/code.
    MEETING_LOOKUP = "MEETING_LOOKUP"
    # Declared for the provider interface; no provider implements it yet.
    CONTACT_SYNC = "CONTACT_SYNC"
    WEBHOOK = "WEBHOOK"


class IntegrationStatus(enum.StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    VALIDATED = "VALIDATED"
    ENABLED = "ENABLED"
    ERROR = "ERROR"


class CapabilityState(enum.StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    VALIDATED = "VALIDATED"
    ENABLED = "ENABLED"
    ERROR = "ERROR"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class IntegrationMode(enum.StrEnum):
    LIVE = "LIVE"
    # Deterministic, credential-free adapters for development and tests. Nothing is sent to
    # a real provider; the UI labels the integration "Mock mode".
    MOCK = "MOCK"


PROVIDER_SLUGS: dict[str, Provider] = {
    "microsoft-teams": Provider.MICROSOFT_TEAMS,
    "whatsapp": Provider.WHATSAPP,
    "plivo": Provider.PLIVO,
    "google-meet": Provider.GOOGLE_MEET,
}
SLUG_OF: dict[Provider, str] = {v: k for k, v in PROVIDER_SLUGS.items()}

CHANNEL_OF: dict[Provider, Channel] = {
    Provider.MICROSOFT_TEAMS: Channel.TEAMS,
    Provider.WHATSAPP: Channel.WHATSAPP,
    Provider.PLIVO: Channel.PHONE,
    Provider.GOOGLE_MEET: Channel.GOOGLE_MEET,
}


class ConversationEventType(enum.StrEnum):
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    MESSAGE_SENT = "MESSAGE_SENT"
    MESSAGE_STATUS_UPDATED = "MESSAGE_STATUS_UPDATED"
    CALL_STARTED = "CALL_STARTED"
    CALL_CONNECTED = "CALL_CONNECTED"
    CALL_ENDED = "CALL_ENDED"
    AUDIO_STREAM_STARTED = "AUDIO_STREAM_STARTED"
    AUDIO_STREAM_ENDED = "AUDIO_STREAM_ENDED"
    TRANSCRIPT_PARTIAL = "TRANSCRIPT_PARTIAL"
    TRANSCRIPT_FINAL = "TRANSCRIPT_FINAL"
    CUSTOMER_JOINED = "CUSTOMER_JOINED"
    SALESPERSON_JOINED = "SALESPERSON_JOINED"
    RECORDING_STATUS_CHANGED = "RECORDING_STATUS_CHANGED"
