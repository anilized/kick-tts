"""FakeEngine: a ~0.6 s 440 Hz sine tone. Dev and test stand-in for EmaEngine."""
from __future__ import annotations

import numpy as np

from app.wavutil import encode_wav

DURATION_S = 0.6
FREQ_HZ = 440.0
AMPLITUDE = 0.3


class FakeEngine:
    name = "fake"

    def synth(self, text: str, sample_rate: int = 24000) -> bytes:
        n = int(DURATION_S * sample_rate)
        t = np.arange(n, dtype=np.float32) / float(sample_rate)
        tone = AMPLITUDE * np.sin(2.0 * np.pi * FREQ_HZ * t)
        fade = min(n // 10, int(0.02 * sample_rate)) or 1  # short fade in/out against clicks
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
        return encode_wav(tone.astype(np.float32), sample_rate)

    def warmup(self) -> None:
        return None
