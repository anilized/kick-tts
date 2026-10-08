"""DSP voice variations (app/engine/dsp.py): pitch really moves, duration is kept, every preset yields clean
audio, WAV round trip. Pure numpy, offline."""
from __future__ import annotations

import numpy as np
import pytest

from app.engine.dsp import (
    DEFAULT_VOICE,
    VOICES,
    VoiceFX,
    apply_fx,
    apply_voice,
    pitch_shift,
    process_wav,
    time_stretch,
    voice_with,
)
from app.wavutil import decode_wav, encode_wav, wav_info

SR = 24000


def tone(freq: float, seconds: float = 1.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    # a few harmonics so WSOLA has something to align on, like a voiced sound
    return (0.6 * np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t) + 0.1 * np.sin(2 * np.pi * 3 * freq * t)).astype(np.float32)


def estimate_f0(x: np.ndarray, sr: int = SR, lo: float = 60.0, hi: float = 1000.0) -> float:
    """Autocorrelation pitch estimate on the middle of the signal."""
    mid = x[len(x) // 4 : 3 * len(x) // 4].astype(np.float64)
    mid -= mid.mean()
    spec = np.fft.rfft(mid, n=2 * len(mid))
    ac = np.fft.irfft(spec * np.conj(spec))[: len(mid)]
    lag_lo, lag_hi = int(sr / hi), int(sr / lo)
    lag = lag_lo + int(np.argmax(ac[lag_lo:lag_hi]))
    return sr / lag


def test_time_stretch_changes_length_not_pitch():
    x = tone(220.0)
    slow = time_stretch(x, 1.5, SR)
    fast = time_stretch(x, 0.7, SR)
    assert abs(len(slow) - 1.5 * len(x)) <= 2 and abs(len(fast) - 0.7 * len(x)) <= 2
    assert estimate_f0(slow) == pytest.approx(220.0, rel=0.03)
    assert estimate_f0(fast) == pytest.approx(220.0, rel=0.03)
    assert np.max(np.abs(slow)) <= 1.05 and np.isfinite(slow).all()
    assert np.array_equal(time_stretch(x, 1.0, SR), x)


@pytest.mark.parametrize("semitones,expected", [(-12.0, 110.0), (-4.0, 220.0 * 2 ** (-4 / 12)), (7.0, 220.0 * 2 ** (7 / 12))])
def test_pitch_shift_moves_f0_and_keeps_duration(semitones, expected):
    x = tone(220.0)
    y = pitch_shift(x, semitones, SR)
    assert abs(len(y) - len(x)) <= 2
    assert estimate_f0(y) == pytest.approx(expected, rel=0.03)


@pytest.mark.parametrize("name", list(VOICES))
def test_every_preset_produces_clean_audio(name):
    x = tone(200.0, 1.2) * 0.5
    y = apply_voice(x, SR, name)
    fx = VOICES[name]
    assert y.dtype == np.float32 and np.isfinite(y).all()
    assert np.max(np.abs(y)) <= 0.96
    assert abs(len(y) - len(x) / fx.speed) <= 3  # duration follows the preset's speed only
    if fx.pitch_semitones:
        assert estimate_f0(y) == pytest.approx(200.0 * 2 ** (fx.pitch_semitones / 12), rel=0.04)
    else:
        assert estimate_f0(y) == pytest.approx(200.0, rel=0.03)


def test_identity_voice_is_untouched():
    x = tone(150.0, 0.3)
    assert VOICES[DEFAULT_VOICE].is_identity()
    y = apply_voice(x, SR, DEFAULT_VOICE)
    assert np.array_equal(x, y) and y is not x
    wav = encode_wav(x, SR)
    assert process_wav(wav, DEFAULT_VOICE) is wav


def test_process_wav_round_trip_and_unknown_voice():
    wav = encode_wav(tone(180.0, 0.5), SR)
    out = process_wav(wav, "male")
    channels, rate, width, nframes = wav_info(out)
    assert (channels, rate, width) == (1, SR, 2)
    audio, sr = decode_wav(out)
    assert sr == SR and len(audio) == nframes
    assert estimate_f0(audio) == pytest.approx(180.0 * 2 ** (-4 / 12), rel=0.04)
    with pytest.raises(ValueError, match="unknown voice"):
        process_wav(wav, "robot")


def test_voice_with_tweaks_a_preset_without_changing_it():
    fx = voice_with("male", pitch_semitones=-6.0)
    assert fx.pitch_semitones == -6.0 and VOICES["male"].pitch_semitones == -4.0
    y = apply_fx(tone(200.0, 0.5), SR, fx)
    assert estimate_f0(y) == pytest.approx(200.0 * 2 ** (-6 / 12), rel=0.04)
    assert apply_fx(np.zeros(0, dtype=np.float32), SR, VoiceFX(pitch_semitones=-3)).size == 0


def test_decode_wav_averages_stereo():
    import io
    import wave

    left = (np.full(100, 0.5, dtype=np.float32) * 32767).astype("<i2")
    right = (np.full(100, -0.5, dtype=np.float32) * 32767).astype("<i2")
    inter = np.empty(200, dtype="<i2")
    inter[0::2], inter[1::2] = left, right
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(inter.tobytes())
    audio, sr = decode_wav(buf.getvalue())
    assert sr == SR and len(audio) == 100 and np.allclose(audio, 0.0, atol=1e-4)
