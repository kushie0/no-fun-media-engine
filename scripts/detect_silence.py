#!/usr/bin/env python3
"""Measure active-signal duration for a batch of WAV files.

Returns seconds-of-signal-above-(-50 dB)-in-runs-of-5+s for each file. The
caller decides whether that's enough to keep. silencedetect answers the
question we actually want — "did this channel carry sustained signal
anywhere?" — without being fooled by faint EMI pickup (which dominates
mean_volume over hour-long files) or by single full-scale clicks (which
dominate max_volume).

Equivalent bash:
    for wav in "$@"; do
        ffmpeg -i "$wav" -af silencedetect=noise=-50dB:d=5 -f null /dev/null 2>&1
        # … parse Duration and silence_duration: lines, subtract.
    done

Process reduction: up to 32 separate ffmpeg calls → 1 script invocation.

Exit codes:
    0 — success (all files probed)
    1 — one or more files failed to probe
    2 — no input files provided

Stdout: JSON {status, results: [{file, active_seconds}]}. active_seconds is
null if detection failed.
Stderr: ffmpeg output (ignored by ScriptRunner for this script).
"""

import argparse
import json
import platform
import re
import subprocess
import sys
from pathlib import Path

_NULL_DEV = 'NUL' if platform.system() == 'Windows' else '/dev/null'


def _active_seconds(wav: Path) -> float | None:
    """Return seconds of audio above -50 dB (in 5 s+ runs)."""
    result = subprocess.run(
        ['ffmpeg', '-i', str(wav), '-af',
         'silencedetect=noise=-50dB:d=5', '-f', 'null', _NULL_DEV],
        capture_output=True, text=True,
    )
    dur_m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', result.stderr)
    if not dur_m:
        return None
    h, m, s = dur_m.groups()
    duration = int(h) * 3600 + int(m) * 60 + float(s)
    silence_total = sum(
        float(d) for d in re.findall(r'silence_duration:\s*([\d.]+)', result.stderr)
    )
    return max(0.0, duration - silence_total)


def main() -> None:
    p = argparse.ArgumentParser(description='Batch active-duration detection.')
    p.add_argument('files', nargs='*', help='WAV files to probe')
    p.add_argument('--file-list', help='Path to a text file with one WAV per line')
    p.add_argument('--dry-run', action='store_true', help='List files, do not probe')
    args = p.parse_args()

    # Collect file list from positional args and/or --file-list
    wav_files: list[Path] = [Path(f) for f in args.files]
    if args.file_list:
        file_list = Path(args.file_list)
        if file_list.exists():
            for line in file_list.read_text().splitlines():
                line = line.strip()
                if line:
                    wav_files.append(Path(line))

    if not wav_files:
        print(json.dumps({'status': 'error', 'reason': 'no files provided', 'results': []}))
        sys.exit(2)

    if args.dry_run:
        print(json.dumps({
            'status':  'dry_run',
            'files':   [str(f) for f in wav_files],
            'count':   len(wav_files),
        }))
        sys.exit(0)

    results = []
    any_failure = False
    for wav in wav_files:
        if not wav.exists():
            results.append({'file': str(wav), 'active_seconds': None, 'error': 'not found'})
            any_failure = True
            continue
        active = _active_seconds(wav)
        results.append({'file': str(wav), 'active_seconds': active})
        if active is None:
            any_failure = True

    print(json.dumps({
        'status':  'ok' if not any_failure else 'partial_error',
        'results': results,
    }))
    sys.exit(1 if any_failure else 0)


if __name__ == '__main__':
    main()
