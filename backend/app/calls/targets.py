"""Validation of a call's target (the meeting link) per channel.

Shared by call creation and editing so both apply the SAME rules, and kept in agreement with
what the provider can actually use: for Teams that is what ``teams-media-gateway``'s
``JoinInfo.Parse`` accepts (a classic ``/l/meetup-join/<thread>/<message>?context={...}`` link
whose context carries the organizer's ``Oid``). A link the gateway would reject must be rejected
HERE, while the call is still PLANNED and editable, not after the start attempt failed.
"""

import json
import re
from urllib.parse import unquote, urlsplit

from app.calls.models import CallChannel

_TEAMS_JOIN = re.compile(
    r"^https://teams\.(?:microsoft|live)\.com/l/meetup-join/(?P<thread>[^/]+)/(?P<message>[^/?]+)"
    r"\?context=(?P<context>\{.*\})",
)
MAX_URL_LENGTH = 2000


class TargetError(ValueError):
    """The meeting link is not usable for the selected channel (message is user-facing)."""


def is_teams_meeting_url(value: str) -> bool:
    """True when the Teams media gateway can join this link."""
    if len(value) > MAX_URL_LENGTH:
        return False
    match = _TEAMS_JOIN.match(unquote(value.strip()))
    if match is None:
        return False
    try:
        context = json.loads(match.group("context"))
    except ValueError:
        return False
    return isinstance(context, dict) and bool(context.get("Oid"))


def _teams(value: str | None) -> str:
    if not value or not value.strip():
        raise TargetError("A Teams call needs the Teams meeting link")
    url = value.strip()
    if is_teams_meeting_url(url):
        return url
    host = (urlsplit(url).hostname or "").lower()
    if not (
        host in ("teams.microsoft.com", "teams.live.com") or host.endswith(".teams.microsoft.com")
    ):
        raise TargetError(
            "A Teams call needs a Microsoft Teams meeting link (https://teams.microsoft.com/...)"
        )
    raise TargetError(
        "This Teams link cannot be joined by the copilot. Use the full 'Join the meeting now' link "
        "(https://teams.microsoft.com/l/meetup-join/...) from the meeting invitation; short "
        "'/meet/...' links are not supported yet."
    )


def _meet(value: str | None) -> str:
    from app.integrations.providers.google_meet import meeting_link, parse_meeting_code

    code = parse_meeting_code(value or "")
    if code is None:
        raise TargetError(
            "A Google Meet call needs a Google Meet link "
            "(https://meet.google.com/abc-defg-hij) or meeting code"
        )
    return meeting_link(code)


def normalize_target(channel: CallChannel, meeting_url: str | None) -> str | None:
    """Validated/normalised meeting_url for ``channel`` (None for phone calls)."""
    if channel == CallChannel.TEAMS:
        return _teams(meeting_url)
    if channel == CallChannel.GOOGLE_MEET:
        return _meet(meeting_url)
    if meeting_url is not None:
        raise TargetError("meeting_url is only used for meeting calls (Teams, Google Meet)")
    return None
