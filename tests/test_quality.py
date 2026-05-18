"""tests/test_quality.py — Encoding quality regression tests.

Compares H.264 pipeline outputs against near-lossless MJPEG reference crops
using SSIM (Structural Similarity Index).  Use this to measure quality loss
at each stage of the encode chain and to catch regressions when tuning
encoder settings.

Excluded from the standard test run.  Run explicitly with:
    pytest tests/test_quality.py --quality -v
    pytest -m quality --quality -v

─────────────────────────────────────────────────────────────────────────────
Generating reference crops  (run once per machine, stored in test_files/reference/)
─────────────────────────────────────────────────────────────────────────────
    mkdir -p test_files/reference
    ffmpeg -i "test_files/26-2-7_NoFun_DeadGowns.mov" \\
      -filter_complex "
        [0:v]scale=out_range=limited:in_range=full,format=yuv420p,split=4[v1][v2][v3][v4];
        [v1]crop=iw/2:ih/2:0:0[ul];
        [v2]crop=iw/2:ih/2:iw/2:0[ur];
        [v3]crop=iw/2:ih/2:0:ih/2[ll];
        [v4]crop=iw/2:ih/2:iw/2:ih/2[lr]
      " \\
      -map "[ul]" -c:v mjpeg -q:v 2 test_files/reference/26-2-7_NoFun_DeadGowns_UL.mov \\
      -map "[ur]" -c:v mjpeg -q:v 2 test_files/reference/26-2-7_NoFun_DeadGowns_UR.mov \\
      -map "[ll]" -c:v mjpeg -q:v 2 test_files/reference/26-2-7_NoFun_DeadGowns_LL.mov \\
      -map "[lr]" -c:v mjpeg -q:v 2 test_files/reference/26-2-7_NoFun_DeadGowns_LR.mov

─────────────────────────────────────────────────────────────────────────────
Quality chain being measured
─────────────────────────────────────────────────────────────────────────────

    source .mov (MJPEG, near-lossless reference crop)
        │
        ▼  h264_videotoolbox q:v 82  /  h264_amf qp 18-20  /  libx264 crf 18
    quadrant .mp4  ←  SSIM vs reference  (archival; target ≥ QUAD_SSIM_MIN)
        │
        ▼  h264_videotoolbox q:v 65  /  h264_amf qp 32  /  libx264 crf 23
    clip .mp4      ←  SSIM vs reference  (proxy; target ≥ CLIP_SSIM_MIN)

─────────────────────────────────────────────────────────────────────────────
SSIM interpretation
─────────────────────────────────────────────────────────────────────────────

    1.000        identical
    0.98+        visually transparent
    0.95–0.98    excellent — minor artefacts visible only on close inspection
    0.90–0.95    good — small but noticeable loss
    0.80–0.90    acceptable for low-bitrate proxies
    < 0.80       significant visible degradation

─────────────────────────────────────────────────────────────────────────────
Calibrating thresholds
─────────────────────────────────────────────────────────────────────────────
After your first run the actual scores are printed to stdout.  Adjust
QUAD_SSIM_MIN and CLIP_SSIM_MIN below to document your accepted baseline,
then tighten them whenever you improve encoder settings.
"""

import pathlib
import re
import subprocess

import pytest

REPO    = pathlib.Path(__file__).parent.parent
TF      = REPO / 'test_files'
REF_DIR = TF / 'reference'

pytestmark = pytest.mark.quality

# ── Thresholds ────────────────────────────────────────────────────────────
# Calibrated on:  macOS Darwin 25.4.0 / Apple M-series / h264_videotoolbox
# Date:           2026-03-30
# Encoder:        h264_videotoolbox q:v 82 (quad), q:v 65 (clip)
# Scores:         quad UL=0.4537 UR=0.4315 LL=0.4302 LR=0.5210
#                 clip UL=0.4569 UR=0.4453 LL=0.4409 LR=0.5261
QUAD_SSIM_MIN = 0.41   # source → H.264 quadrant  (archival quality)
CLIP_SSIM_MIN = 0.42   # source → H.264 proxy clip (intentionally lossy)


