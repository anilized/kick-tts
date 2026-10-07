"""WebSocket fan-out and worker pacing: /speak -> one item, skip push, ack gating, bad key."""
from __future__ import annotations

import asyncio
import base64
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.engine.fake import FakeEngine
from app.interfaces import OverlayItem
from app.main import create_app
from app.metrics import get_value, tts_failures_total, tts_items_dropped_total, tts_overlay_clients
from app.reader.fake import FakeReader
from app.wavutil import wav_info
from app.ws import OverlayHub

AUTH = {"Authorization": "Bearer test-token"}


def _wait(pred, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


@pytest.fixture
def client(make_settings, fake_engine, fake_reader):
    # ACK_GRACE_S small so the "no ack" timeout path finishes within a test (0.6 s tone + grace)
    settings = make_settings(ACK_GRACE_S=0.3)
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        yield c


def test_speak_round_trip_item_shape(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        assert _wait(lambda: get_value(tts_overlay_clients) == 1)
        r = client.post("/speak", json={"text": "Selam 🔥 Millet", "user": "anil"}, headers=AUTH)
        assert r.json()["status"] == "queued"
        msg = ws.receive_json()
        assert msg["type"] == "item"
        assert msg["id"] == r.json()["id"]
        assert msg["user"] == "anil"
        assert msg["caption"] == "selam millet"  # FakeReader: emoji stripped, Turkish lowercase
        assert msg["duration_s"] > 0
        wav = base64.b64decode(msg["audio_b64_wav"])
        assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
        channels, rate, width, nframes = wav_info(wav)
        assert (channels, rate, width) == (1, 24000, 2)
        assert msg["duration_s"] == pytest.approx(nframes / rate)
        ws.send_json({"type": "played", "id": msg["id"]})
    assert _wait(lambda: get_value(tts_overlay_clients) == 0)


def test_skip_is_pushed(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        assert client.post("/skip", headers=AUTH).json() == {"status": "ok"}
        assert ws.receive_json() == {"type": "skip"}
        client.post("/pause", headers=AUTH)
        assert ws.receive_json() == {"type": "paused", "value": True}
        client.post("/resume", headers=AUTH)
        assert ws.receive_json() == {"type": "paused", "value": False}
        client.post("/clear", headers=AUTH)
        assert ws.receive_json() == {"type": "clear"}


def test_second_item_waits_for_ack(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        assert _wait(lambda: get_value(tts_overlay_clients) == 1)
        r1 = client.post("/speak", json={"text": "bir"}, headers=AUTH)
        r2 = client.post("/speak", json={"text": "iki"}, headers=AUTH)
        first = ws.receive_json()
        assert first["id"] == r1.json()["id"] and first["caption"] == "bir"
        # the worker is parked on the ack: the second item stays queued
        time.sleep(0.3)
        assert client.get("/status", headers=AUTH).json()["queue_length"] == 1
        ws.send_json({"type": "played", "id": first["id"]})
        second = ws.receive_json()
        assert second["id"] == r2.json()["id"] and second["caption"] == "iki"
        ws.send_json({"type": "played", "id": second["id"]})


def test_without_ack_next_item_arrives_after_duration_plus_grace(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        assert _wait(lambda: get_value(tts_overlay_clients) == 1)
        client.post("/speak", json={"text": "bir"}, headers=AUTH)
        client.post("/speak", json={"text": "iki"}, headers=AUTH)
        first = ws.receive_json()
        t0 = time.monotonic()
        second = ws.receive_json()  # no ack sent: released by duration (0.6 s) + ACK_GRACE_S (0.3 s)
        elapsed = time.monotonic() - t0
        assert second["caption"] == "iki"
        assert 0.6 <= elapsed < 5.0, elapsed
        assert first["caption"] == "bir"


def test_skip_releases_current_item(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        assert _wait(lambda: get_value(tts_overlay_clients) == 1)
        client.post("/speak", json={"text": "bir"}, headers=AUTH)
        client.post("/speak", json={"text": "iki"}, headers=AUTH)
        assert ws.receive_json()["caption"] == "bir"
        t0 = time.monotonic()
        client.post("/skip", headers=AUTH)
        assert ws.receive_json() == {"type": "skip"}
        second = ws.receive_json()
        assert second["caption"] == "iki"
        assert time.monotonic() - t0 < 0.6  # did not wait for the tone to end
        ws.send_json({"type": "played", "id": second["id"]})


def test_items_wait_for_a_client_then_flow(client):
    client.post("/speak", json={"text": "bekleyen"}, headers=AUTH)
    time.sleep(0.2)
    assert client.get("/status", headers=AUTH).json()["queue_length"] == 1  # no overlay: nothing consumed
    with client.websocket_connect("/ws?key=test-key") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "item" and msg["caption"] == "bekleyen"
        ws.send_json({"type": "played", "id": msg["id"]})
    assert _wait(lambda: client.get("/status", headers=AUTH).json()["queue_length"] == 0)


def test_blocklisted_item_is_dropped_not_spoken(make_settings, fake_engine, fake_reader):
    settings = make_settings(BLOCKLIST="yasak", ACK_GRACE_S=0.1)
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    before = get_value(tts_items_dropped_total, reason="blocklist")
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        with c.websocket_connect("/ws?key=test-key") as ws:
            assert _wait(lambda: get_value(tts_overlay_clients) == 1)
            c.post("/speak", json={"text": "bu YASAK kelime"}, headers=AUTH)
            c.post("/speak", json={"text": "bu serbest"}, headers=AUTH)
            msg = ws.receive_json()
            assert msg["caption"] == "bu serbest"
            ws.send_json({"type": "played", "id": msg["id"]})
    assert get_value(tts_items_dropped_total, reason="blocklist") == before + 1


def test_unknown_client_messages_are_ignored(client):
    with client.websocket_connect("/ws?key=test-key") as ws:
        ws.send_text("not json at all")
        ws.send_json({"type": "whatever", "x": 1})
        ws.send_json({"type": "pong"})
        ws.send_json({"type": "played", "id": "no-such-item"})
        client.post("/skip", headers=AUTH)
        assert ws.receive_json() == {"type": "skip"}  # still alive and routed


# ---- the worker survives per-item failures ---------------------------------------------------------

MARKER = "patla"


class RaisingReader(FakeReader):
    name = "raising-fake"

    async def read(self, text: str, user: str) -> str:
        if MARKER in text:
            raise RuntimeError("reader exploded")
        return await super().read(text, user)


class RaisingEngine(FakeEngine):
    name = "raising-fake"

    def synth(self, text: str, sample_rate: int = 24000) -> bytes:
        if MARKER in text:
            raise RuntimeError("synth exploded")
        return super().synth(text, sample_rate)


@pytest.mark.parametrize(
    "stage,engine,reader",
    [("synth", RaisingEngine(), FakeReader()), ("read", FakeEngine(), RaisingReader())],
)
def test_worker_survives_item_failure_and_delivers_next(make_settings, stage, engine, reader):
    app = create_app(make_settings(ACK_GRACE_S=0.1), engine=engine, reader=reader)
    before = {s: get_value(tts_failures_total, stage=s) for s in ("read", "synth", "broadcast")}
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        with c.websocket_connect("/ws?key=test-key") as ws:
            assert _wait(lambda: get_value(tts_overlay_clients) == 1)
            assert c.post("/speak", json={"text": MARKER}, headers=AUTH).json()["status"] == "queued"
            r2 = c.post("/speak", json={"text": "sonraki"}, headers=AUTH)
            msg = ws.receive_json()  # the failing item never arrives; the next one does
            assert msg["type"] == "item" and msg["id"] == r2.json()["id"] and msg["caption"] == "sonraki"
            ws.send_json({"type": "played", "id": msg["id"]})
            # and the worker is still alive afterwards
            r3 = c.post("/speak", json={"text": "hala yaşıyor"}, headers=AUTH)
            msg = ws.receive_json()
            assert msg["id"] == r3.json()["id"]
            ws.send_json({"type": "played", "id": msg["id"]})
    after = {s: get_value(tts_failures_total, stage=s) for s in before}
    assert after[stage] == before[stage] + 1
    for other in before:
        if other != stage:
            assert after[other] == before[other]


# ---- pacing races (hub and worker primitives) ------------------------------------------------------


class StubSocket:
    """Minimal WebSocket stand-in for OverlayHub: records sends; `on_send` can react mid-broadcast."""

    def __init__(self, on_send=None):
        self.sent: list[str] = []
        self.on_send = on_send
        self.client_state = WebSocketState.CONNECTED

    async def accept(self):
        pass

    async def send_text(self, text: str):
        self.sent.append(text)
        if self.on_send:
            self.on_send(text)

    async def close(self, code=1000, reason=None):
        self.client_state = WebSocketState.DISCONNECTED


async def test_ack_arriving_mid_broadcast_is_not_lost():
    hub = OverlayHub()
    item = OverlayItem(id="it1", user="u", caption="c", audio_b64_wav="", duration_s=5.0)
    # the first client acks synchronously while the item is still being sent to the second one
    fast = StubSocket(on_send=lambda _text: hub.ack("it1"))
    slow = StubSocket()
    await hub.connect(fast)
    await hub.connect(slow)
    hub.expect_ack("it1")
    assert await hub.broadcast(item) == 2
    t0 = time.monotonic()
    assert await hub.wait_ack("it1", timeout_s=5.0) is True
    assert time.monotonic() - t0 < 1.0  # did not wait for the timeout
    assert len(slow.sent) == 1
    assert hub._acks == {}
    await hub.close_all()
    assert hub.clients == 0 and get_value(tts_overlay_clients) == 0


async def test_ack_without_expect_before_broadcast_would_be_dropped():
    """Documents the race the worker avoids: an ack for an unregistered id is ignored."""
    hub = OverlayHub()
    await hub.connect(StubSocket())
    hub.ack("never-registered")
    assert await hub.wait_ack("never-registered", timeout_s=0.05) is False
    await hub.close_all()


async def test_worker_counts_item_lost_to_zero_clients(make_settings, fake_engine, fake_reader):
    from concurrent.futures import ThreadPoolExecutor

    from app.interfaces import TtsItem
    from app.queue import TtsQueue
    from app.worker import Worker

    settings = make_settings(ACK_GRACE_S=0.1)
    hub = OverlayHub()  # nobody connected when the item reaches the broadcast step
    executor = ThreadPoolExecutor(max_workers=1)
    worker = Worker(settings, TtsQueue(5, 0, 600), hub, lambda: fake_reader, lambda: fake_engine, executor, asyncio.Event())
    before = get_value(tts_items_dropped_total, reason="no_client")
    try:
        await worker._process(TtsItem.new("manual", "u", "kimse yok"))
    finally:
        executor.shutdown(wait=True)
    assert get_value(tts_items_dropped_total, reason="no_client") == before + 1
    assert worker.processed == 0
    assert hub._acks == {}  # the pre-registered ack future was discarded


def test_bad_key_is_rejected_with_1008(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?key=wrong") as ws:
            ws.receive_json()
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert get_value(tts_overlay_clients) == 0
