#!/usr/bin/env python3
"""Transcode a single-camera source video into one MP4 (no quad split).

Sibling of encode_quads.py for the "singles pass-through" path: sources that
are a single camera angle (e.g. NAME.2) get one re-encoded output instead of
four cropped quadrants. SINGLE_FILTER (passed via --filter) is a plain
video-filter chain with no labelled pads, so it is applied with -vf and the
single filtered stream is encoded to one temp file.

Equivalent bash:
    ffmpeg -y -hide_banner -loglevel warning -stats \\
        [-hwaccel d3d11va] -i SOURCE.mov \\
        -vf "scale=...,format=yuv420p" \\
        <encoder args> -c:a copy BASE_single_temp.mp4

Exit codes:
    0 — success (one temp file written)
    1 — ffmpeg error
    2 — input file missing

Stdout: JSON with status, file list, and exit code.
Stderr: ffmpeg progress (parsed by ScriptRunner).

This script is stateless. It does NOT rename the temp file to its final name,
move/archive the source, or update any database. That is Python's job
(nofun/video.py:_transcode_single renames BASE_single_temp.mp4 → BASE.mp4).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(
        description='Transcode a single-camera source video into one MP4.',
    )
    p.add_argument('--source',       required=True, help='Input video path')
    p.add_argument('--dest-dir',     required=True, help='Output directory')
    p.add_argument('--base',         required=True, help='Base name (stem)')
    p.add_argument('--accel',        default='none', help='HW accel: d3d11va|videotoolbox|none')
    p.add_argument('--encoder',      required=True, help='Encoder args as JSON list')
    p.add_argument('--filter',       required=True, help='Video filter chain (-vf)')
    p.add_argument('--trial',        type=int, default=0, help='Limit to N seconds (0=full)')
    p.add_argument('--dry-run',      action='store_true', help='Print command, do not execute')
    args = p.parse_args()

    source = Path(args.source)
    dest_dir = Path(args.dest_dir)

    if not source.exists():
        print(json.dumps({'status': 'error', 'reason': 'input missing', 'source': str(source)}))
        sys.exit(2)

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Parse encoder args from JSON
    try:
        encoder_args = json.loads(args.encoder)
    except (json.JSONDecodeError, ValueError):
        # Fallback: treat as space-separated
        encoder_args = args.encoder.split()

    # Must match nofun/video.py:_transcode_single temp name — stdlib-only.
    temp = str(dest_dir / f'{args.base}_single_temp.mp4')

    tlim = ['-t', str(args.trial)] if args.trial else []

    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning', '-stats']
    if args.accel != 'none':
        cmd += ['-hwaccel', args.accel]
    cmd += ['-i', str(source), '-vf', args.filter]
    cmd += encoder_args + ['-c:a', 'copy'] + tlim
    cmd += [temp]

    if args.dry_run:
        print(json.dumps({
            'status':  'dry_run',
            'command': cmd,
            'files':   [temp],
        }))
        sys.exit(0)

    # Run ffmpeg — emit PID so ScriptRunner can kill the grandchild on stall/PAUSE
    proc = subprocess.Popen(cmd)
    sys.stderr.write(f'ffmpeg_pid={proc.pid}\n')
    sys.stderr.flush()
    proc.wait()
    result_rc = proc.returncode

    output = {
        'status':    'ok' if result_rc == 0 else 'error',
        'exit_code': result_rc,
        'files':     [temp],
    }
    print(json.dumps(output))
    sys.exit(result_rc)


if __name__ == '__main__':
    main()
