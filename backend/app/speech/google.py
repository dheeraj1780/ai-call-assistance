"""Google Cloud Speech-to-Text v2 streaming adapter (Chirp 3 by default).

Implements the provider-neutral ``SpeechToTextProvider``/``STTStream`` interface
(app/speech/provider.py); nothing Google-specific leaks into the live pipeline.

Per stream (one per audio track of a call):
- audio from ``send`` is aggregated into ~``STT_CHUNK_MS`` chunks (<= 25 KB per request) and put
  on a BOUNDED queue; when the provider is slow or reconnecting the oldest chunks are dropped and
  counted (live assistance prefers recent audio; memory stays bounded; callers never block);
- a background task runs ``StreamingRecognize`` with an explicit language and decoding config and
  interim results enabled; responses become ``STTResult`` (interim + final, utterance end offsets
  converted to call-relative milliseconds);
- streams are rotated before Google's 5-minute limit; transient failures (UNAVAILABLE,
  DEADLINE_EXCEEDED, INTERNAL, ABORTED, RESOURCE_EXHAUSTED, connect timeout) reconnect with
  backoff up to ``STT_MAX_RECONNECTS`` consecutive times; auth/config failures are not retried;
- ``close`` flushes, ends the request stream, waits briefly for final results, then cancels.

Raw audio exists only in memory until sent or dropped; it is never written anywhere. Logs carry
counts/latencies/error codes only - never audio, transcript text or credentials.

IMPLEMENTED + unit-tested against the real google-cloud-speech request/response types with a fake
transport. EXTERNAL PROVIDER VERIFICATION REQUIRED (needs a Google Cloud project + credentials).
"""

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from app.common.config import Settings
from app.speech.provider import (
    SUPPORTED_LANGUAGES,
    STTAuthError,
    STTConfigError,
    STTError,
    STTResult,
    STTStream,
    STTTimeoutError,
    STTUnavailableError,
)

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 24_000  # Google limit: 25 KB of audio per streaming request
BACKOFF_S = 0.5
_CLOSE = object()


class StreamingClient(Protocol):
    """The subset of ``google.cloud.speech_v2.SpeechAsyncClient`` we use (fakeable)."""

    def streaming_recognize(
        self, requests: AsyncIterator[Any] | None = ..., *, timeout: Any = ...
    ) -> Awaitable[AsyncIterable[Any]]: ...


def _types() -> Any:
    from google.cloud.speech_v2.types import cloud_speech

    return cloud_speech


def default_client_factory(settings: Settings) -> Callable[[], StreamingClient]:
    """Real client using Application Default Credentials and the regional endpoint."""

    def build() -> StreamingClient:
        from google.api_core.client_options import ClientOptions
        from google.cloud.speech_v2 import SpeechAsyncClient

        location = settings.google_stt_location
        options = (
            ClientOptions(api_endpoint=f"{location}-speech.googleapis.com")
            if location != "global"
            else None
        )
        credentials = None
        if settings.google_application_credentials:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        client: StreamingClient = SpeechAsyncClient(credentials=credentials, client_options=options)
        return client

    return build


def classify(exc: BaseException) -> STTError:
    """Map google-api-core / gRPC errors to provider-neutral STT errors (no message text kept,
    it may contain request details)."""
    if isinstance(exc, STTError):
        return exc
    if isinstance(exc, TimeoutError):
        return STTTimeoutError("stt connect/response timeout")
    try:
        from google.api_core import exceptions as g
        from google.auth import exceptions as auth_exc
    except ImportError:  # pragma: no cover - dependency is installed
        return STTUnavailableError(type(exc).__name__)
    if isinstance(exc, g.Unauthenticated | g.PermissionDenied | auth_exc.GoogleAuthError):
        return STTAuthError(type(exc).__name__)
    if isinstance(exc, g.InvalidArgument | g.FailedPrecondition | g.NotFound):
        return STTConfigError(type(exc).__name__)
    if isinstance(exc, g.DeadlineExceeded):
        return STTTimeoutError(type(exc).__name__)
    if isinstance(
        exc,
        g.ServiceUnavailable
        | g.InternalServerError
        | g.Aborted
        | g.ResourceExhausted
        | g.Cancelled,
    ):
        return STTUnavailableError(type(exc).__name__)
    return STTUnavailableError(type(exc).__name__)


