#!/usr/bin/env python3
"""Survey feedback frequencies straight from the raw room-mic WAVs in the archive.

A lightweight alternative to re-mastering every show: the room mics (physical
channels 29/30) hear the PA/monitors in the room, so monitor/PA howl shows up on
them directly. This walks the audio_archive for the room-channel WAVs that
haven't expired yet (~last 14 days), runs the same narrow-resonance detector the
static feedback notch uses (`detect_resonant_peaks`, a long-window Welch PSD that
flags persistent narrow spikes and ignores broad musical content), and
aggregates the peaks across shows.

No zip extraction, no OTT, no encode — just load → detect → tally. Run it where
the archive is reachable (on prod, against the NAS audio_archive).

Usage:
    python scripts/survey_room_feedback.py <archive_dir> [opts]
    python scripts/survey_room_feedback.py \\\\192.168.0.232\\nofun-archive\\audio_archive \\
        --channels 29,30 --band 250 9000 --per-show
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nofun.mastering import detect_resonant_peaks, _load_mono

_BANDS = [(0, 500), (500, 800), (800, 1200), (1200, 1600), (1600, 2000),
          (2000, 2700), (2700, 4000), (4000, 6000), (6000, 99999)]


def _band_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi if hi < 99999 else '+'}Hz"


def _band_of(f: float) -> str:
    for lo, hi in _BANDS:
        if lo <= f < hi:
            return _band_label(lo, hi)
    return "?"


def _show_of(name: str) -> str:
    """Strip _chanNN.* suffix to recover the performance base name."""
    return re.sub(r'_chan\d+\b.*$', '', name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('archive_dir', help='Folder of raw channel WAVs (audio_archive)')
    ap.add_argument('--channels', default='29,30',
                    help='Room channel numbers to scan (default 29,30)')
    ap.add_argument('--band', nargs=2, type=float, default=[250.0, 9000.0],
                    metavar=('LO', 'HI'), help='Scan band Hz (default 250 9000)')
    ap.add_argument('--prominence', type=float, default=8.0, metavar='DB',
                    help='Min dB above baseline to count a peak (default 8)')
    ap.add_argument('--max-peaks', type=int, default=8,
                    help='Max peaks per file (default 8)')
    ap.add_argument('--max-seconds', type=float, default=3600.0, metavar='S',
                    help='Cap analysed length per file to bound memory (default 3600)')
    ap.add_argument('--dead-rms-db', type=float, default=-65.0, metavar='DB',
                    help='Skip a channel whose RMS is below this (dead mic; default -65)')
    ap.add_argument('--per-show', action='store_true',
                    help='Print each show/channel and its peaks')
    args = ap.parse_args()

    arch = Path(args.archive_dir)
    if not arch.is_dir():
        ap.error(f"not a directory: {arch}")
    chans = [int(c) for c in args.channels.split(',') if c.strip()]
    band = (args.band[0], args.band[1])

    # Collect room-channel WAVs: {show: {chan: path}}
    by_show: dict[str, dict[int, Path]] = collections.defaultdict(dict)
    for ch in chans:
        for w in arch.glob(f'*_chan{ch}.*.wav'):
            by_show[_show_of(w.name)][ch] = w
    if not by_show:
        print(f"No *_chan{{{','.join(map(str, chans))}}}.*.wav found in {arch}")
        return

    all_peaks: list[float] = []          # every peak freq across all files
    dom_per_show: list[float] = []       # the single worst peak per show
    analysed = skipped = 0

    for show in sorted(by_show):
        show_peaks: list[tuple[float, float]] = []
        for ch in chans:
            w = by_show[show].get(ch)
            if not w:
                continue
            try:
                mono, sr = _load_mono(w, duration_sec=args.max_seconds)
            except Exception as e:
                print(f"  LOAD FAIL {w.name}: {e}")
                continue
            rms_db = 20 * np.log10(np.sqrt(np.mean(mono ** 2)) + 1e-12)
            if rms_db < args.dead_rms_db:
                skipped += 1
                if args.per_show:
                    print(f"  {show:38} ch{ch}  dead ({rms_db:.0f} dB) — skip")
                continue
            peaks = detect_resonant_peaks(mono, sr, band=band,
                                          prominence_db=args.prominence,
                                          max_peaks=args.max_peaks)
            analysed += 1
            show_peaks += peaks
            all_peaks += [f for f, _ in peaks]
            if args.per_show:
                pk = ', '.join(f'{f:.0f}Hz(+{p:.1f})' for f, p in peaks) or 'none'
                print(f"  {show:38} ch{ch}  {pk}")
        if show_peaks:
            dom_per_show.append(max(show_peaks, key=lambda t: t[1])[0])

    band_all = collections.Counter(_band_of(f) for f in all_peaks)
    band_dom = collections.Counter(_band_of(f) for f in dom_per_show)
    rounded = collections.Counter(round(f / 50) * 50 for f in all_peaks)

    print(f"\n=== {len(by_show)} shows, {analysed} channels analysed, "
          f"{skipped} dead skipped, {len(all_peaks)} peaks ===")
    print(f"    band {band[0]:.0f}-{band[1]:.0f} Hz, prominence >= {args.prominence:g} dB\n")

    print("ALL PEAKS by band:")
    for lo, hi in _BANDS:
        b = _band_label(lo, hi)
        if band_all.get(b):
            print(f"  {b:14} {band_all[b]:4}  {'#' * band_all[b]}")

    print("\nDOMINANT (worst) room peak per show, by band:")
    for lo, hi in _BANDS:
        b = _band_label(lo, hi)
        if band_dom.get(b):
            print(f"  {b:14} {band_dom[b]:4}  {'#' * band_dom[b]}")

    print("\nTop specific frequencies (rounded 50 Hz):")
    for fr, c in rounded.most_common(15):
        print(f"  {fr:5} Hz  x{c}")


if __name__ == '__main__':
    main()
