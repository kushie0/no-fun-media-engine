"""nofun/check_encoding.py — ffprobe codec scan helpers."""

__all__ = [
    'probe_video',
    'is_problematic',
    'scan_encodings',
]

import pathlib
import subprocess
from collections import Counter


def probe_video(path: pathlib.Path) -> tuple[str, str, str]:
    """Return (codec, profile, pix_fmt) for the first video stream."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error',
         '-select_streams', 'v:0',
         '-show_entries', 'stream=codec_name,profile,pix_fmt',
         '-of', 'csv=p=0:s=|', str(path)],
        capture_output=True, text=True,
    )
    parts = result.stdout.strip().split('|')
    while len(parts) < 3:
        parts.append('unknown')
    return parts[0] or 'unknown', parts[1] or 'unknown', parts[2] or 'unknown'


def is_problematic(profile: str, pix_fmt: str) -> bool:
    """Return True if the file will likely fail to play in Windows Media Player."""
    return 'Main 10' in profile or '10le' in pix_fmt or '10be' in pix_fmt


def scan_encodings(
    paths: list[pathlib.Path],
    progress_cb=None,          # optional callable(n, total, path) for live progress
) -> tuple[Counter, list[pathlib.Path], dict[pathlib.Path, tuple[str, str, str]]]:
    """Scan all .mp4 files under *paths*.

    Returns a 3-tuple:
      - summary: Counter keyed by (codec, profile, pix_fmt)
      - bad_files: list of paths with Main10/10-bit issues
      - per_file: dict mapping each path to its (codec, profile, pix_fmt) probe result

    Calls progress_cb(n, total, path) before each file if provided.
    """
    files = [f for p in paths if p.is_dir()
             for f in sorted(p.rglob('*.mp4')) if f.is_file()]
    total    = len(files)
    summary: Counter = Counter()
    bad:     list[pathlib.Path] = []
    per_file: dict[pathlib.Path, tuple[str, str, str]] = {}

    for n, f in enumerate(files, 1):
        if progress_cb:
            progress_cb(n, total, f)
        codec, profile, pix_fmt = probe_video(f)
        summary[(codec, profile, pix_fmt)] += 1
        per_file[f] = (codec, profile, pix_fmt)
        if is_problematic(profile, pix_fmt):
            bad.append(f)

    return summary, bad, per_file
