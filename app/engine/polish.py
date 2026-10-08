"""Output polish applied to every utterance after the engine: de-esser, treble shelf, loudness normalization.

Why: EMA Lightning renders fricatives (ş, s, ç, t) as loud as vowels, in a 5 to 9 kHz band that the small
vocoder makes fizzy, so the voice sounds harsh and "too crisp". A split-band de-esser turns that band down
only while it sticks out above the mid band; the shelf takes the edge off the remaining brightness; the
normalizer makes every utterance land at the same level in the OBS mixer.

numpy only, whole-utterance FFT processing, about 5 ms per second of audio. All three stages are tunable live
from the panel (Polish values live in app/runtime.py's overrides) and from settings.yaml / env.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.wavutil import decode_wav, encode_wav

SIB_LO_HZ, SIB_HI_HZ = 4500.0, 10000.0  # the sibilance band that is turned down
REF_LO_HZ, REF_HI_HZ = 300.0, 4000.0  # the "body" of the voice the band is compared against
TREBLE_HZ = 5000.0  # shelf corner
ENV_MS = 8.0  # envelope window
GAIN_SMOOTH_MS = 3.0
MAX_DEESS_DB = 8.0  # never attenuate the band by more than this (times strength, capped at 24 dB)
PEAK_CEILING = 0.95


@dataclass(frozen=True)
class Polish:
    deess: float = 1.0  # 0 = off; 1 = normal; up to 3 = aggressive (lower threshold, deeper cut)
    treble_db: float = -2.0  # high shelf above TREBLE_HZ; negative = softer, 0 = off
    target_rms_db: float = -20.0  # loudness target in dBFS RMS; 0 = off (peaks are always kept under PEAK_CEILING)

    def is_identity(self) -> bool:
        return self.deess <= 0 and self.treble_db == 0 and self.target_rms_db == 0


DEFAULT = Polish()
OFF = Polish(deess=0.0, treble_db=0.0, target_rms_db=0.0)


# -- primitives ------------------------------------------------------------------------------------------


def _mask(freqs: np.ndarray, lo: float, hi: float, edge: float = 500.0) -> np.ndarray:
    """Band-pass weight with raised-cosine edges `edge` Hz wide, so the split has no ringing."""
    up = np.clip((freqs - (lo - edge)) / edge, 0.0, 1.0)
    down = np.clip(((hi + edge) - freqs) / edge, 0.0, 1.0)
    return (0.5 - 0.5 * np.cos(np.pi * up)) * (0.5 - 0.5 * np.cos(np.pi * down))


def _band(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    return np.fft.irfft(spec * _mask(freqs, lo, hi), n=len(x))


def _moving_rms(x: np.ndarray, sr: int, ms: float) -> np.ndarray:
    n = max(1, int(sr * ms / 1000.0))
    csum = np.cumsum(np.concatenate([[0.0], x.astype(np.float64) ** 2]))
    idx = np.arange(len(x))
    lo = np.maximum(0, idx - n // 2)
    hi = np.minimum(len(x), idx + n // 2 + 1)
    return np.sqrt((csum[hi] - csum[lo]) / (hi - lo))


def _smooth(g: np.ndarray, sr: int, ms: float) -> np.ndarray:
    n = max(1, int(sr * ms / 1000.0))
    if n <= 1:
        return g
    kernel = np.ones(n) / n
    return np.convolve(np.pad(g, (n // 2, n - 1 - n // 2), mode="edge"), kernel, mode="valid")


def deess(x: np.ndarray, sr: int, strength: float = 1.0) -> np.ndarray:
    """Split-band de-esser. The 4.5 to 10 kHz band is attenuated only where its envelope exceeds a fraction
    of the mid-band envelope; the rest of the signal passes untouched."""
    if strength <= 0 or len(x) < 64:
        return np.asarray(x, dtype=np.float32)
    x64 = np.asarray(x, dtype=np.float64)
    sib = _band(x64, sr, SIB_LO_HZ, SIB_HI_HZ)
    body = _band(x64, sr, REF_LO_HZ, REF_HI_HZ)
    env_s = _moving_rms(sib, sr, ENV_MS)
    env_b = _moving_rms(body, sr, ENV_MS)
    # allowed sibilance level relative to the body: 0.7 at strength 1 (-3 dB), lower when stronger
    allowed = env_b * (0.7 / max(strength, 1e-3)) + 1e-5
    excess = np.clip(env_s / allowed, 1.0, 1e6)  # 1 = within the allowance, no change
    gain = excess ** (-0.6)
    floor = 10 ** (-min(MAX_DEESS_DB * strength, 24.0) / 20.0)
    gain = _smooth(np.maximum(gain, floor), sr, GAIN_SMOOTH_MS)
    return (x64 + sib * (gain - 1.0)).astype(np.float32)


def treble_shelf(x: np.ndarray, sr: int, db: float, corner_hz: float = TREBLE_HZ, width_hz: float = 1500.0) -> np.ndarray:
    if db == 0 or len(x) < 64:
        return np.asarray(x, dtype=np.float32)
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64))
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    ramp = 1.0 / (1.0 + np.exp(-(freqs - corner_hz) / (width_hz / 4.0)))  # 0 below the corner, 1 above
    gain = 1.0 + (10 ** (db / 20.0) - 1.0) * ramp
    return np.fft.irfft(spec * gain, n=len(x)).astype(np.float32)


def normalize_loudness(x: np.ndarray, sr: int, target_rms_db: float, ceiling: float = PEAK_CEILING) -> np.ndarray:
    """Scale to the RMS target measured over the louder half of the utterance (pauses do not count), with a hard
    peak ceiling so a short loud burst never clips. target 0 = only the ceiling applies."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x
    gain = 1.0
    if target_rms_db != 0:
        env = _moving_rms(x, sr, 20.0) if len(x) > sr // 50 else np.abs(x)
        loud = env[env >= np.median(env)]
        rms = float(np.sqrt(np.mean(loud**2))) if len(loud) else float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        if rms > 1e-6:
            gain = 10 ** (target_rms_db / 20.0) / rms
    gain = min(gain, ceiling / peak)
    return (x * gain).astype(np.float32)


# -- entry points ----------------------------------------------------------------------------------------


def polish(audio: np.ndarray, sr: int, p: Polish = DEFAULT) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32)
    if p.is_identity() or len(x) == 0:
        return x
    x = deess(x, sr, p.deess)
    x = treble_shelf(x, sr, p.treble_db)
    return normalize_loudness(x, sr, p.target_rms_db)


def polish_wav(wav: bytes, p: Polish = DEFAULT) -> bytes:
    if p.is_identity():
        return wav
    audio, sr = decode_wav(wav)
    return encode_wav(polish(audio, sr, p), sr)


def sibilance_ratio_db(x: np.ndarray, sr: int) -> float:
    """Diagnostic: energy of the sibilance band relative to the body band, over the loud frames. Lower = softer."""
    x64 = np.asarray(x, dtype=np.float64)
    sib = _band(x64, sr, SIB_LO_HZ, SIB_HI_HZ)
    body = _band(x64, sr, REF_LO_HZ, REF_HI_HZ)
    env = _moving_rms(x64, sr, 20.0)
    loud = env >= np.percentile(env, 60)
    return float(10 * np.log10((sib[loud] ** 2).mean() / ((body[loud] ** 2).mean() + 1e-12) + 1e-12))
