"""Token-bucket rate limiter shared by source clients.

Use one ``TokenBucket`` per upstream. ``acquire()`` blocks until a token is
available. ``try_acquire()`` returns False immediately if the bucket is
empty (caller decides whether to wait, skip, or fall back to cache).

The bucket is configured by:
    capacity        — max tokens (burst size)
    refill_rate     — tokens added per second

For Alpha Vantage's 25-requests-per-day cap, use:
    TokenBucket(capacity=25, refill_rate=25 / 86400)

The implementation uses a monotonic clock so it's robust against system
time jumps.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        if self.refill_rate < 0:
            raise ValueError("refill_rate must be >= 0")
        self._tokens = float(self.capacity)
        self._last = self._clock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0 and self.refill_rate > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last = now

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, n: float = 1.0) -> bool:
        if n <= 0:
            raise ValueError("n must be > 0")
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def wait_time(self, n: float = 1.0) -> float:
        """Seconds until ``n`` tokens are available (0 if available now)."""
        if n <= 0:
            raise ValueError("n must be > 0")
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                return 0.0
            if self.refill_rate <= 0:
                return float("inf")
            return (n - self._tokens) / self.refill_rate

    def acquire(self, n: float = 1.0, *, timeout: Optional[float] = None) -> bool:
        """Block until ``n`` tokens are available. Returns True on success.

        If ``timeout`` is set and elapses without a token, returns False
        without consuming any.
        """
        if n <= 0:
            raise ValueError("n must be > 0")
        deadline = (self._clock() + timeout) if timeout is not None else None
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                if self.refill_rate <= 0:
                    return False
                needed = (n - self._tokens) / self.refill_rate
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._sleep(min(needed, remaining))
            else:
                self._sleep(needed)
