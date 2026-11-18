"""RoutedEngine: what the worker talks to. Uses ElevenLabs when one is configured and healthy, otherwise
the local EMA engine (always loaded and warmed), so a bad key, an exhausted quota or an outage never
silences the stream. After a failure the remote is skipped for a while instead of paying its timeout on
every item."""
from __future__ import annotations

import logging
import time
from typing import Callable

from app import metrics
from app.interfaces import Engine

log = logging.getLogger(__name__)

BACKOFF_TRANSIENT_S = 20.0  # timeouts, 429, 5xx
BACKOFF_PERMANENT_S = 120.0  # bad key, no credits, unknown voice


class RoutedEngine:
    name = "router"

    def __init__(
        self,
        local: Engine,
        remote_getter: Callable[[], Engine | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.local = local
        self._remote_getter = remote_getter
        self._clock = clock
        self._down_until = 0.0
        self.last_used: str = getattr(local, "name", "local")  # engine that produced the latest utterance
        self.last_error: str | None = None  # why the remote was skipped or failed last

    # EmaEngine.speed is set by Runtime through attribute access; forward to the local engine.
    @property
    def speed(self) -> float:
        return self.local.speed  # type: ignore[attr-defined]

    @speed.setter
    def speed(self, value: float) -> None:
        if hasattr(self.local, "speed"):
            self.local.speed = value  # type: ignore[attr-defined]

    def reset_backoff(self) -> None:
        self._down_until = 0.0

    def synth(self, text: str, sample_rate: int = 24000) -> bytes:
        remote = self._remote_getter()
        if remote is not None and self._clock() >= self._down_until:
            try:
                wav = remote.synth(text, sample_rate)
            except Exception as exc:
                permanent = bool(getattr(exc, "permanent", False))
                self._down_until = self._clock() + (BACKOFF_PERMANENT_S if permanent else BACKOFF_TRANSIENT_S)
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                metrics.tts_failures_total.labels(stage="synth_remote").inc()
                log.warning("%s failed (%s); local engine for the next %.0f s",
                            getattr(remote, "name", "remote"), self.last_error,
                            BACKOFF_PERMANENT_S if permanent else BACKOFF_TRANSIENT_S)
            else:
                self.last_used = getattr(remote, "name", "remote")
                self.last_error = None
                return wav
        self.last_used = getattr(self.local, "name", "local")
        return self.local.synth(text, sample_rate)

    def warmup(self) -> None:
        self.local.warmup()
