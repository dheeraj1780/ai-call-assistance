"""Telephony audio normalisation: G.711 mu-law (8 kHz, what PSTN providers such as Plivo stream)
to 16-bit little-endian PCM at 16 kHz - the format Teams and Meet deliver - so every provider
reaches speech-to-text in one normalised shape.

Pure Python on purpose (``audioop`` was removed in Python 3.13, no native dependency): a
256-entry decode table plus 2x linear interpolation. A 20 ms frame is 160 samples, so the cost is
negligible. State (the previous sample) is kept per stream, so chunk boundaries are seamless.
Audio exists only in memory; nothing is written anywhere.
"""

import sys
from array import array


def _decode(u: int) -> int:
    u = ~u & 0xFF
    sample = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    sample -= 0x84
    return -sample if u & 0x80 else sample


MULAW_TO_PCM: tuple[int, ...] = tuple(_decode(i) for i in range(256))


def mulaw_to_pcm16(data: bytes) -> "array[int]":
    return array("h", [MULAW_TO_PCM[b] for b in data])


class MulawTo16kPcm:
    """Streaming converter: mu-law 8 kHz bytes in, PCM16 16 kHz little-endian bytes out."""

    def __init__(self) -> None:
        self._prev = 0

    def process(self, data: bytes) -> bytes:
        if not data:
            return b""
        out = array("h")
        prev = self._prev
        for b in data:
            cur = MULAW_TO_PCM[b]
            out.append((prev + cur) >> 1)  # interpolated midpoint
            out.append(cur)
            prev = cur
        self._prev = prev
        if sys.byteorder == "big":  # pragma: no cover - x86/ARM are little-endian
            out.byteswap()
        return out.tobytes()
