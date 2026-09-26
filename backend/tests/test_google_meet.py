"""Google Meet provider: OAuth, meeting lookup, Media API signalling proxy, session events and the
audio path into the existing STT/copilot pipeline.

Google is mocked at the HTTP layer (httpx.MockTransport): these tests prove OUR behaviour
(PASS - simulation). They do NOT prove that Google accepts our requests or that real Meet media
flows; that needs a real, Developer-Preview-enrolled conference (see REAL-GOOGLE-MEET-TEST.md).
"""

import asyncio
import base64
import json
import logging
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.common.config import get_settings
from app.common.crypto import decrypt
from app.common.db import TenantContext
from app.integrations.models import Integration, IntegrationUserConnection
from app.integrations.providers.google_meet import (
    REQUIRED_SCOPES,
    SCOPE_MEDIA_AUDIO,
    SCOPE_SPACE_READONLY,
    parse_meeting_code,
    validate_offer,
)
from app.live.models import TranscriptSegment
from app.speech.provider import STTResult, set_stt_provider
from app.telephony import service as telephony
from app.telephony.provider import parse_json_media_message
from app.telephony.service import MediaIngest
from tests.conftest import Account, open_session
from tests.integration_helpers import FakeProvider, configure, validate_and_enable

Register = Callable[..., Awaitable[Account]]

ACCESS = "ya29.ACCESS-TOKEN-SECRET-value"
ACCESS2 = "ya29.ACCESS-TOKEN-SECRET-rotated"
REFRESH = "1//REFRESH-TOKEN-SECRET-value"
CLIENT_SECRET = "GOCSPX-client-secret-value"
AUTH_CODE = "4/0AUTH-CODE-SECRET"
MEET_LINK = "https://meet.google.com/abc-defg-hij"
OFFER = (
    "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\na=setup:actpass\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\na=setup:actpass\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\na=setup:actpass\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=setup:actpass\r\n"
)
ANSWER = "v=0\r\no=google 1 2 IN IP4 0.0.0.0\r\n(answer)\r\n"


# ---- fakes ---------------------------------------------------------------------------------------


def google(*, scopes: tuple[str, ...] = REQUIRED_SCOPES, rotate: bool = False) -> FakeProvider:
    fake = FakeProvider()

    def token(req: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs(req.content.decode())
        scope = " ".join(("openid", "email", *scopes))
        if form["grant_type"] == ["authorization_code"]:
            assert form["code"] == [AUTH_CODE]
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "scope": scope,
                    "expires_in": 3599,
                },
            )
        body: dict[str, Any] = {"access_token": ACCESS2, "scope": scope, "expires_in": 3599}
        if rotate:
            body["refresh_token"] = "1//REFRESH-ROTATED-SECRET"
        return httpx.Response(200, json=body)

    fake.on("POST", "oauth2.googleapis.com/token", token)
    fake.on("POST", "oauth2.googleapis.com/revoke", httpx.Response(200))
    fake.on(
        "GET",
        "openidconnect.googleapis.com/v1/userinfo",
        httpx.Response(200, json={"sub": "10987654321", "email": "rep@gmail.example"}),
    )
    fake.on(
        "GET",
        "meet.googleapis.com/v2/spaces/abc-defg-hij",
        httpx.Response(
            200,
            json={
                "name": "spaces/SPACE123",
                "meetingCode": "abc-defg-hij",
                "meetingUri": MEET_LINK,
                "activeConference": {"conferenceRecord": "conferenceRecords/CR1"},
            },
        ),
    )
    fake.on(
        "POST",
        "v2beta/spaces/SPACE123:connectActiveConference",
        httpx.Response(200, json={"answer": ANSWER, "traceId": "trace-1"}),
    )
    return fake.install()


def google_error(
    status: int, *, reason: str | None = None, grpc: str | None = None, message: str = "error"
) -> httpx.Response:
    err: dict[str, Any] = {"code": status, "message": message}
    if grpc:
        err["status"] = grpc
    if reason:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return httpx.Response(status, json={"error": err})


