#!/usr/bin/env python3
"""Encode a source video into 4 quadrant MP4s (UL, UR, LL, LR).

Equivalent bash:
    ffmpeg -y -hide_banner -loglevel warning -stats \\
        [-hwaccel d3d11va] -i SOURCE.mov \\
        -filter_complex "[0:v]scale=...,split=4[v1][v2][v3][v4]; \\
            [v1]crop=iw/2:ih/2:0:0[ul]; [v2]crop=iw/2:ih/2:iw/2:0[ur]; \\
            [v3]crop=iw/2:ih/2:0:ih/2[ll]; [v4]crop=iw/2:ih/2:iw/2:ih/2[lr]" \\
        -map [ul] -c:v hevc_amf -c:a copy UL_temp.mp4 \\
        -map [ur] -c:v hevc_amf -c:a copy UR_temp.mp4 \\
        -map [ll] -c:v hevc_amf -c:a copy LL_temp.mp4 \\
        -map [lr] -c:v hevc_amf -c:a copy LR_temp.mp4

Exit codes:
    0 — success (4 temp files written)
    1 — ffmpeg error
    2 — input file missing

Stdout: JSON with status, file list, and exit code.
Stderr: ffmpeg progress (parsed by ScriptRunner).

This script is stateless. It does NOT rename temp files to final names,
move/archive source files, or update any database. That is Python's job.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(
        description='Encode source video into 4 quadrant MP4s.',
    )
    p.add_argument('--source',       required=True, help='Input video path')
    p.add_argument('--dest-dir',     required=True, help='Output directory')
    p.add_argument('--base',         required=True, help='Base name (stem)')
    p.add_argument('--accel',        default='none', help='HW accel: d3d11va|videotoolbox|none')
    p.add_argument('--encoder',      required=True, help='Encoder args as JSON list')
    p.add_argument('--filter',       required=True, help='filter_complex string')
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

    # Build ffmpeg command
    quads = [('CAM1', 'ul'), ('CAM2', 'ur'), ('CAM3', 'll'), ('CAM4', 'lr')]
    # Must match nofun/video.py quad_temp_name() — this script stays stdlib-only.
    temps = {q: str(dest_dir / f'{args.base}_{q}_temp.mp4') for q, _ in quads}

    tlim = ['-t', str(args.trial)] if args.trial else []

    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning', '-stats']
    if args.accel != 'none':
        cmd += ['-hwaccel', args.accel]
    cmd += ['-i', str(source), '-filter_complex', args.filter]

    for q, lbl in quads:
        cmd += ['-map', f'[{lbl}]'] + encoder_args + ['-c:a', 'copy'] + tlim
        cmd += [temps[q]]

    if args.dry_run:
        print(json.dumps({
            'status':  'dry_run',
            'command': cmd,
            'files':   list(temps.values()),
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
        'files':     list(temps.values()),
    }
    print(json.dumps(output))
    sys.exit(result_rc)


if __name__ == '__main__':
    main()
