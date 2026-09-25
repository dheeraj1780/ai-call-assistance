"""DEVELOPMENT / SYNTHETIC-MEDIA end-to-end test (NOT a real Teams call).

    synthetic 16 kHz PCM (in memory)        <- stands in for the .NET gateway
      -> API media stream format            app.telephony.provider.parse_json_media_message
      -> MediaIngest -> LiveSession (per-track streams)
      -> GoogleSpeechToTextProvider (REAL)  with a scripted fake Google transport
      -> STT interim/final results -> transcript segments
      -> CopilotEngine -> notes / cards / agenda
      -> live hub events (what the React live-call screen receives)

The Teams signalling path uses the same gateway-event handler as the real gateway. Nothing here
contacts Google or Microsoft."""

import asyncio
import base64
import itertools
import json
import math
import struct
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from google.cloud.speech_v2.types import cloud_speech as t
from httpx import AsyncClient
from sqlalchemy import func, select

from app.common.db import TenantContext
from app.integrations.webhooks import GatewayEvent, handle_gateway_event
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.speech.google import GoogleSpeechToTextProvider
from app.speech.provider import set_stt_provider
from app.telephony import service as telephony
from app.telephony.models import CallRoute
from app.telephony.provider import parse_json_media_message
from app.telephony.service import MediaIngest
from tests.conftest import Account, open_session
from tests.integration_helpers import configure, validate_and_enable

Register = Callable[..., Awaitable[Account]]
MEETING = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d"
UTTERANCE_MS = 600
# What each track "says". The fake recognizer returns these for each utterance of synthetic audio.
SCRIPT = [
    ("agent", "How do you manage your inventory today?"),
    ("customer", "Right now we track everything in Excel and we have 5 branches."),
    ("agent", "Have you set aside a budget?"),
    ("customer", "Honestly this looks too expensive, our budget is around 2 lakh for this year."),
]


@dataclass
class STTSettings:
    google_cloud_project: str = "synthetic-project"
    google_stt_location: str = "us"
    google_stt_model: str = "chirp_3"
    stt_chunk_ms: int = 200
    stt_audio_queue_chunks: int = 100
    stt_stream_max_seconds: float = 60
    stt_connect_timeout_seconds: float = 2.0
    stt_max_reconnects: int = 2


class ScriptedRecognizer:
    """Fake Google transport: after each utterance's worth of audio on a stream, emits an interim
    and a final result with that track's next scripted sentence. One instance per provider; each
    streaming_recognize call (= one track) takes its lines from the language config it receives."""

    def __init__(self) -> None:
        self.lines = {
            "agent": [text for who, text in SCRIPT if who == "agent"],
            "customer": [text for who, text in SCRIPT if who == "customer"],
        }
        self.streams = 0
        self.languages: list[str] = []
        self.audio_bytes = 0

    def streaming_recognize(
        self, requests: AsyncIterator[Any] | None = None, *, timeout: Any = None
    ) -> Any:
        # Same shape as SpeechAsyncClient: a plain method returning an awaitable.
        assert requests is not None
        return self._open(self._run(requests))

    async def _open(self, responses: AsyncIterator[Any]) -> AsyncIterator[Any]:
        return responses

    async def _run(self, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        track: str | None = None
        received = 0
        position = 0
        async for r in requests:
            if r.streaming_config.config.language_codes:
                self.languages.append(r.streaming_config.config.language_codes[0])
                self.streams += 1
                continue
            received += len(r.audio)
            self.audio_bytes += len(r.audio)
            position += len(r.audio) // 32
            if track is None:
                # Our synthetic audio encodes the speaker in the sine frequency; decode it
                # like a (very) simple recognizer would.
                track = "agent" if _dominant_is_low(r.audio) else "customer"
            if received >= UTTERANCE_MS * 32 and self.lines[track]:
                text = self.lines[track].pop(0)
                received = 0
                words = text.split()
                yield _resp(" ".join(words[: len(words) // 2]), final=False, end_ms=position)
                yield _resp(text, final=True, end_ms=position)


def _resp(text: str, *, final: bool, end_ms: int) -> Any:
    return t.StreamingRecognizeResponse(
        results=[
            t.StreamingRecognitionResult(
                alternatives=[t.SpeechRecognitionAlternative(transcript=text)],
                is_final=final,
                result_end_offset=timedelta(milliseconds=end_ms),
            )
        ]
    )


def synthetic_pcm(ms: int, freq: float) -> bytes:
    """16 kHz 16-bit mono sine wave - synthetic audio, generated in memory only."""
    n = 16 * ms
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / 16000))) for i in range(n)
    )


