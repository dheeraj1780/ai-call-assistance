"""Provider-neutral streaming speech-to-text interface and the mock provider.

A stream is opened per audio track. Raw audio passes through ``send`` and is never kept
by our code. Results carry partial/final flags, timing, confidence and (when the provider
supports it) speaker labels; with separate telephony tracks the speaker is known from the
track instead of diarization.

MockSpeechToTextProvider: MOCKED. Treats each audio chunk as UTF-8 text of one utterance
(so scripted conversations can be "spoken" in tests and demos). It emits a partial and then
a final result. Failure injection lets tests prove that STT outages never end a call.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

SUPPORTED_LANGUAGES = ("en-IN", "en-US", "hi-IN", "de-DE")


class STTError(Exception):
    """Provider-neutral STT failure. ``code`` is safe to log and show."""

    code = "stt_error"
    retryable = False


class STTUnavailableError(STTError):
    code = "stt_unavailable"
    retryable = True


class STTTimeoutError(STTError):
    code = "stt_timeout"
    retryable = True


class STTAuthError(STTError):
    """Credentials missing/invalid or permission denied: retrying will not help."""

    code = "stt_auth_failed"


class STTConfigError(STTError):
    """The provider rejected the session configuration (language/model/encoding)."""

    code = "stt_invalid_config"


@dataclass(frozen=True)
class STTResult:
    text: str
    is_final: bool
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker_label: str | None = None  # provider diarization label, if any


class STTStream(Protocol):
    async def send(self, audio: bytes) -> None: ...
    def results(self) -> AsyncIterator[STTResult]: ...
    async def close(self) -> None: ...


class SpeechToTextProvider(Protocol):
    name: str

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> STTStream: ...


class _MockStream:
    def __init__(self, provider: "MockSpeechToTextProvider") -> None:
        self._provider = provider
        self._queue: asyncio.Queue[STTResult | STTError | None] = asyncio.Queue()
        self._started = time.monotonic()
        self._closed = False

    async def send(self, audio: bytes) -> None:
        if self._closed:
            return
        if self._provider.fail_after is not None:
            self._provider.fail_after -= 1
            if self._provider.fail_after < 0:
                await self._queue.put(STTError("mock STT outage"))
                return
        text = audio.decode("utf-8", errors="replace").strip()
        if not text:
            return
        now = int((time.monotonic() - self._started) * 1000)
        words = text.split()
        if len(words) > 3:
            await self._queue.put(
                STTResult(" ".join(words[: len(words) // 2]), False, now, now, None)
            )
        await self._queue.put(
            STTResult(text, True, now, now + 250 * max(1, len(words)), self._provider.confidence)
        )

    async def results(self) -> AsyncIterator[STTResult]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, STTError):
                raise item
            yield item

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._queue.put(None)


class MockSpeechToTextProvider:
    name = "mock"

    def __init__(self, confidence: float = 0.92) -> None:
        self.confidence = confidence
        self.fail_after: int | None = None  # fail after N chunks (test hook)
        self.fail_open: bool = False

    async def open_stream(self, *, language: str, sample_rate: int, encoding: str) -> STTStream:
        if self.fail_open:
            raise STTError("mock STT unavailable")
        return _MockStream(self)


_provider: SpeechToTextProvider | None = None


def get_stt_provider() -> SpeechToTextProvider:
    global _provider
    if _provider is None:
        from app.common.config import get_settings

        if get_settings().stt_provider == "google":
            from app.speech.google import GoogleSpeechToTextProvider

            _provider = GoogleSpeechToTextProvider(get_settings())
        else:
            _provider = MockSpeechToTextProvider()
    return _provider


def set_stt_provider(provider: SpeechToTextProvider) -> None:
    global _provider
    _provider = provider
