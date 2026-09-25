"""End-of-call ordering: AUDIO_INPUT_FINISHED -> STT_FINAL_RESULTS_DRAINED -> copilot finished
-> SESSION_CLOSED -> post-call job. The last sentence must reach the transcript and the copilot
before post-call processing starts, even when the STT provider answers after the call ended."""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.common.db import TenantContext
from app.jobs.models import Job
from app.live.hub import hub
from app.live.models import TranscriptSegment
from app.speech.provider import STTResult, set_stt_provider
from app.telephony import service as telephony
from app.telephony.provider import ProviderCallState, TelephonyEvent, parse_json_media_message
from app.telephony.service import MediaIngest
from tests.conftest import Account, open_session
from tests.test_realtime import provider_call_id, started_call

Register = Callable[..., Awaitable[Account]]


class SlowStream:
    """Emits each final `delay` seconds after its audio arrived and keeps working after input
    finished - like a real streaming recognizer with a backlog."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.queue: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self.pending: set[asyncio.Task[None]] = set()
        self.finished = False
        self._ender: asyncio.Task[None] | None = None

    async def send(self, audio: bytes) -> None:
        self.pending.add(asyncio.create_task(self._later(audio.decode())))

    async def _later(self, text: str) -> None:
        await asyncio.sleep(self.delay)
        await self.queue.put(STTResult(text, True, 0, 100, 0.9, latency_ms=self.delay * 1000))

    async def results(self) -> AsyncIterator[STTResult]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        tasks = list(self.pending)

        async def end() -> None:
            await asyncio.gather(*tasks)
            await self.queue.put(None)

        self._ender = asyncio.create_task(end())

    def pending_audio_ms(self) -> float:
        return self.delay * 1000

    async def close(self) -> None:
        await self.finish()


class SlowProvider:
    name = "slow-test"

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> Any:
        return SlowStream(self.delay)


@pytest.fixture
async def rep(client: AsyncClient, register: Register) -> Account:
    acct = await register(client, "rep@example.com", "Acme")
    await client.patch("/api/v1/me", headers=acct.headers, json={"phone": "+919800000001"})
    return acct


def frame(track: str, text: str, seq: int) -> str:
    payload = base64.b64encode(text.encode()).decode()
    return json.dumps({"event": "media", "track": track, "seq": seq, "payload": payload})


async def test_last_sentence_survives_call_end_and_postcall_runs_after_drain(
    client: AsyncClient, rep: Account
) -> None:
    set_stt_provider(SlowProvider(delay=0.8))
    call = await started_call(client, rep, agenda=False)
    call_id = uuid.UUID(call["id"])
    browser, _, _ = hub.subscribe(call_id, last_seq=None, epoch=None)
    ingest = MediaIngest(call_id)
    await ingest.handle(parse_json_media_message(json.dumps({"event": "start"})))
    await ingest.handle(
        parse_json_media_message(frame("customer", "We track everything in Excel.", 1))
    )
    await ingest.handle(
        parse_json_media_message(frame("customer", "Our budget is around 2 lakh for this year.", 2))
    )
    # The call ends immediately: nothing has been recognised yet.
    pcid = await provider_call_id(call["id"])
    await telephony.process_event(
        "mock", TelephonyEvent("end-1", pcid, ProviderCallState.COMPLETED, datetime.now(UTC))
    )
    await telephony.wait_finalized(call_id)

    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        segments = await session.scalars(select(TranscriptSegment).order_by(TranscriptSegment.seq))
        texts = [s.text for s in segments]
        job = await session.scalar(select(Job).where(Job.kind == "postcall.process"))
    assert texts == ["We track everything in Excel.", "Our budget is around 2 lakh for this year."]
    assert job is not None  # enqueued only after the drain

    events: list[dict[str, Any]] = []
    while not browser.empty():
        events.append(browser.get_nowait())
    hub.unsubscribe(call_id, browser)
    phases = [m["data"]["phase"] for m in events if m["type"] == "session.phase"]
    assert phases == ["input_finished", "stt_drained", "closed"]
    types = [m["type"] for m in events]
    # Final transcript + copilot notes arrive AFTER the call ended but BEFORE the session closed.
    assert types.index("call.status") < types.index("transcript.final")
    assert types.index("note.upserted") < types.index("session.phase") + 3
    closed_at = max(i for i, m in enumerate(events) if m["type"] == "session.phase")
    assert all(
        i < closed_at for i, t in enumerate(types) if t in ("transcript.final", "note.upserted")
    )


async def test_media_stop_finishes_input_without_ending_the_call(
    client: AsyncClient, rep: Account
) -> None:
    set_stt_provider(SlowProvider(delay=0.1))
    call = await started_call(client, rep, agenda=False)
    call_id = uuid.UUID(call["id"])
    ingest = MediaIngest(call_id)
    await ingest.handle(parse_json_media_message(json.dumps({"event": "start"})))
    await ingest.handle(parse_json_media_message(frame("agent", "Hello there.", 1)))
    await ingest.handle(parse_json_media_message(json.dumps({"event": "stop"})))
    await asyncio.sleep(0.4)
    snap = (await client.get(f"/api/v1/calls/{call['id']}/live", headers=rep.headers)).json()
    assert snap["call"]["status"] == "ACTIVE"  # the call goes on
    assert [s["text"] for s in snap["transcript"]] == ["Hello there."]


async def test_drain_is_bounded_when_stt_never_finishes(
    client: AsyncClient, rep: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.common.config import get_settings

    monkeypatch.setattr(get_settings(), "stt_drain_timeout_seconds", 0.2)
    set_stt_provider(SlowProvider(delay=60))  # pending_audio_ms = 60 s -> capped budget
    monkeypatch.setattr(SlowStream, "pending_audio_ms", lambda self: 0.0)
    call = await started_call(client, rep, agenda=False)
    call_id = uuid.UUID(call["id"])
    ingest = MediaIngest(call_id)
    await ingest.handle(parse_json_media_message(json.dumps({"event": "start"})))
    await ingest.handle(parse_json_media_message(frame("agent", "Never recognised.", 1)))
    pcid = await provider_call_id(call["id"])
    started = asyncio.get_running_loop().time()
    await telephony.process_event(
        "mock", TelephonyEvent("end-2", pcid, ProviderCallState.COMPLETED, datetime.now(UTC))
    )
    await telephony.wait_finalized(call_id)
    assert asyncio.get_running_loop().time() - started < 5
    session = await open_session(TenantContext(company_id=rep.company_id))
    async with session:
        assert await session.scalar(select(Job).where(Job.kind == "postcall.process")) is not None
