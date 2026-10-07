"""HTTP surface, readiness tri-state and lifespan behaviour via create_app + `with TestClient(app)`."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.metrics import get_value, tts_failures_total, tts_items_dropped_total, tts_items_enqueued_total
from app.worker import apply_guards

ROOT = Path(__file__).resolve().parents[1]
AUTH = {"Authorization": "Bearer test-token"}


def _wait(pred, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ---- lifespan / readiness --------------------------------------------------------------------------


def test_lifespan_yields_before_warmup_and_readyz_is_tri_state(settings, blocking_engine, fake_reader):
    app = create_app(settings, engine=blocking_engine, reader=fake_reader)
    t0 = time.monotonic()
    with TestClient(app) as c:
        assert time.monotonic() - t0 < 2.0, "lifespan must yield while warm-up is still running"
        assert blocking_engine.started.wait(2.0)

        assert c.get("/healthz").status_code == 200
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json() == {"status": "warming", "error": None}
        assert c.get("/healthz").json() == {"status": "alive"}

        blocking_engine.release()
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        assert c.get("/readyz").json() == {"status": "ready"}
        assert blocking_engine.warmups == 1
    # after shutdown nothing hangs; the executor thread has finished its one warm-up


def test_failed_warmup_reports_failed_and_counts(settings, failing_engine, fake_reader):
    before = get_value(tts_failures_total, stage="warmup")
    app = create_app(settings, engine=failing_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").json().get("status") == "failed")
        r = c.get("/readyz")
        assert r.status_code == 503
        assert r.json() == {"status": "failed", "error": "WeightsCorrupt"}
        assert c.get("/healthz").status_code == 200  # liveness never depends on the engine
        assert get_value(tts_failures_total, stage="warmup") == before + 1
        s = c.get("/status", headers=AUTH).json()
        assert s["readiness"] == "failed" and s["error"] == "WeightsCorrupt"


def test_metrics_endpoint(settings, fake_engine, fake_reader):
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "tts_queue_length" in r.text
        assert "tts_overlay_clients" in r.text
        assert r.headers["content-type"].startswith("text/plain")


# ---- overlay page ----------------------------------------------------------------------------------


def test_overlay_requires_key_and_serves_html(settings, fake_engine, fake_reader):
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.get("/overlay").status_code == 403
        assert c.get("/overlay?key=wrong").status_code == 403
        r = c.get("/overlay?key=test-key")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<html" in r.text.lower() and "TTS" in r.text


# ---- bearer auth -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/speak", {"text": "selam"}),
        ("POST", "/skip", None),
        ("POST", "/clear", None),
        ("POST", "/pause", None),
        ("POST", "/resume", None),
        ("GET", "/status", None),
    ],
)
def test_control_routes_require_bearer(settings, blocking_engine, fake_reader, method, path, body):
    app = create_app(settings, engine=blocking_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.request(method, path, json=body).status_code == 401
        assert c.request(method, path, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.request(method, path, json=body, headers={"Authorization": "Basic test-token"}).status_code == 401
        r = c.request(method, path, json=body, headers=AUTH)
        assert r.status_code == 200, r.text


def test_speak_enqueues_manual_item_while_warming(settings, blocking_engine, fake_reader):
    before = get_value(tts_items_enqueued_total, kind="manual")
    app = create_app(settings, engine=blocking_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        r = c.post("/speak", json={"text": "selam millet", "user": "anil"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "queued" and len(r.json()["id"]) == 32
        assert get_value(tts_items_enqueued_total, kind="manual") == before + 1
        s = c.get("/status", headers=AUTH).json()
        assert s["queue_length"] == 1 and s["paused"] is False and s["clients"] == 0
        assert s["readiness"] == "warming"
        item = app.state.queue.snapshot()[0]
        assert item.kind == "manual" and item.user == "anil" and item.raw_text == "selam millet"
        assert item.priority == 0


def test_speak_only_emoji_is_ignored(settings, fake_engine, fake_reader):
    before = get_value(tts_items_dropped_total, reason="empty")
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        r = c.post("/speak", json={"text": "😂😂👍"}, headers=AUTH)
        assert r.status_code == 200 and r.json()["status"] == "ignored"
        assert c.post("/speak", json={"text": "   "}, headers=AUTH).json()["status"] == "ignored"
        assert c.post("/speak", json={}, headers=AUTH).status_code == 422
        assert get_value(tts_items_dropped_total, reason="empty") == before + 2
        assert len(app.state.queue) == 0


def test_pause_resume_clear_affect_queue(settings, blocking_engine, fake_reader):
    app = create_app(settings, engine=blocking_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.post("/pause", headers=AUTH).json() == {"status": "ok", "paused": True}
        assert c.get("/status", headers=AUTH).json()["paused"] is True
        assert c.post("/resume", headers=AUTH).json() == {"status": "ok", "paused": False}
        assert c.get("/status", headers=AUTH).json()["paused"] is False
        c.post("/speak", json={"text": "bir"}, headers=AUTH)
        c.post("/speak", json={"text": "iki"}, headers=AUTH)
        assert c.get("/status", headers=AUTH).json()["queue_length"] == 2
        assert c.post("/clear", headers=AUTH).json() == {"status": "ok", "cleared": 2}
        assert c.get("/status", headers=AUTH).json()["queue_length"] == 0


# ---- no redirects ----------------------------------------------------------------------------------


def test_no_route_redirects(settings, fake_engine, fake_reader):
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    assert app.router.redirect_slashes is False
    with TestClient(app) as c:
        for path in ("/healthz/", "/readyz/", "/metrics/", "/overlay/?key=test-key"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 404, path
        for path in ("/speak/", "/skip/", "/webhook/kick/"):
            r = c.post(path, headers=AUTH, json={"text": "x"}, follow_redirects=False)
            assert r.status_code == 404, path


# ---- torch isolation -------------------------------------------------------------------------------


def test_import_app_main_does_not_import_torch():
    code = "import sys, app.main; print('torch' in sys.modules, app.main.app.router.redirect_slashes)"
    env = {**os.environ, "FAKE_ENGINE": "1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "False"], out.stdout + out.stderr


# ---- worker post-guards (pure function) ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("selam abi 🔥🔥", ("selam abi", None)),
        ("🔥🔥🔥", ("", "empty")),
        ("", ("", "empty")),
        ("\n\n  ilk satır \nikinci satır", ("ilk satır", None)),
        ("a" * 300, ("a" * 200, None)),
        ("çok   fazla    boşluk", ("çok fazla boşluk", None)),
    ],
)
def test_apply_guards(text, expected):
    assert apply_guards(text, 200, []) == expected


def test_apply_guards_blocklist_is_turkish_case_insensitive():
    assert apply_guards("bu YASAKLI kelime", 200, ["yasaklı"]) == ("", "blocklist")
    assert apply_guards("İSTANBUL güzel", 200, ["istanbul"]) == ("", "blocklist")
    assert apply_guards("serbest kelime", 200, ["yasaklı"]) == ("serbest kelime", None)
    assert apply_guards("küfür burada kalır amk", 200, []) == ("küfür burada kalır amk", None)
