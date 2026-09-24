"""Manual check of the REAL Google Speech-to-Text adapter with a speech recording (no Teams).

Requires your Google Cloud setup (see docs/integrations/google-stt.md):
    GOOGLE_CLOUD_PROJECT=<project>  GOOGLE_APPLICATION_CREDENTIALS=<service-account.json>

Usage (from backend/):
    uv run python scripts/google_stt_smoke.py path/to/speech.wav --language en-IN

The WAV must be 16 kHz, 16-bit, mono PCM (e.g. `ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav`).
Audio is streamed in real time in 20 ms frames exactly like the Teams gateway sends it, through
GoogleSpeechToTextProvider. Nothing is stored. Transcript text is printed to YOUR terminal only.
This contacts Google (billable) - run it only when you intend to.
"""

import argparse
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("STT_PROVIDER", "google")


async def main(path: Path, language: str, realtime: bool) -> int:
    from app.common.config import get_settings
    from app.speech.google import GoogleSpeechToTextProvider
    from app.speech.provider import STTError

    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
            print("WAV must be 16 kHz, 16-bit, mono PCM", file=sys.stderr)
            return 2
        pcm = w.readframes(w.getnframes())

    settings = get_settings()
    provider = GoogleSpeechToTextProvider(settings)
    started = time.monotonic()
    stream = await provider.open_stream(language=language, sample_rate=16000, encoding="linear16")

    async def feed() -> None:
        for i in range(0, len(pcm), 640):  # 20 ms frames
            await stream.send(pcm[i : i + 640])
            if realtime:
                await asyncio.sleep(0.02)
        await stream.close()

    feeder = asyncio.create_task(feed())
    try:
        async for r in stream.results():
            kind = "FINAL  " if r.is_final else "interim"
            elapsed = time.monotonic() - started
            print(f"[{elapsed:6.2f}s] {kind} {r.start_ms:>6}-{r.end_ms:<6} ms  {r.text}")
    except STTError as exc:
        print(f"STT error: {exc.code}", file=sys.stderr)
        return 1
    finally:
        await feeder
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("wav", type=Path)
    parser.add_argument("--language", default="en-IN", choices=["en-IN", "en-US", "hi-IN", "de-DE"])
    parser.add_argument("--fast", action="store_true", help="do not pace audio in real time")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.wav, args.language, not args.fast)))
