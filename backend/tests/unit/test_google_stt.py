"""Google Speech-to-Text v2 adapter: configuration, streaming normalisation, errors, timeouts,
cancellation, reconnect/rotation, backpressure, malformed responses and log hygiene.

Uses the REAL google-cloud-speech request/response types with a fake gRPC transport - no Google
credentials or network access are needed (and none are used)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from google.api_core import exceptions as gexc
from google.cloud.speech_v2.types import cloud_speech as t

from app.speech.google import GoogleSpeechToTextProvider, GoogleSTTStream, classify
from app.speech.provider import (
    STTAuthError,
    STTConfigError,
    STTResult,
    STTTimeoutError,
    STTUnavailableError,
)


def gerr(cls: type[Exception], message: str = "x") -> Exception:
    """Construct google-api-core exceptions (untyped constructors) in typed code."""
    return cls(message)


@dataclass
class StubSettings:
    google_cloud_project: str | None = "demo-project"
    google_stt_location: str = "us"
    google_stt_model: str = "chirp_3"
    stt_chunk_ms: int = 100
    stt_audio_queue_chunks: int = 50
    stt_stream_max_seconds: float = 60
    stt_connect_timeout_seconds: float = 2.0
    stt_max_reconnects: int = 2
    google_application_credentials: str | None = None
    stt_drain_timeout_seconds: float = 1.0


def pcm(ms: int, rate: int = 16000) -> bytes:
    return b"\x01\x00" * (rate * ms // 1000)


def response(text: str, *, final: bool, end_ms: int, confidence: float = 0.0) -> Any:
    return t.StreamingRecognizeResponse(
        results=[
            t.StreamingRecognitionResult(
                alternatives=[
                    t.SpeechRecognitionAlternative(transcript=text, confidence=confidence)
                ],
                is_final=final,
                result_end_offset=timedelta(milliseconds=end_ms),
                language_code="en-in",
            )
        ]
    )


Behaviour = Callable[["FakeClient", AsyncIterator[Any]], AsyncIterator[Any]]


class FakeClient:
    """Stands in for SpeechAsyncClient.streaming_recognize (same call shape)."""

    def __init__(self, *behaviours: Behaviour) -> None:
        self.behaviours = list(behaviours)
        self.calls = 0
        self.requests: list[list[Any]] = []

    def streaming_recognize(
        self, requests: AsyncIterator[Any] | None = None, *, timeout: Any = None
    ) -> Any:
        # Same shape as SpeechAsyncClient: a plain method returning an awaitable.
        assert requests is not None
        index = min(self.calls, len(self.behaviours) - 1)
        self.calls += 1
        self.requests.append([])
        return self._open(self.behaviours[index](self, self._record(requests)))

    async def _open(self, responses: AsyncIterator[Any]) -> AsyncIterator[Any]:
        return responses

    async def _record(self, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        bucket = self.requests[-1]
        async for r in requests:
            bucket.append(r)
            yield r


def echo(words_per_chunk: str = "hello") -> Behaviour:
    """Emits an interim + final result for every audio request (position-accurate offsets)."""

    async def run(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        position = 0
        n = 0
        async for r in requests:
            if not r.audio:
                continue
            n += 1
            position += len(r.audio) // 32  # 16 kHz * 2 bytes = 32 bytes per ms
            yield response(f"{words_per_chunk} {n}", final=False, end_ms=position)
            yield response(f"{words_per_chunk} {n}.", final=True, end_ms=position, confidence=0.87)

    return run


def failing(exc: Exception) -> Behaviour:
    async def run(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        raise exc
        yield  # pragma: no cover

    return run


def stream(client: FakeClient, settings: StubSettings | None = None, **kw: Any) -> GoogleSTTStream:
    return GoogleSTTStream(
        client=client,
        settings=settings or StubSettings(),  # type: ignore[arg-type]
        language=kw.get("language", "en-IN"),
        sample_rate=kw.get("sample_rate", 16000),
        encoding=kw.get("encoding", "linear16"),
    )


async def collect(s: GoogleSTTStream, count: int, limit_s: float = 3.0) -> list[STTResult]:
    out: list[STTResult] = []

    async def run() -> None:
        async for r in s.results():
            out.append(r)
            if len(out) >= count:
                return

    await asyncio.wait_for(run(), limit_s)
    return out


# ---- configuration -------------------------------------------------------------------------


async def test_session_config_is_explicit() -> None:
    client = FakeClient(echo())
    s = stream(client, language="hi-IN")
    await s.send(pcm(100))
    await collect(s, 2)
    await s.close()
    first = client.requests[0][0]
    assert first.recognizer == "projects/demo-project/locations/us/recognizers/_"
    cfg = first.streaming_config.config
    assert list(cfg.language_codes) == ["hi-IN"]  # explicit, no auto-detection
    assert cfg.model == "chirp_3"
    assert cfg.explicit_decoding_config.encoding == t.ExplicitDecodingConfig.AudioEncoding.LINEAR16
    assert cfg.explicit_decoding_config.sample_rate_hertz == 16000
    assert cfg.explicit_decoding_config.audio_channel_count == 1
    assert cfg.features.enable_automatic_punctuation is True
    assert first.streaming_config.streaming_features.interim_results is True
    assert not first.audio


async def test_mulaw_telephony_audio_config() -> None:
    client = FakeClient(echo())
    s = stream(client, sample_rate=8000, encoding="mulaw")
    await s.close()
    cfg = client.requests[0][0].streaming_config.config
    assert cfg.explicit_decoding_config.encoding == t.ExplicitDecodingConfig.AudioEncoding.MULAW
    assert cfg.explicit_decoding_config.sample_rate_hertz == 8000


@pytest.mark.parametrize(("language", "encoding"), [("fr-FR", "linear16"), ("en-IN", "opus")])
async def test_unsupported_language_or_encoding_is_rejected(language: str, encoding: str) -> None:
    with pytest.raises(STTConfigError):
        stream(FakeClient(echo()), language=language, encoding=encoding)


def test_provider_requires_project() -> None:
    with pytest.raises(STTConfigError):
        GoogleSpeechToTextProvider(StubSettings(google_cloud_project=None))  # type: ignore[arg-type]


def test_settings_validation_requires_project() -> None:
    from app.common.config import Settings

    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        Settings(
            _env_file=None,  # independent of the developer's backend/.env
            database_url="postgresql://u:p@localhost/db",
            jwt_secret="x" * 40,
            stt_provider="google",
            google_cloud_project=None,
        )
    with pytest.raises(ValueError, match="stt_language"):
        Settings(
            _env_file=None,
            database_url="postgresql://u:p@localhost/db",
            jwt_secret="x" * 40,
            stt_language="fr-FR",
        )


async def test_missing_credentials_become_auth_error() -> None:
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials() -> Any:
        raise gerr(DefaultCredentialsError, "no ADC")

    provider = GoogleSpeechToTextProvider(StubSettings(), client_factory=no_credentials)  # type: ignore[arg-type]
    with pytest.raises(STTAuthError):
        await provider.open_stream(language="en-IN", sample_rate=16000, encoding="linear16")


# ---- streaming normalisation ------------------------------------------------------------------


async def test_interim_and_final_results_are_normalised() -> None:
    client = FakeClient(echo())
    s = stream(client)
    for _ in range(10):  # 10 x 20 ms Teams frames -> 2 chunks of 100 ms
        await s.send(pcm(20))
    results = await collect(s, 4)
    await s.close()
    interim, final, _interim2, final2 = results
    assert (interim.is_final, final.is_final) == (False, True)
    assert final.text == "hello 1."
    assert final.end_ms == 100
    assert final.start_ms == 0
    assert final.confidence == pytest.approx(0.87)
    assert interim.confidence is None  # Chirp reports 0.0 in streaming -> unknown
    assert final2.start_ms == 100  # utterance starts where the previous final ended
    assert final2.end_ms == 200
    audio_requests = [r for r in client.requests[0] if r.audio]
    assert [len(r.audio) for r in audio_requests] == [3200, 3200]  # aggregated 100 ms chunks


async def test_chunks_never_exceed_request_limit() -> None:
    client = FakeClient(echo())
    s = stream(client, StubSettings(stt_chunk_ms=1000))
    await s.send(pcm(1000))  # 32,000 bytes -> split to respect the 25 KB request limit
    await collect(s, 2)
    await s.close()
    sizes = [len(r.audio) for r in client.requests[0] if r.audio]
    assert max(sizes) <= 24_000


async def test_malformed_and_empty_responses_are_skipped() -> None:
    async def weird(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        async for r in requests:
            if not r.audio:
                continue
            yield t.StreamingRecognizeResponse(
                results=[t.StreamingRecognitionResult(is_final=True)]
            )
            yield t.StreamingRecognizeResponse(
                results=[
                    t.StreamingRecognitionResult(
                        alternatives=[t.SpeechRecognitionAlternative(transcript="  ")]
                    )
                ]
            )
            yield object()  # not a response at all
            yield response("real text", final=True, end_ms=100)

    s = stream(FakeClient(weird))
    await s.send(pcm(100))
    [only] = await collect(s, 1)
    await s.close()
    assert only.text == "real text"
    assert s.stats["malformed"] == 1


# ---- errors, timeouts, reconnects -------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (gerr(gexc.PermissionDenied, "no"), STTAuthError),
        (gerr(gexc.Unauthenticated, "no"), STTAuthError),
        (gerr(gexc.InvalidArgument, "bad lang"), STTConfigError),
        (gerr(gexc.DeadlineExceeded, "slow"), STTTimeoutError),
        (gerr(gexc.ServiceUnavailable, "down"), STTUnavailableError),
        (gerr(gexc.ResourceExhausted, "quota"), STTUnavailableError),
        (TimeoutError(), STTTimeoutError),
        (RuntimeError("weird"), STTUnavailableError),
    ],
)
def test_error_normalisation(exc: Exception, expected: type) -> None:
    err = classify(exc)
    assert isinstance(err, expected)
    assert str(err) in (type(exc).__name__, "stt connect/response timeout")  # no provider text


async def test_auth_errors_are_not_retried() -> None:
    client = FakeClient(failing(gerr(gexc.PermissionDenied, "denied")))
    s = stream(client)
    await s.send(pcm(100))
    with pytest.raises(STTAuthError):
        await collect(s, 1)
    assert client.calls == 1
    await s.close()


async def test_transient_failure_reconnects_and_continues() -> None:
    client = FakeClient(failing(gerr(gexc.ServiceUnavailable, "blip")), echo())
    s = stream(client)
    await s.send(pcm(100))
    results = await collect(s, 2)
    await s.close()
    assert results[-1].is_final
    assert client.calls == 2
    assert s.stats["reconnects"] == 1


async def test_gives_up_after_bounded_reconnects() -> None:
    client = FakeClient(failing(gerr(gexc.ServiceUnavailable, "down")))
    s = stream(client, StubSettings(stt_max_reconnects=2))
    await s.send(pcm(100))
    with pytest.raises(STTUnavailableError):
        await collect(s, 1, limit_s=10)
    assert client.calls == 3
    with pytest.raises(STTUnavailableError):
        await s.send(pcm(100))  # the stream is dead; the live session contains this
    await s.close()


async def test_connect_timeout() -> None:
    class Hanging(FakeClient):
        def streaming_recognize(self, requests: Any = None, *, timeout: Any = None) -> Any:
            self.calls += 1
            return asyncio.sleep(30)

    client = Hanging(echo())
    s = stream(client, StubSettings(stt_connect_timeout_seconds=0.05, stt_max_reconnects=1))
    with pytest.raises(STTTimeoutError):
        await collect(s, 1, limit_s=5)
    assert client.calls == 2
    await s.close()


async def test_stream_rotation_before_provider_limit() -> None:
    client = FakeClient(echo())
    s = stream(client, StubSettings(stt_stream_max_seconds=0.3))
    await s.send(pcm(100))
    first = await collect(s, 2)
    await asyncio.sleep(0.5)  # the first stream ends at its maximum age
    await s.send(pcm(100))
    second = await collect(s, 2)
    await s.close()
    assert client.calls >= 2
    assert first[-1].end_ms == 100
    assert second[-1].end_ms == 200  # offsets continue across streams
    assert second[-1].start_ms == 100


# ---- cancellation / shutdown / backpressure ----------------------------------------------------


async def test_close_flushes_partial_audio_and_ends_results() -> None:
    client = FakeClient(echo())
    s = stream(client)
    await s.send(pcm(40))  # below one chunk: only sent on close
    await s.close()
    results = [r async for r in s.results()]
    assert results[-1].is_final
    assert [len(r.audio) for r in client.requests[0] if r.audio] == [1280]
    await s.close()  # idempotent
    await s.send(pcm(100))  # ignored after close


async def test_close_cancels_a_hung_provider() -> None:
    async def hang(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    s = stream(FakeClient(hang))
    await s.send(pcm(100))
    await asyncio.wait_for(s.close(), timeout=8)
    assert [r async for r in s.results()] == []


async def test_backpressure_drops_oldest_audio() -> None:
    async def slow(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        await asyncio.sleep(3600)  # never reads audio
        yield  # pragma: no cover

    s = stream(FakeClient(slow), StubSettings(stt_audio_queue_chunks=4))
    for _ in range(20):
        await s.send(pcm(100))
    assert s._audio.qsize() <= 4
    assert s.stats["chunks_dropped"] >= 15
    await asyncio.wait_for(s.close(), timeout=8)


async def test_streams_are_isolated() -> None:
    """Two calls/tracks never see each other's transcripts."""

    def tagged(tag: str) -> Behaviour:
        return echo(tag)

    a = stream(FakeClient(tagged("alpha")))
    b = stream(FakeClient(tagged("beta")))
    await a.send(pcm(100))
    await b.send(pcm(100))
    ra, rb = await asyncio.gather(collect(a, 2), collect(b, 2))
    await a.close()
    await b.close()
    assert all("alpha" in r.text for r in ra)
    assert all("beta" in r.text for r in rb)


