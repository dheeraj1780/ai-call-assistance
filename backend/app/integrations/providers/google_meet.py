"""Google Meet adapter: user OAuth, Meet REST API (space lookup) and the Meet Media API
signalling call (``connectActiveConference``).

Architecture (see docs/integrations/google-meet.md):
- The WebRTC side of the Meet Media API runs in the salesperson's browser, using Google's
  TypeScript reference client (vendored in frontend/src/vendor/meet-media-api). The browser
  builds the SDP offer (3 receive-only audio transceivers + the required ordered data channels)
  and sends it to OUR API; this adapter forwards it to Google with the user's access token and
  returns Google's SDP answer. The access token never leaves the backend.
- The browser mixes the received Meet audio to 16 kHz PCM and streams it into the existing
  media WebSocket (MediaIngest -> LiveSession -> SpeechToTextProvider -> CopilotEngine), exactly
  like the Teams media gateway does. Audio is never stored.

STATUS: implemented against Google's documentation (Media API is a Developer Preview; checked
2026-09-25). Unit/integration tested with a mocked Google transport only. A real Meet conference
has NOT been connected yet.
"""

import re
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from app.integrations.providers.base import (
    ConnectionReport,
    ProviderAuthError,
    ProviderError,
    ProviderPermissionError,
    request,
)
from app.telephony.provider import (
    OutboundCallRequest,
    TelephonyError,
    TelephonyEvent,
    parse_json_media_message,
)

PROVIDER = "google-meet"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
MEET_V2 = "https://meet.googleapis.com/v2"
MEET_V2BETA = "https://meet.googleapis.com/v2beta"

# Only what the live copilot needs (Google's Meet scopes, checked 2026-09-25):
# - meetings.space.readonly (sensitive): spaces.get -> is a conference active in this space?
# - meetings.conference.media.audio.readonly (restricted, Developer Preview):
#   connectActiveConference for audio. Video and "media.readonly" are NOT requested.
SCOPE_SPACE_READONLY = "https://www.googleapis.com/auth/meetings.space.readonly"
SCOPE_MEDIA_AUDIO = "https://www.googleapis.com/auth/meetings.conference.media.audio.readonly"
REQUIRED_SCOPES = (SCOPE_SPACE_READONLY, SCOPE_MEDIA_AUDIO)
IDENTITY_SCOPES = ("openid", "email")

_CODE = re.compile(r"^[a-z]{3}-?[a-z]{4}-?[a-z]{3}$")
MAX_SDP_BYTES = 64 * 1024


def parse_meeting_code(value: str) -> str | None:
    """A Google Meet link (https://meet.google.com/abc-defg-hij[?...]) or a bare meeting code
    -> the normalised code "abc-defg-hij"; None for anything else (other hosts, lookup links,
    personal nicknames)."""
    text = (value or "").strip()
    if not text or len(text) > 2000:
        return None
    if "/" in text or ":" in text:
        parts = urllib.parse.urlsplit(text if "://" in text else f"https://{text}")
        if parts.scheme != "https" or (parts.hostname or "").lower() != "meet.google.com":
            return None
        path = parts.path.strip("/")
        if "/" in path:
            return None
        text = path
    code = text.lower()
    if not _CODE.match(code):
        return None
    letters = code.replace("-", "")
    return f"{letters[:3]}-{letters[3:7]}-{letters[7:]}"


def meeting_link(code: str) -> str:
    return f"https://meet.google.com/{code}"


# ---- errors ------------------------------------------------------------------------------------


class MeetError(ProviderError):
    """A Meet/Media API failure with a stable code and a message safe to show the user."""

    code = "MEET_ERROR"
    status_code = 502
    retryable = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        trace_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.trace_id = trace_id
        self.retryable = retryable


