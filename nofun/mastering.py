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
# Default profile is the "live-energy" master (v4C, 2026-06-13): low OTT depth so
# the band breathes, board carries vocals/presence, room re-added as controlled
# energy (HPF + parallel crush + M/S pocket + micro-upward). See _mix_channels.
OTT_CH_DEPTH:       float = 15.0   # overall effect amount      (0–100); low = glue not crush
OTT_CH_UPWD_STRGTH: float = 100.0  # upward compression         (0–200)
OTT_CH_DNWD_STRGTH: float = 100.0  # downward compression       (0–200)
OTT_CH_THRESH_L:    float = 100.0  # low-band threshold         (0–200)
OTT_CH_THRESH_M:    float = 100.0  # mid-band threshold         (0–200)
OTT_CH_THRESH_H:    float = 100.0  # high-band threshold        (0–200)
OTT_CH_GAIN_L_DB:   float = 0.0  # low-band output level      (-inf–+6 dB)
OTT_CH_GAIN_M_DB:   float = 0.0  # mid-band output level      (-inf–+6 dB)
OTT_CH_GAIN_H_DB:   float = 0.0  # high-band output level     (-inf–+6 dB)

# Per-group depth overrides. None = inherit OTT_CH_DEPTH (i.e. behave as before).
# Set via --room-depth / --board-depth on the CLI to crush room hard while keeping board light.
OTT_ROOM_DEPTH:  float | None = None
OTT_BOARD_DEPTH: float | None = None

# Feedback notch (Option C) — adaptive per-show resonance detection + notch. CLI-overridable.
# Floor lowered 800->350 Hz (2026-06-23): the live-room howl on the Porches set
# extended into the low-mids; the static narrow-notch detector now scans down to
# 350 Hz. Only narrow peaks clearing FEEDBACK_PROMINENCE_DB get notched, so the
# wider scan won't carve broadband low-mid content.
FEEDBACK_BAND: tuple[float, float] = (350.0, 2000.0)
FEEDBACK_PROMINENCE_DB: float = 8.0
FEEDBACK_MAX: int = 4
FEEDBACK_Q: float = 14.0
FEEDBACK_CUT_DB: float = -14.0

# DyERS dynamic resonance suppressor (post-OTT). Defaults = the "35 focused" preset.
# Floor lowered 1500->800 Hz (2026-06-23) to reach the low-mid room howl the old
# floor missed; see FEEDBACK_BAND note above.
DYERS_BAND: tuple[float, float] = (800.0, 6000.0)
DYERS_SENSITIVITY: float = 0.6   # 0-1, higher = more peaks (lower prominence threshold)
DYERS_SHARPNESS: float = 0.8     # 0-1, higher = narrower notch
DYERS_SPEED: float = 0.5         # 0-1, higher = faster attack/release
DYERS_RESONANCE_DB: float = -24.0
DYERS_MAX_PEAKS: int = 4
DYERS_FFT: int = 4096
DYERS_ENV_BINS: int = 65

# Master bus OTT knobs  (applied to the stereo mix after channel summing)
OTT_MASTER_DEPTH:       float = 20.0   # overall effect amount  (0–100); low = glue not pump
OTT_MASTER_IN_GAIN_DB:  float = -10.0  # input gain             (-54–+19 dB)
OTT_MASTER_OUT_GAIN_DB: float =   2.0  # output gain            (-54–+19 dB)
# upward compression     (0–200); 0 = downward only (no master pumping)
OTT_MASTER_UPWD_STRGTH: float = 0.0
OTT_MASTER_DNWD_STRGTH: float = 100.0  # downward compression   (0–200)
OTT_MASTER_THRESH_L:    float = 100.0  # low-band threshold     (0–200)
OTT_MASTER_THRESH_M:    float = 100.0  # mid-band threshold     (0–200)
OTT_MASTER_THRESH_H:    float = 100.0  # high-band threshold    (0–200)
OTT_MASTER_GAIN_L_DB:   float = 0.0  # low-band output level  (-inf–+6 dB)
OTT_MASTER_GAIN_M_DB:   float = 0.0  # mid-band output level  (-inf–+6 dB)
OTT_MASTER_GAIN_H_DB:   float = -1.0  # high-band output level (-inf–+6 dB)

# Room channels (29=L, 30=R) — spatial glue + crowd energy, NOT vocal carrier.
# EQ neutral (the HPF + parallel crush + M/S pocket below do the shaping); upward
# compression kept high to lift low-level crowd/cheers above the dry board feed.
ROOM_CHANNELS:           list[int] = [29, 30]
OTT_ROOM_UPWD_STRGTH:    float = 120.0   # high upward comp — lifts crowd/cheers (0–200)
OTT_ROOM_DNWD_STRGTH:    float = 100.0   # downward compression (0–200)
OTT_ROOM_GAIN_L_DB:      float =  0.0    # neutral — HPF handles the low end
OTT_ROOM_GAIN_M_DB:      float =  0.0    # neutral — M/S pocket carves vocal space
OTT_ROOM_GAIN_H_DB:      float =  0.0    # neutral

# Board channels (31=L, 32=R) — vocals, presence, transients live here (X32 mains).
BOARD_CHANNELS:          list[int] = [31, 32]
OTT_BOARD_UPWD_STRGTH:   float = 70.0    # upward comp (30->70, 2026-06-23): lifts low-level
                                         # board content; bass-weighted by the +3 dB low gain
                                         # below — the "OTT bass boost" without makeup gain
