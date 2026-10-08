"""Output polish (app/engine/polish.py): the de-esser only touches the sibilance band and only when it sticks
out, the shelf lowers treble, normalization hits the target under the ceiling, and the worker applies the
runtime's Polish to every utterance. Offline, numpy only."""
from __future__ import annotations

import base64
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.auth import Identity, Signer
from app.engine.polish import OFF, Polish, deess, normalize_loudness, polish, polish_wav, sibilance_ratio_db, treble_shelf
from app.main import create_app
from app.wavutil import decode_wav, encode_wav

SR = 24000
AUTH = {"Authorization": "Bearer test-token"}


def voice_like(seconds: float = 1.0, sib_gain: float = 1.0, seed: int = 1) -> np.ndarray:
    """A 'vowel' (harmonics of 180 Hz, 0.3 .. 3 kHz) with a loud 6 .. 8 kHz noise burst in the middle: an 'ş'."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    vowel = sum(0.3 / k * np.sin(2 * np.pi * 180 * k * t) for k in range(2, 17))
    noise = rng.standard_normal(n)
    spec = np.fft.rfft(noise)
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < 6000) | (f > 8000)] = 0
    sib = np.fft.irfft(spec, n=n)
    sib /= np.abs(sib).max()
    burst = np.zeros(n)
    a, b = int(0.4 * n), int(0.6 * n)
    window = np.hanning(b - a)
    burst[a:b] = sib_gain * sib[a:b] * window
    voiced = np.ones(n)
    voiced[a:b] -= 0.9 * window  # fricatives are voiceless: the vowel drops out while the 'ş' sounds
    return (0.5 * vowel * voiced + 0.5 * burst).astype(np.float32)


def band_rms(x: np.ndarray, lo: float, hi: float) -> float:
    spec = np.fft.rfft(x.astype(np.float64))
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    return float(np.sqrt(np.mean(np.fft.irfft(spec, n=len(x)) ** 2)))


def test_deess_lowers_sibilance_and_leaves_the_body_alone():
    x = voice_like()
    before = sibilance_ratio_db(x, SR)
    y = deess(x, SR, 1.0)
    after = sibilance_ratio_db(y, SR)
    assert after < before - 5, (before, after)
    assert band_rms(y, 300, 3000) == pytest.approx(band_rms(x, 300, 3000), rel=0.02)  # the body is untouched
    assert band_rms(y, 6000, 8000) < 0.6 * band_rms(x, 6000, 8000)
    assert len(y) == len(x) and y.dtype == np.float32 and np.isfinite(y).all()
    # graded: stronger strength -> lower ratio; 0 = identity
    assert sibilance_ratio_db(deess(x, SR, 2.0), SR) < after
    assert np.array_equal(deess(x, SR, 0.0), x)


def test_deess_does_nothing_when_sibilance_is_already_quiet():
    x = voice_like(sib_gain=0.05)
    y = deess(x, SR, 1.0)
    assert np.max(np.abs(y - x)) < 0.02


def test_treble_shelf_direction():
    x = voice_like()
    softer = treble_shelf(x, SR, -6.0)
    brighter = treble_shelf(x, SR, 3.0)
    assert band_rms(softer, 6000, 10000) < 0.7 * band_rms(x, 6000, 10000)
    assert band_rms(brighter, 6000, 10000) > 1.2 * band_rms(x, 6000, 10000)
    assert band_rms(softer, 300, 2000) == pytest.approx(band_rms(x, 300, 2000), rel=0.05)
    assert np.array_equal(treble_shelf(x, SR, 0.0), x)


def test_normalize_hits_target_and_respects_ceiling():
    x = 0.1 * voice_like()
    y = normalize_loudness(x, SR, -20.0)
    env = np.sqrt(np.convolve(y.astype(np.float64) ** 2, np.ones(480) / 480, mode="same"))
    loud = env[env >= np.median(env)]
    assert 20 * np.log10(np.sqrt(np.mean(loud**2))) == pytest.approx(-20.0, abs=1.0)
    hot = normalize_loudness(x, SR, -3.0)  # would need a huge gain: the ceiling wins
    assert np.max(np.abs(hot)) == pytest.approx(0.95, abs=0.01)
    assert np.array_equal(normalize_loudness(np.zeros(100, dtype=np.float32), SR, -20.0), np.zeros(100))


def test_polish_wav_identity_and_round_trip():
    wav = encode_wav(voice_like(), SR)
    assert polish_wav(wav, OFF) is wav
    assert OFF.is_identity() and not Polish().is_identity()
    out = polish_wav(wav, Polish(deess=1.0, treble_db=-2.0, target_rms_db=-20.0))
    audio, sr = decode_wav(out)
    assert sr == SR and abs(len(audio) - SR) <= 1
    assert sibilance_ratio_db(audio, SR) < sibilance_ratio_db(voice_like(), SR) - 5
    assert np.max(np.abs(audio)) <= 0.96
    assert polish(np.zeros(0, dtype=np.float32), SR).size == 0


# ---- runtime + worker ---------------------------------------------------------------------------------


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def owner_client(make_settings, fake_engine, fake_reader):
    settings = make_settings(KICK_BROADCASTER_USER_ID=4242, DEESS=1.0, TREBLE_DB=-2.0, TARGET_RMS_DB=-20.0, ACK_GRACE_S=0.3)
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        c.cookies.set("kicktts_session", Signer.from_settings(settings).sign(Identity("kick", "4242", "anildev").to_dict(), 3600))
        yield c, settings, app


def test_polish_settings_flow_and_worker_applies_them(owner_client):
    c, settings, app = owner_client
    rt = c.get("/panel/me").json()["runtime"]
    assert rt["polish"]["deess"] == {"value": 1.0, "source": "settings", "default": 1.0, "min": 0.0, "max": 3.0}
    assert rt["polish"]["target_rms_db"]["value"] == -20.0 and rt["polish"]["treble_db"]["value"] == -2.0
    assert app.state.worker._polish_getter() == Polish(1.0, -2.0, -20.0)

    # validation
    assert c.put("/panel/settings", json={"deess": 4}).status_code == 422
    assert c.put("/panel/settings", json={"treble_db": 7}).status_code == 422
    assert c.put("/panel/settings", json={"target_rms_db": -3}).status_code == 422
    assert c.put("/panel/settings", json={"target_rms_db": "loud"}).status_code == 422
    assert c.put("/panel/settings", json={"target_rms_db": 0}).status_code == 200  # 0 = off is allowed

    # set a level, hear it on the overlay: the FakeEngine tone is a 0.3 amplitude sine (-13.5 dBFS RMS)
    r = c.put("/panel/settings", json={"deess": 0, "treble_db": 0, "target_rms_db": -30})
    assert r.status_code == 200
    assert r.json()["runtime"]["polish"]["target_rms_db"] == {"value": -30.0, "source": "panel", "default": -20.0, "min": -40.0, "max": -6.0}
    assert app.state.worker._polish_getter() == Polish(0.0, 0.0, -30.0)
    assert json.loads(open(settings.PANEL_STATE_PATH, encoding="utf-8").read())["target_rms_db"] == -30.0
    with c.websocket_connect("/ws?key=test-key") as ws:
        assert c.post("/speak", json={"text": "selam"}, headers=AUTH).json()["status"] == "queued"
        msg = ws.receive_json()
        audio, sr = decode_wav(base64.b64decode(msg["audio_b64_wav"]))
        rms_db = 20 * np.log10(np.sqrt(np.mean(audio[sr // 20 : -sr // 20].astype(np.float64) ** 2)))
        assert rms_db == pytest.approx(-30.0, abs=1.0)
        ws.send_json({"type": "played", "id": msg["id"]})

    # reset -> settings.yaml values again
    r = c.put("/panel/settings", json={"deess": None, "treble_db": None, "target_rms_db": None})
    assert r.json()["runtime"]["polish"]["target_rms_db"]["source"] == "settings"
    assert app.state.worker._polish_getter() == Polish(1.0, -2.0, -20.0)


def test_polish_off_sends_raw_engine_audio(make_settings, fake_engine, fake_reader):
    settings = make_settings(DEESS=0.0, TREBLE_DB=0.0, TARGET_RMS_DB=0.0, ACK_GRACE_S=0.3)
    app = create_app(settings, engine=fake_engine, reader=fake_reader)
    with TestClient(app) as c:
        assert _wait(lambda: c.get("/readyz").status_code == 200)
        assert app.state.worker._polish_getter().is_identity()
        with c.websocket_connect("/ws?key=test-key") as ws:
            c.post("/speak", json={"text": "selam"}, headers=AUTH)
            msg = ws.receive_json()
            assert base64.b64decode(msg["audio_b64_wav"]) == fake_engine.synth("selam", settings.SAMPLE_RATE)
            ws.send_json({"type": "played", "id": msg["id"]})


def test_settings_validate_polish_ranges(make_settings):
    with pytest.raises(ValueError):
        make_settings(DEESS=3.5)
    with pytest.raises(ValueError):
        make_settings(TARGET_RMS_DB=-3)
    assert make_settings(TARGET_RMS_DB=0).TARGET_RMS_DB == 0
