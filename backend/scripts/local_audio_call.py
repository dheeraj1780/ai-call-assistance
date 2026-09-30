"""LOCAL DEVELOPMENT AUDIO SOURCE - feeds real audio into a running CallCopilot API at real-time
pace and measures the live pipeline. It is NOT Teams media and does not replace a Teams test.

    WAV files / microphone  --(20 ms PCM frames, paced in real time)-->  API media WebSocket
      -> MediaIngest -> LiveSession -> SpeechToTextProvider (STT_PROVIDER=google: real Google)
      -> transcript -> copilot (agenda, notes, objections, suggestions) -> live WebSocket -> UI

Speaker labels (agent = salesperson, customer) are set explicitly per audio file for this test,
exactly like the Teams gateway labels unmixed tracks; they are not inferred by STT.
Audio is read locally and streamed; nothing is uploaded, stored or recorded.

Usage (API running on :8000 with STT_PROVIDER=google, frontend on :5173):

    uv run python scripts/local_audio_call.py --email you@example.com --password ... \\
        --setup --dialogue path/to/dialogue.json --report report.json

``--setup`` creates a test contact, puts Microsoft Teams in MOCK mode (no Microsoft contact),
plans a Teams-channel call with an agenda and starts it; open the printed live-call URL.
``--call <id>`` attaches to a call you started yourself in the UI instead.
``--mic --track customer --seconds 30`` streams your microphone (needs `pip install sounddevice`).

dialogue.json: [{"speaker": "agent"|"customer", "wav": "1.wav"}, ...]  (16 kHz mono 16-bit)
"""

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

FRAME_MS = 20
BYTES_PER_MS = 32  # 16 kHz * 16-bit mono

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


@dataclass
class Stats:
    started: float = field(default_factory=time.perf_counter)
    # per track: list of (audio position ms after this frame, wall time sent)
    sent: dict[str, list[tuple[int, float]]] = field(
        default_factory=lambda: {"agent": [], "customer": []}
    )
    position: dict[str, int] = field(default_factory=lambda: {"agent": 0, "customer": 0})
    finals: list[dict[str, Any]] = field(default_factory=list)
    partials: int = 0
    final_seen_at: dict[str, float] = field(default_factory=dict)
    copilot: list[dict[str, Any]] = field(default_factory=list)
    phases: list[tuple[str, float]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def t(self) -> float:
        return time.perf_counter() - self.started


def log(stats: Stats, text: str) -> None:
    print(f"[{stats.t():7.2f}s] {text}", flush=True)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
            raise SystemExit(f"{path}: must be 16 kHz, 16-bit, mono PCM")
        return w.readframes(w.getnframes())


class PacedSender:
    """Sends 20 ms frames on an absolute real-time schedule (no drift from sleep granularity)."""

    def __init__(self, ws: Any, stats: Stats) -> None:
        self.ws = ws
        self.stats = stats
        self.seq = 0
        self.next_at = time.perf_counter()

    async def frame(self, track: str, pcm: bytes) -> None:
        self.next_at += len(pcm) / BYTES_PER_MS / 1000
        delay = self.next_at - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        self.seq += 1
        await self.ws.send(
            json.dumps(
                {
                    "event": "media",
                    "track": track,
                    "seq": self.seq,
                    "payload": base64.b64encode(pcm).decode(),
                }
            )
        )
        self.stats.position[track] += len(pcm) // BYTES_PER_MS
        self.stats.sent[track].append((self.stats.position[track], time.perf_counter()))

    async def pause(self, ms: int) -> None:
        # Real conversation gap: nobody speaks, nothing is sent (like Teams unmixed audio).
        self.next_at += ms / 1000
        delay = self.next_at - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)


async def stream_dialogue(
    ws: Any, stats: Stats, items: list[dict[str, str]], base: Path, gap_ms: int
) -> None:
    sender = PacedSender(ws, stats)
    await ws.send(
        json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
    )
    for item in items:
        track = item["speaker"]
        pcm = read_wav((base / item["wav"]).resolve())
        log(stats, f"--> streaming {track} audio ({len(pcm) // BYTES_PER_MS} ms)")
        step = FRAME_MS * BYTES_PER_MS
        for i in range(0, len(pcm), step):
            await sender.frame(track, pcm[i : i + step])
        await sender.pause(gap_ms)


