"""CalendarProvider interface, Google implementation and an in-memory mock.

GoogleCalendarProvider: IMPLEMENTED against Google's documented OAuth 2.0 and Calendar v3 REST
endpoints using httpx. NOT LIVE VERIFIED (no Google OAuth client during development). Its
error mapping is tested with ``httpx.MockTransport``.
"""

import contextlib
import itertools
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(Exception):
    code = "calendar_unavailable"
    retry_after: int | None = None


class CalendarAuthError(CalendarError):
    """Token expired/revoked: the user must reconnect."""

    code = "calendar_reauth_required"


class CalendarPermissionError(CalendarError):
    code = "calendar_permission_missing"


class CalendarRateLimitError(CalendarError):
    code = "calendar_rate_limited"


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    scope: str
    account_email: str | None = None


@dataclass(frozen=True)
class CalendarEventData:
    external_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    html_link: str | None = None


@dataclass(frozen=True)
class NewEvent:
    title: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None


class CalendarProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...
    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthTokens: ...
    async def access_token(self, refresh_token: str) -> str: ...
    async def list_events(
        self, access_token: str, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEventData]: ...
    async def create_event(self, access_token: str, event: NewEvent) -> CalendarEventData: ...
    async def update_event(
        self, access_token: str, external_id: str, event: NewEvent
    ) -> CalendarEventData: ...
    async def revoke(self, token: str) -> None: ...


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    error = ""
    with contextlib.suppress(ValueError):
        error = str(resp.json().get("error", ""))
    if resp.status_code == 401 or error == "invalid_grant":
        raise CalendarAuthError(f"status {resp.status_code}")
    if resp.status_code == 403 and "rateLimitExceeded" not in resp.text:
        raise CalendarPermissionError("insufficient permissions")
    if resp.status_code in (403, 429):
        exc = CalendarRateLimitError("rate limited")
        exc.retry_after = int(resp.headers.get("retry-after", "30") or 30)
        raise exc
    raise CalendarError(f"status {resp.status_code}")


def _parse_time(value: dict[str, str]) -> datetime:
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    return datetime.fromisoformat(value["date"] + "T00:00:00+00:00")  # all-day event


class GoogleCalendarProvider:
    name = "google"

    def __init__(
        self, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15.0, transport=self._transport)

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": f"openid email {GOOGLE_SCOPE}",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthTokens:
        async with self._http() as http:
            try:
                resp = await http.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
            except httpx.HTTPError as exc:
                raise CalendarError(type(exc).__name__) from exc
        _raise_for(resp)
        data = resp.json()
        if GOOGLE_SCOPE not in data.get("scope", ""):
            raise CalendarPermissionError("calendar scope was not granted")
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            scope=data.get("scope", ""),
        )

    async def access_token(self, refresh_token: str) -> str:
        async with self._http() as http:
            try:
                resp = await http.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "refresh_token": refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                    },
                )
            except httpx.HTTPError as exc:
                raise CalendarError(type(exc).__name__) from exc
        _raise_for(resp)
        return str(resp.json()["access_token"])

    async def list_events(
        self, access_token: str, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEventData]:
        async with self._http() as http:
            try:
                resp = await http.get(
                    GOOGLE_EVENTS_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "timeMin": time_min.isoformat(),
                        "timeMax": time_max.isoformat(),
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "maxResults": "50",
                    },
                )
            except httpx.HTTPError as exc:
                raise CalendarError(type(exc).__name__) from exc
        _raise_for(resp)
        return [
            CalendarEventData(
                external_id=item["id"],
                title=item.get("summary", "(no title)"),
                starts_at=_parse_time(item["start"]),
                ends_at=_parse_time(item["end"]),
                html_link=item.get("htmlLink"),
            )
            for item in resp.json().get("items", [])
            if item.get("status") != "cancelled"
        ]

    def _body(self, event: NewEvent) -> dict[str, object]:
        return {
            "summary": event.title,
            "description": event.description or "",
            "start": {"dateTime": event.starts_at.isoformat()},
            "end": {"dateTime": event.ends_at.isoformat()},
        }

    async def create_event(self, access_token: str, event: NewEvent) -> CalendarEventData:
        async with self._http() as http:
            try:
                resp = await http.post(
                    GOOGLE_EVENTS_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=self._body(event),
                )
            except httpx.HTTPError as exc:
                raise CalendarError(type(exc).__name__) from exc
        _raise_for(resp)
        data = resp.json()
        return CalendarEventData(
            external_id=data["id"],
            title=event.title,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            html_link=data.get("htmlLink"),
        )

    async def update_event(
        self, access_token: str, external_id: str, event: NewEvent
    ) -> CalendarEventData:
        async with self._http() as http:
            try:
                resp = await http.patch(
                    f"{GOOGLE_EVENTS_URL}/{urllib.parse.quote(external_id, safe='')}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=self._body(event),
                )
            except httpx.HTTPError as exc:
                raise CalendarError(type(exc).__name__) from exc
        _raise_for(resp)
        data = resp.json()
        return CalendarEventData(
            external_id=data["id"],
            title=event.title,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            html_link=data.get("htmlLink"),
        )

    async def revoke(self, token: str) -> None:
        async with self._http() as http:
            # Best effort: the connection is deleted locally regardless.
            with contextlib.suppress(httpx.HTTPError):
                await http.post(GOOGLE_REVOKE_URL, data={"token": token})


@dataclass
class MockCalendarProvider:
    """MOCKED calendar: in-memory events keyed by token. For local dev and tests."""

    name: str = "mock"
    events: dict[str, CalendarEventData] = field(default_factory=dict)
    fail_with: CalendarError | None = None
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        query = urllib.parse.urlencode({"code": "mock-code", "state": state})
        return f"{redirect_uri}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthTokens:
        self._check()
        return OAuthTokens("mock-access", "mock-refresh", GOOGLE_SCOPE, "mock@calendar.local")

    async def access_token(self, refresh_token: str) -> str:
        self._check()
        return "mock-access"

    async def list_events(
        self, access_token: str, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEventData]:
        self._check()
        return sorted(
            (e for e in self.events.values() if time_min <= e.starts_at <= time_max),
            key=lambda e: e.starts_at,
        )

    async def create_event(self, access_token: str, event: NewEvent) -> CalendarEventData:
        self._check()
        created = CalendarEventData(
            external_id=f"mock-{next(self._ids)}",
            title=event.title,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
        )
        self.events[created.external_id] = created
        return created

    async def update_event(
        self, access_token: str, external_id: str, event: NewEvent
    ) -> CalendarEventData:
        self._check()
        updated = CalendarEventData(external_id, event.title, event.starts_at, event.ends_at)
        self.events[external_id] = updated
        return updated

    async def revoke(self, token: str) -> None:
        return None


_provider: CalendarProvider | None = None


def get_calendar_provider() -> CalendarProvider:
    global _provider
    if _provider is None:
        from app.common.config import get_settings

        settings = get_settings()
        if settings.calendar_provider == "google":
            if not settings.google_client_id or settings.google_client_secret is None:
                raise RuntimeError("CALENDAR_PROVIDER=google requires GOOGLE_CLIENT_ID/SECRET")
            _provider = GoogleCalendarProvider(
                settings.google_client_id, settings.google_client_secret.get_secret_value()
            )
        else:
            _provider = MockCalendarProvider()
    return _provider


def set_calendar_provider(provider: CalendarProvider) -> None:
    global _provider
    _provider = provider