class GoogleSTTStream:
    def __init__(
        self,
        *,
        client: StreamingClient,
        settings: Settings,
        language: str,
        sample_rate: int,
        encoding: str,
    ) -> None:
        if language not in SUPPORTED_LANGUAGES:
            raise STTConfigError(f"unsupported language {language}")
        if encoding not in ("linear16", "mulaw"):
            raise STTConfigError(f"unsupported encoding {encoding}")
        self._client = client
        self._settings = settings
        self.language = language
        self.sample_rate = sample_rate
        self.encoding = encoding
        bytes_per_sample = 2 if encoding == "linear16" else 1
        self._bytes_per_ms = sample_rate * bytes_per_sample / 1000
        self._chunk_bytes = max(
            int(self._bytes_per_ms * settings.stt_chunk_ms), 2 if encoding == "linear16" else 1
        )
        self._pending = bytearray()
        self._audio: asyncio.Queue[Any] = asyncio.Queue(maxsize=settings.stt_audio_queue_chunks)
        self._results: asyncio.Queue[STTResult | STTError | None] = asyncio.Queue()
        self._closing = False
        self._closed = False
        # Timing: audio position (ms since stream open) of everything sent + wall time sent.
        self._sent_ms = 0.0  # across rotations
        self._stream_base_ms = 0.0  # audio position where the current provider stream started
        self._sent_marks: deque[tuple[float, float]] = deque(maxlen=2000)
        self._last_final_end_ms = 0
        # Stats (logged at the end; never contents).
        self.stats: dict[str, float] = {
            "chunks_sent": 0,
            "chunks_dropped": 0,
            "bytes_sent": 0,
            "streams": 0,
            "reconnects": 0,
            "interim": 0,
            "final": 0,
            "malformed": 0,
            "interim_latency_ms_max": 0,
            "final_latency_ms_max": 0,
            "final_latency_ms_sum": 0,
        }
        self._opened_at = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="google-stt")
        logger.info(
            "stt_session_started",
            extra={
                "provider": "google",
                "model": settings.google_stt_model,
                "language": language,
                "encoding": encoding,
                "sample_rate": sample_rate,
            },
        )

    # -- ingress ---------------------------------------------------------------------------

    async def send(self, audio: bytes) -> None:
        if self._closing or not audio:
            return
        if self._task.done():
            # The background stream failed for good; surface it to the caller (LiveSession
            # contains it and may reopen a new stream later).
            raise STTUnavailableError("stt stream is not running")
        self._pending.extend(audio)
        while len(self._pending) >= self._chunk_bytes:
            size = min(self._chunk_bytes, MAX_REQUEST_BYTES)
            chunk = bytes(self._pending[:size])
            del self._pending[:size]
            self._enqueue(chunk)

    def _enqueue(self, chunk: bytes) -> None:
        if self._audio.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio.get_nowait()  # drop the OLDEST chunk (bounded memory)
                self.stats["chunks_dropped"] += 1
        self._audio.put_nowait(chunk)

    # -- provider stream ---------------------------------------------------------------------

    def _config_request(self) -> Any:
        t = _types()
        encoding = (
            t.ExplicitDecodingConfig.AudioEncoding.LINEAR16
            if self.encoding == "linear16"
            else t.ExplicitDecodingConfig.AudioEncoding.MULAW
        )
        s = self._settings
        return t.StreamingRecognizeRequest(
            recognizer=f"projects/{s.google_cloud_project}/locations/{s.google_stt_location}/recognizers/_",
            streaming_config=t.StreamingRecognitionConfig(
                config=t.RecognitionConfig(
                    explicit_decoding_config=t.ExplicitDecodingConfig(
                        encoding=encoding,
                        sample_rate_hertz=self.sample_rate,
                        audio_channel_count=1,
                    ),
                    # Explicit language - no automatic language detection.
                    language_codes=[self.language],
                    model=s.google_stt_model,
                    features=t.RecognitionFeatures(enable_automatic_punctuation=True),
                ),
                streaming_features=t.StreamingRecognitionFeatures(interim_results=True),
            ),
        )

    async def _requests(self, deadline: float) -> AsyncIterator[Any]:
        t = _types()
        yield self._config_request()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return  # rotate before the provider's stream limit
            try:
                item = await asyncio.wait_for(self._audio.get(), timeout=min(remaining, 1.0))
            except TimeoutError:
                continue
            if item is _CLOSE:
                return
            now = time.monotonic()
            self._sent_ms += len(item) / self._bytes_per_ms
            self._sent_marks.append((self._sent_ms, now))
            self.stats["chunks_sent"] += 1
            self.stats["bytes_sent"] += len(item)
            yield t.StreamingRecognizeRequest(audio=item)

    async def _run(self) -> None:
        s = self._settings
        failures = 0
        while True:
            self.stats["streams"] += 1
            self._stream_base_ms = self._sent_ms
            deadline = time.monotonic() + s.stt_stream_max_seconds
            try:
                responses = await asyncio.wait_for(
                    self._client.streaming_recognize(
                        requests=self._requests(deadline),
                        timeout=s.stt_stream_max_seconds + 30,
                    ),
                    timeout=s.stt_connect_timeout_seconds,
                )
                async for response in responses:
                    failures = 0
                    await self._on_response(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = classify(exc)
                failures += 1
                logger.warning(
                    "stt_stream_error",
                    extra={"provider": "google", "error": err.code, "attempt": failures},
                )
                if not err.retryable or failures > s.stt_max_reconnects or self._closing:
                    await self._results.put(err)
                    self._finish()
                    return
                self.stats["reconnects"] += 1
                await asyncio.sleep(BACKOFF_S * failures)
                continue
            if self._closing and self._audio.empty():
                self._finish()
                return
            # Normal end without close: the stream reached its maximum age -> rotate.

    def _finish(self) -> None:
        self._results.put_nowait(None)

    def _latency_ms(self, end_ms: float) -> float | None:
        """Wall time since the audio at ``end_ms`` (stream-relative, ms) was sent."""
        target = self._stream_base_ms + end_ms
        for pos, sent_at in self._sent_marks:
            if pos >= target:
                return (time.monotonic() - sent_at) * 1000
        return None

    async def _on_response(self, response: Any) -> None:
        results = getattr(response, "results", None)
        if results is None:
            self.stats["malformed"] += 1
            return
        for r in results:
            alternatives = getattr(r, "alternatives", None) or []
            if not alternatives:
                continue  # e.g. end-of-utterance marker without text
            text = str(getattr(alternatives[0], "transcript", "") or "").strip()
            if not text:
                continue
            is_final = bool(getattr(r, "is_final", False))
            offset = getattr(r, "result_end_offset", None)
            try:
                end_ms = int(offset.total_seconds() * 1000) if offset is not None else 0
            except (AttributeError, TypeError, ValueError):
                self.stats["malformed"] += 1
                continue
            abs_end = int(self._stream_base_ms) + end_ms
            start_ms = min(self._last_final_end_ms, abs_end)
            conf = float(getattr(alternatives[0], "confidence", 0.0) or 0.0)
            latency = self._latency_ms(end_ms)
            if is_final:
                self._last_final_end_ms = abs_end
                self.stats["final"] += 1
                if latency is not None:
                    self.stats["final_latency_ms_sum"] += latency
                    self.stats["final_latency_ms_max"] = max(
                        self.stats["final_latency_ms_max"], latency
                    )
            else:
                self.stats["interim"] += 1
                if latency is not None:
                    self.stats["interim_latency_ms_max"] = max(
                        self.stats["interim_latency_ms_max"], latency
                    )
            await self._results.put(
                STTResult(
                    text=text,
                    is_final=is_final,
                    start_ms=start_ms,
                    end_ms=abs_end,
                    # Chirp does not report confidence in streaming (0.0) -> unknown.
                    confidence=conf if 0 < conf <= 1 else None,
                )
            )

    # -- egress ------------------------------------------------------------------------------

    async def results(self) -> AsyncIterator[STTResult]:
        while True:
            item = await self._results.get()
            if item is None:
                return
            if isinstance(item, STTError):
                raise item
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        if self._pending:
            self._enqueue(bytes(self._pending))
            self._pending.clear()
        self._enqueue_close()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._results.put_nowait(None)
        except Exception:  # pragma: no cover - _run handles its own errors
            self._results.put_nowait(None)
        finals = self.stats["final"]
        logger.info(
            "stt_session_ended",
            extra={
                "provider": "google",
                "language": self.language,
                "seconds": round(time.monotonic() - self._opened_at, 1),
                "chunks_sent": int(self.stats["chunks_sent"]),
                "chunks_dropped": int(self.stats["chunks_dropped"]),
                "streams": int(self.stats["streams"]),
                "reconnects": int(self.stats["reconnects"]),
                "interim_results": int(self.stats["interim"]),
                "final_results": int(finals),
                "malformed": int(self.stats["malformed"]),
                "final_latency_ms_avg": round(self.stats["final_latency_ms_sum"] / finals)
                if finals
                else None,
                "final_latency_ms_max": round(self.stats["final_latency_ms_max"]),
                "interim_latency_ms_max": round(self.stats["interim_latency_ms_max"]),
            },
        )

    def _enqueue_close(self) -> None:
        if self._audio.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio.get_nowait()
                self.stats["chunks_dropped"] += 1
        self._audio.put_nowait(_CLOSE)


class GoogleSpeechToTextProvider:
    name = "google"

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], StreamingClient] | None = None,
    ) -> None:
        if not settings.google_cloud_project:
            raise STTConfigError("GOOGLE_CLOUD_PROJECT is not configured")
        self._settings = settings
        self._factory = client_factory or default_client_factory(settings)
        self._client: StreamingClient | None = None

    def _get_client(self) -> StreamingClient:
        if self._client is None:
            try:
                self._client = self._factory()
            except (OSError, ValueError):
                # Key file missing/unreadable or not a service-account key (path never logged).
                raise STTAuthError("google credentials file unreadable") from None
            except Exception as exc:  # e.g. DefaultCredentialsError: no credentials found
                raise classify(exc) from None
        return self._client

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> STTStream:
        return GoogleSTTStream(
            client=self._get_client(),
            settings=self._settings,
            language=language,
            sample_rate=sample_rate,
            encoding=encoding,
        )
