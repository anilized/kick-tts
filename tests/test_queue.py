import asyncio

import pytest

from app import metrics
from app.interfaces import PRIORITY, TtsItem
from app.queue import TtsQueue


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(kind, user="u", clock=None, text="x"):
    return TtsItem(id=f"{kind}-{user}-{text}", kind=kind, user=user, raw_text=text,
                   priority=PRIORITY[kind], created_at=clock() if clock else 1000.0)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def q(clock):
    return TtsQueue(max_size=5, cooldown_s=30, max_item_age_s=600, clock=clock)


async def drain(q, n):
    return [await asyncio.wait_for(q.get(), 1) for _ in range(n)]


async def test_priority_order(q, clock):
    q.put(mk("command", "a", clock))
    q.put(mk("reward", "b", clock))
    q.put(mk("kicks", "c", clock))
    got = await drain(q, 3)
    assert [i.kind for i in got] == ["kicks", "reward", "command"]


async def test_fifo_within_priority_and_manual_equals_kicks(q, clock):
    q.put(mk("manual", "a", clock, "1"))
    q.put(mk("kicks", "b", clock, "2"))
    q.put(mk("manual", "c", clock, "3"))
    got = await drain(q, 3)
    assert [i.raw_text for i in got] == ["1", "2", "3"]


def test_command_cooldown(q, clock):
    assert q.put(mk("command", "Anil", clock)).accepted
    r = q.put(mk("command", "anil", clock))
    assert (r.accepted, r.reason) == (False, "cooldown")
    clock.t += 29
    assert q.put(mk("command", "ANIL", clock)).reason == "cooldown"
    clock.t += 1
    assert q.put(mk("command", "anil", clock)).accepted


def test_cooldown_not_for_other_kinds(clock):
    q = TtsQueue(50, 30, 600, clock)
    for kind in ("kicks", "reward", "manual"):
        assert q.put(mk(kind, "same", clock, "1")).accepted
        assert q.put(mk(kind, "same", clock, "2")).accepted


def test_full_drops_oldest_command_for_kicks(clock):
    q = TtsQueue(3, 0, 600, clock)
    c1, c2, c3 = (mk("command", f"u{i}", clock) for i in range(3))
    for c in (c1, c2, c3):
        q.put(c)
    r = q.put(mk("kicks", "k", clock))
    assert r.accepted and r.dropped == [c1]
    assert [i.kind for i in q.snapshot()] == ["kicks", "command", "command"]
    assert len(q) == 3


def test_command_dropped_at_full_queue_of_kicks(clock):
    q = TtsQueue(2, 0, 600, clock)
    q.put(mk("kicks", "a", clock))
    q.put(mk("reward", "b", clock))
    r = q.put(mk("command", "c", clock))
    assert (r.accepted, r.reason, r.dropped) == (False, "queue_full", [])
    assert len(q) == 2


def test_command_replaces_oldest_command_when_full(clock):
    q = TtsQueue(2, 0, 600, clock)
    c1, c2, c3 = (mk("command", f"u{i}", clock) for i in range(3))
    q.put(c1)
    q.put(c2)
    r = q.put(c3)
    assert r.accepted and r.dropped == [c1]
    assert q.snapshot() == [c2, c3]


async def test_pause_blocks_and_resume_releases(q, clock):
    q.put(mk("kicks", "a", clock))
    q.pause()
    assert q.paused
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), 0.1)
    task = asyncio.create_task(q.get())
    await asyncio.sleep(0.05)
    assert not task.done()
    q.resume()
    assert not q.paused
    assert (await asyncio.wait_for(task, 1)).user == "a"


async def test_get_waits_for_put(q, clock):
    task = asyncio.create_task(q.get())
    await asyncio.sleep(0.05)
    q.put(mk("kicks", "a", clock))
    assert (await asyncio.wait_for(task, 1)).user == "a"


def test_clear(q, clock):
    q.put(mk("kicks", "a", clock))
    q.put(mk("reward", "b", clock))
    assert q.clear() == 2
    assert len(q) == 0 and q.snapshot() == []
    assert metrics.get_value(metrics.tts_queue_length) == 0


async def test_stale_items_skipped(q, clock):
    before = metrics.get_value(metrics.tts_items_dropped_total, reason="stale")
    q.put(mk("kicks", "old", clock))
    clock.t += 601
    q.put(mk("command", "fresh", clock))
    assert (await asyncio.wait_for(q.get(), 1)).user == "fresh"
    assert metrics.get_value(metrics.tts_items_dropped_total, reason="stale") == before + 1


def test_queue_length_metric_and_counters(q, clock):
    enq = metrics.get_value(metrics.tts_items_enqueued_total, kind="kicks")
    cd = metrics.get_value(metrics.tts_items_dropped_total, reason="cooldown")
    q.put(mk("kicks", "a", clock))
    q.put(mk("kicks", "b", clock))
    assert metrics.get_value(metrics.tts_queue_length) == 2
    assert metrics.get_value(metrics.tts_items_enqueued_total, kind="kicks") == enq + 2
    q.put(mk("command", "x", clock))
    q.put(mk("command", "x", clock))
    assert metrics.get_value(metrics.tts_items_dropped_total, reason="cooldown") == cd + 1
    assert metrics.get_value(metrics.tts_queue_length) == 3