OTT_BOARD_DNWD_STRGTH:   float =  80.0   # softer downward (preserve bass punch)
OTT_BOARD_GAIN_L_DB:     float =  3.0    # bass weight
OTT_BOARD_GAIN_M_DB:     float =  3.0    # vocal clarity / midrange bite
OTT_BOARD_GAIN_H_DB:     float =  2.0    # presence / air
# De-esser on the board (vocal) feed, applied post-OTT so it tames the sibilance the
# +2 dB air boost adds. ffmpeg `deesser` intensity 0-1; 0.0 disables. 0.2 = moderate.
DEESS_BOARD_INTENSITY:   float =  0.2

# Room-energy routing (v3/v4) — re-adds live room/crowd cleanly without the comb
# filtering of the old Haas spread. Applied in _mix_channels; CLI-overridable via
# --room-hpf / --room-parallel-crush / --bus-ms-scoop / --room-upwd / --room-mix.
ROOM_HPF_HZ:          float = 175.0   # high-pass room ch so it can be pushed loud without low-end mud
# v5: pump the room ch up in the final L/R blend — the board (31/32) is the clean vocal/
# presence carrier, so lifting room here adds live ambience/grit without touching the board.
# Applied post-OTT, pre-pan in _mix_channels. CLI-overridable via --room-mix.
ROOM_MIX_GAIN_DB:     float = 4.5     # room level in the L/R blend (dB); board unchanged
ROOM_PARALLEL_CRUSH:  float = 0.35    # blend a fast-release crushed parallel room bus (NY style) for density
BUS_MS_SCOOP_DB:      float = -2.5    # M/S mid scoop on final mix — pockets the board vocal, keeps room wide
BUS_MS_SCOOP_FREQ:    float = 1400.0  # centre of the vocal-pocket scoop (Hz)
BUS_MS_SCOOP_Q:       float = 1.0     # width of the scoop (lower = wider)


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


def _channel_is_silent(path: Path, start_sec: float = 0.0,
                       duration_sec: float | None = None) -> bool:
    """True if a channel reads as dead/silent (mean RMS < DEAD_RMS_DB).

    Lets a present-but-silent room mic (e.g. an unplugged input still recorded
    as a near-zero track) be treated the same as an absent one. When no window
    is given, samples a 60 s mid-file window so a quiet intro/outro is not
    mistaken for a dead channel.
    """
    import soundfile as sf
    from nofun.mastering_meta import DEAD_RMS_DB
    try:
        info = sf.info(str(path))
    except Exception:
        return False
    sr = info.samplerate
    if duration_sec is not None:
        start = max(0, int(start_sec * sr))
        stop = start + int(duration_sec * sr)
    else:
        total = info.frames
        win = min(total, int(60.0 * sr))
        start = max(0, (total - win) // 2)
        stop = start + win
    try:
        audio, _ = sf.read(str(path), start=start, stop=stop,
                           dtype='float32', always_2d=False)
    except Exception:
        return False
    a = audio.astype(np.float64)
    rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
    return 20.0 * math.log10(rms + 1e-12) < DEAD_RMS_DB


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

    Uses LAME VBR -q:a 2 (~190 kbps) — retains the cymbal/HF detail that 128 kbps
    CBR smears on dense live recordings, still ~15× smaller than 32-bit float WAV.

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
                args={'source': str(tmp), 'dest': str(path)},
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
                 '-c:a', 'libmp3lame', '-q:a', '2',
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
    """Run raw f32le bytes through an ffmpeg filter graph via temp files. Returns raw f32le bytes.

    Temp files instead of pipes: Windows pipe buffers (64 KB) make piping 1+ GB of audio
    take hours; disk I/O at 500+ MB/s takes seconds.
    """
    import tempfile as _tf, os as _os
    fd_in,  path_in  = _tf.mkstemp(suffix='.f32le')
    fd_out, path_out = _tf.mkstemp(suffix='.f32le')
    try:
        _os.write(fd_in, raw_in); _os.close(fd_in); _os.close(fd_out)
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'f32le', '-ar', str(sr), '-ac', str(in_ch), '-i', path_in,
            '-filter_complex', filter_str,
            '-f', 'f32le', '-ar', str(sr), '-ac', str(out_ch), '-y', path_out,
        ]
        subprocess.run(cmd, check=True)
        with open(path_out, 'rb') as f:
            return f.read()
    finally:
        for p in (path_in, path_out):
            try:
                _os.unlink(p)
            except OSError:
                pass


def _denoise_mono(mono: np.ndarray, sr: int, nr: float = 10.0, nf: float = -50.0) -> np.ndarray:
    """FFT denoise (afftdn) a mono signal through an ffmpeg pipe. Length-preserving."""
    raw_out = _ffmpeg_pipe(mono.astype(np.float32).tobytes(), sr, 1,
                           f'afftdn=nr={nr}:nf={nf}', 1)
    out = np.frombuffer(raw_out, dtype=np.float32).copy()
    N = len(mono)
    if len(out) < N:
        out = np.pad(out, (0, N - len(out)))
    return out[:N]


def _highpass_mono(mono: np.ndarray, sr: int, hz: float) -> np.ndarray:
    """2-pole high-pass a mono signal through an ffmpeg pipe. Length-preserving.

    Used on the room mic so it can be pushed loud for snare/cymbal/guitar energy
    without its low-end rumble phase-cancelling the board's tight kick/bass.
    """
    raw = _ffmpeg_pipe(mono.astype(np.float32).tobytes(), sr, 1,
                       f'highpass=f={hz:g}:poles=2', 1)
    out = np.frombuffer(raw, dtype=np.float32).copy()
    N = len(mono)
    if len(out) < N:
        out = np.pad(out, (0, N - len(out)))
    return out[:N]


