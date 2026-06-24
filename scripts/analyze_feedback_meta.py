#!/usr/bin/env python3
"""Aggregate feedback-peak data from mastering metadata sidecars.

Reads the per-master JSON sidecars written by nofun/mastering.py (one per
performance, under <audio_dest>/mastering_meta/) and summarises which
frequencies show up most often as feedback across a set of shows.

Two sources of peaks appear in the sidecars:
  - "dyers"    : frequencies DyERS dynamically ducked, with engaged_pct
  - "snapshot" : a static spectral read, with prominence_db
Both carry a `freq`; this tool aggregates on frequency regardless of source.

IMPORTANT — detection-band confound: a sidecar only contains peaks inside the
DyERS band in effect when it was rendered (recipe.dyers.band). If older shows
were rendered with a higher floor (e.g. 1500 Hz), their sub-floor feedback is
absent from the data, NOT absent from the show. The summary prints the set of
bands seen so you can spot this.

Usage:
    python scripts/analyze_feedback_meta.py <meta_dir> [--after 26-06-08]
    python scripts/analyze_feedback_meta.py D:\\tmp\\feedback_survey\\mastering_meta --after 26-06-08
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

# Proportional-ish bands chosen to separate low-mid howl from sibilant ring.
_BANDS = [(0, 500), (500, 800), (800, 1200), (1200, 1600), (1600, 2000),
          (2000, 2700), (2700, 4000), (4000, 6000), (6000, 99999)]


def _band_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi if hi < 99999 else '+'}Hz"


def _band_of(f: float) -> str:
    for lo, hi in _BANDS:
        if lo <= f < hi:
            return _band_label(lo, hi)
    return "?"


def _show_date(name: str) -> str | None:
    m = re.match(r'(\d\d-\d\d-\d\d)_', name)
    return m.group(1) if m else None


def load_shows(meta_dir: Path, after: str | None):
    shows = []
    for jf in sorted(meta_dir.glob('*.json')):
        date = _show_date(jf.stem)
        if date is None:
            continue
        if after and date < after:
            continue
        try:
            d = json.loads(jf.read_text())
        except Exception as e:
            print(f"PARSE FAIL {jf.name}: {e}")
            continue
        fb = d.get('feedback', {}) or {}
        peaks = fb.get('peaks', []) or []
        dom = None
        for fl in d.get('flags', []):
            mm = re.match(r'feedback:(\d+)Hz', fl)
            if mm:
                dom = int(mm.group(1))
                break
        shows.append({
            'name': jf.stem, 'date': date,
            'src': fb.get('source'),
            'sha': d.get('pipeline_sha'),
            'band': (d.get('recipe', {}).get('dyers') or {}).get('band'),
            'dom': dom,
            'freqs': [p['freq'] for p in peaks if p.get('freq')],
        })
    return shows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('meta_dir', help='Directory of *.json mastering sidecars')
    ap.add_argument('--after', default=None, metavar='YY-MM-DD',
                    help='Only include shows with date >= this (e.g. 26-06-08)')
    args = ap.parse_args()

    meta_dir = Path(args.meta_dir)
    if not meta_dir.is_dir():
        ap.error(f"not a directory: {meta_dir}")

    shows = load_shows(meta_dir, args.after)
    if not shows:
        print("No matching shows found.")
        return

    all_freqs = [f for s in shows for f in s['freqs']]
    band_all = collections.Counter(_band_of(f) for f in all_freqs)
    band_dom = collections.Counter(_band_of(s['dom']) for s in shows if s['dom'])
    rounded = collections.Counter(round(f / 50) * 50 for f in all_freqs)
    bands_seen = collections.Counter(str(s['band']) for s in shows)

    n = len(shows)
    print(f"=== {n} shows"
          + (f" on/after {args.after}" if args.after else "")
          + f", {len(all_freqs)} feedback peaks ===\n")

    print("ALL PEAKS by band (count across shows):")
    for lo, hi in _BANDS:
        b = _band_label(lo, hi)
        if band_all.get(b):
            print(f"  {b:14} {band_all[b]:4}  {'#' * band_all[b]}")

    print("\nDOMINANT (worst) peak per show, by band:")
    for lo, hi in _BANDS:
        b = _band_label(lo, hi)
        if band_dom.get(b):
            print(f"  {b:14} {band_dom[b]:4}  {'#' * band_dom[b]}")

    print("\nTop specific frequencies (rounded 50 Hz, all peaks):")
    for fr, c in rounded.most_common(15):
        print(f"  {fr:5} Hz  x{c}")

    print("\nDyERS band in effect at render (detection-band confound check):")
    for b, c in bands_seen.most_common():
        print(f"  {b}: {c} shows")


if __name__ == '__main__':
    main()
