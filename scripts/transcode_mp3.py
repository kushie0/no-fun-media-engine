#!/usr/bin/env python3
"""Transcode a WAV file to MP3 using libmp3lame.

Equivalent bash:
    ffmpeg -y -hide_banner -loglevel error \\
        -i SOURCE.wav \\
        -c:a libmp3lame -b:a 128k -q:a 2 \\
        DEST.mp3

Exit codes: 0 = success, 1 = ffmpeg error, 2 = input missing
"""

import argparse
import json
import pathlib
import subprocess
import sys


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',   required=True,            help='Input WAV path')
    p.add_argument('--dest',     required=True,            help='Output MP3 path')
    p.add_argument('--bitrate',  default='128k',           help='CBR bitrate (default 128k)')
    p.add_argument('--dry-run',  action='store_true',      help='Print command without executing')
    args = p.parse_args()

    source = pathlib.Path(args.source)
    dest   = pathlib.Path(args.dest)

    if not source.exists():
        print(json.dumps({'status': 'error', 'reason': 'source WAV missing', 'path': str(source)}))
        sys.exit(2)

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-progress', 'pipe:2', '-nostats',
        '-i', str(source),
        '-c:a', 'libmp3lame', '-b:a', args.bitrate, '-q:a', '2',
        str(dest),
    ]

    if args.dry_run:
        print(json.dumps({'status': 'dry_run', 'command': cmd}))
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd)
    sys.stderr.write(f'ffmpeg_pid={proc.pid}\n')
    sys.stderr.flush()
    proc.wait()
    result_rc = proc.returncode
    # Heartbeat: reset ScriptRunner stall-timer before post-encode file ops.
    sys.stderr.write('encode_done=1\n')
    sys.stderr.flush()
    if result_rc != 0:
        dest.unlink(missing_ok=True)
        print(json.dumps({'status': 'error', 'exit_code': result_rc}))
        sys.exit(1)

    sz = dest.stat().st_size
    print(json.dumps({'status': 'ok', 'out_path': str(dest), 'size': sz}))


if __name__ == '__main__':
    main()
