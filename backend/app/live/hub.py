"""In-process pub/sub for live call events (browser WebSockets).

Each call has a channel with a monotonically increasing ``seq`` and a bounded replay buffer.
Clients (re)connect with the last seq they saw and receive the missed events; if the gap can
no longer be filled (buffer overflow or server restart - detected via ``epoch``) the client is
told to ``resync`` by reloading the snapshot endpoint.

Single-process by design for the MVP (see ADR-015). Browser disconnects only unsubscribe;
they never affect the phone call or the processing pipeline.
"""

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

BUFFER_SIZE = 500
QUEUE_SIZE = 1000


@dataclass(frozen=True)
class LiveEvent:
    seq: int
    type: str
    payload: dict[str, Any]
    at: str

    def to_message(self, epoch: str) -> dict[str, Any]:
        return {
            "type": self.type,
            "seq": self.seq,
            "epoch": epoch,
            "at": self.at,
            "data": self.payload,
        }


@dataclass
class Channel:
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = 0
    buffer: deque[LiveEvent] = field(default_factory=lambda: deque(maxlen=BUFFER_SIZE))
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)


class LiveHub:
    def __init__(self) -> None:
        self._channels: dict[uuid.UUID, Channel] = {}

    def channel(self, call_id: uuid.UUID) -> Channel:
        return self._channels.setdefault(call_id, Channel())

    def publish(self, call_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> LiveEvent:
        ch = self.channel(call_id)
        ch.seq += 1
        event = LiveEvent(ch.seq, type_, payload, datetime.now(UTC).isoformat())
        if type_ != "transcript.partial":  # partials are not replayed
            ch.buffer.append(event)
        message = event.to_message(ch.epoch)
        for queue in list(ch.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer: drop its backlog and ask it to resync from the snapshot.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync", "epoch": ch.epoch, "seq": ch.seq})
        return event

    def subscribe(
        self, call_id: uuid.UUID, *, last_seq: int | None, epoch: str | None
    ) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]], bool]:
        """Returns (queue, backlog, needs_resync)."""
        ch = self.channel(call_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        ch.subscribers.add(queue)
        if last_seq is None:
            return queue, [], False
        if epoch != ch.epoch:
            return queue, [], True
        oldest = ch.buffer[0].seq if ch.buffer else ch.seq + 1
        if last_seq < ch.seq and last_seq + 1 < oldest:
            return queue, [], True
        backlog = [e.to_message(ch.epoch) for e in ch.buffer if e.seq > last_seq]
        return queue, backlog, False

    def unsubscribe(self, call_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        ch = self._channels.get(call_id)
        if ch is not None:
            ch.subscribers.discard(queue)

    def state(self, call_id: uuid.UUID) -> tuple[str, int]:
        ch = self.channel(call_id)
        return ch.epoch, ch.seq

    def subscriber_count(self, call_id: uuid.UUID) -> int:
        ch = self._channels.get(call_id)
        return len(ch.subscribers) if ch else 0

    def reset(self) -> None:
        self._channels.clear()


hub = LiveHub()
