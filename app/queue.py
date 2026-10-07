"""Priority queue with per-user command cooldown, size limit, pause and stale-item expiry."""
from __future__ import annotations

import asyncio
import bisect
import time
from dataclasses import dataclass, field
from typing import Callable

from app import metrics
from app.interfaces import TtsItem


@dataclass
class PutResult:
    accepted: bool
    reason: str | None = None  # None | "cooldown" | "queue_full"
    dropped: list[TtsItem] = field(default_factory=list)


class TtsQueue:
    """Ordered by (priority, seq): lower priority number first, FIFO within a priority.

    One consumer (get) and many producers (put) inside a single event loop.
    """

    def __init__(
        self,
        max_size: int,
        cooldown_s: float,
        max_item_age_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_size = max_size
        self._cooldown_s = cooldown_s
        self._max_age_s = max_item_age_s
        self._clock = clock
        self._entries: list[tuple[int, int, TtsItem]] = []
        self._seq = 0
        self._paused = False
        self._last_command: dict[str, float] = {}
        self._cond = asyncio.Condition()

    # -- state -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def __len__(self) -> int:
        return len(self._entries)

    def snapshot(self) -> list[TtsItem]:
        return [e[2] for e in self._entries]

    def _sync_gauge(self) -> None:
        metrics.tts_queue_length.set(len(self._entries))

    def _notify(self) -> None:
        """Wake the consumer from synchronous code (Condition.notify needs the lock held)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._wake())

    async def _wake(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # -- producers ---------------------------------------------------------
    def put(self, item: TtsItem) -> PutResult:
        key = item.user.casefold()
        now = self._clock()
        if item.kind == "command":
            last = self._last_command.get(key)
            if last is not None and now < last + self._cooldown_s:
                metrics.tts_items_dropped_total.labels(reason="cooldown").inc()
                return PutResult(False, "cooldown", [])

        dropped: list[TtsItem] = []
        if len(self._entries) >= self._max_size:
            worst = max((e[0] for e in self._entries), default=-1)
            if worst < item.priority:
                metrics.tts_items_dropped_total.labels(reason="queue_full").inc()
                return PutResult(False, "queue_full", [])
            # Oldest (lowest seq) entry of the least important priority present.
            idx = next(i for i, e in enumerate(self._entries) if e[0] == worst)
            dropped.append(self._entries.pop(idx)[2])
            metrics.tts_items_dropped_total.labels(reason="queue_full").inc()

        self._seq += 1
        bisect.insort(self._entries, (item.priority, self._seq, item), key=lambda e: (e[0], e[1]))
        if item.kind == "command":
            self._last_command[key] = now
        metrics.tts_items_enqueued_total.labels(kind=item.kind).inc()
        self._sync_gauge()
        self._notify()
        return PutResult(True, None, dropped)

    # -- consumer ----------------------------------------------------------
    async def get(self) -> TtsItem:
        async with self._cond:
            while True:
                await self._cond.wait_for(lambda: bool(self._entries) and not self._paused)
                item = self._entries.pop(0)[2]
                self._sync_gauge()
                if self._clock() - item.created_at > self._max_age_s:
                    metrics.tts_items_dropped_total.labels(reason="stale").inc()
                    continue
                return item

    # -- control -----------------------------------------------------------
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._notify()

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        if n:
            metrics.tts_items_dropped_total.labels(reason="cleared").inc(n)
        self._sync_gauge()
        return n