async def stream_mic(ws: Any, stats: Stats, track: str, seconds: float) -> None:
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError:
        raise SystemExit("Microphone input needs: uv pip install sounddevice") from None
    sender = PacedSender(ws, stats)
    await ws.send(
        json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
    )
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def callback(indata: Any, frames: int, t: Any, status: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    log(stats, f"--> microphone as {track} for {seconds:.0f} s - speak now")
    with sd.RawInputStream(
        samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=callback
    ):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pcm = await queue.get()
            sender.next_at = time.perf_counter()  # live source: already real time
            await sender.frame(track, pcm)


def sent_wall_time(stats: Stats, track: str, end_ms: int) -> float | None:
    for pos, wall in stats.sent.get(track, []):
        if pos >= end_ms:
            return wall
    return None


async def observe(ws_url: str, token: str, stats: Stats, closed: asyncio.Event) -> None:
    """Consumes the SAME live WebSocket the React live-call screen uses."""
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "last_seq": None, "epoch": None}))
        async for raw in ws:
            msg = json.loads(raw)
            kind, data = msg.get("type"), msg.get("data") or {}
            now = time.perf_counter()
            stats.events.append(kind)
            if kind == "transcript.partial":
                stats.partials += 1
            elif kind == "transcript.final":
                track = "customer" if data.get("speaker") == "CUSTOMER" else "agent"
                sent = sent_wall_time(stats, track, int(data.get("end_ms", 0)))
                visible = (now - sent) * 1000 if sent else None
                stats.finals.append(
                    {
                        "speaker": data.get("speaker"),
                        "text": data.get("text"),
                        "user_visible_ms": round(visible) if visible else None,
                    }
                )
                stats.final_seen_at[str(data.get("id"))] = now
                log(
                    stats,
                    f"TRANSCRIPT {data.get('speaker'):9} {data.get('text')}"
                    + (f"   (+{visible:.0f} ms after the audio was sent)" if visible else ""),
                )
            elif kind in ("note.upserted", "insight.created", "agenda.updated"):
                seg = data.get("source_segment_id")
                seen = stats.final_seen_at.get(str(seg)) if seg else None
                if seen is None and stats.final_seen_at:
                    seen = max(stats.final_seen_at.values())  # attribute to the latest segment
                after = (now - seen) * 1000 if seen else None
                stats.copilot.append(
                    {
                        "type": kind,
                        "data": data,
                        "after_final_ms": round(after) if after is not None else None,
                    }
                )
                if kind == "note.upserted":
                    log(stats, f"COPILOT note     {data.get('kind'):16} {data.get('text')}")
                elif kind == "insight.created":
                    log(stats, f"COPILOT card     {data.get('type'):16} {data.get('content')}")
                else:
                    log(
                        stats,
                        f"COPILOT agenda   {data.get('status'):16} "
                        f"item {str(data.get('id'))[:8]} ({data.get('status_reason')})",
                    )
            elif kind in (
                "session.phase",
                "call.status",
                "media.status",
                "stt.status",
                "copilot.status",
                "copilot.processing",
            ):
                label = data.get("phase") or data.get("status") or data.get("state")
                log(stats, f"STATUS  {kind:18} {label}")
                if kind == "session.phase":
                    stats.phases.append((str(data.get("phase")), now))
                    if data.get("phase") == "closed":
                        closed.set()
                        return


async def setup_call(api: httpx.AsyncClient, language: str) -> str:
    await api.put(
        "/api/v1/integrations/microsoft-teams/config",
        json={"mode": "MOCK", "values": {"call_persistence": "RECORDING_DECLARED"}},
    )
    await api.post("/api/v1/integrations/microsoft-teams/test")
    r = await api.post("/api/v1/integrations/microsoft-teams/capabilities/REAL_TIME_CALL/enable")
    r.raise_for_status()
    contact = (
        await api.post(
            "/api/v1/contacts",
            json={"name": "ABC Industries (local audio test)", "organization": "ABC Industries"},
        )
    ).json()
    call = await api.post(
        "/api/v1/calls",
        json={
            "contact_id": contact["id"],
            "channel": "TEAMS",
            "language": language,
            "meeting_url": "https://teams.microsoft.com/l/meetup-join/19%3alocal_audio_test%40thread.v2/0?context=%7b%22Tid%22%3a%2200000000-0000-0000-0000-000000000001%22%2c%22Oid%22%3a%2200000000-0000-0000-0000-000000000002%22%7d",
            "objective": "Qualify inventory management needs",
        },
    )
    call.raise_for_status()
    call_id = call.json()["id"]
    (await api.put(f"/api/v1/calls/{call_id}/agenda", json={"items": AGENDA})).raise_for_status()
    (await api.post(f"/api/v1/calls/{call_id}/start")).raise_for_status()
    return str(call_id)


def pct(values: list[float], q: float) -> float:
    return sorted(values)[min(len(values) - 1, int(q * len(values)))]


