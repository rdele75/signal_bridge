"""Tiny in-process token-bucket rate limiter (finding M5).

SignalBridge is single-process by design, so a process-local bucket
is sufficient. No external dependency, no shared state, no Redis —
just ``time.monotonic`` + ``threading.Lock``. The current consumer is
``/webhooks/tradingview``; the implementation is generic so future
endpoints can reuse the same shape.
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """Classic token bucket.

    ``rate_per_second`` tokens are added back to the bucket per real
    second, up to a cap of ``burst``. ``allow()`` consumes one token
    and returns ``True``; when the bucket is empty it returns
    ``False`` immediately — callers must enforce the refusal
    themselves (we don't block).
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate_per_second = float(rate_per_second)
        self.burst = int(burst)
        self._tokens: float = float(self.burst)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, *, now: float | None = None) -> bool:
        """Try to consume one token. Returns ``True`` when admitted."""
        with self._lock:
            current = time.monotonic() if now is None else now
            elapsed = current - self._last_refill
            if elapsed > 0:
                self._tokens = min(
                    float(self.burst),
                    self._tokens + elapsed * self.rate_per_second,
                )
                self._last_refill = current
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    @property
    def tokens(self) -> float:
        """Debug accessor — current token count (not strictly real-time)."""
        with self._lock:
            return self._tokens
