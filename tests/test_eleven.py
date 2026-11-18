"""ElevenLabs engine (mocked HTTP), the fallback router with its back-off, and the Runtime wiring. Offline."""
from __future__ import annotations

import asyncio
import io
import json
import wave

import httpx
import pytest

from app.config import Settings
from app.engine.eleven import ElevenLabsEngine, ElevenLabsError
from app.engine.fake import FakeEngine
from app.engine.router import BACKOFF_PERMANENT_S, BACKOFF_TRANSIENT_S, RoutedEngine
from app.reader.fake import FakeReader
from app.runtime import Runtime, RuntimeStore

PCM = b"\x01\x00" * 2400  # 0.1 s at 24 kHz


def engine_with(handler, **kw) -> ElevenLabsEngine:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ElevenLabsEngine("xi-test-placeholder", "voice123", client=client, **kw)


def test_synth_request_and_wav():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["key"] = req.headers["xi-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=PCM)

    wav = engine_with(handler, speed=5.0).synth("selam", 24000)
    assert seen["url"].endswith("/v1/text-to-speech/voice123?output_format=pcm_24000")
    assert seen["key"] == "xi-test-placeholder"
    assert seen["body"]["text"] == "selam"
    assert seen["body"]["language_code"] == "tr"  # turbo v2.5 default accepts it
    assert seen["body"]["voice_settings"]["speed"] == 1.2  # clamped
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 2400)


def test_multilingual_v2_gets_no_language_code():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=PCM)

    engine_with(handler, model_id="eleven_multilingual_v2").synth("x")
    assert "language_code" not in seen["body"]


def test_odd_sample_rate_falls_back_to_24k():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, content=PCM)

    engine_with(handler).synth("x", 48000)
    assert "pcm_24000" in seen["url"]


def test_api_error_carries_status_and_message():
    def handler(req):
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})

    with pytest.raises(ElevenLabsError) as ei:
        engine_with(handler).synth("x")
    assert ei.value.status == 401 and ei.value.permanent and "Invalid API key" in str(ei.value)


def test_network_error_is_transient():
    def handler(req):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(ElevenLabsError) as ei:
        engine_with(handler).synth("x")
    assert not ei.value.permanent


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Flaky:
    name = "elevenlabs"

    def __init__(self, exc=None):
        self.exc, self.calls = exc, 0

    def synth(self, text, sample_rate=24000):
        self.calls += 1
        if self.exc:
            raise self.exc
        return b"remote"

    def warmup(self):
        pass


def test_router_falls_back_and_backs_off():
    clock = Clock()
    remote = Flaky(ElevenLabsError("HTTP 429: busy", 429))
    r = RoutedEngine(FakeEngine(), lambda: remote, clock=clock)
    assert r.synth("a")[:4] == b"RIFF"  # fell back to the local sine
    assert r.last_used == "fake" and "429" in r.last_error
    r.synth("b")
    assert remote.calls == 1  # skipped during the back-off
    clock.t += BACKOFF_TRANSIENT_S + 1
    remote.exc = None
    assert r.synth("c") == b"remote" and r.last_used == "elevenlabs" and r.last_error is None


def test_router_permanent_errors_back_off_longer():
    clock = Clock()
    remote = Flaky(ElevenLabsError("HTTP 401", 401))
    r = RoutedEngine(FakeEngine(), lambda: remote, clock=clock)
    r.synth("a")
    clock.t += BACKOFF_TRANSIENT_S + 1
    r.synth("b")
    assert remote.calls == 1
    clock.t += BACKOFF_PERMANENT_S
    r.synth("c")
    assert remote.calls == 2


def test_router_without_remote_is_the_local_engine():
    r = RoutedEngine(FakeEngine(), lambda: None)
    assert r.synth("a")[:4] == b"RIFF" and r.last_used == "fake"
    r.speed = 0.8
    assert r.local.speed == 0.8 and r.speed == 0.8


def make_runtime(tmp_path, **env):
    s = Settings(FAKE_ENGINE=True, SETTINGS_PATH="", **env)
    rt = Runtime(s, RuntimeStore(tmp_path / "p.json"), lambda _s: FakeReader())
    rt.attach_engine(FakeEngine())
    return rt


def test_runtime_default_is_ema_without_a_key(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.remote_engine() is None and not rt.eleven_active
    assert rt.describe()["elevenlabs"]["configured"] is False


def test_runtime_panel_key_activates_and_switch_turns_off(tmp_path):
    rt = make_runtime(tmp_path)
    d = asyncio.run(rt.update(elevenlabs_api_key="xi-test-placeholder-1234", elevenlabs_voice_id="abcDEF123"))
    assert d["elevenlabs"]["active"] and d["elevenlabs"]["voice_id"] == "abcDEF123"
    assert "xi-test-placeholder-1234" not in json.dumps(d)  # only the hint leaves the server
    assert rt.remote_engine() is rt.remote_engine()  # cached
    d = asyncio.run(rt.update(use_elevenlabs=False))
    assert not d["elevenlabs"]["active"] and rt.remote_engine() is None
    d = asyncio.run(rt.update(use_elevenlabs=None, elevenlabs_api_key=None, elevenlabs_voice_id=None))
    assert not d["elevenlabs"]["configured"]


def test_runtime_env_key_is_default_on_and_bad_voice_rejected(tmp_path):
    rt = make_runtime(tmp_path, ELEVENLABS_API_KEY="xi-env-placeholder-1234")
    assert rt.eleven_active and rt.describe()["elevenlabs"]["source"] == "env"
    with pytest.raises(ValueError):
        asyncio.run(rt.update(elevenlabs_voice_id="../evil"))


def test_check_voice_reports_the_api_error(tmp_path):
    rt = make_runtime(tmp_path, ELEVENLABS_API_KEY="xi-env-placeholder-1234")
    rt.remote_engine()._client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(402, json={"detail": {"message": "quota exceeded"}}))
    )
    r = asyncio.run(rt.check_voice())
    assert r["ok"] is False and "quota exceeded" in r["detail"]
    rt.remote_engine()._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=PCM)))
    assert asyncio.run(rt.check_voice())["ok"] is True
