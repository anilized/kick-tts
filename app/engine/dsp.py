"""Voice variations by signal processing on the engine's output: male / angry / deep / chipmunk from the one
EMA Lightning voice. numpy only, no new dependency, fast (a few ms per second of audio on CPU).

How it works
- pitch_shift: resample (changes pitch AND duration, and moves the formants, which is what makes a lower
  voice sound like a bigger speaker rather than a slowed tape) then WSOLA time-stretch back to the original
  duration. Negative semitones = lower (male), positive = higher.
- time_stretch: WSOLA (waveform-similarity overlap-add). Each output frame is taken from near its nominal
  input position, at the offset that best continues the previous frame, so speech stays clean without a
  phase vocoder's smearing. factor > 1 = longer (slower), < 1 = shorter (faster).
- "angry": faster, slightly higher, tanh saturation (shouting compresses and distorts), a presence boost
  (bright, forward), and a ~30 Hz amplitude modulation for vocal roughness ("growl").

This is a caricature of a different speaker, not a second voice model: fine for joke voices and tiers,
not a substitute for a multi-speaker engine (see README, "Voice variations").
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from app.wavutil import decode_wav, encode_wav


@dataclass(frozen=True)
class VoiceFX:
    pitch_semitones: float = 0.0  # <0 lower, >0 higher; formants move with it
    speed: float = 1.0  # >1 faster (shorter), via WSOLA, pitch unchanged
    drive: float = 0.0  # tanh saturation amount; 0 = off, 2..4 = shouting
    presence: float = 0.0  # 0..1, adds a high-passed copy (brightness)
    growl_depth: float = 0.0  # 0..1 amplitude modulation depth (vocal roughness)
    growl_hz: float = 30.0
    gain: float = 1.0  # final peak target multiplier (<=1 keeps headroom)

    def is_identity(self) -> bool:
        return self == VoiceFX()


# User-facing names (chat command prefix, reward titles, panel). The engine's own voice is "female".
VOICES: dict[str, VoiceFX] = {
    "female": VoiceFX(),
    "male": VoiceFX(pitch_semitones=-4.0, speed=0.97),
    "angry_female": VoiceFX(pitch_semitones=1.0, speed=1.12, drive=2.5, presence=0.35, growl_depth=0.22, growl_hz=32.0),
    "angry_male": VoiceFX(pitch_semitones=-3.5, speed=1.10, drive=3.0, presence=0.4, growl_depth=0.3, growl_hz=28.0),
    "deep": VoiceFX(pitch_semitones=-7.0, speed=0.92, drive=1.2, growl_depth=0.15, growl_hz=24.0),
    "chipmunk": VoiceFX(pitch_semitones=7.0, speed=1.15),
}

DEFAULT_VOICE = "female"


# -- primitives ------------------------------------------------------------------------------------------


def resample(x: np.ndarray, factor: float) -> np.ndarray:
    """Play `factor` times faster: length / factor, pitch * factor. Linear interpolation (good enough here)."""
    if factor == 1.0 or len(x) < 2:
        return x.astype(np.float32, copy=True)
    n_out = max(2, int(round(len(x) / factor)))
    src = np.linspace(0.0, len(x) - 1, n_out)
    return np.interp(src, np.arange(len(x)), x).astype(np.float32)


def time_stretch(x: np.ndarray, factor: float, sr: int, frame_ms: float = 25.0, tol_ms: float = 8.0) -> np.ndarray:
    """WSOLA: output is `factor` times as long, same pitch. factor 1.5 = slower, 0.8 = faster."""
    x = np.asarray(x, dtype=np.float32)
    if abs(factor - 1.0) < 1e-3 or len(x) == 0:
        return x.copy()
    n = max(32, int(sr * frame_ms / 1000.0) & ~1)  # even frame length
    hop = n // 2
    tol = max(1, int(sr * tol_ms / 1000.0))
    k = np.arange(n)
    win = (0.5 - 0.5 * np.cos(2.0 * np.pi * k / n)).astype(np.float32)  # periodic Hann: COLA at 50 % hop

    out_len = int(round(len(x) * factor))
    out = np.zeros(out_len + n, dtype=np.float32)
    norm = np.zeros(out_len + n, dtype=np.float32)
    padded = np.concatenate([x, np.zeros(n + hop + tol, dtype=np.float32)])
    max_pos = len(x) - 1  # analysis frames may run into the zero padding at the very end

    prev: int | None = None
    for s in range(0, out_len, hop):
        nominal = min(int(s / factor), max_pos)
        if prev is None:
            best = nominal
        else:
            target = padded[prev + hop : prev + hop + n]  # the natural continuation of the last frame
            lo = max(0, nominal - tol)
            hi = min(max_pos, nominal + tol)
            corr = np.correlate(padded[lo : hi + n], target, mode="valid")
            best = lo + int(np.argmax(corr))
        frame = padded[best : best + n]
        out[s : s + n] += frame * win
        norm[s : s + n] += win
        prev = best
    norm[norm < 1e-3] = 1.0
    return (out / norm)[:out_len]


def pitch_shift(x: np.ndarray, semitones: float, sr: int) -> np.ndarray:
    """Shift pitch (and formants) by `semitones`, keeping the duration."""
    if abs(semitones) < 1e-3:
        return np.asarray(x, dtype=np.float32).copy()
    factor = 2.0 ** (semitones / 12.0)
    shifted = resample(x, factor)  # pitch * factor, length / factor
    return time_stretch(shifted, factor, sr)[: len(x)]


def saturate(x: np.ndarray, drive: float) -> np.ndarray:
    if drive <= 0:
        return x
    return (np.tanh(drive * x) / np.tanh(drive)).astype(np.float32)


def presence(x: np.ndarray, sr: int, amount: float, cutoff_hz: float = 2000.0) -> np.ndarray:
    """Add a high-passed copy (first-order slope above cutoff_hz, done in the frequency domain): brighter,
    more 'forward', the way a shouting voice sits in a mix."""
    if amount <= 0 or len(x) < 2:
        return x
    spectrum = np.fft.rfft(x.astype(np.float64))
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    gain = freqs / np.sqrt(freqs**2 + cutoff_hz**2)  # 0 at DC, ~0.71 at the cutoff, -> 1 above it
    hp = np.fft.irfft(spectrum * gain, n=len(x))
    return (x + amount * hp).astype(np.float32)


def growl(x: np.ndarray, sr: int, depth: float, hz: float) -> np.ndarray:
    if depth <= 0:
        return x
    t = np.arange(len(x), dtype=np.float32) / sr
    mod = 1.0 - depth * 0.5 * (1.0 + np.sin(2.0 * np.pi * hz * t))
    return (x * mod).astype(np.float32)


def normalize(x: np.ndarray, peak: float = 0.95) -> np.ndarray:
    m = float(np.max(np.abs(x))) if len(x) else 0.0
    if m < 1e-6:
        return x
    return (x * (peak / m)).astype(np.float32)


# -- voices ----------------------------------------------------------------------------------------------


def apply_fx(audio: np.ndarray, sr: int, fx: VoiceFX) -> np.ndarray:
    """float32 mono in [-1, 1] -> same, processed. The identity VoiceFX returns a copy untouched."""
    x = np.asarray(audio, dtype=np.float32)
    if fx.is_identity() or len(x) == 0:
        return x.copy()
    peak_in = float(np.max(np.abs(x))) or 1.0
    if fx.pitch_semitones:
        x = pitch_shift(x, fx.pitch_semitones, sr)
    if fx.speed != 1.0:
        x = time_stretch(x, 1.0 / fx.speed, sr)
    x = growl(x, sr, fx.growl_depth, fx.growl_hz)
    x = presence(x, sr, fx.presence)
    if fx.drive > 0:
        x = saturate(normalize(x, 0.9), fx.drive)
    return normalize(x, min(0.95, peak_in * fx.gain if fx.drive == 0 else 0.95 * fx.gain))


def apply_voice(audio: np.ndarray, sr: int, voice: str) -> np.ndarray:
    try:
        fx = VOICES[voice]
    except KeyError:
        raise ValueError(f"unknown voice {voice!r}; known: {', '.join(VOICES)}") from None
    return apply_fx(audio, sr, fx)


def process_wav(wav: bytes, voice: str) -> bytes:
    """WAV bytes (as the engine returns them) -> WAV bytes with the voice applied. ValueError for an unknown voice."""
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}; known: {', '.join(VOICES)}")
    if VOICES[voice].is_identity():
        return wav
    audio, sr = decode_wav(wav)
    return encode_wav(apply_voice(audio, sr, voice), sr)


def voice_with(voice: str, **changes) -> VoiceFX:
    """A tweaked copy of a preset, for experiments: voice_with("male", pitch_semitones=-5)."""
    return replace(VOICES[voice], **changes)