def _dominant_is_low(chunk: bytes) -> bool:
    samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
    crossings = sum(1 for a, b in itertools.pairwise(samples) if (a < 0) != (b < 0))
    return crossings / (len(samples) / 16000) / 2 < 400  # agent = 220 Hz, customer = 660 Hz


def frame(track: str, seq: int, pcm: bytes) -> str:
    return json.dumps(
        {"event": "media", "track": track, "seq": seq, "payload": base64.b64encode(pcm).decode()}
    )


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    return await register(client, "rep@example.com", "Acme Traders")


async def planned_teams_call(
    client: AsyncClient, rep: Account, persistence: str, language: str
) -> dict[str, Any]:
    await configure(client, rep, "microsoft-teams", {"call_persistence": persistence}, mode="MOCK")
    await validate_and_enable(client, rep, "microsoft-teams", "REAL_TIME_CALL")
    contact = (
        await client.post("/api/v1/contacts", headers=rep.headers, json={"name": "ABC Industries"})
    ).json()
    call = await client.post(
        "/api/v1/calls",
        headers=rep.headers,
        json={
            "contact_id": contact["id"],
            "channel": "TEAMS",
            "meeting_url": MEETING,
            "language": language,
            "objective": "Qualify inventory needs",
        },
    )
    assert call.status_code == 201, call.text
    assert call.json()["language"] == language
    agenda = [
        {"title": "Current process", "question": "How do you manage inventory today?"},
        {"title": "Budget", "question": "Have you set aside a budget?"},
    ]
    await client.put(
        f"/api/v1/calls/{call.json()['id']}/agenda", headers=rep.headers, json={"items": agenda}
    )
    started = await client.post(f"/api/v1/calls/{call.json()['id']}/start", headers=rep.headers)
    assert started.status_code == 200, started.text
    body: dict[str, Any] = started.json()
    return body


async def play_synthetic_meeting(
    call_id: uuid.UUID, gateway_call_id: str, persistence: str
) -> None:
    async def gw(**kw: Any) -> None:
        await handle_gateway_event(
            "teams-mock",
            GatewayEvent(
                event_id=uuid.uuid4().hex, call_id=call_id, gateway_call_id=gateway_call_id, **kw
            ),
        )

    await gw(state="ESTABLISHING")
    if persistence == "RECORDING_DECLARED":
        await gw(recording_status="RECORDING_CONFIRMED")
    await gw(state="ESTABLISHED")
    await gw(media_status="AVAILABLE")
    ingest = MediaIngest(call_id)
    await ingest.handle(
        parse_json_media_message(
            json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
        )
    )
    seq = 0
    for who, _ in SCRIPT:
        audio = synthetic_pcm(UTTERANCE_MS, 220 if who == "agent" else 660)
        for i in range(0, len(audio), 640):  # 20 ms frames, like Teams
            seq += 1
            await ingest.handle(parse_json_media_message(frame(who, seq, audio[i : i + 640])))
        await asyncio.sleep(0.3)  # natural turn-taking; lets the recognizer answer
    await ingest.handle(parse_json_media_message(json.dumps({"event": "stop"})))
    await gw(state="TERMINATED")
    await telephony.wait_finalized(call_id)  # drain: final transcript + copilot, then post-call


async def gateway_call_id(call_id: str) -> str:
    session = await open_session()
    async with session:
        route = await session.get(CallRoute, uuid.UUID(call_id))
    assert route is not None
    assert route.provider_call_id
    return route.provider_call_id


