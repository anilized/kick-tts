from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from app import interfaces as I
from app.config import Settings
from app.engine import get_engine
from app.engine.fake import FakeEngine
from app.metrics import get_value, tts_failures_total, tts_queue_length, tts_reader_latency_seconds
from app.reader.emoji import is_only_emoji, strip_emoji
from app.wavutil import encode_wav, wav_duration, wav_info

ROOT = Path(__file__).resolve().parents[1]


# ---- FakeEngine / wavutil ---------------------------------------------------------------------

@pytest.mark.parametrize("rate", [24000, 16000])
def test_fake_engine_wav_parses(fake_engine, rate):
    wav = fake_engine.synth("selam", sample_rate=rate)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, sr, width, nframes = wav_info(wav)
    assert (channels, sr, width) == (1, rate, 2)
    assert 0.3 <= wav_duration(wav) <= 1.0
    assert nframes == int(0.6 * rate)
    assert fake_engine.name == "fake"
    fake_engine.warmup()


def test_encode_wav_round_trip():
    samples = np.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype=np.float32)
    wav = encode_wav(np.tile(samples, 100), 8000)
    channels, sr, width, nframes = wav_info(wav)
    assert (channels, sr, width, nframes) == (1, 8000, 2, 500)
    assert wav_duration(wav) == pytest.approx(500 / 8000)
    pcm = np.frombuffer(wav[-1000:], dtype="<i2")
    assert pcm.max() == 32767 and pcm.min() == -32767  # clipped, not wrapped
    assert wav_duration(b"not a wav") == 0.0


# ---- interfaces -------------------------------------------------------------------------------

def test_priority_order_and_tts_item_new():
    assert I.PRIORITY["kicks"] == I.PRIORITY["manual"] == 0
    assert I.PRIORITY["kicks"] < I.PRIORITY["reward"] < I.PRIORITY["command"]
    item = I.TtsItem.new("command", "anil", "!tts selam", meta={"message_id": "m1"}, clock=lambda: 123.0)
    assert item.priority == 2 and item.created_at == 123.0 and len(item.id) == 32
    assert item.meta == {"message_id": "m1"}
    other = I.TtsItem.new("kicks", "x", "y")
    assert other.id != item.id and other.meta == {} and other.priority == 0
    assert isinstance(FakeEngine(), I.Engine)


def test_ws_models_serialize_to_exact_shapes():
    item = I.OverlayItem(id="a1", user="anil", caption="selam", audio_b64_wav="UklGRg==", duration_s=0.6)
    assert json.loads(item.model_dump_json()) == {
        "type": "item", "id": "a1", "user": "anil", "caption": "selam",
        "audio_b64_wav": "UklGRg==", "duration_s": 0.6,
    }
    assert I.OverlaySkip().model_dump() == {"type": "skip"}
    assert I.OverlayClear().model_dump() == {"type": "clear"}
    assert I.OverlayPaused(value=True).model_dump() == {"type": "paused", "value": True}
    assert I.OverlayPing().model_dump() == {"type": "ping"}
    assert I.OverlayAck(id="a1").model_dump() == {"type": "played", "id": "a1"}
    assert I.OverlayPong().model_dump() == {"type": "pong"}


def test_parse_client_message():
    ack = I.parse_client_message({"type": "played", "id": "a1"})
    assert isinstance(ack, I.OverlayAck) and ack.id == "a1"
    assert isinstance(I.parse_client_message({"type": "pong"}), I.OverlayPong)
    assert I.parse_client_message({"type": "item"}) is None
    assert I.parse_client_message({"type": "played"}) is None  # missing id
    assert I.parse_client_message({"type": "nope"}) is None
    assert I.parse_client_message("played") is None
    assert I.parse_client_message({}) is None


# ---- emoji ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("🔥🔥🔥 efsane yyn", "efsane yyn"),
    ("😂😂👍", ""),
    ("👍🏽", ""),  # skin tone modifier
    ("👨‍👩‍👧", ""),  # ZWJ family sequence
    ("1️⃣", ""),  # keycap
    ("🇹🇷", ""),  # flag (regional indicators)
    ("çok güzel ❤️", "çok güzel"),  # VS16
    ("☺︎ selam", "selam"),  # VS15
    ("Türk kalbi kırk yıl İÇĞÖŞÜ çğıöşü", "Türk kalbi kırk yıl İÇĞÖŞÜ çğıöşü"),
    ("BU OYUN ÇOK KORKUNÇ!!!!! ne? (evet), 'tamam' - bitti...", "BU OYUN ÇOK KORKUNÇ!!!!! ne? (evet), 'tamam' - bitti..."),
    ("a 😂 b\t\tc", "a b c"),  # whitespace collapsed
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿 ingiltere", "ingiltere"),  # tag sequence
])
def test_strip_emoji(text, expected):
    assert strip_emoji(text) == expected


def test_is_only_emoji():
    assert is_only_emoji("😂😂👍")
    assert is_only_emoji("🇹🇷 ❤️")
    assert is_only_emoji("")
    assert is_only_emoji("!!! ...")
    assert not is_only_emoji("🔥 efsane")
    assert not is_only_emoji("ş")
    assert not is_only_emoji("1")


async def test_fake_reader(fake_reader):
    assert fake_reader.name == "fake"
    assert await fake_reader.read("🔥🔥  SELAM   Abi İYİ 😂", "u") == "selam abi iyi"
    assert await fake_reader.read("😂😂👍", "u") == ""


# ---- settings ---------------------------------------------------------------------------------

