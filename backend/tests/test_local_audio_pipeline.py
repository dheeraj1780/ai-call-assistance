"""The local call-copilot pipeline with per-speaker tracks that go silent between turns (Teams
unmixed audio, the local development audio source): every utterance must be finalised while the
call goes on, and the copilot must understand the transcript exactly as real Google STT returned
it for the 7-line test conversation."""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.common.config import get_settings
from app.jobs.service import drain
from app.speech.provider import STTResult, set_stt_provider
from app.telephony import service as telephony
from app.telephony.provider import ProviderCallState, TelephonyEvent, parse_json_media_message
from app.telephony.service import MediaIngest
from tests.conftest import Account
from tests.test_realtime import provider_call_id, started_call

Register = Callable[..., Awaitable[Account]]

# What real Google STT (chirp_3, en-IN) returned for the local test audio.
DIALOGUE = [
    ("agent", "Hello, thanks for your time today."),
    ("agent", "How do you manage your inventory right now?"),
    ("customer", "Right now we track everything in Excel and we have five branches."),
    ("agent", "Understood."),
    ("agent", "Have you set aside a budget for a new system?"),
    ("customer", "Reconciling stock every week takes a lot of time."),
    (
        "customer",
        "Honestly, it looks expensive. Our budget is around 2 lakh rupees for this year and we "
        "want it running before Diwali.",
    ),
]
AGENDA = [
    {"title": "Current process", "question": "How do you manage your inventory today?"},
    {
        "title": "Pain points",
        "question": "What is the biggest problem with the current way of working?",
    },
    {"title": "Number of SKUs", "question": "Roughly how many products or SKUs do you manage?"},
    {
        "title": "Integration requirements",
        "question": "Which systems would this need to integrate with, for example Tally?",
    },
    {"title": "Budget", "question": "Have you set aside a budget for a new system?"},
    {"title": "Timeline", "question": "By when do you need the system running?"},
    {"title": "Decision maker", "question": "Who else is involved in the decision?"},
    {"title": "Next step", "question": "What would be a good next step from your side?"},
]


class EndpointingStream:
    """Like a streaming recogniser that only closes an utterance when the audio ends: it emits
    the final result when its input is finished - never on its own while audio just stops."""

    def __init__(self, opened: list["EndpointingStream"]) -> None:
        self.texts: list[str] = []
        self.queue: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self.finished = False
        opened.append(self)

    async def send(self, audio: bytes) -> None:
        assert not self.finished, "audio after finish"
        self.texts.append(audio.decode())

    async def results(self) -> AsyncIterator[STTResult]:
        while (item := await self.queue.get()) is not None:
            yield item

    async def finish(self) -> None:
        if not self.finished:
            self.finished = True
            if self.texts:
                await self.queue.put(STTResult(" ".join(self.texts), True, 0, 100, 0.9, 50.0))
            await self.queue.put(None)

    def pending_audio_ms(self) -> float:
        return 0.0

    async def close(self) -> None:
        await self.finish()


class EndpointingProvider:
    name = "endpointing-test"

    def __init__(self) -> None:
        self.opened: list[EndpointingStream] = []

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> Any:
        return EndpointingStream(self.opened)


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme")
    await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    return acct


@pytest.fixture
def fast_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "stt_track_idle_finalize_ms", 200)


def frame(track: str, text: str, seq: int) -> str:
    payload = base64.b64encode(text.encode()).decode()
    return json.dumps({"event": "media", "track": track, "seq": seq, "payload": payload})


async def speak(ingest: MediaIngest, track: str, text: str, seq: int) -> None:
    await ingest.handle(parse_json_media_message(frame(track, text, seq)))


