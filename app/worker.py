"""The single worker task: queue -> reader -> post-guards -> synth (executor thread) -> overlay -> ack.

One item at a time. The worker blocks until warm-up finished (`ready`), until at least one overlay is
connected, and on the queue (which handles pause). A failure in any stage is counted, logged and the
loop continues; the worker never dies.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from concurrent.futures import Executor
from typing import Callable

from app import metrics
from app.config import Settings
from app.interfaces import Engine, OverlayItem, Reader, TtsItem
from app.queue import TtsQueue
from app.reader.emoji import strip_emoji
from app.wavutil import wav_duration
from app.ws import OverlayHub

log = logging.getLogger(__name__)


def _tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def apply_guards(text: str, max_chars: int, blocklist: list[str]) -> tuple[str, str | None]:
    """Code-level guards after the reader: emoji strip, first non-empty line, length cap, blocklist.

    Returns (text, None) to speak, or ("", reason) with reason in {"empty", "blocklist"}.
    """
    lines = (strip_emoji(ln) for ln in (text or "").splitlines())  # strip_emoji also collapses spaces
    first = next((ln for ln in lines if ln), "")
    if max_chars > 0 and len(first) > max_chars:
        first = first[:max_chars].rstrip()
    if not first:
        return "", "empty"
    lowered = _tr_lower(first)
    if any(word and word in lowered for word in blocklist):
        return "", "blocklist"
    return first, None


class Worker:
    def __init__(
        self,
        settings: Settings,
        queue: TtsQueue,
        hub: OverlayHub,
        reader_getter: Callable[[], Reader],
        engine_getter: Callable[[], Engine],
        executor: Executor,
        ready: asyncio.Event,
    ) -> None:
        self._s = settings
        self._queue = queue
        self._hub = hub
        self._reader_getter = reader_getter
        self._engine_getter = engine_getter
        self._executor = executor
        self._ready = ready
        self._blocklist = settings.blocklist
        self.processed = 0  # items that reached the overlay (tests/status)

    async def run(self) -> None:
        while True:
            try:
                await self._ready.wait()
                await self._hub.wait_for_client()
                item = await self._queue.get()
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive: the loop itself must survive anything
                metrics.tts_failures_total.labels(stage="worker").inc()
                log.exception("worker loop error")
                await asyncio.sleep(0.1)

    async def _process(self, item: TtsItem) -> None:
        loop = asyncio.get_running_loop()
        reader = self._reader_getter()
        engine = self._engine_getter()

        # 1. read
        t0 = time.perf_counter()
        try:
            text = await reader.read(item.raw_text, item.user)
        except Exception:
            metrics.tts_failures_total.labels(stage="read").inc()
            log.exception("reader failed for item %s", item.id)
            return
        metrics.tts_reader_latency_seconds.labels(backend=getattr(reader, "name", "unknown")).observe(
            time.perf_counter() - t0
        )

        # 2. post-guards
        text, reason = apply_guards(text, self._s.MAX_TEXT_CHARS, self._blocklist)
        if reason is not None:
            metrics.tts_items_dropped_total.labels(reason=reason).inc()
            log.info("dropped item %s (%s)", item.id, reason)
            return

        # 3. synth on the shared single-thread executor
        t1 = time.perf_counter()
        try:
            wav = await loop.run_in_executor(self._executor, engine.synth, text, self._s.SAMPLE_RATE)
        except Exception:
            metrics.tts_failures_total.labels(stage="synth").inc()
            log.exception("synth failed for item %s", item.id)
            return
        metrics.tts_synth_latency_seconds.observe(time.perf_counter() - t1)
        duration = wav_duration(wav)

        # 4. broadcast, then pace on the overlay's played-ack (or duration + grace)
        message = OverlayItem(
            id=item.id,
            user=item.user,
            caption=text,
            audio_b64_wav=base64.b64encode(wav).decode("ascii"),
            duration_s=duration,
        )
        try:
            sent = await self._hub.broadcast(message)
        except Exception:
            metrics.tts_failures_total.labels(stage="broadcast").inc()
            log.exception("broadcast failed for item %s", item.id)
            return
        self.processed += 1
        log.info("speaking %s item %s from %s: %s (%.2fs, %d clients)", item.kind, item.id, item.user, text, duration, sent)
        if sent == 0:
            return
        acked = await self._hub.wait_ack(item.id, duration + self._s.ACK_GRACE_S)
        if not acked:
            log.debug("item %s: no played-ack, moving on", item.id)