# ── Skip guard ────────────────────────────────────────────────────────────
skip_no_refs = pytest.mark.skipif(
    not (REF_DIR.is_dir() and any(REF_DIR.glob('*.mov'))),
    reason=(
        f"No reference crops in {REF_DIR.relative_to(REPO)} — "
        "see module docstring for the ffmpeg command to generate them"
    ),
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _ssim(ref: pathlib.Path, test: pathlib.Path) -> float:
    """Return the average SSIM All: score comparing ref against test.

    ffmpeg framesync aligns frames by PTS, so mismatched frame rates are
    handled correctly (e.g. 60 fps reference vs 30 fps proxy clips).
    The final summary line (average over all frames) is returned.
    """
    result = subprocess.run(
        ['ffmpeg', '-i', str(ref), '-i', str(test),
         '-filter_complex', '[0:v][1:v]ssim',
         '-f', 'null', '/dev/null'],
        capture_output=True, text=True,
    )
    matches = re.findall(r'All:([\d.]+)', result.stderr)
    if not matches:
        raise RuntimeError(
            f"Could not parse SSIM score comparing {ref.name} ↔ {test.name}.\n"
            f"ffmpeg stderr (last 600 chars):\n{result.stderr[-600:]}"
        )
    return float(matches[-1])  # last match = average over all frames


# ---------------------------------------------------------------------------
# Stage 1 — source → HEVC quadrant
# ---------------------------------------------------------------------------

@skip_no_refs
def test_quadrant_ssim(video_trial: pathlib.Path) -> None:
    """SSIM between each HEVC quadrant and its near-lossless MJPEG reference crop.

    Measures quality lost in the source → archival quadrant encode step.
    """
    vids_dir = video_trial / 'trial_runs' / 'videos'
    refs = sorted(REF_DIR.glob('*.mov'))
    assert refs, f"No .mov files found in {REF_DIR}"

    results: dict[str, float] = {}
    for ref in refs:
        quad_mp4 = vids_dir / (ref.stem + '.mp4')
        if not quad_mp4.exists():
            pytest.skip(
                f"{quad_mp4.name} not found — run the video trial fixture first"
            )
        results[ref.name] = _ssim(ref, quad_mp4)

    _report("Quadrant", results, QUAD_SSIM_MIN)

    failures = {n: s for n, s in results.items() if s < QUAD_SSIM_MIN}
    assert not failures, (
        f"SSIM below {QUAD_SSIM_MIN} for: "
        + ", ".join(f"{n} ({s:.4f})" for n, s in failures.items())
    )


# ---------------------------------------------------------------------------
# Stage 2 — source → HEVC quadrant → proxy clip  (full chain)
# ---------------------------------------------------------------------------

@skip_no_refs
def test_clip_ssim(video_trial: pathlib.Path) -> None:
    """SSIM between each proxy clip and its MJPEG reference crop.

    Measures cumulative quality loss across the full encode chain:
    source → quadrant → proxy clip.
    """
    clips_dir = video_trial / 'trial_runs' / 'clips'
    refs = sorted(REF_DIR.glob('*.mov'))
    assert refs, f"No .mov files found in {REF_DIR}"

    results: dict[str, float] = {}
    for ref in refs:
        # ref.stem e.g. "26-2-7_NoFun_DeadGowns_UL"
        # strip quad suffix to get the clip subdirectory name
        base = re.sub(r'_(UL|UR|LL|LR)$', '', ref.stem, flags=re.IGNORECASE)
        clip_mp4 = clips_dir / base / (ref.stem + '_1.mp4')
        if not clip_mp4.exists():
            pytest.skip(
                f"{clip_mp4.name} not found — run the video trial fixture first"
            )
        results[ref.name] = _ssim(ref, clip_mp4)

    _report("Clip", results, CLIP_SSIM_MIN)

    failures = {n: s for n, s in results.items() if s < CLIP_SSIM_MIN}
    assert not failures, (
        f"SSIM below {CLIP_SSIM_MIN} for: "
        + ", ".join(f"{n} ({s:.4f})" for n, s in failures.items())
    )


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _report(label: str, results: dict[str, float], threshold: float) -> None:
    """Print a formatted SSIM score table to stdout (visible with -v or -s)."""
    rows = "\n".join(
        f"  {name:<45} {score:.4f}  {'✓' if score >= threshold else f'✗  ← below {threshold}'}"
        for name, score in sorted(results.items())
    )
    print(f"\n{label} SSIM (threshold ≥ {threshold}):\n{rows}\n")
