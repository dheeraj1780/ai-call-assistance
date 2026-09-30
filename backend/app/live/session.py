"""LiveSession: the real-time pipeline for one active call.

    provider media frames -> STT stream per track -> final segments -> DB (with expires_at)
                                                  -> browser (live hub)
                                                  -> copilot engine

Raw audio is handed to the STT stream and dropped; it is never written anywhere. When a call's
``transcript_persistence`` is not PERSISTED (e.g. a Teams call without declared recording), the
session is TRANSIENT: transcript and copilot output go to the live screen only and nothing
derived from the audio is written to the database. STT failures
are contained per track (bounded re-open with backoff); copilot failures are contained in the
engine. Neither can end the phone call, which lives at the telephony provider.
"""

import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.calls.models import Call, TranscriptPersistence
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
TRANSIENT_MAX_SEGMENTS = 5000
REOPEN_BACKOFF_S = 2.0


@dataclass
class _TrackStream:
    stream: STTStream | None = None
    reader: asyncio.Task[None] | None = None
    failures: int = 0
    retry_at: float = 0.0
    last_audio: float = 0.0
    audio_ms: float = 0.0  # track audio received so far


@dataclass
class LiveSession:
    company_id: uuid.UUID
    call_id: uuid.UUID
    contact_id: uuid.UUID
    retention_days: int
    engine: CopilotEngine
    persist: bool = True
    encoding: str = "mulaw"
    sample_rate: int = 8000
    language: str = "en-IN"
    opened_at: float = field(default_factory=time.monotonic)
    # Streams whose audio input finished and whose outstanding results are still draining.
    draining: list[tuple[STTStream, asyncio.Task[None]]] = field(default_factory=list)
    input_finished: bool = False
    next_seq: int = 1
    closed: bool = False
    stt_ok: bool = True
    tracks: dict[Track, _TrackStream] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    frames_received: int = 0
    _idle_watch: asyncio.Task[None] | None = None
    idle_finalized: int = 0
    # Live-only (TRANSIENT) calls: the transcript is kept here, in server memory, for the duration
    # of the call so a refreshed/reconnected browser can resume. Never written to the database.
    transient_transcript: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    async def open(
        cls, call_id: uuid.UUID, *, encoding: str = "mulaw", sample_rate: int = 8000
    ) -> "LiveSession":
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
            persist = call.transcript_persistence == TranscriptPersistence.PERSISTED
            engine = await CopilotEngine.create(
                route.company_id,
                call_id,
                call.contact_id,
                call.objective,
                company.transcript_retention_days,
                persist=persist,
            )
            return cls(
                company_id=route.company_id,
                call_id=call_id,
                contact_id=call.contact_id,
                retention_days=company.transcript_retention_days,
                engine=engine,
                persist=persist,
                encoding=encoding,
                sample_rate=sample_rate,
                language=call.language or get_settings().stt_language,
                next_seq=(max_seq or 0) + 1,
            )

    # -- audio ingress --------------------------------------------------------------------

    async def on_audio(self, track: Track, audio: bytes) -> None:
        """Forward one audio chunk to STT. The bytes are not retained after this call."""
        if self.closed:
            return
        self.frames_received += 1
        state = self.tracks.setdefault(track, _TrackStream())
        state.last_audio = time.monotonic()
        position = state.audio_ms
        state.audio_ms += len(audio) / self._bytes_per_ms
        ts = await self._ensure_stream(track, position)
        if ts is None or ts.stream is None:
            return  # STT unavailable: audio is dropped, never buffered to disk
        if self._idle_watch is None:
            self._idle_watch = asyncio.create_task(
                self._watch_idle_tracks(), name=f"stt-idle-{self.call_id}"
            )
        try:
            await ts.stream.send(audio)
        except (STTError, Exception):
            logger.warning("stt_send_failed", extra={"call_id": str(self.call_id)})
            await self._stt_failed(track, ts)

    def find_transient_segment(self, segment_id: str) -> dict[str, object] | None:
        for payload in self.transient_transcript:
            if payload["id"] == segment_id:
                return payload
        return None

    @property
    def _bytes_per_ms(self) -> float:
        return self.sample_rate / 1000 * (2 if self.encoding == "linear16" else 1)

    async def _ensure_stream(self, track: Track, position_ms: float = 0.0) -> _TrackStream | None:
        ts = self.tracks.setdefault(track, _TrackStream())
        if ts.stream is not None:
            return ts
        if ts.failures > MAX_STT_REOPENS or time.monotonic() < ts.retry_at:
            return None
        try:
            ts.stream = await get_stt_provider().open_stream(
                language=self.language,
                sample_rate=self.sample_rate,
                encoding=self.encoding,
            )
        except (STTError, Exception):
            logger.warning("stt_open_failed", extra={"call_id": str(self.call_id)})
            await self._stt_failed(track, ts)
            return None
        # The stream is bound now (not looked up when the task first runs): input may already be
        # finished by then, and the reader must still drain this stream's results. A new stream
        # counts time from zero; its results are placed on the track's timeline.
        ts.reader = asyncio.create_task(
            self._read(track, ts.stream, position_ms), name=f"stt-{self.call_id}-{track}"
        )
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

    async def _read(self, track: Track, stream: STTStream, offset_ms: float) -> None:
        ts = self.tracks[track]
        try:
            async for result in stream.results():
                if offset_ms:
                    result = dataclasses.replace(
                        result,
                        start_ms=result.start_ms + round(offset_ms),
                        end_ms=result.end_ms + round(offset_ms),
                    )
                await self._on_result(track, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("stt_stream_failed", extra={"call_id": str(self.call_id)})
            if ts.stream is stream:
                await self._stt_failed(track, ts)

    async def _watch_idle_tracks(self) -> None:
        """Finish the STT stream of a track that stopped receiving audio (its speaker stopped
        talking), so the utterance's final result is emitted now rather than on the next turn.
        Streams that finished draining are closed and forgotten."""
        limit = get_settings().stt_track_idle_finalize_ms / 1000
        while not self.closed:
            await asyncio.sleep(min(0.2, limit / 4))
            now = time.monotonic()
            for track, ts in list(self.tracks.items()):
                if ts.stream is not None and now - ts.last_audio >= limit:
                    stream, reader = ts.stream, ts.reader
                    ts.stream, ts.reader = None, None
                    if reader is not None:  # registered first: close() waits for it
                        self.draining.append((stream, reader))
                    with contextlib.suppress(Exception):
                        await stream.finish()
                    self.idle_finalized += 1
                    logger.info(
                        "stt_track_idle_finalized",
                        extra={"call_id": str(self.call_id), "track": track.value},
                    )
            for entry in [e for e in self.draining if e[1].done()]:
                self.draining.remove(entry)
                with contextlib.suppress(Exception):
                    await entry[0].close()

    async def finish_input(self) -> None:
        """AUDIO_INPUT_FINISHED (e.g. the provider media stream stopped): tell every STT stream
        that no more audio is coming. Their outstanding results keep flowing into the
        transcript/copilot while they drain. New audio would open fresh streams."""
        finished = 0
        for ts in self.tracks.values():
            stream, reader = ts.stream, ts.reader
            ts.stream, ts.reader = None, None
            if stream is None:
                continue
            with contextlib.suppress(Exception):
                await stream.finish()
            if reader is not None:
                self.draining.append((stream, reader))
            finished += 1
        if finished and not self.input_finished:
            self.input_finished = True
            hub.publish(self.call_id, "session.phase", {"phase": "input_finished"})
            logger.info("audio_input_finished", extra={"call_id": str(self.call_id)})

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
            if self.persist:
                async with get_session_factory()() as session:
                    await set_tenant_context(session, TenantContext(company_id=self.company_id))
                    session.add(segment)
                    await session.commit()
        payload = segment_payload(segment)
        if not self.persist:
            self.transient_transcript.append(payload)
            del self.transient_transcript[:-TRANSIENT_MAX_SEGMENTS]
        hub.publish(self.call_id, "transcript.final", payload)
        processing_started = time.monotonic()
        await self.engine.on_segment(SegmentView(segment.id, seq, speaker, segment.text))
        logger.info(
            "transcript_final",
            extra={
                "call_id": str(self.call_id),
                "seq": seq,
                "stt_latency_ms": result.latency_ms,
                "copilot_ms": round((time.monotonic() - processing_started) * 1000, 1),
            },
        )

    async def close(self) -> None:
        """End of call, in explicit phases (each bounded):
        1. AUDIO_INPUT_FINISHED - stop accepting audio, finish every STT stream;
        2. STT_FINAL_RESULTS_DRAINED - wait until outstanding final results were delivered
           (timeout = STT_DRAIN_TIMEOUT_SECONDS + unrecognised audio backlog), so the last
           sentences reach the transcript/copilot and - if persisted - the database;
        3. COPILOT_FINISHED - let the copilot analyse the final segments;
        4. SESSION_CLOSED - release everything. Post-call processing runs only after this."""
        if self.closed:
            return
        self.closed = True  # no more audio is accepted from here on
        started = time.monotonic()
        if self._idle_watch is not None:
            self._idle_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_watch
        await self.finish_input()
        pending_ms = max((s.pending_audio_ms() for s, _ in self.draining), default=0.0)
        budget = get_settings().stt_drain_timeout_seconds + min(pending_ms / 1000, 120)
        drained = True
        readers = [reader for _, reader in self.draining]
        if readers:
            _, still_running = await asyncio.wait(readers, timeout=budget)
            drained = not still_running
            if still_running:
                logger.warning(
                    "stt_drain_timeout",
                    extra={"call_id": str(self.call_id), "timeout_s": round(budget, 1)},
                )
        for stream, _ in self.draining:
            with contextlib.suppress(Exception):
                await stream.close()
        for _, reader in self.draining:
            if not reader.done():
                reader.cancel()
        drain_ms = round((time.monotonic() - started) * 1000)
        hub.publish(self.call_id, "session.phase", {"phase": "stt_drained", "complete": drained})
        copilot_started = time.monotonic()
        await self.engine.finish(limit_s=get_settings().ai_realtime_timeout_seconds + 2)
        await self.engine.close()
        hub.publish(self.call_id, "session.phase", {"phase": "closed"})
        logger.info(
            "live_session_closed",
            extra={
                "call_id": str(self.call_id),
                "seconds": round(time.monotonic() - self.opened_at, 1),
                "audio_frames_received": self.frames_received,
                "final_segments": self.next_seq - 1,
                "persisted": self.persist,
                "stt_ok": self.stt_ok,
                "drained": drained,
                "drain_ms": drain_ms,
                "idle_finalized": self.idle_finalized,
                "copilot_finish_ms": round((time.monotonic() - copilot_started) * 1000),
            },
        )


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
        # Provenance of human corrections: ``text`` is current; ``original_text`` the STT output.
        "edited": s.edited_at is not None,
        "original_text": s.original_text,
    }


