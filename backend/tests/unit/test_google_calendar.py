"""GoogleCalendarProvider request/error mapping against a mocked transport (no network, no DB).

Run standalone:  uv run pytest --noconftest tests/unit
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.calendar.provider import (
    GOOGLE_SCOPE,
    CalendarAuthError,
    CalendarError,
    CalendarPermissionError,
    CalendarRateLimitError,
    GoogleCalendarProvider,
    NewEvent,
)


def provider(handler: httpx.MockTransport) -> GoogleCalendarProvider:
    return GoogleCalendarProvider("cid", "csecret", transport=handler)


def test_authorization_url_requests_offline_calendar_scope() -> None:
    url = GoogleCalendarProvider("cid", "s").authorization_url(
        state="st", redirect_uri="https://x/cb"
    )
    assert "access_type=offline" in url
    assert "state=st" in url
    assert "calendar.events" in url


async def test_exchange_code_requires_calendar_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "scope": "email"}
        )

    with pytest.raises(CalendarPermissionError):
        await provider(httpx.MockTransport(handler)).exchange_code(code="c", redirect_uri="u")


async def test_exchange_code_ok() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "scope": f"email {GOOGLE_SCOPE}"}
        )

    tokens = await provider(httpx.MockTransport(handler)).exchange_code(code="c", redirect_uri="u")
    assert tokens.refresh_token == "r"
    assert seen["grant_type"] == "authorization_code"


@pytest.mark.parametrize(
    ("status", "body", "headers", "error"),
    [
        (400, {"error": "invalid_grant"}, {}, CalendarAuthError),
        (401, {}, {}, CalendarAuthError),
        (403, {"error": {"message": "insufficientPermissions"}}, {}, CalendarPermissionError),
        (429, {}, {"retry-after": "7"}, CalendarRateLimitError),
        (500, {}, {}, CalendarError),
    ],
)
async def test_error_mapping(
    status: int, body: dict[str, object], headers: dict[str, str], error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers)

    with pytest.raises(error) as exc:
        await provider(httpx.MockTransport(handler)).access_token("r")
    if isinstance(exc.value, CalendarRateLimitError):
        assert exc.value.retry_after == 7


async def test_network_failure_is_calendar_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(CalendarError):
        await provider(httpx.MockTransport(handler)).access_token("r")


async def test_list_and_create_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "summary": "Demo",
                            "start": {"dateTime": "2030-01-01T10:00:00Z"},
                            "end": {"dateTime": "2030-01-01T11:00:00Z"},
                        },
                        {
                            "id": "2",
                            "status": "cancelled",
                            "start": {"date": "2030-01-02"},
                            "end": {"date": "2030-01-03"},
                        },
                        {"id": "3", "start": {"date": "2030-01-05"}, "end": {"date": "2030-01-06"}},
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["summary"] == "Follow-up"
        return httpx.Response(200, json={"id": "new", "htmlLink": "https://cal/new"})

    p = provider(httpx.MockTransport(handler))
    now = datetime(2030, 1, 1, tzinfo=UTC)
    events = await p.list_events("tok", time_min=now, time_max=now)
    assert [e.external_id for e in events] == ["1", "3"]
    assert events[1].title == "(no title)"
    created = await p.create_event(
        "tok", NewEvent("Follow-up", now, now.replace(hour=1), description=None)
    )
    assert created.external_id == "new"