class PcmStream:
    """Records what the pipeline sends to STT; one final result when the input finishes."""

    def __init__(self, owner: "PcmStt") -> None:
        self.owner = owner
        self.queue: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self.done = False

    async def send(self, audio: bytes) -> None:
        self.owner.bytes_received += len(audio)
        self.owner.chunks.append(len(audio))

    async def results(self) -> AsyncIterator[STTResult]:
        while (item := await self.queue.get()) is not None:
            yield item

    async def finish(self) -> None:
        if not self.done:
            self.done = True
            if self.owner.bytes_received:
                await self.queue.put(STTResult(self.owner.say, True, 0, 1000, 0.9, latency_ms=10.0))
            await self.queue.put(None)

    def pending_audio_ms(self) -> float:
        return 0.0

    async def close(self) -> None:
        await self.finish()


class PcmStt:
    name = "pcm-test"

    def __init__(self, say: str) -> None:
        self.say = say
        self.formats: list[tuple[str, int]] = []
        self.bytes_received = 0
        self.chunks: list[int] = []

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> Any:
        self.formats.append((encoding, sample_rate))
        return PcmStream(self)


# ---- fixtures / helpers --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def meet_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "google_meet_client_id", "1234-meet.apps.googleusercontent.com")
    monkeypatch.setattr(s, "google_meet_client_secret", type(s.jwt_secret)(CLIENT_SECRET))
    monkeypatch.setattr(s, "public_base_url", "https://cc.example.com")
    # LIVE real-time capabilities require a streaming STT; the test STT below stands in for it.
    monkeypatch.setattr(s, "stt_provider", "google")


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    return await register(client, "rep@example.com", "Acme")


async def connect_google(client: AsyncClient, acct: Account) -> httpx.Response:
    start = await client.post("/api/v1/integrations/google-meet/connect", headers=acct.headers)
    assert start.status_code == 200, start.text
    url = urllib.parse.urlsplit(start.json()["authorization_url"])
    state = urllib.parse.parse_qs(url.query)["state"][0]
    return await client.get(
        "/api/v1/integrations/google-meet/oauth/callback",
        params={"code": AUTH_CODE, "state": state},
    )


async def meet_ready(client: AsyncClient, acct: Account, persistence: str = "PERSISTED") -> None:
    await configure(client, acct, "google-meet", {"call_persistence": persistence})
    resp = await connect_google(client, acct)
    assert "google_meet=connected" in resp.headers["location"], resp.headers["location"]
    await validate_and_enable(client, acct, "google-meet", "REAL_TIME_CALL", "MEETING_LOOKUP")


async def meet_call(client: AsyncClient, acct: Account, link: str = MEET_LINK) -> dict[str, Any]:
    contact = (
        await client.post("/api/v1/contacts", headers=acct.headers, json={"name": "Ravi Kumar"})
    ).json()
    call = await client.post(
        "/api/v1/calls",
        headers=acct.headers,
        json={
            "contact_id": contact["id"],
            "channel": "GOOGLE_MEET",
            "meeting_url": link,
            "objective": "Qualify inventory needs",
        },
    )
    assert call.status_code == 201, call.text
    started = await client.post(f"/api/v1/calls/{call.json()['id']}/start", headers=acct.headers)
    assert started.status_code == 200, started.text
    body: dict[str, Any] = started.json()
    return body


async def event(
    client: AsyncClient,
    acct: Account,
    call_id: str,
    state: str,
    reason: str | None = None,
    event_id: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/calls/{call_id}/google-meet/events",
        headers=acct.headers,
        json={
            "event_id": event_id or uuid.uuid4().hex,
            "state": state,
            **({"reason": reason} if reason else {}),
        },
    )


