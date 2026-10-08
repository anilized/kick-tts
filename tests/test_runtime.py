"""Panel runtime overrides: JSON persistence, live reader swap and engine speed, the /panel/settings API,
and AnthropicReader.check() against a mocked API. Offline."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.auth import Identity, Signer
from app.engine.fake import FakeEngine
from app.main import create_app
from app.reader.fake import FakeReader
from app.reader.llm import AnthropicReader
from app.reader.rules import RulesReader
from app.runtime import UNSET, Runtime, RuntimeOverrides, RuntimeStore


class RecordingFactory:
    """Reader factory that remembers which effective settings it was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, float]] = []
        self.closed: list[str] = []

    def __call__(self, settings):
        key = settings.ANTHROPIC_API_KEY.get_secret_value() if settings.ANTHROPIC_API_KEY else None
        self.calls.append((key, settings.SPEECH_SPEED))
        factory = self

        class R(FakeReader):
            name = "llm" if key else "rules"

            async def aclose(self_inner) -> None:
                factory.closed.append(self_inner.name)

        return R()


# ---- store -------------------------------------------------------------------------------------------


def test_store_round_trip_and_permissions(tmp_path):
    store = RuntimeStore(tmp_path / "nested" / "panel.json")
    assert store.load() == RuntimeOverrides()  # missing file
    store.save(RuntimeOverrides(anthropic_api_key="sk-ant-test-placeholder", speech_speed=0.9))
    assert json.loads(store.path.read_text()) == {"anthropic_api_key": "sk-ant-test-placeholder", "speech_speed": 0.9, "deess": None, "treble_db": None, "target_rms_db": None}
    assert store.load() == RuntimeOverrides(anthropic_api_key="sk-ant-test-placeholder", speech_speed=0.9)
    if sys.platform != "win32":
        assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in store.path.parent.iterdir() if p.name.startswith(".panel-")]  # no temp files left


def test_store_corrupt_file_is_ignored(tmp_path, caplog):
    path = tmp_path / "panel.json"
    path.write_text("{not json", encoding="utf-8")
    assert RuntimeStore(path).load() == RuntimeOverrides()
    path.write_text('{"speech_speed": "fast"}', encoding="utf-8")
    assert RuntimeStore(path).load() == RuntimeOverrides()
    assert "corrupt" in caplog.text


# ---- runtime -----------------------------------------------------------------------------------------


async def test_runtime_applies_overrides_live(make_settings, tmp_path):
    settings = make_settings(SPEECH_SPEED=0.85)
    factory = RecordingFactory()
    rt = Runtime(settings, RuntimeStore(tmp_path / "p.json"), factory)
    assert factory.calls == [(None, 0.85)] and rt.reader.name == "rules"
    d = rt.describe()
    assert d["anthropic_api_key"] == {"configured": False, "source": None, "hint": None}
    assert d["speech_speed"] == {"value": 0.85, "source": "settings", "default": 0.85, "min": 0.25, "max": 4.0}
    assert d["engine_speed_applied"] is False

    # speed before the engine exists: remembered, then applied on attach
    await rt.update(speech_speed=1.2)
    engine = FakeEngine()
    assert engine.speed == 1.0
    rt.attach_engine(engine)
    assert engine.speed == 1.2 and rt.describe()["engine_speed_applied"] is True
    assert factory.calls == [(None, 0.85)]  # speed alone never rebuilds the reader

    # key: reader rebuilt with the key, previous reader closed; speed stays
    await rt.update(anthropic_api_key="  sk-ant-test-placeholder ")
    assert factory.calls[-1] == ("sk-ant-test-placeholder", 1.2) and rt.reader.name == "llm"
    assert factory.closed == ["rules"]
    d = rt.describe()
    assert d["anthropic_api_key"] == {"configured": True, "source": "panel", "hint": "sk-ant-test-p…lder"}
    assert d["reader"] == "llm"

    # same key again: nothing rebuilt
    await rt.update(anthropic_api_key="sk-ant-test-placeholder")
    assert len(factory.calls) == 2

    # clear key -> rules again; reset speed -> yaml value on the engine
    await rt.update(anthropic_api_key="", speech_speed=None)
    assert rt.reader.name == "rules" and factory.closed == ["rules", "llm"]
    assert engine.speed == 0.85 and rt.describe()["speech_speed"]["source"] == "settings"

    # persisted and reloaded by a fresh Runtime
    await rt.update(speech_speed=0.7, anthropic_api_key="sk-ant-test-placeholder")
    rt2 = Runtime(settings, RuntimeStore(tmp_path / "p.json"), RecordingFactory())
    assert rt2.overrides == RuntimeOverrides(anthropic_api_key="sk-ant-test-placeholder", speech_speed=0.7)
    assert rt2.reader.name == "llm" and rt2.speech_speed == 0.7