# Google's documented connectActiveConference reasons -> our code + user-facing explanation.
# https://developers.google.com/workspace/meet/media-api/guides/troubleshoot
REASONS: dict[str, tuple[str, str, int, bool]] = {
    "NO_ACTIVE_CONFERENCE": (
        "MEET_CONFERENCE_NOT_ACTIVE",
        "The meeting has not started yet. Join the Google Meet first, then start the copilot.",
        409,
        True,
    ),
    "CONSENTER_ABSENT": (
        "MEET_CONSENT_REQUIRED",
        "No one who can consent is in the meeting. For a Gmail-owned meeting the person who "
        "started it must be present to approve the copilot.",
        409,
        True,
    ),
    "CONNECTIONS_EXHAUSTED": (
        "MEET_CONNECTIONS_EXHAUSTED",
        "Another Meet Media API client is already connected to this meeting (only one is "
        "allowed). Wait about 30 seconds and try again.",
        409,
        True,
    ),
    "INVALID_OFFER": (
        "MEET_INVALID_OFFER",
        "Google rejected the connection offer (browser WebRTC requirements not met).",
        502,
        False,
    ),
    "INCOMPATIBLE_DEVICE": (
        "MEET_INCOMPATIBLE_PARTICIPANT",
        "A participant's device or account (for example an underage account) is not "
        "compatible with the Meet Media API, so Google does not allow the copilot to join.",
        409,
        False,
    ),
    "UNSUPPORTED_PLATFORM_PRESENT": (
        "MEET_INCOMPATIBLE_PARTICIPANT",
        "A participant uses a Meet app version that is not compatible with the Meet Media API.",
        409,
        False,
    ),
    "DISABLED_BY_ADMIN": (
        "MEET_DISABLED",
        "The organisation's administrator has disabled the Meet Media API.",
        403,
        False,
    ),
    "DISABLED_BY_HOST_CONTROL": (
        "MEET_DISABLED",
        "The meeting host has disabled apps' access to meeting media.",
        403,
        False,
    ),
    "DISABLED_DUE_TO_WATERMARKING": (
        "MEET_DISABLED",
        "Watermarking is on in this meeting; Google does not allow Media API clients.",
        403,
        False,
    ),
    "DISABLED_DUE_TO_ENCRYPTION": (
        "MEET_DISABLED",
        "Encryption is on in this meeting; Google does not allow Media API clients.",
        403,
        False,
    ),
}

NOT_ELIGIBLE_MESSAGE = (
    "Google refused access to live meeting media. The Meet Media API is a Developer Preview: "
    "the Google Cloud project, your Google account and every participant must be enrolled in "
    "the Google Workspace Developer Preview Program, and the OAuth client must be allowed the "
    "restricted media scope."
)


def _error_info(resp: httpx.Response) -> tuple[str | None, str | None, str | None]:
    """(status, reason, message) from a Google error body. Nothing is logged."""
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return None, None, None
    if not isinstance(err, dict):
        return None, None, None
    reason = None
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason"):
            reason = str(detail["reason"])
            break
    message = str(err.get("message") or "")[:300]
    if reason is None:
        # Some Meet errors carry the reason only in the message text.
        for known in REASONS:
            if known in message:
                reason = known
                break
    return (str(err.get("status")) if err.get("status") else None), reason, message


def map_meet_error(resp: httpx.Response, *, operation: str) -> MeetError:
    status, reason, message = _error_info(resp)
    if reason in REASONS:
        code, text, http, retry = REASONS[reason]
        return MeetError(code, text, status_code=http, retryable=retry)
    if resp.status_code == 401:
        return MeetError(
            "MEET_AUTH_EXPIRED",
            "Your Google sign-in expired. Reconnect Google Meet.",
            status_code=401,
        )
    if resp.status_code == 403:
        lowered = (message or "").lower()
        if (
            reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
            or "insufficient authentication scopes" in lowered
        ):
            return MeetError(
                "MEET_SCOPE_MISSING",
                "Google Meet access was not granted. Reconnect Google Meet and allow all requested "
                "permissions.",
                status_code=403,
            )
        if reason == "SERVICE_DISABLED" or "has not been used in project" in lowered:
            return MeetError(
                "MEET_API_DISABLED",
                "The Google Meet REST API is not enabled in the Google Cloud project.",
                status_code=403,
            )
        if operation == "connect":
            return MeetError("MEET_MEDIA_API_NOT_ELIGIBLE", NOT_ELIGIBLE_MESSAGE, status_code=403)
        return MeetError(
            "MEET_PERMISSION_DENIED",
            "Your Google account cannot access this meeting.",
            status_code=403,
        )
    if resp.status_code == 404:
        return MeetError(
            "MEET_MEETING_NOT_FOUND",
            "No Google Meet meeting with this link exists, or your Google account cannot see it.",
            status_code=404,
        )
    if resp.status_code == 429:
        return MeetError(
            "MEET_RATE_LIMITED",
            "Google rate limit reached; try again shortly.",
            status_code=429,
            retryable=True,
        )
    if resp.status_code >= 500:
        return MeetError(
            "MEET_UNAVAILABLE",
            "Google Meet is temporarily unavailable.",
            status_code=503,
            retryable=True,
        )
    if status == "FAILED_PRECONDITION":
        return MeetError(
            "MEET_PRECONDITION_FAILED",
            "Google did not allow the connection to this meeting right now.",
            status_code=409,
        )
    return MeetError("MEET_REQUEST_REJECTED", f"Google rejected the request ({resp.status_code}).")