def test_settings_requires_tokens_without_fake_engine(make_settings):
    with pytest.raises(ValidationError, match="CONTROL_TOKEN"):
        make_settings(FAKE_ENGINE=False, CONTROL_TOKEN="", OVERLAY_KEY="k")
    with pytest.raises(ValidationError, match="OVERLAY_KEY"):
        make_settings(FAKE_ENGINE=False, CONTROL_TOKEN="t", OVERLAY_KEY="")
    s = make_settings(FAKE_ENGINE=False, CONTROL_TOKEN="t", OVERLAY_KEY="k")
    assert (s.CONTROL_TOKEN, s.OVERLAY_KEY) == ("t", "k")


def test_settings_fake_engine_dev_defaults(make_settings):
    s = make_settings(CONTROL_TOKEN="", OVERLAY_KEY="")
    assert s.FAKE_ENGINE and (s.CONTROL_TOKEN, s.OVERLAY_KEY) == ("dev-token", "dev-key")


def test_settings_defaults_and_list_properties(make_settings):
    s = make_settings()
    assert s.ANTHROPIC_API_KEY is None
    assert s.READER_MODEL == "claude-haiku-4-5-20251001" and s.READER_TIMEOUT_S == 1.5
    assert s.READER_CACHE_SIZE == 512 and s.EMA_BATCH_SIZE == 1 and s.TORCH_NUM_THREADS == 2
    assert s.SAMPLE_RATE == 24000 and s.KICK_BROADCASTER_USER_ID == 0 and s.MIN_KICKS == 1
    assert s.REWARD_TITLE == "TTS" and s.COMMAND_PREFIX == "!tts" and s.COMMAND_COOLDOWN_S == 30
    assert (s.MAX_TEXT_CHARS, s.MAX_QUEUE, s.MAX_ITEM_AGE_S) == (200, 50, 600)
    assert s.WS_PING_INTERVAL_S == 20 and s.ACK_GRACE_S == 2.0 and s.LOG_LEVEL == "INFO"
    assert s.KICK_PUBLIC_KEY_URL == "https://api.kick.com/public/v1/public-key"
    assert s.KICK_PUBLIC_KEY_PEM is None
    assert s.EMA_WEIGHTS_DIR == Path("/opt/weights")
    assert s.PRONOUNCE_PATH == ROOT / "app" / "reader" / "pronounce.yaml"
    assert s.reward_statuses == ["pending", "accepted"]
    assert s.command_roles == ["broadcaster", "moderator", "subscriber"]
    assert s.blocklist == []
    s2 = make_settings(BLOCKLIST=" Foo, ,BAR ,İşte", REWARD_STATUSES="ACCEPTED,PENDING", COMMAND_ROLES="VIP,SUBSCRIBER")
    assert s2.blocklist == ["foo", "bar", "işte"]
    # ASCII Kick identifiers must not get the Turkish I -> ı mapping ('vıp' would never match the API)
    assert s2.reward_statuses == ["accepted", "pending"]
    assert s2.command_roles == ["vip", "subscriber"]
    key = make_settings(ANTHROPIC_API_KEY="sk-x").ANTHROPIC_API_KEY
    assert key.get_secret_value() == "sk-x" and "sk-x" not in repr(key)


def test_settings_reads_env(monkeypatch, make_settings):
    monkeypatch.setenv("MAX_QUEUE", "7")
    assert Settings(_env_file=None, FAKE_ENGINE=True).MAX_QUEUE == 7


def test_settings_empty_env_values_stay_none(monkeypatch, make_settings, tmp_path):
    # as written in .env.example: ANTHROPIC_API_KEY= / KICK_PUBLIC_KEY_PEM= must mean "unset"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("KICK_PUBLIC_KEY_PEM", "")
    s = Settings(_env_file=None, FAKE_ENGINE=True)
    assert s.ANTHROPIC_API_KEY is None and s.KICK_PUBLIC_KEY_PEM is None
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("KICK_PUBLIC_KEY_PEM")
    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_ENGINE=1\nANTHROPIC_API_KEY=\nKICK_PUBLIC_KEY_PEM=\nPRONOUNCE_PATH=\n", encoding="utf-8")
    s = Settings(_env_file=env_file)
    assert s.ANTHROPIC_API_KEY is None and s.KICK_PUBLIC_KEY_PEM is None
    assert s.PRONOUNCE_PATH == ROOT / "app" / "reader" / "pronounce.yaml"


# ---- factories / metrics ----------------------------------------------------------------------

def test_get_engine_fake(settings):
    assert isinstance(get_engine(settings), FakeEngine)


def test_metrics_helper():
    before = get_value(tts_failures_total, stage="warmup")
    tts_failures_total.labels(stage="warmup").inc()
    assert get_value(tts_failures_total, stage="warmup") == before + 1
    tts_queue_length.set(3)
    assert get_value(tts_queue_length) == 3
    tts_reader_latency_seconds.labels(backend="fake").observe(0.1)
    assert get_value(tts_reader_latency_seconds, backend="fake") >= 1


def test_foundation_modules_do_not_import_torch():
    code = (
        "import sys, app.interfaces, app.config, app.wavutil, app.metrics, app.engine, app.reader, "
        "app.engine.fake, app.reader.fake, app.reader.emoji\n"
        "from app.config import Settings\n"
        "from app.engine import get_engine\n"
        "get_engine(Settings(_env_file=None, FAKE_ENGINE=True)).synth('x')\n"
        "print('torch' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", out.stdout + out.stderr
