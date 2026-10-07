"""TTL set used to drop duplicate Kick deliveries (message ids, 10 min) and redemption ids (1 h)."""
from __future__ import annotations

import time
from typing import Callable


class TtlSet:
    """Remembers keys for `ttl_s` seconds. Expiry is lazy; at `max_size` the oldest keys are evicted."""

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.time, max_size: int = 10000) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._max = max_size
        self._seen: dict[str, float] = {}  # key -> expiry; insertion order == expiry order (constant ttl)

    def _purge(self, now: float) -> None:
        while self._seen:
            key = next(iter(self._seen))
            if self._seen[key] > now:
                break
            del self._seen[key]

    def seen_or_add(self, key: str) -> bool:
        """True when `key` is already present (and unexpired); otherwise record it and return False."""
        now = self._clock()
        self._purge(now)
        if key in self._seen:
            return True
        while len(self._seen) >= self._max:
            del self._seen[next(iter(self._seen))]
        self._seen[key] = now + self._ttl
        return False

    def __len__(self) -> int:
        self._purge(self._clock())
        return len(self._seen)