async def test_synthetic_teams_call_through_google_adapter_to_live_events(
    client: AsyncClient, rep: Account
) -> None:
    recognizer = ScriptedRecognizer()
    set_stt_provider(GoogleSpeechToTextProvider(STTSettings(), client_factory=lambda: recognizer))  # type: ignore[arg-type]
    call = await planned_teams_call(client, rep, "RECORDING_DECLARED", "en-IN")
    call_id = uuid.UUID(call["id"])
    # Subscribe like the browser's live WebSocket does (interim results are live-only).
    browser, _, _ = hub.subscribe(call_id, last_seq=None, epoch=None)
    await play_synthetic_meeting(call_id, await gateway_call_id(call["id"]), "RECORDING_DECLARED")
    received: list[dict[str, Any]] = []
    while not browser.empty():
        received.append(browser.get_nowait())
    hub.unsubscribe(call_id, browser)

    # STT: two streams (one per speaker track), explicit language, synthetic audio consumed.
    assert recognizer.streams == 2
    assert recognizer.languages == ["en-IN", "en-IN"]
    assert recognizer.audio_bytes == len(SCRIPT) * UTTERANCE_MS * 32

    # Live screen events, in the order the browser received them.
    events = [m["type"] for m in received]
    assert "media.status" in events
    assert events.count("transcript.partial") >= len(SCRIPT)
    assert events.count("transcript.final") == len(SCRIPT)
    assert "note.upserted" in events
    assert "insight.created" in events
    assert "agenda.updated" in events

    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "COMPLETED"
    assert snap["pipeline"]["media"] == "available"
    assert [(s["speaker"], s["text"]) for s in snap["transcript"]] == [
        ("SALES_REP" if who == "agent" else "CUSTOMER", text) for who, text in SCRIPT
    ]
    assert all(s["source"] == "google" for s in snap["transcript"])
    kinds = {n["kind"] for n in snap["notes"]}
    assert {"CURRENT_SOLUTION", "BUDGET", "OBJECTION"} <= kinds
    assert any(i["type"] == "OBJECTION" for i in snap["insights"])
    statuses = {a["title"]: a["status"] for a in snap["agenda"]}
    assert statuses["Current process"] in ("IN_PROGRESS", "COMPLETED")


async def test_synthetic_transient_teams_call_stores_nothing(
    client: AsyncClient, rep: Account
) -> None:
    recognizer = ScriptedRecognizer()
    set_stt_provider(GoogleSpeechToTextProvider(STTSettings(), client_factory=lambda: recognizer))  # type: ignore[arg-type]
    call = await planned_teams_call(client, rep, "TRANSIENT", "hi-IN")
    call_id = uuid.UUID(call["id"])
    await play_synthetic_meeting(call_id, await gateway_call_id(call["id"]), "TRANSIENT")
    assert recognizer.languages == ["hi-IN", "hi-IN"]
    events = [e.type for e in hub.channel(call_id).buffer]
    assert events.count("transcript.final") == len(SCRIPT)
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        stored = await session.scalar(select(func.count()).select_from(TranscriptSegment))
    assert stored == 0


async def test_media_unavailable_is_shown_and_call_continues(
    client: AsyncClient, rep: Account
) -> None:
    call = await planned_teams_call(client, rep, "RECORDING_DECLARED", "en-US")
    call_id = uuid.UUID(call["id"])
    gw_id = await gateway_call_id(call["id"])
    for kw in (
        {"state": "ESTABLISHING"},
        {"state": "ESTABLISHED"},
        {
            "recording_status": "RECORDING_FAILED",
            "media_status": "UNAVAILABLE",
            "error_code": "recording_status_failed",
        },
    ):
        await handle_gateway_event(
            "teams-mock",
            GatewayEvent(event_id=uuid.uuid4().hex, call_id=call_id, gateway_call_id=gw_id, **kw),
        )
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "CONNECTED"  # the meeting goes on
    assert snap["pipeline"]["media"] == "unavailable"
    assert snap["pipeline"]["media_reason"] == "recording_status_failed"
    assert (
        snap["call"]["transcript_persistence"] == "TRANSIENT"
    )  # never persisted without recording


async def test_stt_outage_during_teams_call_is_contained(client: AsyncClient, rep: Account) -> None:
    from google.api_core import exceptions as gexc

    class Down:
        def streaming_recognize(self, requests: Any = None, *, timeout: Any = None) -> Any:
            return self._fail()

        async def _fail(self) -> Any:
            error: type[Exception] = gexc.PermissionDenied
            raise error("service account lacks speech.recognizer.recognize")

    set_stt_provider(GoogleSpeechToTextProvider(STTSettings(), client_factory=lambda: Down()))  # type: ignore[arg-type]
    call = await planned_teams_call(client, rep, "TRANSIENT", "en-IN")
    call_id = uuid.UUID(call["id"])
    await play_synthetic_meeting(call_id, await gateway_call_id(call["id"]), "TRANSIENT")
    events = [e for e in hub.channel(call_id).buffer if e.type == "stt.status"]
    assert events
    assert events[0].payload["state"] == "unavailable"
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "COMPLETED"  # STT failure never ends the call
