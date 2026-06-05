"""nofun/multiplex.py — content-based video layout detection (1×1 vs 2×2).

Routes a raw camera source to the right encode path by detecting montage seams
rather than relying on folder/naming.  A stitched montage (2×2, side-by-side, or
stacked) has a hard pixel discontinuity at its internal seam(s) that a single
continuous camera frame does not.  We sample a few frames, measure the
normalised discontinuity at the exact W/2 and H/2 midlines, and classify.

Pure-ffmpeg: each sample frame is scaled to a small grayscale grid and piped as
raw bytes; the seam math is plain-Python (no numpy/PIL).
"""

from __future__ import annotations

import enum
import logging
import subprocess
from pathlib import Path
from statistics import median
from typing import Callable

from nofun.media_io import probe_format

__all__ = ['Layout', 'detect_layout', 'route_by_layout']

_GRID       = 160   # even square; the midline falls on the (mid-1, mid) boundary
_N_FRAMES   = 5
_SEAM_RATIO = 3.0   # midline jump must be >= this x the mean adjacent jump → seam
_FLAT_RATIO = 1.8   # below this → no seam on that axis


class Layout(enum.Enum):
    SINGLE_1x1   = '1x1'     # one continuous camera
    QUAD_2x2     = '2x2'     # four cameras (vertical + horizontal seam)
    SIDE_BY_SIDE = '2x1'     # two cameras left|right (vertical seam only)
    STACKED      = '1x2'     # two cameras top/bottom (horizontal seam only)
    UNKNOWN      = 'unknown'  # ambiguous → caller keeps the conservative default


def _sample_gray(path: Path, ts: float, grid: int) -> 'bytes | None':
    """Grab one frame at *ts*, scaled to grid×grid grayscale, as raw bytes.

    ``flags=neighbor`` keeps a montage seam a sharp 1-px jump instead of
    smearing it across the midline.  Returns ``grid*grid`` bytes, or ``None`` on
    any ffmpeg/IO failure or a short read.
    """
    cmd = [
        'ffmpeg', '-v', 'error', '-ss', str(ts), '-i', str(path),
        '-frames:v', '1',
        '-vf', f'scale={grid}:{grid}:flags=neighbor,format=gray',
        '-f', 'rawvideo', '-',
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if len(out) < grid * grid:
        return None
    return out[:grid * grid]


def _axis_ratios(buf: bytes, grid: int) -> 'tuple[float, float]':
    """Return (vertical_ratio, horizontal_ratio) for one grayscale frame.

    ``buf`` is row-major: pixel (row r, col c) is ``buf[r*grid + c]``.  Each
    ratio is the discontinuity at the exact midline divided by the mean
    adjacent-pixel discontinuity along that axis — a seam spikes its ratio.
    """
    mid = grid // 2  # boundary sits between index mid-1 and mid

    # Vertical seam: columns mid-1 vs mid, averaged over rows; baseline = mean
    # adjacent-column difference across the whole frame.
    seam_v = 0
    base_v_sum = 0
    for r in range(grid):
        row = r * grid
        seam_v += abs(buf[row + mid] - buf[row + mid - 1])
        acc = 0
        for c in range(1, grid):
            acc += abs(buf[row + c] - buf[row + c - 1])
        base_v_sum += acc / (grid - 1)
    seam_v /= grid
    base_v = base_v_sum / grid
    ratio_v = seam_v / base_v if base_v > 0 else 0.0

    # Horizontal seam: rows mid-1 vs mid, averaged over cols; baseline = mean
    # adjacent-row difference across the whole frame.
    seam_h = 0
    for c in range(grid):
        seam_h += abs(buf[mid * grid + c] - buf[(mid - 1) * grid + c])
    seam_h /= grid
    base_h_sum = 0
    for r in range(1, grid):
        row = r * grid
        prev = (r - 1) * grid
        acc = 0
        for c in range(grid):
            acc += abs(buf[row + c] - buf[prev + c])
        base_h_sum += acc / grid
    base_h = base_h_sum / (grid - 1)
    ratio_h = seam_h / base_h if base_h > 0 else 0.0

    return ratio_v, ratio_h


def detect_layout(
    path: Path,
    *,
    n_frames: int = _N_FRAMES,
    grid: int = _GRID,
    logger: 'logging.Logger | None' = None,
) -> Layout:
    """Classify the camera layout of a video by seam discontinuity.

    Samples *n_frames* across the middle 80% of the clip and takes the median
    per-axis ratio (robust to a transient black/blank frame).  Returns UNKNOWN
    on any probe/sample failure so the caller can keep its conservative default.
    """
    try:
        dur = float(probe_format(path, 'duration') or 0.0)
    except (ValueError, TypeError):
        dur = 0.0
    if dur <= 0:
        return Layout.UNKNOWN

    if n_frames <= 1:
        timestamps = [dur / 2]
    else:
        lo, hi = 0.1 * dur, 0.9 * dur
        step = (hi - lo) / (n_frames - 1)
        timestamps = [lo + i * step for i in range(n_frames)]

    vrs: list[float] = []
    hrs: list[float] = []
    for ts in timestamps:
        buf = _sample_gray(path, ts, grid)
        if buf is None:
            continue
        rv, rh = _axis_ratios(buf, grid)
        vrs.append(rv)
        hrs.append(rh)

    if not vrs:
        return Layout.UNKNOWN

    v, h = median(vrs), median(hrs)
    v_seam, h_seam = v >= _SEAM_RATIO, h >= _SEAM_RATIO
    v_flat, h_flat = v < _FLAT_RATIO, h < _FLAT_RATIO

    if v_seam and h_seam:
        layout = Layout.QUAD_2x2
    elif v_seam and h_flat:
        layout = Layout.SIDE_BY_SIDE
    elif h_seam and v_flat:
        layout = Layout.STACKED
    elif v_flat and h_flat:
        layout = Layout.SINGLE_1x1
    else:
        layout = Layout.UNKNOWN

    if logger is not None:
        logger.debug(
            f'LAYOUT  {Path(path).name}: v={v:.2f} h={h:.2f} → {layout.value}'
        )
    return layout


def route_by_layout(
    perf_mov: 'dict[str, list[Path]]',
    perf_singles: 'dict[str, list[Path]]',
    detect: Callable[[Path], Layout],
    logger: 'logging.Logger | None' = None,
) -> None:
    """Divert main-path movs that auto-detect as non-quad into *perf_singles*.

    QUAD and UNKNOWN movs stay in *perf_mov* (status-quo quad split — most
    sources are 2×2, so ambiguity defaults to the safe existing behaviour).
    Confident SINGLE/SIDE/STACKED detections move to *perf_singles* so they're
    transcoded whole instead of cropped into four garbage cameras.  SIDE/STACKED
    have no split filter yet, so they transcode whole with a warning.  Entries
    already in *perf_singles* (the ``Singles/`` folder override) are untouched.
    """
    for key in list(perf_mov):
        keep: list[Path] = []
        for mov in perf_mov[key]:
            layout = detect(mov)
            if layout in (Layout.SINGLE_1x1, Layout.SIDE_BY_SIDE, Layout.STACKED):
                if layout is not Layout.SINGLE_1x1 and logger is not None:
                    logger.warning(
                        f'LAYOUT  {mov.name}: detected {layout.value} — '
                        f'no split filter yet, transcoding whole'
                    )
                perf_singles[key].append(mov)
            else:
                keep.append(mov)
        if keep:
            perf_mov[key] = keep
        else:
            del perf_mov[key]
