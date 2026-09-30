"""Static description of every provider: configuration fields, capabilities and the
requirements the backend checks before a capability can be enabled.

The frontend renders forms, capability lists and "what is missing" checklists ONLY from this
data (via the API) - it contains no provider-specific business logic.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.common.config import Settings
from app.integrations.domain import Capability, Channel, IntegrationMode, Provider

FieldKind = Literal["text", "secret", "select"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: FieldKind
    required: bool
    help: str
    pattern: str | None = None
    max_length: int = 256
    options: tuple[tuple[str, str], ...] = ()
    default: str | None = None
    # Capabilities that need this field (empty = all capabilities).
    capabilities: tuple[Capability, ...] = ()


@dataclass(frozen=True)
class CapabilitySpec:
    capability: Capability
    label: str
    description: str
    channel: Channel
    available: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ProviderSpec:
    provider: Provider
    slug: str
    name: str
    subtitle: str
    channel: Channel
    fields: tuple[FieldSpec, ...]
    capabilities: tuple[CapabilitySpec, ...]
    docs: str
    # Config field that identifies the provider account (unique across workspaces).
    external_account_field: str | None = None

    def field(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)

    def capability(self, cap: Capability) -> CapabilitySpec | None:
        return next((c for c in self.capabilities if c.capability == cap), None)


@dataclass(frozen=True)
class Requirement:
    key: str
    label: str
    ok: bool
    scope: Literal["company", "server", "user", "provider"]
    capability: Capability | None = None
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "ok": self.ok,
            "scope": self.scope,
            "capability": self.capability.value if self.capability else None,
            "hint": self.hint,
        }


GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
DIGITS = r"^[0-9]{5,32}$"
E164 = r"^\+[1-9][0-9]{6,14}$"

# Audio is NEVER recorded or stored, whatever is chosen here: this only decides whether the TEXT
# derived from a Teams meeting (transcript, AI notes, summary) may be kept. Microsoft's Graph terms
# for updateRecordingStatus: an application "may NOT ... record or otherwise persist media content
# ... or data derived from that media content" without first calling updateRecordingStatus and
# receiving success - which requires Teams policy-based recording. Hence live-only by default.
TEAMS_PERSISTENCE_OPTIONS = (
    (
        "TRANSIENT",
        "Live-only (default): transcript and AI notes are shown during the meeting and not kept "
        "afterwards. Audio is never recorded.",
    ),
    (
        "RECORDING_DECLARED",
        "Keep transcript text & AI notes (retention policy): the bot sets Teams' recording status "
        "first, so all participants see the recording indicator; needs Teams policy-based "
        "recording. Audio is still never recorded.",
    ),
)

TEAMS = ProviderSpec(
    provider=Provider.MICROSOFT_TEAMS,
    slug="microsoft-teams",
    name="Microsoft Teams",
    subtitle="Microsoft 365 / Teams",
    channel=Channel.TEAMS,
    docs="docs/integrations/microsoft-teams.md",
    fields=(
        FieldSpec(
            "tenant_id",
            "Microsoft Entra tenant (directory) ID",
            "text",
            True,
            "Your organisation's directory ID (Entra admin center > Overview).",
            pattern=GUID,
            max_length=36,
        ),
        FieldSpec(
            "client_id",
            "Application (client) ID (optional)",
            "text",
            False,
            "Only if you use your own app registration. Leave empty to use the platform app "
            "(MICROSOFT_CLIENT_ID).",
            pattern=GUID,
            max_length=36,
        ),
        FieldSpec(
            "client_secret",
            "Client secret (optional)",
            "secret",
            False,
            "Secret of your own app registration. Stored encrypted and never shown again.",
            max_length=512,
        ),
        FieldSpec(
            "call_persistence",
            "Teams meeting text retention",
            "select",
            True,
            "Audio is never recorded. Microsoft requires a bot to set the call's recording status "
            "before keeping anything derived from meeting media (such as a transcript), so "
            "live-only is the default.",
            options=TEAMS_PERSISTENCE_OPTIONS,
            default="TRANSIENT",
            capabilities=(Capability.REAL_TIME_CALL,),
        ),
    ),
    capabilities=(
        CapabilitySpec(
            Capability.MESSAGE,
            "Messages",
            "Read Teams chats you link to a customer, AI reply suggestions, send after review.",
            Channel.TEAMS,
        ),
        CapabilitySpec(
            Capability.REAL_TIME_CALL,
            "Real-time Call Copilot",
            "A copilot bot joins your Teams meeting and assists you live (via the Teams media "
            "gateway).",
            Channel.TEAMS,
        ),
    ),
)

WHATSAPP = ProviderSpec(
    provider=Provider.WHATSAPP,
    slug="whatsapp",
    name="WhatsApp Business",
    subtitle="WhatsApp Business Platform (Cloud API)",
    channel=Channel.WHATSAPP,
    docs="docs/integrations/whatsapp.md",
    external_account_field="phone_number_id",
    fields=(
        FieldSpec(
            "phone_number_id",
            "Phone number ID",
            "text",
            True,
            "Meta App Dashboard > WhatsApp > API Setup (not the phone number itself).",
            pattern=DIGITS,
            max_length=32,
        ),
        FieldSpec(
            "business_account_id",
            "WhatsApp Business Account ID",
            "text",
            True,
            "Meta App Dashboard > WhatsApp > API Setup.",
            pattern=DIGITS,
            max_length=32,
        ),
        FieldSpec(
            "app_id",
            "Meta App ID (optional)",
            "text",
            False,
            "For reference only.",
            pattern=DIGITS,
            max_length=32,
        ),
        FieldSpec(
            "access_token",
            "Access token",
            "secret",
            True,
            "A System User access token with whatsapp_business_messaging (and "
            "whatsapp_business_management) permission.",
            max_length=1024,
        ),
        FieldSpec(
            "app_secret",
            "App secret",
            "secret",
            True,
            "Meta App Dashboard > App settings > Basic. Used to verify webhook signatures.",
            max_length=256,
        ),
        FieldSpec(
            "verify_token",
            "Webhook verify token",
            "secret",
            True,
            "Any random string (16+ characters). Enter the same value in Meta's webhook setup.",
            pattern=r"^[A-Za-z0-9_\-.~]{16,128}$",
            max_length=128,
        ),
    ),
    capabilities=(
        CapabilitySpec(
            Capability.MESSAGE,
            "Messages",
            "Receive customer messages, AI reply suggestions, send after human review.",
            Channel.WHATSAPP,
        ),
        CapabilitySpec(
            Capability.WHATSAPP_VOICE_CALL,
            "Voice Calls",
            "WhatsApp voice calls with the live copilot.",
            Channel.WHATSAPP,
            available=False,
            unavailable_reason=(
                "Not available in this version. Meta's WhatsApp Business Calling API delivers "
                "call audio over WebRTC (or SIP) and needs a media service that terminates "
                "WebRTC/SIP, which is not implemented here; the number must also be eligible "
                "for calling (messaging-limit tier, country rules). Messaging is unaffected."
            ),
        ),
    ),
)

PLIVO = ProviderSpec(
    provider=Provider.PLIVO,
    slug="plivo",
    name="Plivo",
    subtitle="Phone / PSTN",
    channel=Channel.PHONE,
    docs="docs/integrations/plivo.md",
    external_account_field="auth_id",
    fields=(
        FieldSpec(
            "auth_id",
            "Auth ID",
            "text",
            True,
            "Plivo console > Overview.",
            pattern=r"^[A-Z0-9]{10,40}$",
            max_length=40,
        ),
        FieldSpec(
            "auth_token",
            "Auth token",
            "secret",
            True,
            "Plivo console > Overview. Also used to verify Plivo's webhook signatures.",
            max_length=128,
        ),
        FieldSpec(
            "phone_number",
            "Plivo phone number (caller ID)",
            "text",
            True,
            "A voice-enabled number on this Plivo account, in E.164 format (e.g. +9180...).",
            pattern=E164,
            max_length=16,
        ),
        FieldSpec(
            "application_id",
            "Plivo application ID (optional)",
            "text",
            False,
            "Only needed for inbound calls: the Plivo application attached to the number.",
            pattern=r"^[0-9]{5,32}$",
            max_length=32,
        ),
    ),
    capabilities=(
        CapabilitySpec(
            Capability.PHONE_CALL,
            "Phone Calls",
            "Plivo rings you, then connects the customer (outbound bridge).",
            Channel.PHONE,
        ),
        CapabilitySpec(
            Capability.MEDIA_STREAM,
            "Real-time Audio Streaming",
            "Streams both sides of the call to the live copilot (audio is never stored).",
            Channel.PHONE,
        ),
    ),
)

MEET_PERSISTENCE_OPTIONS = (
    (
        "TRANSIENT",
        "Live-only: transcript and AI notes are shown during the meeting and not kept afterwards. "
        "Audio is never recorded.",
    ),
    (
        "PERSISTED",
        "Keep transcript text & AI notes (retention policy): only with the participants' consent "
        "(Google shows them that an app is accessing the meeting). Audio is never recorded.",
    ),
)

GOOGLE_MEET = ProviderSpec(
    provider=Provider.GOOGLE_MEET,
    slug="google-meet",
    name="Google Meet",
    subtitle="Google Meet REST API + Meet Media API (Developer Preview)",
    channel=Channel.GOOGLE_MEET,
    docs="docs/integrations/google-meet.md",
    fields=(
        FieldSpec(
            "call_persistence",
            "Meeting text retention",
            "select",
            True,
            "Audio is never recorded. Keep the transcript text only when the participants have "
            "agreed; live-only shows assistance during the meeting.",
            options=MEET_PERSISTENCE_OPTIONS,
            default="TRANSIENT",
            capabilities=(Capability.REAL_TIME_CALL,),
        ),
    ),
    capabilities=(
        CapabilitySpec(
            Capability.REAL_TIME_CALL,
            "Real-time Meeting Copilot",
            "The copilot receives the live audio of a Google Meet you are in (Meet Media API, "
            "listen-only) and assists you in real time.",
            Channel.GOOGLE_MEET,
        ),
        CapabilitySpec(
            Capability.MEETING_LOOKUP,
            "Meeting lookup",
            "Check a Google Meet link: does the meeting exist and has it started?",
            Channel.GOOGLE_MEET,
        ),
    ),
)

SPECS: dict[Provider, ProviderSpec] = {s.provider: s for s in (TEAMS, WHATSAPP, PLIVO, GOOGLE_MEET)}


@dataclass
class RequirementContext:
    mode: IntegrationMode
    config: dict[str, Any]
    secret_keys: set[str]
    settings: Settings
    last_test: dict[str, Any] | None
    user_connected: bool | None = None
    extra: dict[str, bool] = field(default_factory=dict)

    def has(self, key: str) -> bool:
        return key in self.secret_keys or bool(self.config.get(key))


def requirements(spec: ProviderSpec, ctx: RequirementContext) -> list[Requirement]:
    """What is configured and what is still missing, per capability."""
    if ctx.mode == IntegrationMode.MOCK:
        return [
            Requirement(
                "mock_mode",
                "Mock mode: no credentials needed; nothing is sent to the provider",
                True,
                "company",
            )
        ]
    reqs = [
        Requirement(
            f"field:{f.key}",
            f.label,
            ctx.has(f.key),
            "company",
            f.capabilities[0] if len(f.capabilities) == 1 else None,
        )
        for f in spec.fields
        if f.required
    ]
    s = ctx.settings
    public_https = s.public_base_url.startswith("https://")
    stt_real = s.stt_provider != "mock"
    stt_hint = (
        "Live transcription of real call audio needs a streaming speech-to-text provider: set "
        "STT_PROVIDER=google (Google Cloud Speech-to-Text, Chirp 3) on the server. The mock STT "
        "cannot transcribe real audio."
    )
    missing_perms = (ctx.last_test or {}).get("missing_permissions") or []
    if spec.provider == Provider.MICROSOFT_TEAMS:
        has_client = ctx.has("client_id") or bool(s.microsoft_client_id)
        has_secret = ctx.has("client_secret") or s.microsoft_client_secret is not None
        reqs += [
            Requirement(
                "client_id",
                "Application (client) ID",
                has_client,
                "server" if not ctx.has("client_id") else "company",
                hint="Set MICROSOFT_CLIENT_ID on the server or enter your own app's ID.",
            ),
            Requirement(
                "client_secret",
                "Client secret",
                has_secret,
                "server" if not ctx.has("client_secret") else "company",
                hint="Set MICROSOFT_CLIENT_SECRET on the server or enter your own app's secret.",
            ),
            Requirement(
                "oauth",
                "Microsoft sign-in (OAuth) configured",
                has_client and has_secret and ctx.has("tenant_id"),
                "company",
            ),
            Requirement(
                "public_url",
                "Public HTTPS URL for Microsoft change notifications (PUBLIC_BASE_URL)",
                public_https,
                "server",
                Capability.MESSAGE,
                hint="Microsoft only delivers notifications to a public https URL.",
            ),
            Requirement(
                "user_connection",
                "Your Teams account connected",
                bool(ctx.user_connected),
                "user",
                Capability.MESSAGE,
                hint="Each salesperson connects their own Teams account to send messages.",
            ),
            Requirement(
                "calling_permissions",
                "Calling bot permissions granted (Calls.JoinGroupCall.All, Calls.AccessMedia.All)",
                bool(ctx.last_test) and not any(p.startswith("Calls.") for p in missing_perms),
                "provider",
                Capability.REAL_TIME_CALL,
                hint="A Microsoft 365 admin must grant admin consent to the application.",
            ),
            Requirement(
                "media_gateway",
                "Teams media gateway configured (TEAMS_MEDIA_GATEWAY_URL / _SECRET)",
                bool(s.teams_media_gateway_url and s.teams_media_gateway_secret),
                "server",
                Capability.REAL_TIME_CALL,
                hint="Deploy teams-media-gateway/ on a Windows Server VM in Azure.",
            ),
            Requirement(
                "stt",
                "Real-time speech-to-text provider",
                stt_real,
                "server",
                Capability.REAL_TIME_CALL,
                hint=stt_hint,
            ),
        ]
    elif spec.provider == Provider.GOOGLE_MEET:
        oauth_ok = bool(s.google_meet_client_id and s.google_meet_client_secret is not None)
        reqs += [
            Requirement(
                "oauth_client",
                "Google OAuth client for Meet (GOOGLE_MEET_CLIENT_ID / _SECRET)",
                oauth_ok,
                "server",
                hint="Create a Web application OAuth client in the Google Cloud project and set "
                "GOOGLE_MEET_CLIENT_ID / GOOGLE_MEET_CLIENT_SECRET on the server.",
            ),
            Requirement(
                "user_connection",
                "Your Google account connected (Meet scopes granted)",
                bool(ctx.user_connected),
                "user",
                hint="Each salesperson connects the Google account they use in Meet.",
            ),
            Requirement(
                "stt",
                "Real-time speech-to-text provider",
                stt_real,
                "server",
                Capability.REAL_TIME_CALL,
                hint=stt_hint,
            ),
            Requirement(
                "developer_preview",
                "Meet Media API Developer Preview access (confirmed by the first live connection)",
                bool(ctx.config.get("media_verified_at")),
                "provider",
                Capability.REAL_TIME_CALL,
                hint="The Google Cloud project, your Google account and all meeting participants "
                "must be enrolled in the Google Workspace Developer Preview Program.",
            ),
        ]
    elif spec.provider == Provider.WHATSAPP:
        reqs += [
            Requirement(
                "public_url",
                "Public HTTPS URL for Meta webhooks (PUBLIC_BASE_URL)",
                public_https,
                "server",
                Capability.MESSAGE,
                hint="Meta only delivers webhooks to a public https URL.",
            ),
            Requirement(
                "webhook_verified",
                "Webhook registered and verified in the Meta App Dashboard",
                bool(ctx.config.get("webhook_verified_at")),
                "provider",
                Capability.MESSAGE,
                hint="Paste the callback URL and verify token shown below into Meta, then "
                "subscribe to the 'messages' field.",
            ),
        ]
    elif spec.provider == Provider.PLIVO:
        reqs += [
            Requirement(
                "public_url",
                "Public HTTPS URL for Plivo callbacks (PUBLIC_BASE_URL)",
                public_https,
                "server",
                Capability.PHONE_CALL,
                hint="Plivo must reach the answer/hangup URLs and the media WebSocket.",
            ),
            Requirement(
                "stt",
                "Real-time speech-to-text provider",
                stt_real,
                "server",
                Capability.MEDIA_STREAM,
                hint=stt_hint,
            ),
        ]
    return reqs


# Requirements that must be satisfied before a capability can be ENABLED (LIVE mode).
# "user" requirements are per salesperson and do not block enabling for the company.
BLOCKING_SCOPES = {"company", "server", "provider"}


def blocking_missing(reqs: list[Requirement], cap: Capability) -> list[Requirement]:
    return [
        r
        for r in reqs
        if not r.ok
        and r.scope in BLOCKING_SCOPES
        and r.capability in (None, cap)
        # Provider-side facts that can only be confirmed after enabling (webhook delivery,
        # Google's Developer Preview enrolment on the first real connection).
        and r.key not in ("webhook_verified", "developer_preview")
    ]
