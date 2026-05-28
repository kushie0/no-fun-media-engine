"""Tests for feedback-resonance detection in nofun.mastering."""
import numpy as np

from nofun.mastering import detect_resonant_peaks

SR = 48000


def _noise(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * 0.05).astype(np.float32)


def _tone(f: float, n: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def test_detects_inband_tone():
    x = _noise(SR * 5) + _tone(2440, SR * 5)
    peaks = detect_resonant_peaks(x, SR, band=(2000, 3000))
    assert peaks
    assert abs(peaks[0][0] - 2440) < 20


def test_clean_noise_no_peaks():
    assert detect_resonant_peaks(_noise(SR * 5), SR, band=(2000, 3000)) == []


def test_out_of_band_ignored():
    x = _noise(SR * 5) + _tone(500, SR * 5)
    assert detect_resonant_peaks(x, SR, band=(2000, 3000)) == []


def test_caps_max_peaks():
    x = _noise(SR * 5)
    for f in (2100, 2300, 2500, 2700, 2900):
        x = x + _tone(f, len(x), amp=0.4)
    assert len(detect_resonant_peaks(x, SR, band=(2000, 3000), max_peaks=3)) <= 3


def test_too_short_returns_empty():
    assert detect_resonant_peaks(_noise(1000), SR, band=(2000, 3000)) == []