async def wait_for(predicate: Callable[[], Awaitable[bool]], limit_s: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit_s
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


async def live(client: AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.get(f"/api/v1/calls/{call_id}/live", headers=acct.headers)
    ).json()
    return body


async def test_silent_track_finalises_the_utterance_while_the_call_goes_on(
    client: AsyncClient, rep: Account, fast_idle: None
) -> None:
    provider = EndpointingProvider()
    set_stt_provider(provider)
    call = await started_call(client, rep, agenda=False)
    ingest = MediaIngest(uuid.UUID(call["id"]))
    await ingest.handle(parse_json_media_message(json.dumps({"event": "start"})))
    await speak(ingest, "agent", "Hello, thanks for your time today.", 1)

    async def texts() -> list[str]:
        return [s["text"] for s in (await live(client, rep, call["id"]))["transcript"]]

    # No more agent audio: the utterance is finalised without the agent speaking again.
    await wait_for(lambda: _eq(texts(), ["Hello, thanks for your time today."]))
    await speak(ingest, "agent", "How do you manage your inventory right now?", 2)
    await wait_for(lambda: _len(texts(), 2))
    snap = await live(client, rep, call["id"])
    assert snap["call"]["status"] == "ACTIVE"
    assert len(provider.opened) == 2  # the next turn opened a fresh stream
    first, second = snap["transcript"]
    # The new stream counts from zero; its segment is placed after the first on the track timeline
    # (8 kHz mu-law default: 8 bytes per ms of audio).
    assert first["start_ms"] == 0
    assert second["start_ms"] == round(len(b"Hello, thanks for your time today.") / 8)
    assert all(s.finished for s in provider.opened)


async def _eq(value: Awaitable[list[str]], expected: list[str]) -> bool:
    return await value == expected


async def _len(value: Awaitable[list[str]], n: int) -> bool:
    return len(await value) == n


async def test_seven_line_conversation_drives_the_copilot(
    client: AsyncClient, rep: Account, fast_idle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_stt_provider(EndpointingProvider())
    monkeypatch.setattr("tests.test_realtime.AGENDA", AGENDA)
    call = await started_call(client, rep)
    call_id = uuid.UUID(call["id"])
    ingest = MediaIngest(call_id)
    await ingest.handle(parse_json_media_message(json.dumps({"event": "start"})))
    for seq, (track, text) in enumerate(DIALOGUE, start=1):
        await speak(ingest, track, text, seq)

        async def arrived(n: int = seq) -> bool:
            return len((await live(client, rep, call["id"]))["transcript"]) >= n

        await wait_for(arrived)

    async def copilot_done() -> bool:
        # transcript.final is published before the copilot analysed the segment
        snap = await live(client, rep, call["id"])
        # (notes first, then the agenda): the last step for the last line is the Timeline item.
        return any(a["title"] == "Timeline" and a["status"] == "COMPLETED" for a in snap["agenda"])

    await wait_for(copilot_done)
    snap = await live(client, rep, call["id"])
    assert [(s["speaker"], s["text"]) for s in snap["transcript"]] == [
        ("SALES_REP" if t == "agent" else "CUSTOMER", x) for t, x in DIALOGUE
    ]
    notes = {(n["kind"], n["category"], n["text"]) for n in snap["notes"]}
    assert ("CURRENT_SOLUTION", None, "Uses Excel") in notes
    assert ("REQUIREMENT", None, "5 branches") in notes
    assert ("PAIN_POINT", None, "Reconciling stock every week takes a lot of time.") in notes
    assert ("BUDGET", None, "around 2 lakh rupees for this year") in notes
    assert ("TIMELINE", None, "Before Diwali") in notes
    assert any(k == "OBJECTION" and c == "PRICE" for k, c, _ in notes)

    agenda = {a["title"]: a["status"] for a in snap["agenda"]}
    assert agenda == {
        "Current process": "COMPLETED",
        "Pain points": "COMPLETED",
        "Number of SKUs": "NOT_STARTED",
        "Integration requirements": "NOT_STARTED",
        "Budget": "COMPLETED",
        "Timeline": "COMPLETED",
        "Decision maker": "NOT_STARTED",
        "Next step": "NOT_STARTED",
    }
    # Missing questions come from the agenda: never-discussed items, not a hard-coded list.
    missing = [i["content"] for i in snap["insights"] if i["type"] == "MISSING_AGENDA_ITEM"]
    assert missing
    titles = {a["title"] for a in AGENDA}
    assert all(m.removesuffix(" hasn't been discussed yet.") in titles for m in missing)

    pcid = await provider_call_id(call["id"])
    await telephony.process_event(
        "mock", TelephonyEvent("end", pcid, ProviderCallState.COMPLETED, datetime.now(UTC))
    )
    await telephony.wait_finalized(call_id)
    final = await live(client, rep, call["id"])
    # The final copilot pass after the drain adds no duplicate of a fact already shown.
    texts = [(n["kind"], n["text"].lower()) for n in final["notes"]]
    assert len(texts) == len(set(texts))
    contents = [i["content"] for i in final["insights"]]
    assert len(contents) == len(set(contents))

    await drain()  # post-call job (enqueued only after the drain) -> summary
    post = (await client.get(f"/api/v1/calls/{call['id']}/post-call", headers=rep.headers)).json()
    assert post["summary"]["status"] == "READY"
    assert "2 lakh" in post["summary"]["budget"]["value"]
    assert post["summary"]["timeline"]["value"] == "Before Diwali"

    async def action_titles() -> list[str]:
        resp = await client.get(
            "/api/v1/action-items", headers=rep.headers, params={"call_id": call["id"]}
        )
        return [i["title"] for i in resp.json()["items"]]

    # No next step was agreed, so the post-call analysis invents no action item ...
    assert await action_titles() == []
    # ... and the salesperson creates one from the call.
    created = await client.post(
        "/api/v1/action-items",
        headers=rep.headers,
        json={"call_id": call["id"], "title": "Send pricing for 5 branches before Diwali"},
    )
    assert created.status_code == 201, created.text
    assert await action_titles() == ["Send pricing for 5 branches before Diwali"]
