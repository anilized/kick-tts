"""Engine tests: FakeEngine, weights lock/verify, EmaEngine construction (stubbed loaders, no weights, no network).

Everything that imports torch runs in a subprocess so this process never has torch in sys.modules
(tests/test_api.py asserts that importing app.main leaves torch out).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.engine.fake import FakeEngine
from app.wavutil import wav_duration, wav_info
from scripts import fetch_weights as fw

ROOT = Path(__file__).resolve().parent.parent
HAS_TORCH = importlib.util.find_spec("torch") is not None
HAS_EMA = importlib.util.find_spec("ema_lightning") is not None


def run_py(code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "HF_HUB_OFFLINE")}
    env.update({"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", **(env_extra or {})})
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


# ---------------------------------------------------------------- FakeEngine


@pytest.mark.parametrize("rate", [24000, 16000, 48000])
def test_fake_engine_wav_is_valid_pcm16_mono(rate):
    wav = FakeEngine().synth("merhaba", rate)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, sr, width, _ = wav_info(wav)
    assert (channels, sr, width) == (1, rate, 2)
    assert 0.3 <= wav_duration(wav) <= 1.0


# ---------------------------------------------------------------- weights lock


def make_weights(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in fw.FILES:
        (directory / name).write_bytes(f"content of {name}".encode())


def test_sha256_file(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"abc")
    assert fw.sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_lock_schema_round_trips(tmp_path):
    make_weights(tmp_path / "w")
    lock_path = tmp_path / "weights.lock.json"
    lock = fw.write_lock(lock_path, "a" * 40, tmp_path / "w")
    assert fw.load_lock(lock_path) == lock
    assert set(lock) == {"revision", "files"} and lock["revision"] == "a" * 40
    assert set(lock["files"]) == {"ema.pt", "decoder.pt", "config.json"}
    assert not fw.is_placeholder(lock)
    assert fw.verify(tmp_path / "w", lock) == []


def test_verify_rejects_sha256_mismatch(tmp_path):
    make_weights(tmp_path)
    lock = fw.write_lock(tmp_path / "lock.json", "b" * 40, tmp_path)
    (tmp_path / "decoder.pt").write_bytes(b"tampered")
    problems = fw.verify(tmp_path, lock)
    assert len(problems) == 1 and "decoder.pt" in problems[0] and "mismatch" in problems[0]


def test_verify_reports_missing_file_and_unlisted_name(tmp_path):
    make_weights(tmp_path)
    lock = fw.write_lock(tmp_path / "lock.json", "c" * 40, tmp_path)
    (tmp_path / "ema.pt").unlink()
    del lock["files"]["config.json"]
    problems = " | ".join(fw.verify(tmp_path, lock))
    assert "ema.pt: missing" in problems and "config.json: not listed" in problems


def test_load_lock_rejects_bad_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"revision": "x"}), encoding="utf-8")
    with pytest.raises(ValueError):
        fw.load_lock(bad)


def test_committed_lock_is_a_placeholder_and_detected_as_invalid():
    lock = fw.load_lock(ROOT / "weights.lock.json")
    assert set(lock["files"]) == set(fw.FILES)
    assert fw.is_placeholder(lock)


def test_main_refuses_placeholder_lock_without_downloading(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fw, "download", lambda *a, **k: pytest.fail("must not download"))
    rc = fw.main(["--revision", "d" * 40, "--out", str(tmp_path), "--lock", str(ROOT / "weights.lock.json")])
    assert rc != 0 and "placeholder" in capsys.readouterr().err


def test_main_fails_on_mismatch_and_passes_on_match(monkeypatch, tmp_path):
    rev = "e" * 40

    def fake_download(revision, out):
        assert revision == rev
        make_weights(out)

    monkeypatch.setattr(fw, "download", fake_download)
    src = tmp_path / "src"
    make_weights(src)
    lock_path = tmp_path / "lock.json"
    fw.write_lock(lock_path, rev, src)
    assert fw.main(["--revision", rev, "--out", str(tmp_path / "out"), "--lock", str(lock_path)]) == 0

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["files"]["ema.pt"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    assert fw.main(["--revision", rev, "--out", str(tmp_path / "out2"), "--lock", str(lock_path)]) == 1
    # a revision that differs from the lock is refused too
    assert fw.main(["--revision", "f" * 40, "--out", str(tmp_path / "out3"), "--lock", str(lock_path)]) == 2


def test_main_write_lock_records_hashes(monkeypatch, tmp_path):
    monkeypatch.setattr(fw, "download", lambda revision, out: make_weights(out))
    lock_path = tmp_path / "lock.json"
    lock_path.write_text((ROOT / "weights.lock.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert fw.main(["--revision", "9" * 40, "--out", str(tmp_path / "w"), "--lock", str(lock_path), "--write-lock"]) == 0
    lock = fw.load_lock(lock_path)
    assert lock["revision"] == "9" * 40 and not fw.is_placeholder(lock)
    assert fw.verify(tmp_path / "w", lock) == []


# ---------------------------------------------------------------- torch stays lazy


def test_importing_engine_modules_does_not_import_torch():
    r = run_py(
        """
        import sys
        import app.engine, app.engine.ema, app.engine.fake
        assert "torch" not in sys.modules, "torch imported"
        assert "ema_lightning" not in sys.modules, "ema_lightning imported"
        print("ok")
        """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


# ---------------------------------------------------------------- EmaEngine construction (stubbed ema_lightning)

STUB_PRELUDE = """
    import os, sys, types, importlib.abc, tempfile
    from pathlib import Path
    import numpy as np
    from app.config import Settings

    events = []

    class Watch(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "torch":
                events.append(("torch_import", os.environ.get("OMP_NUM_THREADS"), os.environ.get("MKL_NUM_THREADS")))
            return None

    sys.meta_path.insert(0, Watch())

    class StubEMA:
        @classmethod
        def _from_parts(cls, model, decoder, frontend, device):
            import torch
            events.append(("from_parts", device, torch.get_num_threads()))
            self = cls.__new__(cls)
            self._batch_size = None
            self.parts = (model, decoder, frontend)
            return self
        def say(self, text, sample_rate=48000):
            assert self._batch_size == 1
            n = sample_rate // 2
            return types.SimpleNamespace(audio=(0.2 * np.sin(np.arange(n) / 10)).astype(np.float32))

    def module(name, **attrs):
        m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m

    class Vocab: vocab = "abc"
    def load_acoustic(path, device):
        import torch
        events.append(("load_acoustic", Path(path).name, torch.get_num_threads()))
        return Vocab()
    def load_decoder(path, device):
        events.append(("load_decoder", Path(path).name)); return object()

    module("ema_lightning", EMA=StubEMA)
    module("ema_lightning.model", load_acoustic=load_acoustic)
    module("ema_lightning.decoder", load_decoder=load_decoder)
    module("ema_lightning.frontend", Frontend=lambda vocab: ("frontend", tuple(vocab)))

    def settings(weights, **kw):
        return Settings(_env_file=None, CONTROL_TOKEN="t", OVERLAY_KEY="k", FAKE_ENGINE=False,
                        EMA_WEIGHTS_DIR=weights, **kw)
"""


@pytest.mark.skipif(not HAS_TORCH, reason="torch not installed")
def test_ema_engine_construction_order_threads_and_batch_size(tmp_path):
    make_weights(tmp_path)
    r = run_py(
        STUB_PRELUDE
        + f"""
    from app.wavutil import wav_info
    assert "torch" not in sys.modules
    from app.engine.ema import EmaEngine
    assert "torch" not in sys.modules
    eng = EmaEngine(settings(Path(r"{tmp_path}"), TORCH_NUM_THREADS=3, EMA_BATCH_SIZE=1))
    # env was set before torch was first imported
    assert events[0] == ("torch_import", "3", "3"), events
    assert os.environ["OMP_NUM_THREADS"] == "3" and os.environ["MKL_NUM_THREADS"] == "3"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    # torch.set_num_threads was called before the loaders ran
    assert ("load_acoustic", "ema.pt", 3) in events and ("load_decoder", "decoder.pt") in events
    assert ("from_parts", "cpu", 3) in events
    assert eng.name == "ema" and eng._batch_size == 1
    wav = eng.synth("merhaba", 24000)
    assert wav_info(wav)[:3] == (1, 24000, 2)
    eng.warmup()
    print("ok")
    """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(not HAS_TORCH, reason="torch not installed")
def test_ema_engine_respects_preset_omp_and_skips_batch_size_when_zero(tmp_path):
    make_weights(tmp_path)
    r = run_py(
        STUB_PRELUDE
        + f"""
    from app.engine.ema import EmaEngine
    eng = EmaEngine(settings(Path(r"{tmp_path}"), TORCH_NUM_THREADS=2, EMA_BATCH_SIZE=0))
    assert os.environ["OMP_NUM_THREADS"] == "5", os.environ["OMP_NUM_THREADS"]   # setdefault keeps the preset
    assert os.environ["MKL_NUM_THREADS"] == "2"
    assert eng._batch_size is None
    print("ok")
    """,
        env_extra={"OMP_NUM_THREADS": "5"},
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(not HAS_TORCH, reason="torch not installed")
def test_missing_weights_raise_and_are_not_masked_by_the_factory(tmp_path):
    (tmp_path / "ema.pt").write_bytes(b"x")  # decoder.pt and config.json missing
    r = run_py(
        STUB_PRELUDE
        + f"""
    from app.engine import get_engine
    from app.engine.ema import WeightsNotFoundError
    try:
        get_engine(settings(Path(r"{tmp_path}")))
    except WeightsNotFoundError as e:
        assert "decoder.pt, config.json" in str(e), str(e)
        print("ok")
    else:
        raise SystemExit("factory fell back instead of raising")
    """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_factory_falls_back_to_fake_when_ema_unimportable(tmp_path):
    make_weights(tmp_path)
    r = run_py(
        f"""
        import sys
        sys.modules["torch"] = None
        sys.modules["ema_lightning"] = None
        from pathlib import Path
        from app.config import Settings
        from app.engine import get_engine
        s = Settings(_env_file=None, CONTROL_TOKEN="t", OVERLAY_KEY="k", FAKE_ENGINE=False,
                     EMA_WEIGHTS_DIR=Path(r"{tmp_path}"))
        assert get_engine(s).name == "fake"
        s = Settings(_env_file=None, FAKE_ENGINE=True)
        assert get_engine(s).name == "fake"
        print("ok")
        """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- real model (needs ema_lightning + pinned weights)


def _real_weights_dir() -> Path | None:
    d = Path(os.environ.get("EMA_WEIGHTS_DIR", "/opt/weights"))
    return d if all((d / n).is_file() for n in fw.FILES) else None


@pytest.mark.skipif(not HAS_EMA or _real_weights_dir() is None, reason="ema_lightning or pinned weights not available")
def test_real_ema_engine_synthesizes_turkish():
    r = run_py(
        f"""
        from pathlib import Path
        from app.config import Settings
        from app.engine.ema import EmaEngine
        from app.wavutil import wav_info, wav_duration
        e = EmaEngine(Settings(_env_file=None, CONTROL_TOKEN="t", OVERLAY_KEY="k",
                               EMA_WEIGHTS_DIR=Path(r"{_real_weights_dir()}")))
        e.warmup()
        wav = e.synth("selam millet nasılsınız", 24000)
        assert wav_info(wav)[:3] == (1, 24000, 2) and wav_duration(wav) > 0.5
        print("ok")
        """
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