# ---- 5. meeting link parsing / offer validation --------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("https://meet.google.com/abc-defg-hij", "abc-defg-hij"),
        ("https://meet.google.com/ABC-DEFG-HIJ?authuser=0&hs=1", "abc-defg-hij"),
        ("meet.google.com/abc-defg-hij", "abc-defg-hij"),
        ("abc-defg-hij", "abc-defg-hij"),
        ("abcdefghij", "abc-defg-hij"),
        ("  https://meet.google.com/abc-defg-hij/  ", "abc-defg-hij"),
        ("http://meet.google.com/abc-defg-hij", None),
        ("https://evil.example.com/abc-defg-hij", None),
        ("https://meet.google.com.evil.com/abc-defg-hij", None),
        ("https://meet.google.com/lookup/abcdef", None),
        ("https://teams.microsoft.com/l/meetup-join/x", None),
        ("abc-defg-hi", None),
        ("", None),
    ],
)
def test_meeting_code_parsing(value: str, code: str | None) -> None:
    assert parse_meeting_code(value) == code


def test_offer_validation_mirrors_google_requirements() -> None:
    validate_offer(OFFER)
    with pytest.raises(Exception, match="exactly 3 audio"):
        validate_offer(OFFER.replace("m=audio", "m=video", 1))
    with pytest.raises(Exception, match="no data channels"):
        validate_offer(OFFER.split("m=application")[0])
    with pytest.raises(Exception, match="Not an SDP"):
        validate_offer("hello")


# ---- 1, 6. configuration and capability reporting ------------------------------------------------


