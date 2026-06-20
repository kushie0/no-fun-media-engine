"""engine_timing_profile.py — how long engine operations take per 30 min of show.

Parses a `convert_recent.log` and reports, for each heavy per-performance
operation (quad re-encode, clip export, audio split+zip, master, reel), the
median real-work duration and the cost normalised to 30 minutes of performance
footage. Useful for capacity planning and for sizing batch work so it doesn't
swamp a live recording (see the 2026-06-14 recorder-crash incident).

Methodology / why the filters:
  * Each finished job logs `JobQueue: finish <perf> <OP>  (done Xm Ys)`.
  * The log is full of *fast skips/retries* (no_audio, force-reuse, sync-reel
    0.1 s, re-detection loops) that drag the mean to ~0. We therefore keep only
    real work (>= MIN_REAL_SEC) and report the **median** (robust to outliers).
  * Show length comes from `MASTER  processing N ch  (MM.M min)` and the reel
    `… dur MM:SS)` lines; we average them for the per-30-min normalisation.
  * Lanes overlap (GPU: quads/clips/reel; CPU: audio/master), so the per-op
    sum is an upper bound on wall-clock per show, not the wall-clock itself.

Usage:
    python scripts/engine_timing_profile.py [path/to/convert_recent.log]
    # on prod: C:\\Users\\NOFUNadmin\\clips\\convert_recent.log
"""
from __future__ import annotations

import re
import statistics as st
import sys

MIN_REAL_SEC = 30.0   # below this a "finish" is a skip/retry, not real work

# label keyword -> display name + lane (length-proportional heavy ops)
_HEAVY = [
    ('REENCODE', 'REENCODE (4× quad video)', 'GPU'),
    ('CLIPS',    'CLIPS (clip export)',      'GPU'),
    ('AUDIO',    'AUDIO (split+zip channels)', 'CPU'),
    ('REMASTER', 'REMASTER (master → mp3)',  'CPU'),
    ('REEL',     'REEL (instagram encode)',  'GPU/CPU'),
]


def _dur_to_sec(line: str) -> float | None:
    m = re.search(r'\(done\s+(?:(\d+)m\s*)?([\d.]+)s\)', line)
    return (int(m.group(1) or 0)) * 60 + float(m.group(2)) if m else None


def _classify(label: str) -> str | None:
    L = label.upper()
    if 'SYNC' in L:            # sync quads/reel/audio/perfs are light, skip
        return None
    for key, name, _lane in _HEAVY:
        if key in L:
            return name
    return None


def main(path: str) -> int:
    finishes: dict[str, list[float]] = {}
    show_mins: list[float] = []
    mults: list[float] = []
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if 'JobQueue: finish ' in line:
                sec = _dur_to_sec(line)
                m = re.search(r'finish\s+(.*?)\s+\(done', line)
                if sec is not None and m:
                    name = _classify(m.group(1))
                    if name:
                        finishes.setdefault(name, []).append(sec)
            elif 'MASTER  processing' in line:
                m = re.search(r'\(([\d.]+)\s*min\)', line)
                if m:
                    show_mins.append(float(m.group(1)))
            elif ' dur ' in line and 'INSTAGRAM' in line:
                m = re.search(r'dur (\d+):(\d+)\)', line)
                if m:
                    show_mins.append((int(m.group(1)) * 60 + int(m.group(2))) / 60)
            elif 'encoded,' in line:
                m = re.search(r'encoded, ([\d.]+)x', line)
                if m:
                    mults.append(float(m.group(1)))

    if not show_mins:
        print('No show-length signals found — is this a convert_recent.log?')
        return 1
    avg_show = st.mean(show_mins)
    print(f'avg show length: {avg_show:.0f} min  (n={len(show_mins)})'
          + (f'  ·  reel encode ~{st.mean(mults):.1f}x realtime' if mults else ''))
    print(f'\n{"operation":<28}{"real n":>7}{"median":>9}{"per 30-min show":>18}')
    total = 0.0
    for _key, name, _lane in _HEAVY:
        real = [s for s in finishes.get(name, []) if s >= MIN_REAL_SEC]
        if not real:
            print(f'{name:<28}{"(no real work)":>16}')
            continue
        med = st.median(real)
        per30 = med / 60 / avg_show * 30
        total += per30
        print(f'{name:<28}{len(real):>7}{med / 60:>8.1f}m{"~" + format(per30, ".1f") + " min":>18}')
    print(f'\nFULL PIPELINE per 30-min show: ~{total:.1f} min  (~{total / 30:.2f}× show length)')
    print('NOTE: GPU + CPU lanes run concurrently, so wall-clock per show < this sum.')
    return 0


if __name__ == '__main__':
    log = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\NOFUNadmin\clips\convert_recent.log'
    raise SystemExit(main(log))