def _deess_mono(mono: np.ndarray, sr: int, intensity: float) -> np.ndarray:
    """De-ess a mono signal via ffmpeg `deesser`. Length-preserving.

    Dynamic sibilance ducking keyed to high-frequency energy — only attenuates
    when an ess actually hits, so the vocal stays bright between sibilants.
    intensity 0-1 (0 = bypass). Applied to the board/vocal feed post-OTT.
    """
    raw = _ffmpeg_pipe(mono.astype(np.float32).tobytes(), sr, 1,
                       f'deesser=i={intensity:g}:m=0.5:f=0.5:s=o', 1)
    out = np.frombuffer(raw, dtype=np.float32).copy()
    N = len(mono)
    if len(out) < N:
        out = np.pad(out, (0, N - len(out)))
    return out[:N]


def _crush_mono(mono: np.ndarray, sr: int) -> np.ndarray:
    """Heavily downward-compress a mono signal (NY-parallel style). Length-preserving.

    Fast attack/release + high ratio explodes the room tail/density; blended
    subtly under the main mix it adds aggression without touching board transients.
    """
    raw = _ffmpeg_pipe(mono.astype(np.float32).tobytes(), sr, 1,
                       'acompressor=threshold=0.03:ratio=10:attack=1:release=40:makeup=4', 1)
    out = np.frombuffer(raw, dtype=np.float32).copy()
    N = len(mono)
    if len(out) < N:
        out = np.pad(out, (0, N - len(out)))
    return out[:N]


