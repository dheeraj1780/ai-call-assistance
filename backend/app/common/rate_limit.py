"""Minimal in-process fixed-window rate limiter.

Adequate for the single-instance MVP. With multiple API instances each instance keeps its
own counters (limits become per-instance); swap for a shared store only when we scale out.
"""

import math
import threading
import time

from fastapi import Request

from app.common.config import get_settings
from app.common.errors import RateLimitedError


class FixedWindowRateLimiter:
    def __init__(self, max_keys: int = 50_000) -> None:
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        """Record a hit. Returns None if allowed, else seconds until the window resets."""
        now = time.monotonic()
        with self._lock:
            if len(self._windows) >= self._max_keys:
                self._evict_expired(now)
            start, count = self._windows.get(key, (now, 0))
            if now - start >= window_seconds:
                start, count = now, 0
            count += 1
            self._windows[key] = (start, count)
            if count > limit:
                return max(1, math.ceil(window_seconds - (now - start)))
            return None

    def _evict_expired(self, now: float, horizon: float = 3600) -> None:
        stale = [k for k, (start, _) in self._windows.items() if now - start >= horizon]
        for k in stale:
            del self._windows[k]
        if len(self._windows) >= self._max_keys:
            self._windows.clear()

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


limiter = FixedWindowRateLimiter()


def client_ip(request: Request) -> str:
    # Behind Render's proxy, uvicorn runs with --proxy-headers so request.client is the real
    # client address. We do not parse X-Forwarded-For ourselves (it is client-spoofable).
    return request.client.host if request.client else "unknown"


def enforce_auth_rate_limit(request: Request, *, bucket: str, extra_key: str | None = None) -> None:
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    keys = [f"{bucket}:ip:{client_ip(request)}"]
    if extra_key:
        keys.append(f"{bucket}:k:{extra_key}")
    for key in keys:
        retry_after = limiter.hit(key, settings.rate_limit_auth_per_minute, 60)
        if retry_after is not None:
            raise RateLimitedError(retry_after)