async def test_runtime_env_key_is_reported_but_never_returned(make_settings, tmp_path):
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-env-placeholder-1234")
    rt = Runtime(settings, RuntimeStore(tmp_path / "p.json"), RecordingFactory())
    d = rt.describe()
    assert d["anthropic_api_key"] == {"configured": True, "source": "env", "hint": "sk-ant-env-pl…1234"}
    assert "sk-ant-env-placeholder-1234" not in json.dumps(d)
    # a panel key overrides the env key; clearing it falls back to env, not to rules-only
    await rt.update(anthropic_api_key="sk-ant-panel-placeholder-9999")
    assert rt.describe()["anthropic_api_key"]["source"] == "panel"
    await rt.update(anthropic_api_key=None)
    assert rt.describe()["anthropic_api_key"] == {"configured": True, "source": "env", "hint": "sk-ant-env-pl…1234"}
    assert rt.effective().ANTHROPIC_API_KEY.get_secret_value() == "sk-ant-env-placeholder-1234"


async def test_runtime_rejects_bad_speed_without_persisting(make_settings, tmp_path):
    rt = Runtime(make_settings(), RuntimeStore(tmp_path / "p.json"), RecordingFactory())
    for bad in (0.1, 5, "fast", -1):
        with pytest.raises(ValueError):
            await rt.update(speech_speed=bad)
    assert not (tmp_path / "p.json").exists()
    await rt.update(speech_speed=UNSET)  # no-op still persists the (empty) file
    assert json.loads((tmp_path / "p.json").read_text()) == {"anthropic_api_key": None, "speech_speed": None, "deess": None, "treble_db": None, "target_rms_db": None}


async def test_check_reader_without_api_is_trivially_ok(make_settings, tmp_path):
    rt = Runtime(make_settings(), RuntimeStore(tmp_path / "p.json"), lambda s: FakeReader())
    assert (await rt.check_reader())["ok"] is True


# ---- AnthropicReader.check ---------------------------------------------------------------------------


def _llm_reader(make_settings, status: int) -> AnthropicReader:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if status == 200:
            body = {"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001",
                    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
            return httpx2.Response(200, json=body)
        return httpx2.Response(status, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})

    settings = make_settings(ANTHROPIC_API_KEY="sk-ant-test-placeholder")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicReader(settings, RulesReader(settings), http_client=client)


async def test_anthropic_check_ok_and_bad_key(make_settings):
    ok, detail = await _llm_reader(make_settings, 200).check()
    assert ok is True and "claude-haiku-4-5-20251001" in detail
    ok, detail = await _llm_reader(make_settings, 401).check()
    assert ok is False and detail.startswith("HTTP 401") and "bad key" in detail


# ---- API ---------------------------------------------------------------------------------------------


@pytest.fixture
def panel_client(make_settings, fake_engine):
    """Logged-in, authorized panel session (forged with the server's own signer; no OAuth needed here)."""
    settings = make_settings(KICK_BROADCASTER_USER_ID=4242, SPEECH_SPEED=0.85)
    factory = RecordingFactory()
    app = create_app(settings, engine=fake_engine, reader=None, reader_factory=factory)
    with TestClient(app) as c:
        import time

        deadline = time.monotonic() + 5
        while c.get("/readyz").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.02)
        c.cookies.set("kicktts_session", Signer.from_settings(settings).sign(Identity("kick", "4242", "anildev").to_dict(), 3600))
        yield c, settings, factory