async def main(args: argparse.Namespace) -> int:
    base_url = args.api.rstrip("/")
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as api:
        login = await api.post(
            "/api/v1/auth/login", json={"email": args.email, "password": args.password}
        )
        if login.status_code != 200:
            print(f"login failed ({login.status_code})", file=sys.stderr)
            return 1
        token = login.json()["access_token"]
        api.headers["Authorization"] = f"Bearer {token}"
        call_id = await setup_call(api, args.language) if args.setup else args.call
        if not call_id:
            print("use --setup or --call <id>", file=sys.stderr)
            return 1
        print(
            "\nLOCAL DEVELOPMENT AUDIO SOURCE  (not Teams media)\n"
            f"live-call screen: {args.ui.rstrip('/')}/calls/{call_id}/live\n"
        )
        if args.start_delay:
            print(f"waiting {args.start_delay:.0f} s so you can open the live screen ...")
            await asyncio.sleep(args.start_delay)
        source = await api.post(f"/api/v1/calls/{call_id}/dev/audio-source")
        source.raise_for_status()
        info = source.json()
        stats = Stats()
        closed = asyncio.Event()
        observer = asyncio.create_task(
            observe(f"{ws_base}/api/v1/calls/{call_id}/live/ws", token, stats, closed)
        )
        await asyncio.sleep(0.5)
        log(stats, f"media stream attached (language {info['language']})")
        async with websockets.connect(f"{ws_base}{info['media_ws_path']}", max_size=2**20) as media:
            if args.mic:
                await stream_mic(media, stats, args.track, args.seconds)
            else:
                raw = await asyncio.to_thread(Path(args.dialogue).read_text, "utf-8-sig")
                items = json.loads(raw)
                await stream_dialogue(media, stats, items, Path(args.dialogue).parent, args.gap_ms)
            input_done = stats.t()
            await media.send(json.dumps({"event": "stop"}))
            log(stats, "AUDIO_INPUT_FINISHED sent; ending the call")
        (await api.post(f"/api/v1/calls/{call_id}/end")).raise_for_status()
        try:
            await asyncio.wait_for(closed.wait(), timeout=90)
        except TimeoutError:
            log(stats, "session did not report 'closed' within 90 s")
        observer.cancel()

        snap = (await api.get(f"/api/v1/calls/{call_id}/live")).json()
        post: dict[str, Any] = {}
        for _ in range(60):
            post = (await api.get(f"/api/v1/calls/{call_id}/post-call")).json()
            if (post.get("summary") or {}).get("status") in ("READY", "FAILED"):
                break
            await asyncio.sleep(1)
        items = (await api.get("/api/v1/action-items", params={"call_id": call_id})).json()

    visible = [f["user_visible_ms"] for f in stats.finals if f["user_visible_ms"] is not None]
    copilot_ms = [c["after_final_ms"] for c in stats.copilot if c["after_final_ms"] is not None]
    report = {
        "call_id": call_id,
        "label": "LOCAL DEVELOPMENT AUDIO SOURCE (not Teams media)",
        "language": info["language"],
        "transcript": [(s["speaker"], s["text"]) for s in snap["transcript"]],
        "finals_received_live": len(stats.finals),
        "partials_received_live": stats.partials,
        "user_visible_latency_ms": {
            "per_final": visible,
            "median": statistics.median(visible) if visible else None,
            "max": max(visible) if visible else None,
        },
        "copilot_after_final_ms": {
            "median": statistics.median(copilot_ms) if copilot_ms else None,
            "max": max(copilot_ms) if copilot_ms else None,
        },
        "end_of_call": {
            "input_finished_at_s": round(input_done, 2),
            "phases": [(p, round(t - stats.started, 2)) for p, t in stats.phases],
            "last_final_after_input_end": any(
                (stats.final_seen_at.get(k, 0) - stats.started) > input_done
                for k in stats.final_seen_at
            ),
        },
        "agenda": [(a["title"], a["status"], a.get("status_source")) for a in snap["agenda"]],
        "notes": [(n["kind"], n.get("category"), n["text"], n["source"]) for n in snap["notes"]],
        "insights": [(i["type"], i["content"]) for i in snap["insights"]],
        "pipeline": snap["pipeline"],
        "call_status": snap["call"]["status"],
        "transcript_persistence": snap["call"].get("transcript_persistence"),
        "post_call": {
            "status": (post.get("summary") or {}).get("status"),
            "summary": (post.get("summary") or {}).get("summary"),
            "budget": (post.get("summary") or {}).get("budget"),
            "timeline": (post.get("summary") or {}).get("timeline"),
        },
        "action_items": [
            (i["title"], i["source"], i["is_confirmed"]) for i in items.get("items", [])
        ],
    }
    print("\n=== REPORT ===")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.report:
        text = json.dumps(report, indent=2, ensure_ascii=False)
        await asyncio.to_thread(Path(args.report).write_text, text, "utf-8")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--ui", default="http://localhost:5173")
    p.add_argument("--email", default=os.environ.get("CC_DEV_EMAIL"))
    p.add_argument("--password", default=os.environ.get("CC_DEV_PASSWORD"))
    p.add_argument(
        "--setup", action="store_true", help="create contact + mock Teams call + agenda, start it"
    )
    p.add_argument("--call", help="attach to an already started call id")
    p.add_argument("--language", default="en-IN", choices=["en-IN", "en-US", "hi-IN", "de-DE"])
    p.add_argument("--dialogue", help="dialogue JSON (speaker + wav)")
    p.add_argument("--gap-ms", type=int, default=700, help="pause between utterances")
    p.add_argument("--mic", action="store_true")
    p.add_argument("--track", default="customer", choices=["agent", "customer"])
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--start-delay", type=float, default=0)
    p.add_argument("--report")
    a = p.parse_args()
    if not a.email or not a.password:
        p.error("--email/--password (or CC_DEV_EMAIL/CC_DEV_PASSWORD) are required")
    if not a.mic and not a.dialogue:
        p.error("--dialogue or --mic is required")
    raise SystemExit(asyncio.run(main(a)))