async def test_logs_contain_no_audio_transcripts_or_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client = FakeClient(echo("confidential-quotation-details"))
    s = stream(client)
    await s.send(pcm(100))
    await collect(s, 2)
    await s.close()
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "confidential-quotation-details" not in text
    assert "\\x01\\x00" not in text
    ended = [r for r in caplog.records if r.getMessage() == "stt_session_ended"]
    assert ended
    assert ended[0].__dict__["final_results"] == 1
    assert ended[0].__dict__["language"] == "en-IN"


async def test_unreadable_key_file_is_an_auth_error(tmp_path: Any) -> None:
    from app.speech.google import default_client_factory

    missing = StubSettings()
    missing.google_application_credentials = str(tmp_path / "missing.json")
    provider = GoogleSpeechToTextProvider(missing, client_factory=default_client_factory(missing))  # type: ignore[arg-type]
    with pytest.raises(STTAuthError):
        await provider.open_stream(language="en-IN", sample_rate=16000, encoding="linear16")
    not_a_key = tmp_path / "bad.json"
    not_a_key.write_text('{"type": "authorized_user"}')
    bad = StubSettings()
    bad.google_application_credentials = str(not_a_key)
    provider = GoogleSpeechToTextProvider(bad, client_factory=default_client_factory(bad))  # type: ignore[arg-type]
    with pytest.raises(STTAuthError):
        await provider.open_stream(language="en-IN", sample_rate=16000, encoding="linear16")