async def test_provider_configuration_and_capabilities(
    client: AsyncClient, rep: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    assert {c["capability"] for c in detail["capabilities"]} == {"REAL_TIME_CALL", "MEETING_LOOKUP"}
    assert detail["setup"]["oauth_redirect_uri"] == (
        "https://cc.example.com/api/v1/integrations/google-meet/oauth/callback"
    )
    assert set(REQUIRED_SCOPES) <= set(detail["setup"]["oauth_scopes"])
    assert not any(
        "video" in s or s.endswith("media.readonly") for s in detail["setup"]["oauth_scopes"]
    )
    reqs = {r["key"]: r for r in detail["requirements"]}
    assert reqs["oauth_client"]["ok"] is True
    assert reqs["user_connection"]["ok"] is False
    assert reqs["developer_preview"]["ok"] is False  # only a real connection can confirm it
    # Without the server OAuth client the requirement is reported as missing.
    monkeypatch.setattr(get_settings(), "google_meet_client_id", None)
    detail = (await client.get("/api/v1/integrations/google-meet", headers=rep.headers)).json()
    assert {r["key"]: r["ok"] for r in detail["requirements"]}["oauth_client"] is False
    start = await client.post("/api/v1/integrations/google-meet/connect", headers=rep.headers)
    assert start.status_code == 409


async def test_connection_test_reports_what_can_be_verified(
    client: AsyncClient, rep: Account
) -> None:
    fake = google()
    await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    report = (
        await client.post("/api/v1/integrations/google-meet/test", headers=rep.headers)
    ).json()
    checks = {c["key"]: c["ok"] for c in report["last_test"]["checks"]}
    assert checks == {"oauth_client": True, "user_connection": False}
    await connect_google(client, rep)
    report = (
        await client.post("/api/v1/integrations/google-meet/test", headers=rep.headers)
    ).json()
    checks = {c["key"]: c for c in report["last_test"]["checks"]}
    assert checks["token"]["ok"]
    assert checks["scopes"]["ok"]
    assert checks["developer_preview"]["ok"] is False
    assert checks["developer_preview"]["required"] is False
    assert report["last_test"]["ok"] is True
    assert fake.calls("POST", "oauth2.googleapis.com/token")


# ---- 2, 3, 4. OAuth ------------------------------------------------------------------------------


async def test_oauth_start_requests_only_needed_scopes(client: AsyncClient, rep: Account) -> None:
    await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    url = (
        await client.post("/api/v1/integrations/google-meet/connect", headers=rep.headers)
    ).json()["authorization_url"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert set(q["scope"][0].split()) == {
        "openid",
        "email",
        SCOPE_SPACE_READONLY,
        SCOPE_MEDIA_AUDIO,
    }
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["redirect_uri"] == [
        "https://cc.example.com/api/v1/integrations/google-meet/oauth/callback"
    ]
    assert CLIENT_SECRET not in url


async def test_oauth_callback_stores_encrypted_refresh_token(
    client: AsyncClient, rep: Account
) -> None:
    google()
    await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    resp = await connect_google(client, rep)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/settings/integrations?google_meet=connected")
    conn = (
        await client.get("/api/v1/integrations/google-meet/connection", headers=rep.headers)
    ).json()
    assert conn == {"connected": True, "account_email": "rep@gmail.example", "status": "ACTIVE"}
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        row = await session.scalar(select(IntegrationUserConnection))
        assert row is not None
        assert REFRESH not in row.encrypted_refresh_token
        assert ACCESS not in row.encrypted_refresh_token
        assert decrypt(row.encrypted_refresh_token) == REFRESH


async def test_oauth_state_is_signed_and_bound(client: AsyncClient, rep: Account) -> None:
    google()
    await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    bad = await client.get(
        "/api/v1/integrations/google-meet/oauth/callback",
        params={"code": AUTH_CODE, "state": "forged.state"},
    )
    assert bad.headers["location"].endswith("google_meet=invalid_state")
    denied = await client.get(
        "/api/v1/integrations/google-meet/oauth/callback", params={"error": "access_denied"}
    )
    assert denied.headers["location"].endswith("google_meet=denied")
    teams_state = (
        await client.post("/api/v1/integrations/google-meet/connect", headers=rep.headers)
    ).json()["authorization_url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(teams_state).query)["state"][0]
    other = await client.get(
        "/api/v1/integrations/microsoft-teams/oauth/callback",
        params={"code": AUTH_CODE, "state": state},
    )
    assert "teams=invalid_state" in other.headers["location"]  # purpose-bound


async def test_missing_scope_is_rejected_and_nothing_stored(
    client: AsyncClient, rep: Account
) -> None:
    google(scopes=(SCOPE_SPACE_READONLY,))  # the user unticked the media scope
    await configure(client, rep, "google-meet", {"call_persistence": "TRANSIENT"})
    resp = await connect_google(client, rep)
    assert resp.headers["location"].endswith("google_meet=MEET_SCOPE_MISSING")
    conn = (
        await client.get("/api/v1/integrations/google-meet/connection", headers=rep.headers)
    ).json()
    assert conn["connected"] is False


async def test_token_refresh_rotation_and_revocation(client: AsyncClient, rep: Account) -> None:
    fake = google(rotate=True)
    await meet_ready(client, rep)
    lookup = await client.get(
        "/api/v1/integrations/google-meet/meetings", headers=rep.headers, params={"link": MEET_LINK}
    )
    assert lookup.status_code == 200
    space_call = fake.calls("GET", "/v2/spaces/abc-defg-hij")[-1]
    assert space_call.headers["authorization"] == f"Bearer {ACCESS2}"  # minted, never stored
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        row = await session.scalar(select(IntegrationUserConnection))
        assert row is not None
        assert decrypt(row.encrypted_refresh_token) == "1//REFRESH-ROTATED-SECRET"
    # Google revokes the grant -> reauth required, a clear error instead of a crash.
    fake.on(
        "POST", "oauth2.googleapis.com/token", httpx.Response(400, json={"error": "invalid_grant"})
    )
    again = await client.get(
        "/api/v1/integrations/google-meet/meetings", headers=rep.headers, params={"link": MEET_LINK}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "MEET_AUTH_EXPIRED"
    conn = (
        await client.get("/api/v1/integrations/google-meet/connection", headers=rep.headers)
    ).json()
    assert conn["status"] == "REAUTH_REQUIRED"
    gone = await client.delete("/api/v1/integrations/google-meet/connection", headers=rep.headers)
    assert gone.status_code == 204
    assert fake.calls("POST", "oauth2.googleapis.com/revoke")
    conn = (
        await client.get("/api/v1/integrations/google-meet/connection", headers=rep.headers)
    ).json()
    assert conn["connected"] is False


# ---- meeting lookup ------------------------------------------------------------------------------


async def test_meeting_lookup_and_not_active(client: AsyncClient, rep: Account) -> None:
    fake = google()
    await meet_ready(client, rep)
    ok = await client.get(
        "/api/v1/integrations/google-meet/meetings",
        headers=rep.headers,
        params={"link": "abc-defg-hij"},
    )
    assert ok.json() == {
        "space": "spaces/SPACE123",
        "meeting_code": "abc-defg-hij",
        "meeting_uri": MEET_LINK,
        "active_conference": True,
    }
    fake.on(
        "GET",
        "meet.googleapis.com/v2/spaces/abc-defg-hij",
        httpx.Response(200, json={"name": "spaces/SPACE123", "meetingCode": "abc-defg-hij"}),
    )
    idle = await client.get(
        "/api/v1/integrations/google-meet/meetings", headers=rep.headers, params={"link": MEET_LINK}
    )
    assert idle.json()["active_conference"] is False
    bad = await client.get(
        "/api/v1/integrations/google-meet/meetings",
        headers=rep.headers,
        params={"link": "https://evil.example.com/x"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "MEET_INVALID_LINK"


# ---- 7, 8, 11. media session lifecycle, event normalisation, reconnection ------------------------


async def test_call_lifecycle_connect_events_and_end(client: AsyncClient, rep: Account) -> None:
    fake = google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep, "abc-defg-hij")
    assert call["channel"] == "GOOGLE_MEET"
    assert call["status"] == "INITIATED"
    assert call["meeting_url"] == MEET_LINK
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == ANSWER
    assert body["trace_id"] == "trace-1"
    assert body["mock"] is False
    assert body["media_ws_path"].startswith(
        f"/api/v1/telephony/media/google-meet/{call['id']}?token="
    )
    sent = fake.calls("POST", ":connectActiveConference")[-1]
    assert json.loads(sent.content) == {"offer": OFFER}
    assert ACCESS not in json.dumps(body)

    async def status() -> str:
        live = await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)
        return str(live.json()["call"]["status"])

    assert (await event(client, rep, call["id"], "waiting")).json()["outcome"] == "applied"
    assert await status() == "CONNECTED"
    same = uuid.uuid4().hex
    assert (await event(client, rep, call["id"], "joined", event_id=same)).json()[
        "outcome"
    ] == "applied"
    assert (await event(client, rep, call["id"], "joined", event_id=same)).json()[
        "outcome"
    ] == "duplicate"
    assert await status() == "ACTIVE"
    # Reconnect while the call is live (e.g. after a network drop): signalling works again.
    again = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert again.status_code == 200
    assert (
        await event(client, rep, call["id"], "disconnected", "CONFERENCE_ENDED")
    ).status_code == 200
    await telephony.wait_finalized(uuid.UUID(call["id"]))
    assert await status() == "COMPLETED"
    late = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert late.status_code == 409  # ended calls cannot be re-attached
    # Developer Preview access is now confirmed by a real (here: simulated) connection.
    detail = (await client.get("/api/v1/integrations/google-meet", headers=rep.headers)).json()
    assert {r["key"]: r["ok"] for r in detail["requirements"]}["developer_preview"] is True


async def test_failure_before_join_marks_call_failed(client: AsyncClient, rep: Account) -> None:
    google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    await event(client, rep, call["id"], "failed", "SESSION_UNHEALTHY")
    await telephony.wait_finalized(uuid.UUID(call["id"]))
    live = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert live["call"]["status"] == "FAILED"


async def test_end_button_ends_meet_call(client: AsyncClient, rep: Account) -> None:
    google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    await event(client, rep, call["id"], "joined")
    ended = await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    assert ended.status_code == 200
    await telephony.wait_finalized(uuid.UUID(call["id"]))
    live = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert live["call"]["status"] == "COMPLETED"


# ---- 12-17. failures mapped to clear codes -------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "code", "status"),
    [
        (
            google_error(
                403, grpc="PERMISSION_DENIED", message="The caller does not have permission"
            ),
            "MEET_MEDIA_API_NOT_ELIGIBLE",
            403,
        ),
        (
            google_error(400, reason="CONSENTER_ABSENT", grpc="FAILED_PRECONDITION"),
            "MEET_CONSENT_REQUIRED",
            409,
        ),
        (
            google_error(400, reason="NO_ACTIVE_CONFERENCE", grpc="FAILED_PRECONDITION"),
            "MEET_CONFERENCE_NOT_ACTIVE",
            409,
        ),
        (
            google_error(400, grpc="FAILED_PRECONDITION", message="CONNECTIONS_EXHAUSTED"),
            "MEET_CONNECTIONS_EXHAUSTED",
            409,
        ),
        (google_error(400, reason="DISABLED_DUE_TO_ENCRYPTION"), "MEET_DISABLED", 403),
        (google_error(400, reason="INCOMPATIBLE_DEVICE"), "MEET_INCOMPATIBLE_PARTICIPANT", 409),
        (google_error(403, reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT"), "MEET_SCOPE_MISSING", 403),
        (google_error(503, grpc="UNAVAILABLE"), "MEET_UNAVAILABLE", 503),
    ],
)
async def test_connect_failures_are_explained(
    client: AsyncClient, rep: Account, response: httpx.Response, code: str, status: int
) -> None:
    fake = google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    fake.on("POST", ":connectActiveConference", response)
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code
    assert resp.json()["error"]["message"]
    live = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert live["call"]["status"] == "INITIATED"  # a failed attach does not end the call
    detail = (await client.get("/api/v1/integrations/google-meet", headers=rep.headers)).json()
    assert {r["key"]: r["ok"] for r in detail["requirements"]}["developer_preview"] is False


async def test_meeting_not_active_or_missing(client: AsyncClient, rep: Account) -> None:
    fake = google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    fake.on(
        "GET",
        "meet.googleapis.com/v2/spaces/abc-defg-hij",
        httpx.Response(200, json={"name": "spaces/SPACE123"}),
    )
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MEET_CONFERENCE_NOT_ACTIVE"
    assert not fake.calls("POST", ":connectActiveConference")
    fake.on(
        "GET", "meet.googleapis.com/v2/spaces/abc-defg-hij", google_error(404, grpc="NOT_FOUND")
    )
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MEET_MEETING_NOT_FOUND"


async def test_invalid_offer_never_reaches_google(client: AsyncClient, rep: Account) -> None:
    fake = google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER.replace("m=audio", "m=video", 1)},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MEET_INVALID_OFFER"
    assert not fake.calls("POST", ":connectActiveConference")


async def test_not_connected_user_gets_clear_error(
    client: AsyncClient, rep: Account, register: Register
) -> None:
    google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        await session.execute(text("DELETE FROM integration_user_connections"))
        await session.commit()
    resp = await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "google_meet_not_connected"


# ---- 18. tenant isolation / authorisation --------------------------------------------------------


async def test_other_tenant_cannot_attach_or_report(
    client: AsyncClient, rep: Account, register: Register
) -> None:
    fake = google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    other = await register(client, "intruder@example.com", "Other Co")
    for path, body in (
        ("connect", {"offer": OFFER}),
        ("events", {"event_id": uuid.uuid4().hex, "state": "joined"}),
    ):
        resp = await client.post(
            f"/api/v1/calls/{call['id']}/google-meet/{path}", headers=other.headers, json=body
        )
        assert resp.status_code == 404, resp.text
    assert not fake.calls("POST", ":connectActiveConference")
    conn = (
        await client.get("/api/v1/integrations/google-meet/connection", headers=other.headers)
    ).json()
    assert conn["connected"] is False
    session = await open_session(TenantContext(company_id=other.company_id))
    async with session:
        assert (await session.scalars(select(IntegrationUserConnection))).all() == []
        assert (await session.scalars(select(Integration))).all() == []


# ---- 9, 10, 19. audio normalisation -> STT -> copilot; no raw audio stored -----------------------


def pcm_frame(seq: int) -> str:
    audio = bytes([seq % 256, 0]) * 320  # 20 ms of 16 kHz 16-bit mono
    return json.dumps(
        {
            "event": "media",
            "track": "mixed",
            "seq": seq,
            "payload": base64.b64encode(audio).decode(),
        }
    )


async def test_meet_audio_reaches_stt_and_copilot_without_being_stored(
    client: AsyncClient, rep: Account, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stt = PcmStt("Right now we track everything in Excel and we have five branches.")
    set_stt_provider(stt)
    google()
    await meet_ready(client, rep, persistence="PERSISTED")
    call = await meet_call(client, rep)
    await client.post(
        f"/api/v1/calls/{call['id']}/google-meet/connect",
        headers=rep.headers,
        json={"offer": OFFER},
    )
    await event(client, rep, call["id"], "joined")
    call_id = uuid.UUID(call["id"])
    ingest = MediaIngest(call_id)
    await ingest.handle(
        parse_json_media_message(
            json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
        )
    )
    for seq in range(1, 51):  # 1 s of audio
        await ingest.handle(parse_json_media_message(pcm_frame(seq)))
    await ingest.handle(parse_json_media_message(json.dumps({"event": "stop"})))
    await event(client, rep, call["id"], "disconnected", "CLIENT_LEFT")
    await telephony.wait_finalized(call_id)

    assert stt.formats == [("linear16", 16000)]
    assert stt.bytes_received == 50 * 640
    live = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert [(s["speaker"], s["text"]) for s in live["transcript"]] == [("UNKNOWN", stt.say)]
    notes = {(n["kind"], n["text"]) for n in live["notes"]}
    assert ("CURRENT_SOLUTION", "Uses Excel") in notes
    assert ("REQUIREMENT", "5 branches") in notes
    session = await open_session()
    async with session:
        binary = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type IN ('bytea', 'oid')"
                )
            )
        ).all()
    assert binary == []
    payload = base64.b64encode(bytes([7, 0]) * 320).decode()
    assert payload not in caplog.text


async def test_transient_meeting_stores_no_transcript(client: AsyncClient, rep: Account) -> None:
    stt = PcmStt("We track everything in Excel.")
    set_stt_provider(stt)
    google()
    await meet_ready(client, rep, persistence="TRANSIENT")
    call = await meet_call(client, rep)
    assert call["transcript_persistence"] == "TRANSIENT"
    call_id = uuid.UUID(call["id"])
    ingest = MediaIngest(call_id)
    await ingest.handle(
        parse_json_media_message(
            json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
        )
    )
    for seq in range(1, 11):
        await ingest.handle(parse_json_media_message(pcm_frame(seq)))
    await ingest.handle(parse_json_media_message(json.dumps({"event": "stop"})))
    await client.post(f"/api/v1/calls/{call['id']}/end", headers=rep.headers)
    await telephony.wait_finalized(call_id)
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        assert (await session.scalars(select(TranscriptSegment))).all() == []


def test_media_socket_rejects_bad_token_for_meet() -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app

    with TestClient(app, base_url="https://testserver") as tc:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            tc.websocket_connect(
                f"/api/v1/telephony/media/google-meet/{uuid.uuid4()}?token=x"
            ) as ws,
        ):
            ws.receive_text()
        assert exc.value.code == 4401


# ---- 20. no secret / token leakage ---------------------------------------------------------------


async def test_no_tokens_or_secrets_in_logs_responses_or_audit(
    client: AsyncClient, rep: Account, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    google()
    await meet_ready(client, rep)
    call = await meet_call(client, rep)
    bodies = [
        (
            await client.post(
                f"/api/v1/calls/{call['id']}/google-meet/connect",
                headers=rep.headers,
                json={"offer": OFFER},
            )
        ).text,
        (await client.get("/api/v1/integrations/google-meet", headers=rep.headers)).text,
        (await client.get("/api/v1/integrations/google-meet/connection", headers=rep.headers)).text,
    ]
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        audit_rows = (
            await session.execute(text("SELECT action, details::text FROM audit_logs"))
        ).all()
    haystack = caplog.text + "".join(bodies) + json.dumps([list(r) for r in audit_rows])
    for secret in (ACCESS, ACCESS2, REFRESH, CLIENT_SECRET, AUTH_CODE):
        assert secret not in haystack, secret
    actions = {r[0] for r in audit_rows}
    assert {"google_meet.user_connected", "google_meet.media_connected"} <= actions
