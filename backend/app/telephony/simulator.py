"""Mock-provider conversation simulator (MOCKED telephony + STT).

Drives a scripted call through the SAME code paths a real provider would use: provider events
go through ``process_event`` (idempotency + ordering), audio goes through ``MediaIngest`` and the
STT provider. Only available when TELEPHONY_PROVIDER=mock and SIMULATION_ENABLED=true; it never
pretends a real phone call happened (the call records provider "mock").
"""

import asyncio
import base64
import json
import logging
import uuid
from datetime import UTC, datetime

from app.live import session as live
from app.telephony.provider import ProviderCallState, TelephonyEvent, Track, get_telephony_provider
from app.telephony.service import MediaIngest, process_event

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT: list[tuple[str, str]] = [
    ("agent", "Hello, am I speaking with the owner? I'm calling about your inventory management."),
    ("customer", "Yes, speaking. Tell me, what is this about?"),
    ("agent", "I wanted to understand how you manage your inventory today."),
    (
        "customer",
        "Right now we track everything in Excel and we have 5 branches, it takes a lot of time to reconcile stock every week.",
    ),
    ("agent", "I see. What is the biggest problem with the current way of working?"),
    (
        "customer",
        "The main problem is stock mismatch between branches and my staff makes manual errors in the sheets every day.",
    ),
    ("agent", "Understood. What would an ideal solution look like for you?"),
    ("customer", "We need a single dashboard for all branches. Do you support multiple branches?"),
    ("agent", "That is a common need. Have you set aside any budget for this?"),
    (
        "customer",
        "Honestly this looks too expensive for us right now, our budget is around 2 lakh for this year.",
    ),
    ("agent", "That helps. By when would you like a solution in place?"),
    ("customer", "We want it running before Diwali if possible."),
    ("agent", "Who else is involved in the decision?"),
    ("customer", "I will have to check with my partner before deciding."),
    ("agent", "Makes sense. What would be a good next step from your side?"),
    ("customer", "Please send me the quotation on WhatsApp and let's schedule a demo next week."),
]

running: dict[uuid.UUID, asyncio.Task[None]] = {}
TURN_TIMEOUT_S = 2.0


def _processed(call_id: uuid.UUID) -> int:
    session = live.get_session(call_id)
    return session.next_seq if session is not None else 0


async def _wait_for_segment(call_id: uuid.UUID, before: int) -> None:
    """Wait (bounded) until the live session has stored one more final segment."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TURN_TIMEOUT_S
    while loop.time() < deadline:
        session = live.get_session(call_id)
        if session is None or session.next_seq > max(before, 1) or not session.stt_ok:
            return
        await asyncio.sleep(0.01)


def _media(track: str, seq: int, text: str) -> str:
    return json.dumps(
        {
            "event": "media",
            "track": track,
            "seq": seq,
            "payload": base64.b64encode(text.encode()).decode(),
        }
    )


async def run(
    call_id: uuid.UUID,
    provider_call_id: str,
    *,
    script: list[tuple[str, str]] | None = None,
    delay: float = 0.0,
    complete: bool = True,
) -> None:
    provider = get_telephony_provider()
    events_seen = 0

    async def event(state: ProviderCallState, duration: int | None = None) -> None:
        nonlocal events_seen
        events_seen += 1
        await process_event(
            provider.name,
            TelephonyEvent(
                event_id=f"sim-{call_id}-{events_seen}-{state}",
                provider_call_id=provider_call_id,
                state=state,
                occurred_at=datetime.now(UTC),
                duration_seconds=duration,
            ),
        )

    try:
        await event(ProviderCallState.RINGING)
        await asyncio.sleep(delay)
        await event(ProviderCallState.CONNECTED)
        ingest = MediaIngest(call_id)
        await ingest.handle(provider.parse_media_message(json.dumps({"event": "start"})))
        for seq, (speaker, text) in enumerate(script or DEFAULT_SCRIPT, start=1):
            await asyncio.sleep(delay)
            before = _processed(call_id)
            await ingest.handle(
                provider.parse_media_message(_media(Track(speaker).value, seq, text))
            )
            # Natural turn-taking: the next person speaks after this utterance was heard
            # (otherwise, at zero delay, the two tracks' STT results could interleave).
            await _wait_for_segment(call_id, before)
        await asyncio.sleep(max(delay, 0.05))  # let STT results flush
        if complete:
            await event(
                ProviderCallState.COMPLETED,
                duration=int(len(script or DEFAULT_SCRIPT) * max(delay, 1)),
            )
            from app.telephony.service import wait_finalized

            await wait_finalized(call_id)
    except Exception:
        logger.exception("simulation_failed", extra={"call_id": str(call_id)})
    finally:
        running.pop(call_id, None)


def start(
    call_id: uuid.UUID, provider_call_id: str, *, script: list[tuple[str, str]] | None, delay: float
) -> asyncio.Task[None]:
    task = asyncio.create_task(run(call_id, provider_call_id, script=script, delay=delay))
    running[call_id] = task
    return task
