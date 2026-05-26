"""nofun/mastering.py — Experimental quick-master from raw channel WAVs.

Generates up to 4 stereo WAV masters per performance by combining two mix
strategies (selected channels vs all channels) with two processing backends
(ffmpeg approximation vs real OTT VST). All outputs are non-fatal — failures
are logged and skipped.
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from pathlib import Path
from nofun.paths import detect_platform, is_darwin
from typing import Any, Callable

import numpy as np

__all__ = [
    'generate_masters',
    'find_channels',
    'SELECTED_CHANNELS',
    'SELECTED_PANS',
    'OTT_PATHS',
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Channels to include in the "selected" mix and their pan positions
# -1.0 = full left, 0.0 = centre, +1.0 = full right
SELECTED_CHANNELS: list[int] = [29, 30, 31, 32]
SELECTED_PANS: dict[int, float] = {
    29: -1.0,  # room L — left (falls back to centre mono when ch30 absent)
    30:  1.0,  # room R — right
    31: -1.0,  # board L — left
    32:  1.0,  # board R — right
}

# Per-channel OTT knobs  (applied to every channel before mixing)
OTT_CH_DEPTH:       float = 100.0  # overall effect amount      (0–100)
OTT_CH_UPWD_STRGTH: float = 100.0  # upward compression         (0–200)
OTT_CH_DNWD_STRGTH: float = 100.0  # downward compression       (0–200)
OTT_CH_THRESH_L:    float = 100.0  # low-band threshold         (0–200)
OTT_CH_THRESH_M:    float = 100.0  # mid-band threshold         (0–200)
OTT_CH_THRESH_H:    float = 100.0  # high-band threshold        (0–200)
OTT_CH_GAIN_L_DB:   float = 0.0  # low-band output level      (-inf–+6 dB)
OTT_CH_GAIN_M_DB:   float = 0.0  # mid-band output level      (-inf–+6 dB)
OTT_CH_GAIN_H_DB:   float = 0.0  # high-band output level     (-inf–+6 dB)

# Master bus OTT knobs  (applied to the stereo mix after channel summing)
OTT_MASTER_DEPTH:       float = 100.0  # overall effect amount  (0–100)
OTT_MASTER_IN_GAIN_DB:  float = -10.0  # input gain             (-54–+19 dB)
OTT_MASTER_OUT_GAIN_DB: float =   2.0  # output gain            (-54–+19 dB)
# upward compression     (0–200); 0 = downward only
OTT_MASTER_UPWD_STRGTH: float = 100.0
OTT_MASTER_DNWD_STRGTH: float = 100.0  # downward compression   (0–200)
OTT_MASTER_THRESH_L:    float = 100.0  # low-band threshold     (0–200)
OTT_MASTER_THRESH_M:    float = 100.0  # mid-band threshold     (0–200)
OTT_MASTER_THRESH_H:    float = 100.0  # high-band threshold    (0–200)
OTT_MASTER_GAIN_L_DB:   float = 0.0  # low-band output level  (-inf–+6 dB)
OTT_MASTER_GAIN_M_DB:   float = 0.0  # mid-band output level  (-inf–+6 dB)
OTT_MASTER_GAIN_H_DB:   float = -1.0  # high-band output level (-inf–+6 dB)

# Room channels (29=L, 30=R) — dominant in mix, maximum squish
# Up/down at 200 (max) for hard compression in both directions.
# L pulled back to cede bass to board; M/H boosted for room dominance.
ROOM_CHANNELS:           list[int] = [29, 30]
OTT_ROOM_UPWD_STRGTH:    float = 200.0   # max upward compression   (0–200)
OTT_ROOM_DNWD_STRGTH:    float = 200.0   # max downward compression (0–200)
OTT_ROOM_GAIN_L_DB:      float = -3.0    # pull back bass (board owns ~70%)
OTT_ROOM_GAIN_M_DB:      float =  4.0    # push presence / vocals
OTT_ROOM_GAIN_H_DB:      float =  3.0    # push air

# Board channels (31=L, 32=R) — bass-forward, vocal-reduced
# Softer downward compression preserves bass transient punch.
# L=+4 vs room L=−3 → ~69% bass contribution; M=−5 → ~25% vocal.
BOARD_CHANNELS:          list[int] = [31, 32]
OTT_BOARD_UPWD_STRGTH:   float = 100.0   # normal upward compression
OTT_BOARD_DNWD_STRGTH:   float =  80.0   # softer downward (preserve bass punch)
OTT_BOARD_GAIN_L_DB:     float =  4.0    # heavy bass boost
OTT_BOARD_GAIN_M_DB:     float = -5.0    # cut mids / vocals
OTT_BOARD_GAIN_H_DB:     float = -4.0    # cut highs


OTT_PATHS: dict[str, list[str]] = {
    'darwin': [
        '/Library/Audio/Plug-Ins/VST3/OTT.vst3',
        '/Library/Audio/Plug-Ins/VST3/Xfer Records/OTT.vst3',
        '/Library/Audio/Plug-Ins/Components/OTT.component',
    ],
    'windows': [
        r'C:\Program Files\Common Files\VST3\Xfer Records\OTT.vst3',
        r'C:\Program Files\Common Files\VST3\OTT.vst3',
        r'C:\Program Files\VSTPlugins\OTT.vst3',
    ],
}

# Maximum lag searched during cross-correlation alignment (ms)
ALIGN_MAX_LAG_MS:     float = 100.0
# Reference channel for alignment — all others are shifted to match this one
ALIGN_REF_CHANNEL:    int = 31

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel discovery
# ---------------------------------------------------------------------------

def find_channels(wav_files: list[Path]) -> dict[int, Path]:
    """Return {channel_number: path} for all files matching 'chan<N>' in their name."""
    result: dict[int, Path] = {}
    for f in wav_files:
        m = re.search(r'chan(\d+)', f.name, re.IGNORECASE)
        if m:
            result[int(m.group(1))] = f
    _log.debug(f"MASTER  found channels {sorted(result.keys())}")
    return result


# ---------------------------------------------------------------------------
# Audio I/O helpers
# ---------------------------------------------------------------------------

def _load_mono(path: Path, start_sec: float = 0.0, duration_sec: float | None = None) -> tuple[np.ndarray, int]:
    """Load a mono WAV file. Returns (float32 array shape N, sample_rate).

    start_sec / duration_sec clip the read window; both default to full file.
    Uses soundfile (libsndfile) which is thread-safe and handles 24-bit PCM.
    """
    import soundfile as sf
    info = sf.info(str(path))
    sr = info.samplerate
    start_frame = int(start_sec * sr) if start_sec > 0.0 else 0
    if duration_sec is not None:
        stop_frame = start_frame + int(duration_sec * sr)
    else:
        stop_frame = -1
    audio, _ = sf.read(str(path), start=start_frame, stop=stop_frame if stop_frame > 0 else None,
                       dtype='float32', always_2d=False)
    return audio, sr


def _write_stereo(path: Path, left: np.ndarray, right: np.ndarray, sr: int) -> None:
    """Write a stereo 32-bit float WAV file."""
    import soundfile as sf
    stereo = np.stack(
        [left.astype(np.float32), right.astype(np.float32)], axis=1)
    sf.write(str(path), stereo, sr, subtype='FLOAT')


def _write_stereo_mp3(
    path: Path,
    left: np.ndarray,
    right: np.ndarray,
    sr: int,
    script_runner=None,  # ScriptRunner | None
) -> None:
    """Write a stereo MP3 via a temporary WAV → ffmpeg transcode.

    Uses 128 kbps CBR (libmp3lame).  Adequate quality for live concert
    recordings and ~22× smaller than 32-bit float WAV at 44.1 kHz.

    When *script_runner* is provided the transcode runs through
    ``scripts/transcode_mp3.py`` instead of an inline subprocess call.
    """
    import soundfile as sf
    tmp = path.with_suffix('.tmp.wav')
    try:
        stereo = np.stack(
            [left.astype(np.float32), right.astype(np.float32)], axis=1)
        sf.write(str(tmp), stereo, sr, subtype='FLOAT')

        if script_runner is not None:
            from nofun.script_runner import ScriptJob
            job = ScriptJob(
                script='transcode_mp3',
                args={'source': str(tmp), 'dest': str(path), 'bitrate': '128k'},
                label=f'MP3  {path.name}',
            )
            result = script_runner.run(job)
            if result.exit_code != 0:
                raise RuntimeError(
                    f'transcode_mp3 script failed (exit {result.exit_code})'
                )
        else:
            subprocess.run(
                ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                 '-i', str(tmp),
                 '-c:a', 'libmp3lame', '-b:a', '128k', '-q:a', '2',
                 str(path)],
                check=True,
            )
    finally:
        tmp.unlink(missing_ok=True)


def _pan_weights(pan: float) -> tuple[float, float]:
    """Equal-power pan. pan=-1 full L, 0 centre, +1 full R. Returns (L, R) weights."""
    angle = (pan + 1) / 2 * (math.pi / 2)
    return math.cos(angle), math.sin(angle)


# ---------------------------------------------------------------------------
# Processor A: ffmpeg 3-band approximation
# ---------------------------------------------------------------------------

_APPROX_MONO_FILTER = (
    '[0:a]asplit=3[lo][mid][hi];'
    '[lo]lowpass=f=250[lo_f];'
    '[mid]bandpass=f=1000:width_type=h:w=1800[mid_f];'
    '[hi]highpass=f=2500[hi_f];'
    '[lo_f]acompressor=threshold=0.05:ratio=6:attack=2:release=80:makeup=2[lo_c];'
    '[mid_f]acompressor=threshold=0.05:ratio=6:attack=2:release=80:makeup=2[mid_c];'
    '[hi_f]acompressor=threshold=0.05:ratio=6:attack=2:release=80:makeup=2[hi_c];'
    '[lo_c][mid_c][hi_c]amix=3:normalize=0,'
    'acompressor=threshold=0.1:ratio=4:attack=1:release=50:makeup=2'
)

_APPROX_STEREO_FILTER = (
    'acompressor=threshold=0.1:ratio=4:attack=1:release=50:makeup=2'
)


def _ffmpeg_pipe(raw_in: bytes, sr: int, in_ch: int, filter_str: str, out_ch: int) -> bytes:
    """Pipe raw f32le bytes through an ffmpeg filter graph. Returns raw f32le bytes."""
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'f32le', '-ar', str(sr), '-ac', str(in_ch), '-i', 'pipe:0',
        '-filter_complex', filter_str,
        '-f', 'f32le', '-ar', str(sr), '-ac', str(out_ch), 'pipe:1',
    ]
    result = subprocess.run(cmd, input=raw_in, capture_output=True)
    return result.stdout


def make_approx_processor(sr: int) -> Callable[[np.ndarray], np.ndarray]:
    """Return a mono→mono ffmpeg 3-band compressor processor."""
    def process(mono: np.ndarray) -> np.ndarray:
        raw_in = mono.astype(np.float32).tobytes()
        raw_out = _ffmpeg_pipe(raw_in, sr, 1, _APPROX_MONO_FILTER, 1)
        out = np.frombuffer(raw_out, dtype=np.float32)
        N = len(mono)
        if len(out) < N:
            out = np.pad(out, (0, N - len(out)))
        return out[:N]
    return process


def make_approx_master_processor(sr: int) -> Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """Return a stereo→stereo ffmpeg master compressor processor."""
    def process(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        N = len(left)
        # Interleave L/R into stereo f32le
        stereo = np.empty(N * 2, dtype=np.float32)
        stereo[0::2] = left.astype(np.float32)
        stereo[1::2] = right.astype(np.float32)
        raw_in = stereo.tobytes()
        raw_out = _ffmpeg_pipe(raw_in, sr, 2, _APPROX_STEREO_FILTER, 2)
        out = np.frombuffer(raw_out, dtype=np.float32)
        # Deinterleave — pad/trim defensively
        out_len = (min(len(out), N * 2) // 2) * 2
        out_l = out[0:out_len:2]
        out_r = out[1:out_len:2]
        if len(out_l) < N:
            out_l = np.pad(out_l, (0, N - len(out_l)))
            out_r = np.pad(out_r, (0, N - len(out_r)))
        return out_l[:N], out_r[:N]
    return process


# ---------------------------------------------------------------------------
# Processor B: real OTT via pedalboard
# ---------------------------------------------------------------------------

# Module-level cache: None = not yet attempted; False = failed/unavailable; plugin otherwise.
_ott_plugin_cache: Any = None
_ott_plugin_attempted: bool = False


def _load_ott_plugin(logger: logging.Logger) -> Any | None:
    """Try to load the OTT VST3/AU plugin. Returns the plugin or None.

    Result is cached for the lifetime of the process — the plugin is only
    loaded once regardless of how many processor factories call this.

    AU plugins on macOS require the main AppKit runloop and will deadlock
    when loaded from a background thread. We detect that case and skip OTT,
    falling back to the approx processor instead.
    """
    global _ott_plugin_cache, _ott_plugin_attempted

    if _ott_plugin_attempted:
        return _ott_plugin_cache  # None if previously failed

    _ott_plugin_attempted = True

    # macOS Audio Units require the main AppKit runloop — skip in background threads.
    # On Windows, COM is initialised by the caller before entering this function.
    if is_darwin():
        import threading
        if threading.current_thread() is not threading.main_thread():
            logger.debug('LOAD    OTT  skipped (macOS AU restriction)')
            _ott_plugin_cache = None
            return None

    try:
        import pedalboard
    except ImportError:
        logger.warning('LOAD    OTT  pedalboard missing — approx only')
        _ott_plugin_cache = None
        return None

    paths = OTT_PATHS.get(detect_platform()) or OTT_PATHS.get('darwin', [])
    for p in paths:
        if not Path(p).exists():
            continue
        try:
            import concurrent.futures as _cf
            _OTT_TIMEOUT = 5  # seconds; hung if exceeded
            for _attempt in (1, 2):
                label = f'LOAD    OTT  {p}' + ('  (retry)' if _attempt == 2 else '')
                logger.info(label, extra={'tui': False})
                # Use an explicit executor (not a context manager) so that
                # shutdown(wait=False) can abandon a hung thread rather than
                # blocking in __exit__ until it finishes.
                _exe = _cf.ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix='ott_load')
                _fut = _exe.submit(pedalboard.load_plugin, p)
                try:
                    plugin = _fut.result(timeout=_OTT_TIMEOUT)
                except _cf.TimeoutError:
                    _exe.shutdown(wait=False)   # abandon; daemon thread dies with process
                    if _attempt == 1:
                        logger.warning(
                            f'LOAD    OTT  timed out ({_OTT_TIMEOUT}s) — retrying')
                        continue
                    logger.warning(
                        f'LOAD    OTT  timed out ({_OTT_TIMEOUT}s) on retry — using approx')
                    _ott_plugin_cache = None
                    return None
                _exe.shutdown(wait=False)
                logger.info('LOAD    OTT  ok', extra={'tui': False})
                _ott_plugin_cache = plugin
                return plugin
        except Exception as e:
            logger.warning(f'LOAD    OTT  failed from {p}: {e}')
    logger.warning('LOAD    OTT  not found — using approx')
    _ott_plugin_cache = None
    return None


def make_ott_processor(sr: int, logger: logging.Logger) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a mono→mono OTT processor, or None if OTT is unavailable."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])

    def process(mono: np.ndarray) -> np.ndarray:
        # Re-apply settings each call: all factories share one cached plugin instance,
        # so settings must be set immediately before use, not just at factory time.
        plugin.depth = OTT_CH_DEPTH
        plugin.upwd_strgth = OTT_CH_UPWD_STRGTH
        plugin.dnwd_strgth = OTT_CH_DNWD_STRGTH
        plugin.thresh_l = OTT_CH_THRESH_L
        plugin.thresh_m = OTT_CH_THRESH_M
        plugin.thresh_h = OTT_CH_THRESH_H
        plugin.gain_l_db = OTT_CH_GAIN_L_DB
        plugin.gain_m_db = OTT_CH_GAIN_M_DB
        plugin.gain_h_db = OTT_CH_GAIN_H_DB
        stereo_in = np.stack([mono.astype(np.float32), mono.astype(np.float32)])
        out = board(stereo_in, sample_rate=sr)
        return ((out[0] + out[1]) / 2).astype(np.float32)
    return process


def make_ott_board_processor(sr: int, logger: logging.Logger) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a mono→mono OTT processor for board channels (31/32) with band-gain overrides."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])

    def process(mono: np.ndarray) -> np.ndarray:
        plugin.depth = OTT_CH_DEPTH
        plugin.upwd_strgth = OTT_BOARD_UPWD_STRGTH
        plugin.dnwd_strgth = OTT_BOARD_DNWD_STRGTH
        plugin.thresh_l = OTT_CH_THRESH_L
        plugin.thresh_m = OTT_CH_THRESH_M
        plugin.thresh_h = OTT_CH_THRESH_H
        plugin.gain_l_db = OTT_BOARD_GAIN_L_DB
        plugin.gain_m_db = OTT_BOARD_GAIN_M_DB
        plugin.gain_h_db = OTT_BOARD_GAIN_H_DB
        stereo_in = np.stack([mono.astype(np.float32), mono.astype(np.float32)])
        out = board(stereo_in, sample_rate=sr)
        return ((out[0] + out[1]) / 2).astype(np.float32)
    return process


def make_ott_room_processor(sr: int, logger: logging.Logger) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a mono→mono OTT processor for room channels (29/30) with band-gain overrides."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])

    def process(mono: np.ndarray) -> np.ndarray:
        plugin.depth = OTT_CH_DEPTH
        plugin.upwd_strgth = OTT_ROOM_UPWD_STRGTH
        plugin.dnwd_strgth = OTT_ROOM_DNWD_STRGTH
        plugin.thresh_l = OTT_CH_THRESH_L
        plugin.thresh_m = OTT_CH_THRESH_M
        plugin.thresh_h = OTT_CH_THRESH_H
        plugin.gain_l_db = OTT_ROOM_GAIN_L_DB
        plugin.gain_m_db = OTT_ROOM_GAIN_M_DB
        plugin.gain_h_db = OTT_ROOM_GAIN_H_DB
        stereo_in = np.stack([mono.astype(np.float32), mono.astype(np.float32)])
        out = board(stereo_in, sample_rate=sr)
        return ((out[0] + out[1]) / 2).astype(np.float32)
    return process


def make_ott_custom_processor(
    sr: int, logger: logging.Logger,
    gain_l_db: float, gain_m_db: float, gain_h_db: float,
) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a mono→mono OTT processor with fully custom band gains (for ad-hoc channels)."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])
    _gl, _gm, _gh = gain_l_db, gain_m_db, gain_h_db

    def process(mono: np.ndarray) -> np.ndarray:
        plugin.depth = OTT_CH_DEPTH
        plugin.upwd_strgth = OTT_CH_UPWD_STRGTH
        plugin.dnwd_strgth = OTT_CH_DNWD_STRGTH
        plugin.thresh_l = OTT_CH_THRESH_L
        plugin.thresh_m = OTT_CH_THRESH_M
        plugin.thresh_h = OTT_CH_THRESH_H
        plugin.gain_l_db = _gl
        plugin.gain_m_db = _gm
        plugin.gain_h_db = _gh
        stereo_in = np.stack([mono.astype(np.float32), mono.astype(np.float32)])
        out = board(stereo_in, sample_rate=sr)
        return ((out[0] + out[1]) / 2).astype(np.float32)
    return process


def make_ott_master_processor(sr: int, logger: logging.Logger) -> Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]] | None:
    """Return a stereo→stereo OTT master processor, or None if OTT is unavailable."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])

    def process(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        plugin.depth = OTT_MASTER_DEPTH
        plugin.in_gain_db = OTT_MASTER_IN_GAIN_DB
        plugin.out_gain_db = OTT_MASTER_OUT_GAIN_DB
        plugin.upwd_strgth = OTT_MASTER_UPWD_STRGTH
        plugin.dnwd_strgth = OTT_MASTER_DNWD_STRGTH
        plugin.thresh_l = OTT_MASTER_THRESH_L
        plugin.thresh_m = OTT_MASTER_THRESH_M
        plugin.thresh_h = OTT_MASTER_THRESH_H
        plugin.gain_l_db = OTT_MASTER_GAIN_L_DB
        plugin.gain_m_db = OTT_MASTER_GAIN_M_DB
        plugin.gain_h_db = OTT_MASTER_GAIN_H_DB
        stereo_in = np.stack([left.astype(np.float32), right.astype(np.float32)])
        out = board(stereo_in, sample_rate=sr)
        return out[0].astype(np.float32), out[1].astype(np.float32)
    return process


# ---------------------------------------------------------------------------
# Channel alignment
# ---------------------------------------------------------------------------

def _xcorr_lag(r: np.ndarray, s: np.ndarray, max_lag: int) -> int | None:
    """FFT cross-correlation on one pair of chunks. Returns None if either is silent.

    Positive return = sig lags ref (sig gets trimmed).
    """
    r = r.astype(np.float64)
    s = s.astype(np.float64)
    r -= r.mean()
    s -= s.mean()
    std_r, std_s = r.std(), s.std()
    if std_r < 1e-6 or std_s < 1e-6:
        return None
    r /= std_r
    s /= std_s
    # C[k] = Σ_t r[t]*s[t-k]; peak at k>0 means sig leads ref by k samples.
    # Negate so callers receive positive = sig lags ref (will be trimmed).
    n = len(r) + len(s) - 1
    n_fft = 1 << (n - 1).bit_length()
    C = np.fft.irfft(np.fft.rfft(r, n_fft) *
                     np.conj(np.fft.rfft(s, n_fft)), n_fft)
    neg_idx = n_fft - np.arange(max_lag, 0, -1)
    pos_idx = np.arange(0, max_lag + 1)
    all_idx = np.concatenate([neg_idx, pos_idx])
    all_lags = np.arange(-max_lag, max_lag + 1)
    return -int(all_lags[np.argmax(C[all_idx])])


_ALIGN_WINDOW_S = 10   # seconds per probe window


def _find_lag_samples(ref: np.ndarray, sig: np.ndarray, max_lag: int, sr: int = 48000) -> int:
    """Return lag in samples for sig relative to ref (positive = sig lags ref, will be trimmed).

    Probes one window per minute of audio and returns the median, making the
    estimate robust to silence or noise in any single section.
    Returns 0 if no valid windows are found.
    """
    length = min(len(ref), len(sig))
    win_size = min(int(_ALIGN_WINDOW_S * sr), length)
    duration_m = max(1, int(round(length / sr / 60)))
    lags: list[int] = []

    for i in range(duration_m):
        centre = int(length * (i + 0.5) / duration_m)
        start = max(0, centre - win_size // 2)
        end = min(length, start + win_size)
        lag = _xcorr_lag(ref[start:end], sig[start:end], max_lag)
        if lag is not None:
            lags.append(lag)

    if not lags:
        return 0
    return int(np.median(lags))


# ---------------------------------------------------------------------------
# Mixing engine
# ---------------------------------------------------------------------------

def _mix_channels(
    channels: dict[int, Path],
    channel_list: list[int],
    pan_map: dict[int, float],
    channel_processor: Callable[[np.ndarray], np.ndarray],
    sr: int,
    logger: logging.Logger,
    start_sec: float = 0.0,
    duration_sec: float | None = None,
    ch_processor_map: dict[int, Callable[[
        np.ndarray], np.ndarray]] | None = None,
    align: bool = True,
    manual_offsets: dict[int, int] | None = None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Load, process, pan and sum channels into a stereo (L, R) pair.

    ch_processor_map: per-channel OTT processor overrides.
    align: cross-correlate channels and trim for hardware timing offsets.
    manual_offsets: {ch: samples_ahead_of_ref} — bypasses auto-align when provided.
    """
    available = [ch for ch in channel_list if ch in channels]
    missing = [ch for ch in channel_list if ch not in channels]
    if missing:
        logger.debug(f'MASTER  skipping channels: {missing}')
    if not available:
        logger.warning('MASTER  no available channels — skipping')
        return None

    # Load all channels up front so we can align before processing
    logger.info(
        f'MASTER  reading {len(available)} channel(s)', extra={'tui': False})
    raws: dict[int, np.ndarray] = {}
    for ch in available:
        mono, _ = _load_mono(channels[ch], start_sec, duration_sec)
        raws[ch] = mono

    # Manual or auto alignment
    if manual_offsets is not None and len(available) > 1:
        leads: dict[int, int] = {
            ch: manual_offsets.get(ch, 0) for ch in available}
        min_lead = min(leads.values())
        trims = {ch: leads[ch] - min_lead for ch in available}
        max_trim = max(trims.values())
        if max_trim > 0:
            for ch in available:
                raws[ch] = raws[ch][trims[ch]:]
            lag_ms = {ch: leads[ch] / sr * 1000 for ch in available}
            logger.debug(
                'ALIGN   manual offsets (ms): '
                + ', '.join(f'ch{ch}:{lag_ms[ch]:+.1f}' for ch in available)
            )

    elif align and len(available) > 1:
        logger.info(f'ALIGN   {len(available)} channel(s)',
                    extra={'tui': False})
        max_lag = int(ALIGN_MAX_LAG_MS / 1000.0 * sr)
        ref_ch = ALIGN_REF_CHANNEL if ALIGN_REF_CHANNEL in raws else available[0]
        leads: dict[int, int] = {ref_ch: 0}
        for ch in available:
            if ch == ref_ch:
                continue
            leads[ch] = _find_lag_samples(raws[ref_ch], raws[ch], max_lag, sr)
        min_lead = min(leads.values())
        trims = {ch: leads[ch] - min_lead for ch in available}
        max_trim = max(trims.values())
        if max_trim > 0:
            for ch in available:
                t = trims[ch]
                raws[ch] = raws[ch][t:]
            lag_ms = {ch: leads[ch] / sr * 1000 for ch in available}
            logger.debug(
                'ALIGN   lags — '
                + ', '.join(f'ch{ch}:{lag_ms[ch]:+.1f}ms' for ch in available)
            )

    N = min(len(v) for v in raws.values())
    left = np.zeros(N, dtype=np.float32)
    right = np.zeros(N, dtype=np.float32)

    logger.info(f'MASTER  processing {len(available)} ch  ({N / sr / 60:.1f} min)',
                extra={'tui': False})
    for ch in available:
        mono = raws[ch][:N]
        if len(mono) < N:
            mono = np.pad(mono, (0, N - len(mono)))

        proc = (ch_processor_map.get(ch)
                if ch_processor_map else None) or channel_processor
        processed = proc(mono)
        l_w, r_w = _pan_weights(pan_map.get(ch, 0.0))
        left += processed * l_w
        right += processed * r_w

    return left, right


# ---------------------------------------------------------------------------
# Settings slug
# ---------------------------------------------------------------------------

def _settings_slug() -> str:
    """Encode non-default OTT constants into a compact filename segment.

    Only values that differ from their default are included, keeping
    filenames short when settings are mostly untouched.
    """
    parts: list[str] = []

    def _g(val: float) -> str:
        v = round(val, 1)
        return f'+{v:g}' if v > 0 else f'{v:g}'

    def _i(val: float) -> str:
        return str(int(round(val)))

    # Channel band gains (default 0 — skip zeros)
    if OTT_CH_GAIN_L_DB:
        parts.append(f'CL{_g(OTT_CH_GAIN_L_DB)}')
    if OTT_CH_GAIN_M_DB:
        parts.append(f'CM{_g(OTT_CH_GAIN_M_DB)}')
    if OTT_CH_GAIN_H_DB:
        parts.append(f'CH{_g(OTT_CH_GAIN_H_DB)}')
    # Room channel overrides (skip if same as regular channel gains)
    if OTT_ROOM_GAIN_L_DB != OTT_CH_GAIN_L_DB:
        parts.append(f'RL{_g(OTT_ROOM_GAIN_L_DB)}')
    if OTT_ROOM_GAIN_M_DB != OTT_CH_GAIN_M_DB:
        parts.append(f'RM{_g(OTT_ROOM_GAIN_M_DB)}')
    if OTT_ROOM_GAIN_H_DB != OTT_CH_GAIN_H_DB:
        parts.append(f'RH{_g(OTT_ROOM_GAIN_H_DB)}')
    # Board channel overrides (skip if same as regular channel gains)
    if OTT_BOARD_GAIN_L_DB != OTT_CH_GAIN_L_DB:
        parts.append(f'BL{_g(OTT_BOARD_GAIN_L_DB)}')
    if OTT_BOARD_GAIN_M_DB != OTT_CH_GAIN_M_DB:
        parts.append(f'BM{_g(OTT_BOARD_GAIN_M_DB)}')
    if OTT_BOARD_GAIN_H_DB != OTT_CH_GAIN_H_DB:
        parts.append(f'BH{_g(OTT_BOARD_GAIN_H_DB)}')
    # Channel thresholds (default 100 — skip)
    if OTT_CH_THRESH_L != 100:
        parts.append(f'CTL{_i(OTT_CH_THRESH_L)}')
    if OTT_CH_THRESH_M != 100:
        parts.append(f'CTM{_i(OTT_CH_THRESH_M)}')
    if OTT_CH_THRESH_H != 100:
        parts.append(f'CTH{_i(OTT_CH_THRESH_H)}')
    # Channel compression strengths (defaults: upwd=100, dnwd=100)
    if OTT_CH_UPWD_STRGTH != 100:
        parts.append(f'CUP{_i(OTT_CH_UPWD_STRGTH)}')
    if OTT_CH_DNWD_STRGTH != 100:
        parts.append(f'CDN{_i(OTT_CH_DNWD_STRGTH)}')

    # Master band gains (default 0 — skip zeros)
    if OTT_MASTER_GAIN_L_DB:
        parts.append(f'ML{_g(OTT_MASTER_GAIN_L_DB)}')
    if OTT_MASTER_GAIN_M_DB:
        parts.append(f'MM{_g(OTT_MASTER_GAIN_M_DB)}')
    if OTT_MASTER_GAIN_H_DB:
        parts.append(f'MH{_g(OTT_MASTER_GAIN_H_DB)}')
    # Master thresholds (default 100 — skip)
    if OTT_MASTER_THRESH_L != 100:
        parts.append(f'MTL{_i(OTT_MASTER_THRESH_L)}')
    if OTT_MASTER_THRESH_M != 100:
        parts.append(f'MTM{_i(OTT_MASTER_THRESH_M)}')
    if OTT_MASTER_THRESH_H != 100:
        parts.append(f'MTH{_i(OTT_MASTER_THRESH_H)}')
    # Master dynamics (defaults: depth=100, upwd=0, dnwd=100, in=-15, out=3)
    if OTT_MASTER_DEPTH != 100:
        parts.append(f'DEP{_i(OTT_MASTER_DEPTH)}')
    if OTT_MASTER_UPWD_STRGTH != 0:
        parts.append(f'MUP{_i(OTT_MASTER_UPWD_STRGTH)}')
    if OTT_MASTER_DNWD_STRGTH != 100:
        parts.append(f'MDN{_i(OTT_MASTER_DNWD_STRGTH)}')
    if OTT_MASTER_IN_GAIN_DB != -15:
        parts.append(f'IGN{_g(OTT_MASTER_IN_GAIN_DB)}')
    if OTT_MASTER_OUT_GAIN_DB != 3:
        parts.append(f'OGN{_g(OTT_MASTER_OUT_GAIN_DB)}')

    return '_'.join(parts) if parts else 'flat'


def _verbose_slug() -> str:
    """Return a slug of every knob that differs from OTT's factory defaults.

    Omits knobs still at OTT stock values; always includes knobs the script
    has intentionally changed (even if not overridden on the CLI this run).
    """
    # OTT Xfer factory defaults
    _F_GAIN = 0.0    # gain_l/m/h_db
    _F_DEPTH = 100.0
    _F_UPWD = 100.0
    _F_DNWD = 100.0
    _F_THRESH = 100.0
    _F_IN_GAIN = 0.0
    _F_OUT_GAIN = 0.0

    def _g(val: float) -> str:
        v = round(val, 1)
        return f'+{v:g}' if v > 0 else f'{v:g}'

    def _i(val: float) -> str:
        return str(int(round(val)))

    parts: list[str] = []
    # Channel gains
    if OTT_CH_GAIN_L_DB != _F_GAIN:
        parts.append(f'CL{_g(OTT_CH_GAIN_L_DB)}')
    if OTT_CH_GAIN_M_DB != _F_GAIN:
        parts.append(f'CM{_g(OTT_CH_GAIN_M_DB)}')
    if OTT_CH_GAIN_H_DB != _F_GAIN:
        parts.append(f'CH{_g(OTT_CH_GAIN_H_DB)}')
    # Room channel gains
    if OTT_ROOM_GAIN_L_DB != _F_GAIN:
        parts.append(f'RL{_g(OTT_ROOM_GAIN_L_DB)}')
    if OTT_ROOM_GAIN_M_DB != _F_GAIN:
        parts.append(f'RM{_g(OTT_ROOM_GAIN_M_DB)}')
    if OTT_ROOM_GAIN_H_DB != _F_GAIN:
        parts.append(f'RH{_g(OTT_ROOM_GAIN_H_DB)}')
    # Board channel gains
    if OTT_BOARD_GAIN_L_DB != _F_GAIN:
        parts.append(f'BL{_g(OTT_BOARD_GAIN_L_DB)}')
    if OTT_BOARD_GAIN_M_DB != _F_GAIN:
        parts.append(f'BM{_g(OTT_BOARD_GAIN_M_DB)}')
    if OTT_BOARD_GAIN_H_DB != _F_GAIN:
        parts.append(f'BH{_g(OTT_BOARD_GAIN_H_DB)}')
    # Channel dynamics
    if OTT_CH_UPWD_STRGTH != _F_UPWD:
        parts.append(f'CUP{_i(OTT_CH_UPWD_STRGTH)}')
    if OTT_CH_DNWD_STRGTH != _F_DNWD:
        parts.append(f'CDN{_i(OTT_CH_DNWD_STRGTH)}')
    if OTT_CH_THRESH_L != _F_THRESH:
        parts.append(f'CTL{_i(OTT_CH_THRESH_L)}')
    if OTT_CH_THRESH_M != _F_THRESH:
        parts.append(f'CTM{_i(OTT_CH_THRESH_M)}')
    if OTT_CH_THRESH_H != _F_THRESH:
        parts.append(f'CTH{_i(OTT_CH_THRESH_H)}')
    if OTT_CH_DEPTH != _F_DEPTH:
        parts.append(f'DEP{_i(OTT_CH_DEPTH)}')
    # Master gains
    if OTT_MASTER_GAIN_L_DB != _F_GAIN:
        parts.append(f'ML{_g(OTT_MASTER_GAIN_L_DB)}')
    if OTT_MASTER_GAIN_M_DB != _F_GAIN:
        parts.append(f'MM{_g(OTT_MASTER_GAIN_M_DB)}')
    if OTT_MASTER_GAIN_H_DB != _F_GAIN:
        parts.append(f'MH{_g(OTT_MASTER_GAIN_H_DB)}')
    # Master dynamics
    if OTT_MASTER_UPWD_STRGTH != _F_UPWD:
        parts.append(f'MUP{_i(OTT_MASTER_UPWD_STRGTH)}')
    if OTT_MASTER_DNWD_STRGTH != _F_DNWD:
        parts.append(f'MDN{_i(OTT_MASTER_DNWD_STRGTH)}')
    if OTT_MASTER_IN_GAIN_DB != _F_IN_GAIN:
        parts.append(f'IGN{_g(OTT_MASTER_IN_GAIN_DB)}')
    if OTT_MASTER_OUT_GAIN_DB != _F_OUT_GAIN:
        parts.append(f'OGN{_g(OTT_MASTER_OUT_GAIN_DB)}')
    if OTT_MASTER_THRESH_L != _F_THRESH:
        parts.append(f'MTL{_i(OTT_MASTER_THRESH_L)}')
    if OTT_MASTER_THRESH_M != _F_THRESH:
        parts.append(f'MTM{_i(OTT_MASTER_THRESH_M)}')
    if OTT_MASTER_THRESH_H != _F_THRESH:
        parts.append(f'MTH{_i(OTT_MASTER_THRESH_H)}')
    if OTT_MASTER_DEPTH != _F_DEPTH:
        parts.append(f'MDEP{_i(OTT_MASTER_DEPTH)}')

    return '_'.join(parts) if parts else 'ott_stock'


def _explain_slug(slug: str) -> list[str]:
    """Return a list of human-readable lines explaining each token in a settings slug."""
    import re as _re
    _DESCRIPTIONS: dict[str, str] = {
        'CL':   'all-channel low band gain (< 88 Hz)',
        'CM':   'all-channel mid band gain (88 Hz – 2.8 kHz)',
        'CH':   'all-channel high band gain (> 2.8 kHz)',
        'RL':   'room channels 29/30 low band gain (< 88 Hz)',
        'RM':   'room channels 29/30 mid band gain (88 Hz – 2.8 kHz)',
        'RH':   'room channels 29/30 high band gain (> 2.8 kHz)',
        'BL':   'board channels 31/32 low band gain (< 88 Hz)',
        'BM':   'board channels 31/32 mid band gain (88 Hz – 2.8 kHz)',
        'BH':   'board channels 31/32 high band gain (> 2.8 kHz)',
        'ML':   'master bus low band gain (< 88 Hz)',
        'MM':   'master bus mid band gain (88 Hz – 2.8 kHz)',
        'MH':   'master bus high band gain (> 2.8 kHz)',
        'CUP':  'channel upward compression strength (OTT default 100)',
        'CDN':  'channel downward compression strength (OTT default 100)',
        'CTL':  'channel low band threshold (OTT default 100)',
        'CTM':  'channel mid band threshold (OTT default 100)',
        'CTH':  'channel high band threshold (OTT default 100)',
        'DEP':  'channel OTT depth / overall effect amount (OTT default 100)',
        'MUP':  'master upward compression strength (OTT default 100; 0 = downward only)',
        'MDN':  'master downward compression strength (OTT default 100)',
        'MTL':  'master low band threshold (OTT default 100)',
        'MTM':  'master mid band threshold (OTT default 100)',
        'MTH':  'master high band threshold (OTT default 100)',
        'IGN':  'master input gain dB (OTT default 0)',
        'OGN':  'master output gain dB (OTT default 0)',
        'MDEP': 'master OTT depth / overall effect amount (OTT default 100)',
    }
    lines: list[str] = []
    for token in slug.split('_'):
        m = _re.match(r'^([A-Z]+)([-+]?\d+(?:\.\d+)?)$', token)
        if not m:
            continue
        prefix, val = m.group(1), m.group(2)
        desc = _DESCRIPTIONS.get(prefix, f'unknown knob ({prefix})')
        unit = ' dB' if any(prefix.endswith(s)
                            for s in ('L', 'M', 'H', 'GN')) else ''
        lines.append(f'  {token:<12}  {desc}: {val}{unit}')
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_masters(
    wav_files: list[Path],
    base: str,
    audio_dest: Path,
    logger: logging.Logger | None = None,
    sr: int = 48000,
    clip: tuple[float, float] | None = None,
    selected_only: bool = False,
    settings_slug: str | None = None,
    extra_ch_gains: dict[int, tuple[float, float, float]] | None = None,
    align: bool = True,
    manual_offsets: dict[int, int] | None = None,
    slow: int = 1,
    slow_hq: int = 1,
    script_runner=None,  # ScriptRunner | None — passed to _write_stereo_mp3
) -> list[Path]:
    """Generate stereo WAV masters for one (date, band) performance group.

    Parameters
    ----------
    wav_files     : active (non-silent) channel WAVs for this group
    base          : performance key, e.g. '26-3-20_HORSE_GRAVE'
    audio_dest    : destination directory (same folder as the ZIP)
    logger        : logger instance; uses module logger if None
    sr            : sample rate (default 48000)
    clip          : optional (start_sec, end_sec) to render only a time window,
                    e.g. (1010.0, 1050.0) for 16:50–17:30. Output filenames get
                    a '_clip' suffix when this is set.
    selected_only : when True, only generate the selected-channel mix (OTT if
                    available, approx fallback). Skips the full-channel variants.
    settings_slug : when provided, appended to the output filename so settings
                    are encoded in the name. Pass _settings_slug() to auto-generate.

    Returns list of successfully written output paths.
    """
    log = logger or _log
    channels = find_channels(wav_files)
    if not channels:
        log.warning(f'MASTER  no channel WAVs in {base} — skipping')
        return []

    # Build selected channel list (SELECTED_CHANNELS + any extras)
    selected_ch_list = list(SELECTED_CHANNELS)
    if extra_ch_gains:
        for ch in extra_ch_gains:
            if ch not in selected_ch_list:
                selected_ch_list.append(ch)

    available_selected = [ch for ch in selected_ch_list if ch in channels]
    log.debug(
        f'MASTER  {len(channels)} channels found, using {available_selected}'
    )

    # Dynamic panning: room L (ch29) falls back to centre mono when room R (ch30) absent
    effective_pans = dict(SELECTED_PANS)
    if 29 in available_selected and 30 not in available_selected:
        effective_pans[29] = 0.0
    # Extra channels default to centre
    if extra_ch_gains:
        for ch in extra_ch_gains:
            effective_pans.setdefault(ch, 0.0)

    # Build processors
    approx_ch = make_approx_processor(sr)
    approx_master = make_approx_master_processor(sr)
    ott_ch = make_ott_processor(sr, log)
    ott_board_ch = make_ott_board_processor(sr, log)
    ott_room_ch = make_ott_room_processor(sr, log)
    ott_master = make_ott_master_processor(sr, log)

    # Combined per-channel processor map: room + board overrides + extra channels
    ch_map: dict[int, Callable] = {}
    if ott_room_ch is not None:
        ch_map.update({ch: ott_room_ch for ch in ROOM_CHANNELS})
    if ott_board_ch is not None:
        ch_map.update({ch: ott_board_ch for ch in BOARD_CHANNELS})
    if extra_ch_gains and ott_ch is not None:
        for ch, (gl, gm, gh) in extra_ch_gains.items():
            proc = make_ott_custom_processor(sr, log, gl, gm, gh)
            if proc is not None:
                ch_map[ch] = proc
    proc_map: dict[int, Callable] | None = ch_map or None

    # Auto-pan for full mix: spread all channels evenly L→R
    sorted_chs = sorted(channels.keys())
    if len(sorted_chs) > 1:
        pan_values = np.linspace(-1.0, 1.0, len(sorted_chs)).tolist()
    else:
        pan_values = [0.0]
    full_pan_map = dict(zip(sorted_chs, pan_values))

    if selected_only:
        if ott_ch is not None and ott_master is not None:
            combos: list[tuple[str, str, list[int], dict[int, float], Callable, Callable]] = [
                ('selected', 'ott', selected_ch_list,
                 effective_pans, ott_ch, ott_master),
            ]
        else:
            combos = [
                ('selected', 'approx', selected_ch_list,
                 effective_pans, approx_ch, approx_master),
            ]
    else:
        combos = [
            ('selected', 'approx', selected_ch_list,
             effective_pans, approx_ch, approx_master),
            ('full',     'approx', sorted_chs,
             full_pan_map,  approx_ch, approx_master),
        ]
        if ott_ch is not None and ott_master is not None:
            combos += [
                ('selected', 'ott', selected_ch_list,
                 effective_pans, ott_ch, ott_master),
                ('full',     'ott', sorted_chs,
                 full_pan_map,  ott_ch, ott_master),
            ]

    start_sec = clip[0] if clip else 0.0
    duration_sec = (clip[1] - clip[0]) if clip else None
    suffix = '_clip' if clip else ''

    audio_dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    import time as _time
    for mix_name, proc_name, ch_list, pan_map, ch_proc, master_proc in combos:
        label = 'AUDIO' if selected_only else f'master_{mix_name}_{proc_name}'
        if settings_slug:
            label = f'{label}_{settings_slug}'
        # AUDIO outputs are MP3 (128 kbps CBR); non-AUDIO masters stay as WAV.
        ext = '.mp3' if selected_only else '.wav'
        out_path = audio_dest / f'{base}_{label}{suffix}{ext}'
        try:
            log.info(
                f'MASTER  mixing  ({mix_name}/{proc_name})', extra={'tui': False})
            t0 = _time.monotonic()
            result = _mix_channels(channels, ch_list, pan_map, ch_proc, sr, log,
                                   start_sec=start_sec, duration_sec=duration_sec,
                                   ch_processor_map=proc_map if proc_name == 'ott' else None,
                                   align=align,
                                   manual_offsets=manual_offsets)
            if result is None:
                continue
            log.info('MASTER  applying bus processor', extra={'tui': False})
            left, right = master_proc(result[0], result[1])
            log.info(f'WRITE   {out_path.name}', extra={'tui': False})
            if slow_hq > 1:
                import pyrubberband as rb
                stereo = np.stack([left, right], axis=1)
                stretched = rb.time_stretch(stereo, sr, 1.0 / slow_hq)
                left, right = stretched[:, 0], stretched[:, 1]
                if ext == '.mp3':
                    _write_stereo_mp3(out_path, left, right, sr,
                                      script_runner=script_runner)
                else:
                    _write_stereo(out_path, left, right, sr)
            else:
                _sr = sr // max(1, slow)
                if ext == '.mp3':
                    _write_stereo_mp3(out_path, left, right, _sr,
                                      script_runner=script_runner)
                else:
                    _write_stereo(out_path, left, right, _sr)
            elapsed = _time.monotonic() - t0
            written.append(out_path)
            log.info(f'MASTER  {out_path.name}  ({elapsed:.0f}s)')
        except Exception as e:
            log.warning(f'MASTER  failed ({mix_name}/{proc_name}): {e}')

    return written


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import shutil
    import tempfile
    import zipfile as _zf

    parser = argparse.ArgumentParser(
        description=(
            'Generate stereo masters from WAV files.\n\n'
            'Single folder:  mastering.py <folder>\n'
            'Multi-band:     mastering.py <search_root> --select 26-05-07'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('folder', nargs='?', default=None,
                        help='WAV folder, or search root when --select is used')
    parser.add_argument('--select', '-s', default=None, metavar='STRING',
                        help='Search folder for ZIPs/subdirs whose names contain STRING '
                             'and process each match (e.g. --select 26-05-07)')
    parser.add_argument('--output', '-o', default=None,
                        help='Output directory (default: same as source)')
    parser.add_argument('--all', dest='all_combos', action='store_true',
                        help='Generate all 4 combos instead of just selected+OTT')
    parser.add_argument('--clip', nargs=2, type=float, metavar=('START', 'END'),
                        help='Render only this time window in seconds, e.g. --clip 60 120')
    parser.add_argument('--no-align', action='store_true',
                        help='Disable cross-correlation channel alignment')
    parser.add_argument('--align-ref', type=int, default=None, metavar='CH',
                        help=f'Reference channel for alignment (default {ALIGN_REF_CHANNEL})')
    parser.add_argument('--only-channels', default=None, metavar='N,N,...',
                        help='Comma-separated list of channels to include, e.g. 29,31')
    parser.add_argument('--pan', default=None, metavar='CH:V,...',
                        help='Override pan for channels, e.g. 29:-1,31:1')
    parser.add_argument('--slow', type=int, default=1, metavar='N',
                        help='Write output at 1/N sample rate so it plays back N× slower (no processing)')
    parser.add_argument('--slow-hq', type=int, default=1, metavar='N',
                        help='Like --slow but pitch-preserving via rubberband (better for hearing flamming)')
    parser.add_argument('--offset', action='append', default=[], metavar='CH:MS',
                        help='Manual offset: ch is this many ms ahead of ref (repeatable). '
                             'Bypasses auto-align. e.g. --offset 29:-27.3')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress DEBUG output (verbose is on by default)')
    parser.add_argument('--settings', action='store_true',
                        help='Append non-default OTT settings shorthand to output filenames')
    parser.add_argument('--verbose-filename', action='store_true',
                        help='Append every OTT knob (including defaults) to output filenames')
    parser.add_argument('--ott-params', action='store_true',
                        help='Print all knobs exposed by the OTT plugin and exit')
    parser.add_argument('--flat', action='store_true',
                        help='Reset all OTT gains to factory defaults (all band gains=0, in/out gain=0, upwd=100) before any other overrides')
    # Per-run gain overrides (override module-level constants for this invocation only)
    gains = parser.add_argument_group('gain overrides (dB)')
    gains.add_argument('--room-l', type=float, default=None, metavar='DB',
                       help=f'Room ch L gain / ch29+30 (default {OTT_ROOM_GAIN_L_DB:+g})')
    gains.add_argument('--room-m', type=float, default=None, metavar='DB',
                       help=f'Room ch M gain (default {OTT_ROOM_GAIN_M_DB:+g})')
    gains.add_argument('--room-h', type=float, default=None, metavar='DB',
                       help=f'Room ch H gain (default {OTT_ROOM_GAIN_H_DB:+g})')
    gains.add_argument('--board-l', type=float, default=None, metavar='DB',
                       help=f'Board ch L gain (default {OTT_BOARD_GAIN_L_DB:+g})')
    gains.add_argument('--board-m', type=float, default=None, metavar='DB',
                       help=f'Board ch M gain (default {OTT_BOARD_GAIN_M_DB:+g})')
    gains.add_argument('--board-h', type=float, default=None, metavar='DB',
                       help=f'Board ch H gain (default {OTT_BOARD_GAIN_H_DB:+g})')
    gains.add_argument('--master-l', type=float, default=None, metavar='DB',
                       help=f'Master L gain (default {OTT_MASTER_GAIN_L_DB:+g})')
    gains.add_argument('--master-m', type=float, default=None, metavar='DB',
                       help=f'Master M gain (default {OTT_MASTER_GAIN_M_DB:+g})')
    gains.add_argument('--master-h', type=float, default=None, metavar='DB',
                       help=f'Master H gain (default {OTT_MASTER_GAIN_H_DB:+g})')
    gains.add_argument('--master-upwd', type=float, default=None, metavar='PCT',
                       help=f'Master upward compression strength 0–200 (OTT default 100, script default {OTT_MASTER_UPWD_STRGTH:g})')
    gains.add_argument('--in-gain', type=float, default=None, metavar='DB',
                       help=f'Master input gain dB (OTT default 0, script default {OTT_MASTER_IN_GAIN_DB:+g})')
    gains.add_argument('--out-gain', type=float, default=None, metavar='DB',
                       help=f'Master output gain dB (OTT default 0, script default {OTT_MASTER_OUT_GAIN_DB:+g})')
    # --N-l / --N-m / --N-h for arbitrary channels are parsed manually below
    args, extra_argv = parser.parse_known_args()

    logging.basicConfig(
        level=logging.INFO if args.quiet else logging.DEBUG,
        format='%(levelname)-8s  %(message)s',
    )

    # --flat: reset everything to OTT factory defaults before individual overrides
    if args.flat:
        OTT_ROOM_GAIN_L_DB = OTT_ROOM_GAIN_M_DB = OTT_ROOM_GAIN_H_DB = 0.0
        OTT_BOARD_GAIN_L_DB = OTT_BOARD_GAIN_M_DB = OTT_BOARD_GAIN_H_DB = 0.0
        OTT_MASTER_GAIN_L_DB = OTT_MASTER_GAIN_M_DB = OTT_MASTER_GAIN_H_DB = 0.0
        OTT_MASTER_UPWD_STRGTH = 100.0
        OTT_MASTER_IN_GAIN_DB = 0.0
        OTT_MASTER_OUT_GAIN_DB = 0.0

    # Apply named gain overrides before any processor is built
    if args.room_l is not None:
        OTT_ROOM_GAIN_L_DB = args.room_l
    if args.room_m is not None:
        OTT_ROOM_GAIN_M_DB = args.room_m
    if args.room_h is not None:
        OTT_ROOM_GAIN_H_DB = args.room_h
    if args.board_l is not None:
        OTT_BOARD_GAIN_L_DB = args.board_l
    if args.board_m is not None:
        OTT_BOARD_GAIN_M_DB = args.board_m
    if args.board_h is not None:
        OTT_BOARD_GAIN_H_DB = args.board_h
    if args.master_l is not None:
        OTT_MASTER_GAIN_L_DB = args.master_l
    if args.master_m is not None:
        OTT_MASTER_GAIN_M_DB = args.master_m
    if args.master_h is not None:
        OTT_MASTER_GAIN_H_DB = args.master_h
    if args.master_upwd is not None:
        OTT_MASTER_UPWD_STRGTH = args.master_upwd
    if args.in_gain is not None:
        OTT_MASTER_IN_GAIN_DB = args.in_gain
    if args.out_gain is not None:
        OTT_MASTER_OUT_GAIN_DB = args.out_gain
    if args.align_ref is not None:
        ALIGN_REF_CHANNEL = args.align_ref

    # --only-channels: override SELECTED_CHANNELS for this run
    if args.only_channels:
        SELECTED_CHANNELS = [int(x.strip())
                             for x in args.only_channels.split(',')]

    # --slow / --slow-hq: default pan to ch29=L, ch31=R for diagnostic stereo comparison
    if args.slow > 1 or args.slow_hq > 1:
        SELECTED_PANS[29] = -1.0
        SELECTED_PANS[31] = 1.0

    # --pan CH:V,...: override SELECTED_PANS for specific channels
    if args.pan:
        for item in args.pan.split(','):
            ch_str, val_str = item.strip().split(':')
            SELECTED_PANS[int(ch_str)] = float(val_str)

    # --offset CH:MS (repeatable): manual per-channel offset in ms → samples
    manual_offsets: dict[int, int] | None = None
    if args.offset:
        sr_default = 48000
        manual_offsets = {}
        for item in args.offset:
            ch_str, ms_str = item.strip().split(':')
            manual_offsets[int(ch_str)] = int(
                round(float(ms_str) / 1000.0 * sr_default))
        _log.info('Manual offsets (ms): ' + ', '.join(
            f'ch{ch}:{v / sr_default * 1000:+.1f}' for ch, v in manual_offsets.items()
        ))

    # Parse generic --N-l / --N-m / --N-h flags for arbitrary channel adds
    import re as _re
    _extra_raw: dict[int, list[float]] = {}
    _i = 0
    while _i < len(extra_argv):
        _m = _re.match(r'^--(\d+)-([lmh])$', extra_argv[_i])
        if _m and _i + 1 < len(extra_argv):
            _ch, _band = int(_m.group(1)), _m.group(2)
            try:
                _val = float(extra_argv[_i + 1])
                _extra_raw.setdefault(_ch, [0.0, 0.0, 0.0])
                _extra_raw[_ch]['lmh'.index(_band)] = _val
                _i += 2
                continue
            except ValueError:
                pass
        _i += 1
    extra_ch_gains: dict[int, tuple[float, float, float]] | None = (
        {ch: (v[0], v[1], v[2])
         for ch, v in _extra_raw.items()} if _extra_raw else None
    )
    if extra_ch_gains:
        _log.info(f'Extra channels: {sorted(extra_ch_gains)}')

    if args.ott_params:
        plugin = _load_ott_plugin(_log)
        if plugin is None:
            print('OTT plugin not found — check OTT_PATHS in mastering.py')
        else:
            print('OTT parameters:')
            for name, param in plugin.parameters.items():
                print(f'  {name:<30s}  value={param.raw_value!r}  '
                      f'range=[{param.min_value}, {param.max_value}]')
        raise SystemExit(0)

    if args.folder is None:
        parser.error('folder is required unless --ott-params is used')

    root = Path(args.folder).resolve()
    if not root.is_dir():
        parser.error(f'Not a directory: {root}')

    from nofun.inventory import extract_date_band
    from nofun.paths import detect_mounts

    clip = tuple(args.clip) if args.clip else None  # type: ignore[arg-type]
    slug = (_verbose_slug() if args.verbose_filename
            else _settings_slug() if args.settings
            else None)
    if slug:
        print(f'Filename slug: {slug}')
        for line in _explain_slug(slug):
            print(line)

    # Resolve default output paths the same way the TUI does
    mount_c, mount_d = detect_mounts()
    default_audio_dest = mount_d / 'audio' if mount_d != Path('.') else None
    sharepoint_root = (mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun')
                       / 'OneDrive - No Fun Troy LLC' / 'Multitracks')
    sharepoint_root = sharepoint_root if sharepoint_root.is_dir() else None

    def _find_sp_folder(date_prefix: str) -> Path | None:
        """Return the SharePoint date subfolder matching date_prefix, or None."""
        if not sharepoint_root:
            return None
        n = len(date_prefix)
        try:
            return next(
                f for f in sharepoint_root.iterdir()
                if f.is_dir()
                and f.name[:n] == date_prefix
                and (len(f.name) == n or not f.name[n].isdigit())
            )
        except StopIteration:
            return None

    def _base_from_name(name: str) -> str:
        date, band = extract_date_band(name)
        if date == 'TBD':
            return name
        if len(date) == 10 and date.startswith('20'):
            date = date[2:]
        return f'{date}_{band}'

    def _run_one(wav_files: list[Path], base: str, out_dir: Path) -> list[Path]:
        _log.info(f'Base name: {base}  ({len(wav_files)} WAVs)')
        if slug:
            _log.info(f'Settings slug: {slug}')
        # Encode manual offsets in base name so each file is distinct
        run_base = base
        if manual_offsets:
            sr_default = 48000
            run_base += '_' + '_'.join(
                f'ch{ch}_{int(round(s / sr_default * 1000)):+d}ms'
                for ch, s in sorted(manual_offsets.items())
            )
        written = generate_masters(
            wav_files, run_base, out_dir,
            selected_only=not args.all_combos,
            clip=clip,           # type: ignore[arg-type]
            settings_slug=slug,
            extra_ch_gains=extra_ch_gains,
            align=not args.no_align and not manual_offsets,
            manual_offsets=manual_offsets,
            slow=args.slow,
            slow_hq=args.slow_hq,
        )
        # Copy to SharePoint date folder if found and output is the default audio_dest
        if written and args.output is None:
            date_prefix = base[:8]  # e.g. '26-04-11'
            sp_folder = _find_sp_folder(date_prefix)
            if sp_folder:
                for p in written:
                    dest = sp_folder / p.name
                    shutil.copy2(str(p), str(dest))
                    _log.info(f'COPY    {p.name} → {sp_folder.name}/')
            else:
                _log.debug(
                    f'SharePoint folder not found for {date_prefix} — skipping cloud copy')
        return written

    total_written: list[Path] = []

    if args.select:
        # Multi-band mode: search root for matching ZIPs or WAV subdirs
        needle = args.select.lower()
        matches: list[tuple[str, list[Path], Path]] = []

        for zip_path in sorted(root.glob('*.zip')):
            if needle in zip_path.stem.lower():
                matches.append(('__zip__', [], zip_path))

        for sub in sorted(root.iterdir()):
            if sub.is_dir() and needle in sub.name.lower():
                wavs = sorted(sub.glob('*.wav'))
                if wavs:
                    matches.append(('__dir__', wavs, sub))

        if not matches:
            parser.error(
                f'No ZIPs or WAV folders matching "{args.select}" found in {root}')

        _log.info(f'Found {len(matches)} match(es) for "{args.select}"')

        for kind, wavs, path in matches:
            if kind == '__zip__':
                zip_path = path
                base = _base_from_name(zip_path.stem)
                out_dir = Path(args.output).resolve() if args.output else (
                    default_audio_dest or root)
                _log.info(f'Source: ZIP — {zip_path.name}')
                with tempfile.TemporaryDirectory() as tmp:
                    with _zf.ZipFile(zip_path) as zf:
                        zf.extractall(tmp)
                    wavs = sorted(Path(tmp).glob('*.wav'))
                    if not wavs:
                        _log.warning(f'ZIP contains no WAVs: {zip_path.name}')
                        continue
                    total_written += _run_one(wavs, base, out_dir)
            else:
                base = _base_from_name(path.name)
                out_dir = Path(args.output).resolve() if args.output else (
                    default_audio_dest or path)
                _log.info(f'Source: folder — {path.name}')
                total_written += _run_one(wavs, base, out_dir)
    else:
        # Single folder mode
        wav_files = sorted(root.glob('*.wav'))
        if not wav_files:
            parser.error(f'No WAV files found in {root}')
        out_dir = Path(args.output).resolve() if args.output else (
            default_audio_dest or root)
        base = _base_from_name(root.name)
        _log.info(f'Source: folder — {root.name}')
        total_written += _run_one(wav_files, base, out_dir)

    print(f'Wrote {len(total_written)} master(s)')
    for p in total_written:
        print(f'  {p}')