# ---- Media status (Teams gateway / provider stream), shown on the live screen ------------------

_media_status: dict[uuid.UUID, dict[str, str | None]] = {}


def set_media_status(call_id: uuid.UUID, state: str, reason: str | None = None) -> None:
    """``state``: "available" | "unavailable". Published to the live screen and kept for the
    snapshot. Unavailable media never ends the call."""
    _media_status[call_id] = {"state": state, "reason": reason}
    hub.publish(call_id, "media.status", {"state": state, "reason": reason})


def clear_media_status(call_id: uuid.UUID) -> None:
    """Forget the media state of a previous attempt (a re-opened call starts clean)."""
    _media_status.pop(call_id, None)


def media_status(call_id: uuid.UUID) -> dict[str, str | None] | None:
    return _media_status.get(call_id)


# ---- Registry ---------------------------------------------------------------------------------

_sessions: dict[uuid.UUID, LiveSession] = {}
_open_lock = asyncio.Lock()


async def get_or_open(
    call_id: uuid.UUID, *, encoding: str = "mulaw", sample_rate: int = 8000
) -> LiveSession:
    async with _open_lock:
        existing = _sessions.get(call_id)
        if existing is not None and not existing.closed:
            return existing
        session = await LiveSession.open(call_id, encoding=encoding, sample_rate=sample_rate)
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
