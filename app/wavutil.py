"""PCM16 mono WAV helpers built on the stdlib wave module and numpy."""
from __future__ import annotations

import io
import wave

import numpy as np


def encode_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    """Encode float32 samples in [-1, 1] (clipped) as a PCM16 mono WAV."""
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_info(wav: bytes) -> tuple[int, int, int, int]:
    """Return (channels, sample_rate, sampwidth_bytes, nframes)."""
    with wave.open(io.BytesIO(wav), "rb") as r:
        return r.getnchannels(), r.getframerate(), r.getsampwidth(), r.getnframes()


def wav_duration(wav: bytes) -> float:
    """Duration in seconds; 0.0 for an unparseable or zero-rate file."""
    try:
        _, rate, _, nframes = wav_info(wav)
    except (wave.Error, EOFError):
        return 0.0
    return nframes / rate if rate else 0.0
