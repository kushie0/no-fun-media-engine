#!/usr/bin/env python3
"""Split a multichannel WAV into per-channel mono WAVs.

Equivalent bash:
    CHANNELS=$(ffprobe -v error -select_streams a:0 \\
        -show_entries stream=channels -of csv=p=0 INPUT.wav)
    ffmpeg -y -hide_banner -loglevel warning -stats \\
        [-t TRIAL] -i INPUT.wav \\
        -filter_complex "[0:a]asplit=${CHANNELS}[a0][a1]...; \\
            [a0]pan=mono|c0=c0[c0]; [a1]pan=mono|c0=c1[c1]; ..." \\
        -map [c0] -c:a pcm_s24le "${BASE}_ch01.wav" \\
        -map [c1] -c:a pcm_s24le "${BASE}_ch02.wav" ...

Exit codes:
    0 — success (all channels written)
    1 — ffmpeg error
    2 — input file missing
    3 — no channels to split (mono input)

Stdout: JSON with channel count, output file list, exit code.
Stderr: ffmpeg progress (parsed by ScriptRunner).

This script does NOT delete the original WAV, drop silent channels,
or archive anything. That is Python's job.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _probe_channels(source: Path) -> int | None:
    """Return channel count for the first audio stream, or None."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=channels', '-of', 'csv=p=0',
         str(source)],
        capture_output=True, text=True,
    )
    val = result.stdout.strip()
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(description='Split multichannel WAV into mono channels.')
    p.add_argument('--source',   required=True, help='Input WAV path')
    p.add_argument('--dest-dir', required=True, help='Output directory for channel files')
    p.add_argument('--base',     required=True, help='Base name (stem)')
    p.add_argument('--trial',    type=int, default=0, help='Limit to N seconds (0=full)')
    p.add_argument('--dry-run',  action='store_true', help='Print command, do not execute')
    args = p.parse_args()

    source = Path(args.source)
    dest_dir = Path(args.dest_dir)

    if not source.exists():
        print(json.dumps({'status': 'error', 'reason': 'input missing', 'source': str(source)}))
        sys.exit(2)

    num_ch = _probe_channels(source)
    if num_ch is None or num_ch <= 1:
        print(json.dumps({
            'status': 'skip', 'reason': 'mono or unreadable',
            'channels': num_ch, 'source': str(source),
        }))
        sys.exit(3)

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Build asplit + pan filter chain
    labels = ''.join(f'[a{i}]' for i in range(num_ch))
    filt = f'[0:a]asplit={num_ch}{labels}'
    maps: list[str] = []
    output_files: list[str] = []
    for ch in range(num_ch):
        pad = f'{ch + 1:02d}'
        filt += f';[a{ch}]pan=mono|c0=c{ch}[c{ch}]'
        # Must match nofun/audio.py chan_wav_name() — this script stays stdlib-only.
        out_path = str(dest_dir / f'{args.base}_ch{pad}.wav')
        maps += ['-map', f'[c{ch}]', '-c:a', 'pcm_s24le', out_path]
        output_files.append(out_path)

    tlim = ['-t', str(args.trial)] if args.trial else []
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning', '-stats']
    cmd += tlim + ['-i', str(source), '-filter_complex', filt] + maps

    if args.dry_run:
        print(json.dumps({
            'status':   'dry_run',
            'command':  cmd,
            'channels': num_ch,
            'files':    output_files,
        }))
        sys.exit(0)

    proc = subprocess.Popen(cmd)
    sys.stderr.write(f'ffmpeg_pid={proc.pid}\n')
    sys.stderr.flush()
    proc.wait()
    result_rc = proc.returncode

    print(json.dumps({
        'status':    'ok' if result_rc == 0 else 'error',
        'exit_code': result_rc,
        'channels':  num_ch,
        'files':     output_files,
    }))
    sys.exit(result_rc)


if __name__ == '__main__':
    main()