# ---- end of call: input finished -> finals drained -> closed -----------------------------------


def delayed_final(delay_s: float) -> Behaviour:
    """Like a real recognizer working through a backlog: the final arrives after input ended."""

    async def run(client: FakeClient, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        position = 0
        async for r in requests:
            position += len(r.audio) // 32
        await asyncio.sleep(delay_s)
        yield response("the very last sentence.", final=True, end_ms=position)

    return run


async def test_finish_drains_final_result_after_fast_push() -> None:
    # The 2026-09-25 standalone test lost this final (audio pushed faster than real time, then
    # a fixed 5 s close). finish() now ends input and results() runs until drained.
    s = stream(FakeClient(delayed_final(1.2)), StubSettings(stt_drain_timeout_seconds=0.2))
    await s.send(pcm(3000))  # 3 s of audio, instantly
    await s.finish()
    assert s.pending_audio_ms() >= 2900  # the drain budget is sized to the backlog
    got = [r async for r in s.results()]
    assert [r.text for r in got] == ["the very last sentence."]
    assert got[0].latency_ms is not None
    await s.close()


async def test_close_waits_for_backlog_then_cancels_when_budget_exceeded() -> None:
    s = stream(FakeClient(delayed_final(1.0)), StubSettings(stt_drain_timeout_seconds=0.2))
    await s.send(pcm(1500))
    await s.close()  # budget 0.2 s + 1.5 s backlog > 1.0 s provider delay -> final kept
    assert [r.text async for r in s.results()] == ["the very last sentence."]

    slow = stream(FakeClient(delayed_final(30)), StubSettings(stt_drain_timeout_seconds=0.1))
    await slow.send(pcm(200))
    started = asyncio.get_running_loop().time()
    await slow.close()  # 0.1 s + 0.2 s backlog, then cancelled: bounded
    assert asyncio.get_running_loop().time() - started < 3
    assert [r async for r in slow.results()] == []


async def test_no_audio_is_accepted_after_finish() -> None:
    client = FakeClient(echo())
    s = stream(client)
    await s.send(pcm(100))
    await s.finish()
    await s.send(pcm(100))  # ignored: input already finished
    results = [r async for r in s.results()]
    assert sum(1 for r in results if r.is_final) == 1
    assert s.stats["chunks_sent"] == 1