# ---- OAuth -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    scopes: frozenset[str]
    expires_in: int


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str | None


def missing_scopes(granted: frozenset[str] | set[str]) -> list[str]:
    return [s for s in REQUIRED_SCOPES if s not in granted]


class GoogleMeetOAuthClient:
    """Authorization-code flow with offline access for one platform OAuth client (web app)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    def authorization_url(
        self, *, state: str, redirect_uri: str, login_hint: str | None = None
    ) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join((*IDENTITY_SCOPES, *REQUIRED_SCOPES)),
            "access_type": "offline",
            # Always show consent so Google issues a refresh token and the user sees the
            # restricted media scope explicitly.
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def _token(self, data: dict[str, str], operation: str) -> GoogleTokens:
        async with self._client() as client:
            resp = await request(
                client,
                "POST",
                TOKEN_URL,
                provider=PROVIDER,
                operation=operation,
                data={**data, "client_id": self._client_id, "client_secret": self._client_secret},
            )
        if resp.status_code in (400, 401):
            raise ProviderAuthError("google: token request rejected")
        if resp.status_code >= 400:
            raise map_meet_error(resp, operation=operation)
        body = resp.json()
        return GoogleTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            scopes=frozenset(str(body.get("scope", "")).split()),
            expires_in=int(body.get("expires_in", 0)),
        )

    async def exchange_code(self, *, code: str, redirect_uri: str) -> GoogleTokens:
        return await self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            "oauth_exchange",
        )

    async def refresh(self, refresh_token: str) -> GoogleTokens:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}, "oauth_refresh"
        )

    async def identity(self, access_token: str) -> GoogleIdentity:
        async with self._client() as client:
            resp = await request(
                client,
                "GET",
                USERINFO_URL,
                provider=PROVIDER,
                operation="userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code >= 400:
            raise map_meet_error(resp, operation="userinfo")
        body = resp.json()
        return GoogleIdentity(subject=str(body.get("sub", "")), email=body.get("email"))

    async def revoke(self, token: str) -> None:
        async with self._client() as client:
            await request(
                client,
                "POST",
                REVOKE_URL,
                provider=PROVIDER,
                operation="oauth_revoke",
                data={"token": token},
            )


# ---- Meet REST + Media API signalling ----------------------------------------------------------


@dataclass(frozen=True)
class MeetSpace:
    name: str  # "spaces/{space}"
    meeting_code: str | None
    meeting_uri: str | None
    active_conference: bool


@dataclass(frozen=True)
class MediaAnswer:
    answer: str
    trace_id: str | None


class GoogleMeetApiClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self._transport)

    async def get_space(self, access_token: str, meeting_code: str) -> MeetSpace:
        """spaces.get accepts the meeting code as an alias: spaces/{meetingCode}."""
        async with self._client() as client:
            resp = await request(
                client,
                "GET",
                f"{MEET_V2}/spaces/{meeting_code}",
                provider=PROVIDER,
                operation="space_get",
                retries=1,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code >= 400:
            raise map_meet_error(resp, operation="space_get")
        body = resp.json()
        return MeetSpace(
            name=str(body.get("name", "")),
            meeting_code=body.get("meetingCode"),
            meeting_uri=body.get("meetingUri"),
            active_conference=bool((body.get("activeConference") or {}).get("conferenceRecord")),
        )

    async def connect_active_conference(
        self, access_token: str, space_name: str, offer: str
    ) -> MediaAnswer:
        if not space_name.startswith("spaces/"):
            raise MeetError("MEET_MEETING_NOT_FOUND", "Unknown meeting space.", status_code=404)
        async with self._client() as client:
            resp = await request(
                client,
                "POST",
                f"{MEET_V2BETA}/{space_name}:connectActiveConference",
                provider=PROVIDER,
                operation="connect",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"offer": offer},
            )
        if resp.status_code >= 400:
            raise map_meet_error(resp, operation="connect")
        body = resp.json()
        answer = body.get("answer")
        if not answer:
            raise MeetError("MEET_NO_ANSWER", "Google returned no SDP answer.")
        return MediaAnswer(answer=str(answer), trace_id=body.get("traceId"))


def validate_offer(offer: str) -> None:
    """Cheap sanity checks on the browser's offer before it goes to Google (Google's own
    requirements: exactly 3 receive-only audio sections, session-control + media-stats data
    channels, DTLS actpass/active)."""
    if not offer or len(offer.encode()) > MAX_SDP_BYTES:
        raise MeetError("MEET_INVALID_OFFER", "Missing or oversized SDP offer.", status_code=422)
    if not offer.startswith("v=0"):
        raise MeetError("MEET_INVALID_OFFER", "Not an SDP offer.", status_code=422)
    audio = [m for m in offer.split("\nm=")[1:] if m.startswith("audio")]
    if len(audio) != 3:
        raise MeetError(
            "MEET_INVALID_OFFER",
            "The offer must contain exactly 3 audio media sections.",
            status_code=422,
        )
    if "m=application" not in offer:
        raise MeetError("MEET_INVALID_OFFER", "The offer has no data channels.", status_code=422)


# ---- mock (MOCK mode and tests; never contacts Google) -----------------------------------------


class MockGoogleMeetApiClient:
    """MOCKED: any well-formed code is an existing space; codes starting with "zzz" have no
    active conference. connect returns a placeholder answer - it cannot produce real media."""

    connected: ClassVar[list[str]] = []

    async def get_space(self, access_token: str, meeting_code: str) -> MeetSpace:
        return MeetSpace(
            name=f"spaces/mock-{meeting_code}",
            meeting_code=meeting_code,
            meeting_uri=meeting_link(meeting_code),
            active_conference=not meeting_code.startswith("zzz"),
        )

    async def connect_active_conference(
        self, access_token: str, space_name: str, offer: str
    ) -> MediaAnswer:
        self.connected.append(space_name)
        return MediaAnswer(answer="v=0\r\n(mock answer: no real media)\r\n", trace_id="mock")


def mock_test_report() -> ConnectionReport:
    report = ConnectionReport()
    report.add("mock", "Mock mode: no Google account is contacted", True)
    report.capabilities = {"REAL_TIME_CALL": True, "MEETING_LOOKUP": True}
    return report


# ---- calling provider --------------------------------------------------------------------------


class GoogleMeetCallingProvider:
    """TelephonyProvider for Google Meet. Starting a call does not contact Google: the copilot
    attaches when the salesperson's browser connects (POST /calls/{id}/google-meet/connect),
    and the browser reports the session state (POST /calls/{id}/google-meet/events)."""

    emits_end_events = True  # ending is local: the browser leaves the conference

    def __init__(self, *, name: str = PROVIDER) -> None:
        self.name = name

    async def create_call(self, request_: OutboundCallRequest) -> str:
        code = parse_meeting_code(request_.meeting_url or "")
        if code is None:
            raise TelephonyError("google meet call without a valid meeting link")
        return f"meet:{code}:{uuid.uuid4().hex[:12]}"

    async def end_call(self, provider_call_id: str) -> None:
        return None

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> None:
        raise ProviderPermissionError("google meet has no provider webhooks")

    def parse_webhook(self, body: bytes) -> list[TelephonyEvent]:
        return []

    def parse_media_message(self, message: str) -> Any:
        return parse_json_media_message(message)


def meeting_code_of(provider_call_id: str | None) -> str | None:
    if not provider_call_id or not provider_call_id.startswith("meet:"):
        return None
    parts = provider_call_id.split(":")
    return parts[1] if len(parts) == 3 else None
