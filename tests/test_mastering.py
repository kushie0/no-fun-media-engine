"""Tests for feedback-resonance detection in nofun.mastering."""
import numpy as np
import soundfile as sf

from nofun.mastering import detect_resonant_peaks, _channel_is_silent
from nofun.mastering_meta import DEAD_RMS_DB

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


# ---------------------------------------------------------------------------
# _channel_is_silent — present-but-dead room-mic detection (fix 2026-06-03)
# ---------------------------------------------------------------------------

def _write_wav(path, data):
    sf.write(str(path), data.astype(np.float32), SR, subtype='FLOAT')


def test_silent_channel_detected(tmp_path):
    """A near-zero track (e.g. unplugged mic) reads as silent."""
    p = tmp_path / 'chan30.wav'
    _write_wav(p, _tone(440, SR * 3, amp=1e-5))  # ~-97 dB, below DEAD_RMS_DB
    assert _channel_is_silent(p) is True


def test_live_channel_not_silent(tmp_path):
    """A normal-level track is not silent."""
    p = tmp_path / 'chan29.wav'
    _write_wav(p, _tone(440, SR * 3, amp=0.5))  # ~-9 dB
    assert _channel_is_silent(p) is False


def test_threshold_boundary(tmp_path):
    """RMS just above DEAD_RMS_DB counts as live; well below counts as dead."""
    # sine RMS = amp/sqrt(2); pick amps straddling DEAD_RMS_DB (-80 dB → rms 1e-4)
    loud = tmp_path / 'loud.wav'
    quiet = tmp_path / 'quiet.wav'
    _write_wav(loud, _tone(440, SR * 2, amp=10 ** (DEAD_RMS_DB / 20) * np.sqrt(2) * 4))
    _write_wav(quiet, _tone(440, SR * 2, amp=10 ** (DEAD_RMS_DB / 20) * np.sqrt(2) / 4))
    assert _channel_is_silent(loud) is False
    assert _channel_is_silent(quiet) is True


def test_quiet_intro_not_false_dead(tmp_path):
    """Default sampling reads a mid-file window, so a silent intro/outro around
    a live middle is NOT mistaken for a dead channel."""
    silent = np.zeros(SR * 60, dtype=np.float32)
    live = _tone(440, SR * 60, amp=0.5)
    data = np.concatenate([silent, live, silent])  # 180s: dead, live, dead
    p = tmp_path / 'chan29.wav'
    _write_wav(p, data)
    assert _channel_is_silent(p) is False                      # mid window = live
    assert _channel_is_silent(p, 0.0, 30.0) is True            # explicit intro window = dead


def test_missing_file_not_silent(tmp_path):
    """A read failure must not be reported as silence (fail safe = treat as live)."""
    assert _channel_is_silent(tmp_path / 'nope.wav') is False