def _ms_scoop(left: np.ndarray, right: np.ndarray, sr: int,
              freq: float, q: float, gain_db: float) -> tuple[np.ndarray, np.ndarray]:
    """Mid/Side EQ: scoop the Mid channel in the vocal band, leave the Side untouched.

    Carves a centre pocket so the dry board vocal punches through while the wide,
    ambient room roar stays on the sides. Encodes L/R→M/S, equalises M, decodes back.
    """
    import tempfile as _tf, os as _os
    N = len(left)
    inter = np.empty(N * 2, dtype=np.float32)
    inter[0::2] = left.astype(np.float32)
    inter[1::2] = right.astype(np.float32)
    fd_in, path_in = _tf.mkstemp(suffix='.f32le')
    fd_out, path_out = _tf.mkstemp(suffix='.f32le')
    try:
        _os.write(fd_in, inter.tobytes()); _os.close(fd_in); _os.close(fd_out)
        # M=(L+R)/2, S=(L-R)/2 → EQ M → L=M+S, R=M-S (unity round-trip)
        graph = (
            '[0:a]asplit=2[a][b];'
            '[a]pan=mono|c0=0.5*c0+0.5*c1[m];'
            '[b]pan=mono|c0=0.5*c0-0.5*c1[s];'
            f'[m]equalizer=f={freq:g}:t=q:w={q:g}:g={gain_db:g}[meq];'
            '[meq][s]join=inputs=2:channel_layout=stereo[ms];'
            '[ms]pan=stereo|c0=c0+c1|c1=c0-c1[out]'
        )
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
               '-f', 'f32le', '-ar', str(sr), '-ac', '2', '-i', path_in,
               '-filter_complex', graph, '-map', '[out]',
               '-f', 'f32le', '-ar', str(sr), '-ac', '2', '-y', path_out]
        subprocess.run(cmd, check=True)
        with open(path_out, 'rb') as f:
            raw = f.read()
    finally:
        for p in (path_in, path_out):
            try:
                _os.unlink(p)
            except OSError:
                pass
    out = np.frombuffer(raw, dtype=np.float32)
    m = (min(len(out), N * 2) // 2) * 2
    l, r = out[0:m:2].copy(), out[1:m:2].copy()
    if len(l) < N:
        l = np.pad(l, (0, N - len(l)))
        r = np.pad(r, (0, N - len(r)))
    return l[:N], r[:N]


def detect_resonant_peaks(
    mono: np.ndarray, sr: int,
    band: tuple[float, float] = (2000.0, 3000.0),
    prominence_db: float = 8.0, max_peaks: int = 4,
    nfft: int = 16384, smooth_bins: int = 121,
) -> list[tuple[float, float]]:
    """Return [(freq_hz, prominence_db), ...] of narrow spectral peaks within `band`,
    sorted by prominence desc, capped at max_peaks. Empty if nothing stands out, so a
    clean show notches nothing. Long-window Welch PSD isolates persistent resonance from
    moving musical content; a wide moving-average baseline ignores broad tonal humps
    (loud bass) and only flags narrow spikes."""
    if len(mono) < nfft:
        return []
    win = np.hanning(nfft)
    hop = nfft // 2
    acc = np.zeros(nfft // 2 + 1)
    n = 0
    for s in range(0, len(mono) - nfft, hop):
        acc += np.abs(np.fft.rfft(mono[s:s + nfft] * win)) ** 2
        n += 1
    if n == 0:
        return []
    psd_db = 10 * np.log10(acc / n + 1e-12)
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    pad = smooth_bins // 2
    baseline = np.convolve(np.pad(psd_db, pad, mode='edge'),
                           np.ones(smooth_bins) / smooth_bins, mode='valid')
    prom = psd_db - baseline
    out: list[tuple[float, float]] = []
    for i in range(3, len(psd_db) - 3):
        f = float(freqs[i])
        if f < band[0] or f > band[1]:
            continue
        if psd_db[i] == max(psd_db[i - 3:i + 4]) and prom[i] >= prominence_db:
            out.append((f, float(prom[i])))
    out.sort(key=lambda t: -t[1])
    return out[:max_peaks]


def _apply_notches(left: np.ndarray, right: np.ndarray, sr: int,
                   freqs: list[float], q: float = 14.0, cut_db: float = -14.0
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Notch each freq with a narrow ffmpeg equalizer band; process stereo through one pipe."""
    if not freqs:
        return left, right
    chain = ','.join(f'equalizer=f={f:.1f}:t=q:w={q:g}:g={cut_db:g}' for f in freqs)
    N = len(left)
    inter = np.empty(N * 2, dtype=np.float32)
    inter[0::2] = left.astype(np.float32)
    inter[1::2] = right.astype(np.float32)
    raw = _ffmpeg_pipe(inter.tobytes(), sr, 2, chain, 2)
    out = np.frombuffer(raw, dtype=np.float32)
    m = (min(len(out), N * 2) // 2) * 2
    l, r = out[0:m:2].copy(), out[1:m:2].copy()
    if len(l) < N:
        l = np.pad(l, (0, N - len(l)))
        r = np.pad(r, (0, N - len(r)))
    return l[:N], r[:N]


def dyers_suppress_mono(
    x: np.ndarray, sr: int, fft_size: int = DYERS_FFT,
    sensitivity: float = DYERS_SENSITIVITY, sharpness: float = DYERS_SHARPNESS,
    speed: float = DYERS_SPEED, resonance_gain_db: float = DYERS_RESONANCE_DB,
    band: tuple[float, float] = DYERS_BAND, max_peaks: int = DYERS_MAX_PEAKS,
    env_bins: int = DYERS_ENV_BINS, report: dict | None = None,
) -> np.ndarray:
    """DyERS-style WOLA dynamic resonance suppressor (numpy port). Per STFT frame, detect
    narrow peaks by LOCAL prominence (mag minus a moving-average envelope) — not a global
    mean+std, which over-flags broadband HF and dulls the top end — cap to max_peaks, build
    Gaussian notch gains (phase preserved), smooth across frames with attack/release."""
    hop = fft_size // 4
    w = np.hanning(fft_size).astype(np.float64)
    w2 = w * w
    freqs = np.fft.rfftfreq(fft_size, 1 / sr)
    nbins = len(freqs)
    bandmask = (freqs >= band[0]) & (freqs <= band[1])
    prom_thresh = (1.0 - sensitivity) * 12.0 + 3.0
    epad = env_bins // 2
    sigma = (1.0 - sharpness) * 6.0 + 0.8
    gwin = int(np.ceil(5.0 * sigma))   # gaussian half-window; beyond ±5σ the dip is ~0
    target_gain = 10.0 ** (resonance_gain_db / 20.0)
    atk = float(np.exp(-1.0 / max(1.0, (1.0 - speed) * 6.0 + 1.0)))
    rel = float(np.exp(-1.0 / max(1.0, (1.0 - speed) * 40.0 + 4.0)))

    pad = fft_size
    xp = np.concatenate([np.zeros(pad), x.astype(np.float64), np.zeros(pad + hop)])
    n_frames = 1 + (len(xp) - fft_size) // hop
    out = np.zeros(len(xp))
    norm = np.zeros(len(xp))
    smoothed = np.ones(nbins)

    # Optional activity report: count how many frames each bin was a suppressed peak, so the
    # caller can record which frequencies DyERS actually reduced (not a static snapshot).
    if report is not None:
        report.setdefault('counts', np.zeros(nbins))
        report.setdefault('n_frames', 0)
        report['freqs'] = freqs
        report['n_frames'] += n_frames

    # Block-vectorized WOLA: batch the FFTs/envelope/peak-detection per block; only the
    # attack/release recursion (+ tiny local-window Gaussians) runs per frame. Memory-bounded
    # so it doesn't thrash on long files. Output matches the per-frame reference to ~1e-6.
    BLOCK = 1024
    win_idx = np.arange(fft_size)
    for b0 in range(0, n_frames, BLOCK):
        nb = min(b0 + BLOCK, n_frames) - b0
        starts = (b0 + np.arange(nb)) * hop
        frames = xp[starts[:, None] + win_idx[None, :]] * w        # (nb, fft)
        spec = np.fft.rfft(frames, axis=1)                          # (nb, nbins)
        mag = np.abs(spec)
        mag_db = 20.0 * np.log10(mag + 1e-9)
        # centered moving-average envelope along freq (edge-padded) via cumsum
        mp = np.pad(mag_db, ((0, 0), (epad, epad)), mode='edge')
        csum = np.concatenate([np.zeros((nb, 1)), np.cumsum(mp, axis=1)], axis=1)
        env = (csum[:, env_bins:] - csum[:, :-env_bins]) / env_bins  # (nb, nbins)
        prom = mag_db - env
        is_peak = np.zeros_like(mag, dtype=bool)
        is_peak[:, 1:-1] = (mag[:, 1:-1] >= mag[:, :-2]) & (mag[:, 1:-1] >= mag[:, 2:])
        is_peak &= bandmask[None, :] & (prom >= prom_thresh)
        gains = np.empty_like(mag)
        for j in range(nb):
            pk = np.nonzero(is_peak[j])[0]
            if pk.size > max_peaks:
                pk = pk[np.argsort(prom[j, pk])[-max_peaks:]]
            if report is not None and pk.size:
                report['counts'][pk] += 1
            g = np.ones(nbins)
            for p in pk:
                lo, hi = max(0, p - gwin), min(nbins, p + gwin + 1)
                rng = np.arange(lo, hi)
                g[lo:hi] *= 1.0 - (1.0 - target_gain) * np.exp(-((rng - p) ** 2) / (2.0 * sigma ** 2))
            down = g < smoothed
            smoothed = np.where(down, atk * smoothed + (1 - atk) * g,
                                rel * smoothed + (1 - rel) * g)
            gains[j] = smoothed
        out_frames = np.fft.irfft(spec * gains, fft_size, axis=1) * w  # (nb, fft)
        for j in range(nb):
            s = starts[j]
            out[s:s + fft_size] += out_frames[j]
            norm[s:s + fft_size] += w2
    out /= np.maximum(norm, 1e-9)
    return out[pad:pad + len(x)].astype(np.float32)


def dyers_suppress_stereo(left: np.ndarray, right: np.ndarray, sr: int,
                          report: dict | None = None, **kw) -> tuple[np.ndarray, np.ndarray]:
    """Apply DyERS suppression to a stereo pair (per channel). A shared report dict
    accumulates suppressed-peak activity across both channels."""
    lo = dyers_suppress_mono(left, sr, report=report, **kw)
    ro = dyers_suppress_mono(right, sr, report=report, **kw)
    return lo, ro


def dyers_report_peaks(report: dict, min_engaged: float = 0.02, max_peaks: int = 8,
                       min_sep_bins: int = 4) -> list[tuple[float, float]]:
    """Reduce a DyERS activity report to [(freq_hz, engaged_pct), ...] — the frequencies it
    actually suppressed, by how persistently. Picks count local-maxima active in >= min_engaged
    of frames, de-duped by min_sep_bins, sorted by engagement desc."""
    counts = report.get('counts')
    nf = report.get('n_frames', 0)
    if counts is None or nf <= 0:
        return []
    freqs = report['freqs']
    eng = counts / nf
    cand = [b for b in range(1, len(counts) - 1)
            if counts[b] > 0 and counts[b] >= counts[b - 1] and counts[b] >= counts[b + 1]
            and eng[b] >= min_engaged]
    cand.sort(key=lambda b: -counts[b])
    chosen: list[int] = []
    for b in cand:
        if all(abs(b - c) >= min_sep_bins for c in chosen):
            chosen.append(b)
        if len(chosen) >= max_peaks:
            break
    peaks = [(float(freqs[b]), round(100.0 * eng[b], 1)) for b in chosen]
    peaks.sort(key=lambda t: -t[1])
    return peaks


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
        plugin.depth = OTT_BOARD_DEPTH if OTT_BOARD_DEPTH is not None else OTT_CH_DEPTH
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
        mixed = ((out[0] + out[1]) / 2).astype(np.float32)
        if DEESS_BOARD_INTENSITY > 0:
            mixed = _deess_mono(mixed, sr, DEESS_BOARD_INTENSITY)
        return mixed
    return process


def make_ott_room_processor(sr: int, logger: logging.Logger) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a mono→mono OTT processor for room channels (29/30) with band-gain overrides."""
    plugin = _load_ott_plugin(logger)
    if plugin is None:
        return None
    import pedalboard
    board = pedalboard.Pedalboard([plugin])

    def process(mono: np.ndarray) -> np.ndarray:
        plugin.depth = OTT_ROOM_DEPTH if OTT_ROOM_DEPTH is not None else OTT_CH_DEPTH
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
    room_match: str | None = None,
    denoise_room: bool = False,
    room_spread: bool = False,
    spread_ms: float = 14.0,
    stats: dict | None = None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Load, process, pan and sum channels into a stereo (L, R) pair.

    stats: if a dict is passed, it's populated with Tier-1 metadata inputs —
        'levels_db' {ch: (rms_db, peak_db)} (raw input) and 'align_ms' {ch: lag_ms}.

    ch_processor_map: per-channel OTT processor overrides.
    align: cross-correlate channels and trim for hardware timing offsets.
    manual_offsets: {ch: samples_ahead_of_ref} — bypasses auto-align when provided.
    room_match: 'rms' | 'peak' — apply pre-OTT makeup gain to ROOM_CHANNELS so their
        level matches BOARD_CHANNELS before compression. None = no matching.
    denoise_room: if True, FFT-denoise ROOM_CHANNELS (afftdn) before any other processing.
        Lets you crush room hard without lifting noise floor.
    room_spread: if True, ROOM_CHANNELS are widened to pseudo-stereo via a complementary
        comb (L = x + g·delay, R = x - g·delay) instead of mono panning. For a single live
        room mic this gives stereo width; mono-compatible (L+R sums delayed part back out).
    spread_ms: comb delay in ms for room_spread (controls comb spacing / width).
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

    if stats is not None:  # raw input levels, before align/denoise/processing
        lv: dict[int, tuple[float, float]] = {}
        for ch in available:
            a = raws[ch].astype(np.float64)
            rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
            peak = float(np.max(np.abs(a))) if a.size else 0.0
            lv[ch] = (20.0 * math.log10(rms + 1e-12), 20.0 * math.log10(peak + 1e-12))
        stats['levels_db'] = lv

    # Manual or auto alignment
    leads: dict[int, int] = {}
    if manual_offsets is not None and len(available) > 1:
        leads = {ch: manual_offsets.get(ch, 0) for ch in available}
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
        leads = {ref_ch: 0}
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

    if stats is not None:
        stats['align_ms'] = {ch: leads.get(ch, 0) / sr * 1000.0 for ch in available}

    # Pre-OTT denoise on room channels — strip noise floor before upward compression amplifies it.
    # Skip silent/dead channels (e.g. an unplugged room mic): denoising silence is wasted work.
    # BOOKMARK: when both room mics are live, the per-channel afftdn passes could run concurrently
    # (one ffmpeg subprocess each) to ~halve denoise wall time — deferred until a 2nd mic returns.
    if denoise_room:
        to_denoise = []
        for c in (c for c in ROOM_CHANNELS if c in raws):
            a = raws[c].astype(np.float64)
            rms_db = 20.0 * math.log10((float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0) + 1e-12)
            if rms_db < -80.0:   # dead/silent — matches mastering_meta DEAD_RMS_DB
                logger.info(f'DENOISE skip ch{c} (silent {rms_db:.0f} dB)', extra={'tui': False})
            else:
                to_denoise.append(c)
        if to_denoise:
            logger.info(f'DENOISE room ch{to_denoise} (afftdn)', extra={'tui': False})
            for c in to_denoise:
                raws[c] = _denoise_mono(raws[c], sr)

    # Pre-OTT level match: gain room channels to board level before compression.
    if room_match and len(raws) > 1:
        room_present = [c for c in ROOM_CHANNELS if c in raws]
        board_present = [c for c in BOARD_CHANNELS if c in raws]
        if room_present and board_present:
            def _lvl(arr: np.ndarray) -> float:
                a = arr.astype(np.float64)
                if room_match == 'peak':
                    return float(np.max(np.abs(a))) or 1e-9
                return float(np.sqrt(np.mean(a ** 2))) or 1e-9
            room_lvl = float(np.mean([_lvl(raws[c]) for c in room_present]))
            board_lvl = float(np.mean([_lvl(raws[c]) for c in board_present]))
            gain = board_lvl / room_lvl if room_lvl > 0 else 1.0
            for c in room_present:
                raws[c] = (raws[c].astype(np.float32) * gain).astype(np.float32)
            logger.info(
                f'MATCH   room->board ({room_match}): {20 * math.log10(max(gain, 1e-9)):+.1f} dB '
                f'applied to ch{room_present}', extra={'tui': False})

    N = min(len(v) for v in raws.values())
    left = np.zeros(N, dtype=np.float32)
    right = np.zeros(N, dtype=np.float32)

    logger.info(f'MASTER  processing {len(available)} ch  ({N / sr / 60:.1f} min)',
                extra={'tui': False})
    spread_delay = int(spread_ms / 1000.0 * sr) if room_spread else 0
    parallel_room = np.zeros(N, dtype=np.float32) if ROOM_PARALLEL_CRUSH > 0 else None
    for ch in available:
        mono = raws[ch][:N]
        if len(mono) < N:
            mono = np.pad(mono, (0, N - len(mono)))

        # v3: high-pass the room mic before anything else, so it can be pushed
        # loud for energy without its low-end rumble fighting the board's kick/bass.
        if ROOM_HPF_HZ > 0 and ch in ROOM_CHANNELS:
            mono = _highpass_mono(mono, sr, ROOM_HPF_HZ)

        # v3: tap the post-HPF, pre-OTT room for the parallel crush bus.
        if parallel_room is not None and ch in ROOM_CHANNELS:
            parallel_room += mono

        proc = (ch_processor_map.get(ch)
                if ch_processor_map else None) or channel_processor
        processed = proc(mono)

        # v5: pump the room up in the blend (board stays put). Post-OTT, pre-pan so it
        # lifts the clean panned room contribution; the parallel crush bus keeps its own
        # ROOM_PARALLEL_CRUSH blend independent below.
        if ROOM_MIX_GAIN_DB and ch in ROOM_CHANNELS:
            processed = (processed * 10.0 ** (ROOM_MIX_GAIN_DB / 20.0)).astype(np.float32)

        if room_spread and ch in ROOM_CHANNELS and spread_delay > 0:
            # Complementary comb pseudo-stereo: L = x + g·delay, R = x - g·delay.
            # Scaled 0.5 so the mono sum (L+R) returns to unity — mono-compatible.
            delayed = np.empty_like(processed)
            delayed[:spread_delay] = 0.0
            delayed[spread_delay:] = processed[:-spread_delay]
            g = 0.8
            left += 0.5 * (processed + g * delayed)
            right += 0.5 * (processed - g * delayed)
        else:
            l_w, r_w = _pan_weights(pan_map.get(ch, 0.0))
            left += processed * l_w
            right += processed * r_w

    # v3: blend the crushed parallel room bus equally under the mix (centre).
    if parallel_room is not None:
        crushed = _crush_mono(parallel_room, sr)
        logger.info(f'PARALLEL room crush blend {ROOM_PARALLEL_CRUSH:.0%}', extra={'tui': False})
        left += ROOM_PARALLEL_CRUSH * crushed * 0.707
        right += ROOM_PARALLEL_CRUSH * crushed * 0.707

    # v3: Mid/Side vocal-pocket scoop on the pre-master mix.
    if BUS_MS_SCOOP_DB != 0:
        logger.info(
            f'MS SCOOP mid {BUS_MS_SCOOP_DB:+g}dB @ {BUS_MS_SCOOP_FREQ:g}Hz Q{BUS_MS_SCOOP_Q:g}',
            extra={'tui': False})
        left, right = _ms_scoop(left, right, sr, BUS_MS_SCOOP_FREQ,
                                BUS_MS_SCOOP_Q, BUS_MS_SCOOP_DB)

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
    room_match: str | None = None,
    denoise_room: bool = False,
    room_spread: bool = False,
    spread_ms: float = 14.0,
    kill_feedback: bool = False,
    dyers: bool = False,
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

    # Room-mic handling (decision 2026-06-03):
    #   both room mics live → normal hard L/R pan;
    #   exactly one live    → centre it (mono) and omit the dead/absent partner;
    #   none live           → omit room entirely, mix house stereo (board 31/32) only.
    # "Live" = present AND not silent, so an unplugged-but-recorded room mic
    # (reads ~-90 dB) is treated as absent rather than panned hard to one side.
    _win_start = clip[0] if clip else 0.0
    _win_dur = (clip[1] - clip[0]) if clip else None
    live_room = [c for c in ROOM_CHANNELS
                 if c in available_selected
                 and not _channel_is_silent(channels[c], _win_start, _win_dur)]
    dead_room = [c for c in ROOM_CHANNELS
                 if c in selected_ch_list and c not in live_room]
    if dead_room:
        selected_ch_list = [c for c in selected_ch_list if c not in dead_room]
        available_selected = [c for c in available_selected if c not in dead_room]
        log.info(
            'MASTER  room ch%s dead/absent -> %s'
            % (dead_room, 'centring single room mic' if live_room else 'house stereo only'),
            extra={'tui': False})

    effective_pans = dict(SELECTED_PANS)
    if len(live_room) == 1:
        effective_pans[live_room[0]] = 0.0  # single room mic → centre (mono)
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

    from nofun import mastering_meta as _meta
    write_meta = _meta.should_write_metadata(selected_only, clip)

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
            mstats: dict | None = {} if write_meta else None
            result = _mix_channels(channels, ch_list, pan_map, ch_proc, sr, log,
                                   start_sec=start_sec, duration_sec=duration_sec,
                                   ch_processor_map=proc_map if proc_name == 'ott' else None,
                                   align=align,
                                   manual_offsets=manual_offsets,
                                   room_match=room_match,
                                   denoise_room=denoise_room,
                                   room_spread=room_spread,
                                   spread_ms=spread_ms,
                                   stats=mstats)
            if result is None:
                continue
            # Fallback feedback snapshot (only when DyERS is off) — a static long-term spectral
            # read of the pre-master mix. When DyERS runs, we instead report what it actually
            # suppressed per-frame (below), which catches intermittent resonances this misses.
            meta_peaks = (detect_resonant_peaks((result[0] + result[1]) * 0.5, sr,
                                                band=DYERS_BAND, prominence_db=6.0, max_peaks=6)
                          if (write_meta and not dyers) else [])
            if kill_feedback:
                mono_sum = (result[0] + result[1]) * 0.5
                peaks = detect_resonant_peaks(
                    mono_sum, sr, band=FEEDBACK_BAND,
                    prominence_db=FEEDBACK_PROMINENCE_DB, max_peaks=FEEDBACK_MAX)
                if peaks:
                    log.info('FEEDBACK notch: ' + ', '.join(
                        f'{f:.0f}Hz(+{p:.1f}dB)' for f, p in peaks))
                    result = _apply_notches(result[0], result[1], sr,
                                            [f for f, _ in peaks],
                                            q=FEEDBACK_Q, cut_db=FEEDBACK_CUT_DB)
                else:
                    log.info(
                        f'FEEDBACK none found in {FEEDBACK_BAND[0]:.0f}-{FEEDBACK_BAND[1]:.0f}Hz')
            log.info('MASTER  applying bus processor', extra={'tui': False})
            left, right = master_proc(result[0], result[1])
            dyers_report: dict = {}
            if dyers:
                log.info(
                    f'DYERS   dynamic suppress {DYERS_BAND[0]:.0f}-{DYERS_BAND[1]:.0f}Hz',
                    extra={'tui': False})
                left, right = dyers_suppress_stereo(
                    left, right, sr, report=(dyers_report if write_meta else None))
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

            if write_meta and mstats is not None:
                try:
                    levels = mstats.get('levels_db', {})
                    chans = _meta.channel_stats(levels, mstats.get('align_ms', {}))
                    def _avg_rms(chs):  # live channels only — dead ones skew the average
                        vals = [levels[c][0] for c in chs
                                if c in levels and levels[c][0] >= _meta.DEAD_RMS_DB]
                        return sum(vals) / len(vals) if vals else 0.0
                    room_board = {'rms_delta_db': round(_avg_rms(ROOM_CHANNELS) - _avg_rms(BOARD_CHANNELS), 1)}
                    recipe = {
                        'ch_depth': OTT_CH_DEPTH,
                        'room_gain_db': [OTT_ROOM_GAIN_L_DB, OTT_ROOM_GAIN_M_DB, OTT_ROOM_GAIN_H_DB],
                        'board_gain_db': [OTT_BOARD_GAIN_L_DB, OTT_BOARD_GAIN_M_DB, OTT_BOARD_GAIN_H_DB],
                        'master_gain_db': [OTT_MASTER_GAIN_L_DB, OTT_MASTER_GAIN_M_DB, OTT_MASTER_GAIN_H_DB],
                        'denoise_room': denoise_room,
                        'dyers': ({'band': list(DYERS_BAND), 'sensitivity': DYERS_SENSITIVITY,
                                   'sharpness': DYERS_SHARPNESS, 'resonance_db': DYERS_RESONANCE_DB,
                                   'max_peaks': DYERS_MAX_PEAKS} if dyers else None),
                    }
                    if dyers and dyers_report.get('n_frames'):
                        dpk = dyers_report_peaks(dyers_report)  # [(freq, engaged_pct)]
                        feedback = {'source': 'dyers',
                                    'peaks': [{'freq': round(f, 1), 'engaged_pct': p} for f, p in dpk]}
                        peak_freqs = [f for f, _ in dpk]
                    else:
                        feedback = {'source': 'snapshot',
                                    'peaks': [{'freq': round(f, 1), 'prominence_db': round(p, 1)}
                                              for f, p in meta_peaks]}
                        peak_freqs = [f for f, _ in meta_peaks]
                    flags = _meta.derive_flags(chans, room_board, peak_freqs)
                    meta = _meta.build_metadata(base, recipe, chans, room_board, feedback, flags)
                    meta_dir = audio_dest / 'mastering_meta'
                    _meta.write_sidecar(meta, meta_dir, base)
                    _meta.append_log(meta, meta_dir)
                    _meta.log_summary(meta, log)
                except Exception as me:
                    log.warning(f'METADATA  failed for {base}: {me!r}')
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
    parser.add_argument('--ch-depth', type=float, default=None, metavar='0-100',
                        help=f'Per-channel OTT depth / overall effect amount (default {OTT_CH_DEPTH:g})')
    parser.add_argument('--room-depth', type=float, default=None, metavar='0-100',
                        help='Per-channel OTT depth for ROOM ch (29/30); falls back to --ch-depth')
    parser.add_argument('--board-depth', type=float, default=None, metavar='0-100',
                        help='Per-channel OTT depth for BOARD ch (31/32); falls back to --ch-depth')
    parser.add_argument('--master-depth', type=float, default=None, metavar='0-100',
                        help=f'Master-bus OTT depth / overall effect amount (default {OTT_MASTER_DEPTH:g})')
    parser.add_argument('--match-room', choices=['rms', 'peak'], default=None,
                        help='Pre-OTT: gain room ch (29/30) to match board ch (31/32) level (rms or peak)')
    parser.add_argument('--denoise-room', action='store_true',
                        help='Pre-OTT: FFT-denoise (afftdn) room ch (29/30) before any other processing')
    parser.add_argument('--room-spread', action='store_true',
                        help='Widen room ch (29/30) to pseudo-stereo via complementary comb (good for a single room mic)')
    parser.add_argument('--spread-ms', type=float, default=14.0, metavar='MS',
                        help='Comb delay in ms for --room-spread (default 14)')
    parser.add_argument('--room-hpf', type=float, default=None, metavar='HZ',
                        help='v3: high-pass room ch before processing (Hz), so it can be pushed loud cleanly')
    parser.add_argument('--room-parallel-crush', type=float, default=None, metavar='FRAC',
                        help='v3: blend a crushed parallel room bus at FRAC (0-1) for NY-style density')
    parser.add_argument('--room-mix', type=float, default=None, metavar='DB',
                        help=f'v5: room level in the final L/R blend (dB, post-OTT pre-pan); board unchanged (default {ROOM_MIX_GAIN_DB:+g})')
    parser.add_argument('--bus-ms-scoop', type=float, default=None, metavar='DB',
                        help='v3: Mid/Side scoop the final-mix Mid in the vocal band (dB<0) to pocket the board vocal')
    parser.add_argument('--dyers', action='store_true',
                        help='Post-OTT DyERS dynamic resonance suppression (the 35-focused preset)')
    parser.add_argument('--kill-feedback', action='store_true',
                        help='Detect narrow resonances in --feedback-band and notch them per show')
    parser.add_argument('--feedback-band', nargs=2, type=float, default=None, metavar=('LO', 'HI'),
                        help=f'Hz range to scan (default {FEEDBACK_BAND[0]:g}-{FEEDBACK_BAND[1]:g})')
    parser.add_argument('--feedback-prominence', type=float, default=None, metavar='DB',
                        help=f'Min dB above baseline to count as a peak (default {FEEDBACK_PROMINENCE_DB:g})')
    parser.add_argument('--feedback-max', type=int, default=None, metavar='N',
                        help=f'Max notches (default {FEEDBACK_MAX})')
    parser.add_argument('--feedback-cut', type=float, default=None, metavar='DB',
                        help=f'Notch depth dB (default {FEEDBACK_CUT_DB:g})')
    parser.add_argument('--feedback-q', type=float, default=None, metavar='Q',
                        help=f'Notch Q / narrowness (default {FEEDBACK_Q:g})')
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
    gains.add_argument('--room-upwd', type=float, default=None, metavar='PCT',
                       help=f'v4: room ch upward compression strength 0–200, lifts low-level crowd/cheers (script default {OTT_ROOM_UPWD_STRGTH:g})')
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
    if args.room_upwd is not None:
        OTT_ROOM_UPWD_STRGTH = args.room_upwd
    if args.in_gain is not None:
        OTT_MASTER_IN_GAIN_DB = args.in_gain
    if args.out_gain is not None:
        OTT_MASTER_OUT_GAIN_DB = args.out_gain
    if args.ch_depth is not None:
        OTT_CH_DEPTH = args.ch_depth
    if args.room_depth is not None:
        OTT_ROOM_DEPTH = args.room_depth
    if args.board_depth is not None:
        OTT_BOARD_DEPTH = args.board_depth
    if args.master_depth is not None:
        OTT_MASTER_DEPTH = args.master_depth
    if args.room_hpf is not None:
        ROOM_HPF_HZ = args.room_hpf
    if args.room_parallel_crush is not None:
        ROOM_PARALLEL_CRUSH = args.room_parallel_crush
    if args.room_mix is not None:
        ROOM_MIX_GAIN_DB = args.room_mix
    if args.bus_ms_scoop is not None:
        BUS_MS_SCOOP_DB = args.bus_ms_scoop
    if args.feedback_band is not None:
        FEEDBACK_BAND = (args.feedback_band[0], args.feedback_band[1])
    if args.feedback_prominence is not None:
        FEEDBACK_PROMINENCE_DB = args.feedback_prominence
    if args.feedback_max is not None:
        FEEDBACK_MAX = args.feedback_max
    if args.feedback_cut is not None:
        FEEDBACK_CUT_DB = args.feedback_cut
    if args.feedback_q is not None:
        FEEDBACK_Q = args.feedback_q
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

    from nofun.inventory import extract_date_band, perf_key
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
        return perf_key(date, band)

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
            room_match=args.match_room,
            denoise_room=args.denoise_room,
            room_spread=args.room_spread,
            spread_ms=args.spread_ms,
            kill_feedback=args.kill_feedback,
            dyers=args.dyers,
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
                    wavs = sorted([*Path(tmp).glob('*.wav'), *Path(tmp).glob('*.flac')])
                    if not wavs:
                        _log.warning(f'ZIP contains no channel audio: {zip_path.name}')
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