def test_settings_api_requires_authorized_session(make_settings, fake_engine, fake_reader):
    settings = make_settings(PANEL_ALLOWED_USERS="")
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert c.put("/panel/settings", json={"speech_speed": 1}).status_code == 401
        assert c.post("/panel/settings/test-reader").status_code == 401
        c.cookies.set("kicktts_session", Signer.from_settings(settings).sign(Identity("discord", "1", "x").to_dict(), 3600))
        assert c.put("/panel/settings", json={"speech_speed": 1}).status_code == 403
        assert c.post("/panel/settings/test-reader").status_code == 403
        assert "runtime" not in c.get("/panel/me").json()


def test_settings_api_speed_and_key(panel_client, fake_engine):
    c, settings, factory = panel_client
    me = c.get("/panel/me").json()
    assert me["runtime"]["speech_speed"]["value"] == 0.85 and me["runtime"]["engine_speed_applied"] is True
    assert fake_engine.speed == 0.85  # settings.yaml value applied on warm-up

    r = c.put("/panel/settings", json={"speech_speed": 1.3})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["runtime"]["speech_speed"] == {"value": 1.3, "source": "panel", "default": 0.85, "min": 0.25, "max": 4.0}
    assert fake_engine.speed == 1.3
    assert json.loads(Path(settings.PANEL_STATE_PATH).read_text())["speech_speed"] == 1.3

    assert c.put("/panel/settings", json={"speech_speed": 9}).status_code == 422
    assert c.put("/panel/settings", json={"speech_speed": "fast"}).status_code == 422
    assert fake_engine.speed == 1.3

    r = c.put("/panel/settings", json={"anthropic_api_key": "sk-ant-test-placeholder"})
    rt = r.json()["runtime"]
    assert rt["anthropic_api_key"] == {"configured": True, "source": "panel", "hint": "sk-ant-test-p…lder"}
    assert rt["reader"] == "llm" and "sk-ant-test-placeholder" not in r.text
    assert c.get("/status", headers={"Authorization": "Bearer test-token"}).json()["reader"] == "llm"
    assert factory.calls[-1] == ("sk-ant-test-placeholder", 1.3)
    assert c.post("/panel/settings/test-reader").json()["ok"] is True  # FakeReader: no check(), trivially ok

    r = c.put("/panel/settings", json={"anthropic_api_key": None, "speech_speed": None})
    rt = r.json()["runtime"]
    assert rt["anthropic_api_key"]["configured"] is False and rt["reader"] == "rules"
    assert rt["speech_speed"]["source"] == "settings" and fake_engine.speed == 0.85

    # an empty body changes nothing
    assert c.put("/panel/settings", json={}).status_code == 200
    assert c.get("/panel/me").json()["runtime"]["speech_speed"]["value"] == 0.85


def test_overrides_survive_restart_and_reach_the_worker(make_settings, tmp_path):
    """A key saved on the panel is in effect for the next process too (the file is read at startup)."""
    settings = make_settings(KICK_BROADCASTER_USER_ID=4242)
    Path(settings.PANEL_STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.PANEL_STATE_PATH).write_text(json.dumps({"anthropic_api_key": "sk-ant-test-placeholder", "speech_speed": 0.5}))
    factory = RecordingFactory()
    engine = FakeEngine()
    app = create_app(settings, engine=engine, reader_factory=factory)
    with TestClient(app) as c:
        import time

        deadline = time.monotonic() + 5
        while c.get("/readyz").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert factory.calls == [("sk-ant-test-placeholder", 0.5)]
        assert engine.speed == 0.5
        assert c.get("/status", headers={"Authorization": "Bearer test-token"}).json()["reader"] == "llm"
        assert app.state.worker._reader_getter().name == "llm"


def test_real_factory_builds_anthropic_reader_from_panel_key(make_settings, tmp_path):
    """Through the default factory (get_reader): a panel key really selects AnthropicReader, offline."""
    from app.reader import get_reader

    rt = Runtime(make_settings(), RuntimeStore(tmp_path / "p.json"), get_reader)
    assert rt.reader.name == "rules"
    import asyncio

    asyncio.run(rt.update(anthropic_api_key="sk-ant-test-placeholder"))
    assert rt.reader.name == "anthropic"
    asyncio.run(rt.update(anthropic_api_key=None))
    assert rt.reader.name == "rules"
