"""EmaEngine: EMA Lightning on CPU, built from pinned local weights, never from the network.

Nothing at module level imports torch or ema_lightning (ema_lightning.api itself imports torch at import time).
EmaEngine.__init__ sets the OMP/MKL thread caps first, then imports torch, so the caps are honoured by every
thread torch/ema_lightning spawns later (ema_lightning infers on its own "ema-playhead" daemon thread, which
torch.set_num_threads alone does not reach).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from app.wavutil import encode_wav

log = logging.getLogger(__name__)

WEIGHT_FILES = ("ema.pt", "decoder.pt", "config.json")
WARMUP_TEXT = "Merhaba, bugün hava çok güzel."


class WeightsNotFoundError(FileNotFoundError):
    """The pinned weights are missing from EMA_WEIGHTS_DIR. Deliberately not an ImportError, so the engine
    factory does not mask it behind the fake engine."""


def missing_weights(weights_dir: Path) -> list[str]:
    return [name for name in WEIGHT_FILES if not (weights_dir / name).is_file()]


class EmaEngine:
    name = "ema"

    def __init__(self, settings) -> None:
        threads = int(settings.TORCH_NUM_THREADS)
        # 1. thread caps and offline mode, before torch is imported anywhere in this process
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(threads))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

        # 2. lazy imports (ImportError propagates: get_engine turns it into the loud FakeEngine fallback)
        import torch

        torch.set_num_threads(threads)

        from ema_lightning import EMA
        from ema_lightning.decoder import load_decoder
        from ema_lightning.frontend import Frontend
        from ema_lightning.model import load_acoustic

        # 3. pinned weights must be on disk; EMA() itself would download them, so it is never used
        weights_dir = Path(settings.EMA_WEIGHTS_DIR)
        missing = missing_weights(weights_dir)
        if missing:
            raise WeightsNotFoundError(
                f"EMA weights missing in {weights_dir}: {', '.join(missing)} "
                "(run scripts/fetch_weights.py --revision <sha> --out <dir>)"
            )

        # 4. build from parts on CPU (no hf_hub_download on this path)
        device = "cpu"
        acoustic = load_acoustic(weights_dir / "ema.pt", device)
        decoder = load_decoder(weights_dir / "decoder.pt", device)
        self._tts = EMA._from_parts(acoustic, decoder, Frontend(acoustic.vocab), device)
        # setting this before warm-up makes best_batch_size() return it, skipping the multi-second CPU probe
        if int(settings.EMA_BATCH_SIZE) > 0:
            self._tts._batch_size = int(settings.EMA_BATCH_SIZE)

        self.sample_rate = int(settings.SAMPLE_RATE)
        self.speed = float(settings.SPEECH_SPEED)
        log.info(
            "EmaEngine ready: torch threads=%s OMP_NUM_THREADS=%s MKL_NUM_THREADS=%s batch_size=%s speed=%s weights=%s",
            torch.get_num_threads(),
            os.environ.get("OMP_NUM_THREADS"),
            os.environ.get("MKL_NUM_THREADS"),
            self._tts._batch_size,
            self.speed,
            weights_dir,
        )

    @property
    def _batch_size(self):
        return self._tts._batch_size

    @_batch_size.setter
    def _batch_size(self, value) -> None:
        self._tts._batch_size = value

    def synth(self, text: str, sample_rate: int = 24000) -> bytes:
        speech = self._tts.say(text, speed=self.speed, sample_rate=sample_rate)
        return encode_wav(speech.audio, sample_rate)

    def warmup(self) -> None:
        self.synth(WARMUP_TEXT, self.sample_rate)
