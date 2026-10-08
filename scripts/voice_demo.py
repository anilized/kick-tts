#!/usr/bin/env python3
"""Render one sentence in every DSP voice variation (app/engine/dsp.py) and write a WAV per voice.

    python scripts/voice_demo.py                                   # real EMA engine, weights/ next to the repo
    python scripts/voice_demo.py --text "selam millet" --out demo  # custom sentence / folder
    python scripts/voice_demo.py --in some.wav                     # process an existing WAV instead of synthesizing
    python scripts/voice_demo.py --voices male,angry_male          # a subset

Output: <out>/<voice>.wav for each voice plus <out>/original.wav, and a table with the durations. The sentence
goes through the rules reader first, like a real chat message would. Needs the EMA weights (EMA_WEIGHTS_DIR,
default ./weights) and torch; with --in nothing is synthesized.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_TEXT = "selam millet, bu akşam yayın çok iyi geçti, herkese teşekkürler kanka, gege vepe"


def synthesize(text: str, weights: Path, speed: float) -> bytes:
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    os.environ.setdefault("TORCH_NUM_THREADS", "2")
    from app.config import Settings
    from app.engine.ema import EmaEngine
    from app.reader.rules import RulesReader

    settings = Settings(
        _env_file=None, SETTINGS_PATH="", CONTROL_TOKEN="demo", OVERLAY_KEY="demo",
        EMA_WEIGHTS_DIR=weights, SPEECH_SPEED=speed, FAKE_ENGINE=False,
    )
    spoken = asyncio.run(RulesReader(settings).read(text, "demo"))
    print(f"reader : {spoken}")
    t0 = time.perf_counter()
    engine = EmaEngine(settings)
    print(f"engine : ready in {time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    wav = engine.synth(spoken, settings.SAMPLE_RATE)
    print(f"synth  : {time.perf_counter() - t0:.2f}s")
    return wav


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Turkish letters on the Windows console
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--in", dest="src", type=Path, help="process this WAV instead of synthesizing")
    ap.add_argument("--out", type=Path, default=ROOT / "demo-voices")
    ap.add_argument("--voices", default="", help="comma-separated subset of the presets (default: all)")
    ap.add_argument("--weights", type=Path, default=Path(os.environ.get("EMA_WEIGHTS_DIR") or ROOT / "weights"))
    ap.add_argument("--speed", type=float, default=0.85, help="engine speaking rate for the synthesized original")
    args = ap.parse_args()

    from app.engine.dsp import VOICES, apply_voice
    from app.wavutil import decode_wav, encode_wav, wav_duration

    names = [v.strip() for v in args.voices.split(",") if v.strip()] or list(VOICES)
    unknown = [v for v in names if v not in VOICES]
    if unknown:
        print(f"unknown voice(s): {', '.join(unknown)}; known: {', '.join(VOICES)}", file=sys.stderr)
        return 2

    if args.src:
        wav = args.src.read_bytes()
    else:
        try:
            wav = synthesize(args.text, args.weights, args.speed)
        except Exception as exc:  # missing weights / torch: say so instead of a traceback
            print(f"cannot synthesize ({type(exc).__name__}: {exc}); pass --in <wav> to process a file", file=sys.stderr)
            return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "original.wav").write_bytes(wav)
    audio, sr = decode_wav(wav)
    print(f"\n{'voice':<14}{'seconds':>8}{'ms to process':>15}   file")
    print(f"{'original':<14}{wav_duration(wav):>8.2f}{'':>15}   {args.out / 'original.wav'}")
    for name in names:
        t0 = time.perf_counter()
        out = apply_voice(audio, sr, name)
        ms = (time.perf_counter() - t0) * 1000
        path = args.out / f"{name}.wav"
        path.write_bytes(encode_wav(out, sr))
        print(f"{name:<14}{len(out) / sr:>8.2f}{ms:>15.0f}   {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
