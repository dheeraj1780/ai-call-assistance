"""LiveSession: the real-time pipeline for one active call.

    provider media frames -> STT stream per track -> final segments -> DB (with expires_at)
                                                  -> browser (live hub)
                                                  -> copilot engine

Raw audio is handed to the STT stream and dropped; it is never written anywhere. STT failures
are contained per track (bounded re-open with backoff); copilot failures are contained in the
engine. Neither can end the phone call, which lives at the telephony provider.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.calls.models import Call
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.copilot.engine import CopilotEngine, SegmentView
from app.live.hub import hub
from app.live.models import Speaker, TranscriptSegment
from app.speech.provider import STTError, STTResult, STTStream, get_stt_provider
from app.telephony.models import CallRoute
from app.telephony.provider import Track
from app.tenants.models import Company

logger = logging.getLogger(__name__)

TRACK_SPEAKER: dict[Track, tuple[Speaker, float | None]] = {
    Track.AGENT: (Speaker.SALES_REP, 1.0),
    Track.CUSTOMER: (Speaker.CUSTOMER, 1.0),
    Track.MIXED: (Speaker.UNKNOWN, None),
}
MAX_STT_REOPENS = 3
REOPEN_BACKOFF_S = 2.0


@dataclass
class _TrackStream:
    stream: STTStream | None = None
    reader: asyncio.Task[None] | None = None
    failures: int = 0
    retry_at: float = 0.0


@dataclass
class LiveSession:
    company_id: uuid.UUID
    call_id: uuid.UUID
    contact_id: uuid.UUID
    retention_days: int
    engine: CopilotEngine
    next_seq: int = 1
    closed: bool = False
    stt_ok: bool = True
    tracks: dict[Track, _TrackStream] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    frames_received: int = 0

    @classmethod
    async def open(cls, call_id: uuid.UUID) -> "LiveSession":
        async with get_session_factory()() as session:
            route = await session.get(CallRoute, call_id)
            if route is None:
                raise LookupError("unknown call")
            await set_tenant_context(session, TenantContext(company_id=route.company_id))
            call = await session.scalar(
                select(Call).where(Call.company_id == route.company_id, Call.id == call_id)
            )
            company = await session.scalar(select(Company).where(Company.id == route.company_id))
            if call is None or company is None:
                raise LookupError("unknown call")
            max_seq = await session.scalar(
                select(func.max(TranscriptSegment.seq)).where(
                    TranscriptSegment.company_id == route.company_id,
                    TranscriptSegment.call_id == call_id,
                )
            )
            engine = await CopilotEngine.create(
                route.company_id,
                call_id,
                call.contact_id,
                call.objective,
                company.transcript_retention_days,
            )
            return cls(
                company_id=route.company_id,
                call_id=call_id,
                contact_id=call.contact_id,
                retention_days=company.transcript_retention_days,
                engine=engine,
                next_seq=(max_seq or 0) + 1,
            )

    # -- audio ingress --------------------------------------------------------------------

    async def on_audio(self, track: Track, audio: bytes) -> None:
        """Forward one audio chunk to STT. The bytes are not retained after this call."""
        if self.closed:
            return
        self.frames_received += 1
        ts = await self._ensure_stream(track)
        if ts is None or ts.stream is None:
            return  # STT unavailable: audio is dropped, never buffered to disk
        try:
            await ts.stream.send(audio)
        except (STTError, Exception):
            logger.warning("stt_send_failed", extra={"call_id": str(self.call_id)})
            await self._stt_failed(track, ts)

    async def _ensure_stream(self, track: Track) -> _TrackStream | None:
        ts = self.tracks.setdefault(track, _TrackStream())
        if ts.stream is not None:
            return ts
        if ts.failures > MAX_STT_REOPENS or time.monotonic() < ts.retry_at:
            return None
        try:
            ts.stream = await get_stt_provider().open_stream(
                language=get_settings().stt_language, sample_rate=8000, encoding="mulaw"
            )
        except (STTError, Exception):
            logger.warning("stt_open_failed", extra={"call_id": str(self.call_id)})
            await self._stt_failed(track, ts)
            return None
        ts.reader = asyncio.create_task(self._read(track, ts), name=f"stt-{self.call_id}-{track}")
        if not self.stt_ok:
            self.stt_ok = True
            hub.publish(self.call_id, "stt.status", {"state": "ok"})
        return ts

    async def _stt_failed(self, track: Track, ts: _TrackStream) -> None:
        stream, ts.stream = ts.stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        ts.failures += 1
        ts.retry_at = time.monotonic() + REOPEN_BACKOFF_S * ts.failures
        if self.stt_ok:
            self.stt_ok = False
            hub.publish(
                self.call_id,
                "stt.status",
                {
                    "state": "unavailable",
                    "detail": "Live transcription interrupted; the call continues.",
                },
            )

    async def _read(self, track: Track, ts: _TrackStream) -> None:
        stream = ts.stream
        if stream is None:
            return
        try:
            async for result in stream.results():
                await self._on_result(track, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("stt_stream_failed", extra={"call_id": str(self.call_id)})
            if ts.stream is stream:
                await self._stt_failed(track, ts)

    async def _on_result(self, track: Track, result: STTResult) -> None:
        speaker, speaker_conf = TRACK_SPEAKER[track]
        text = result.text.strip()
        if not text:
            return
        if not result.is_final:
            hub.publish(
                self.call_id, "transcript.partial", {"speaker": speaker.value, "text": text[:1000]}
            )
            return
        async with self._lock:
            seq = self.next_seq
            self.next_seq += 1
            segment = TranscriptSegment(
                id=uuid.uuid4(),
                company_id=self.company_id,
                call_id=self.call_id,
                seq=seq,
                speaker=speaker.value,
                speaker_confidence=speaker_conf,
                text=text[:5000],
                start_ms=result.start_ms,
                end_ms=max(result.end_ms, result.start_ms),
                stt_confidence=result.confidence,
                source=get_stt_provider().name,
                expires_at=datetime.now(UTC) + timedelta(days=self.retention_days),
            )
            async with get_session_factory()() as session:
                await set_tenant_context(session, TenantContext(company_id=self.company_id))
                session.add(segment)
                await session.commit()
        hub.publish(self.call_id, "transcript.final", segment_payload(segment))
        await self.engine.on_segment(SegmentView(segment.id, seq, speaker, segment.text))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for ts in self.tracks.values():
            if ts.stream is not None:
                with contextlib.suppress(Exception):
                    await ts.stream.close()
        readers = [ts.reader for ts in self.tracks.values() if ts.reader is not None]
        if readers:
            await asyncio.wait(readers, timeout=5)
        if self.engine.llm_task is not None and not self.engine.llm_task.done():
            await asyncio.wait([self.engine.llm_task], timeout=10)
        await self.engine.close()


def segment_payload(s: TranscriptSegment) -> dict[str, object]:
    return {
        "id": str(s.id),
        "seq": s.seq,
        "speaker": s.speaker,
        "speaker_confidence": s.speaker_confidence,
        "text": s.text,
        "start_ms": s.start_ms,
        "end_ms": s.end_ms,
        "stt_confidence": s.stt_confidence,
        "source": s.source,
        "is_final": True,
    }


# ---- Registry ---------------------------------------------------------------------------------

_sessions: dict[uuid.UUID, LiveSession] = {}
_open_lock = asyncio.Lock()


async def get_or_open(call_id: uuid.UUID) -> LiveSession:
    async with _open_lock:
        existing = _sessions.get(call_id)
        if existing is not None and not existing.closed:
            return existing
        session = await LiveSession.open(call_id)
        _sessions[call_id] = session
        return session


def get_session(call_id: uuid.UUID) -> LiveSession | None:
    s = _sessions.get(call_id)
    return s if s is not None and not s.closed else None


async def close_session(call_id: uuid.UUID) -> None:
    session = _sessions.pop(call_id, None)
    if session is not None:
        await session.close()


async def close_all() -> None:
    for call_id in list(_sessions):
        await close_session(call_id)
