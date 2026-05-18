#!/usr/bin/env python3
"""Generate an Instagram Reel from four quadrant MP4s and a FULLSET WAV.

Equivalent bash:
    ffmpeg -y -hide_banner -loglevel warning -progress pipe:2 -nostats \\
        -i UL.mp4 -i UR.mp4 -i LL.mp4 -i LR.mp4 \\
        -i UL.mp4 -i UR.mp4 -i LL.mp4 -i LR.mp4 \\
        -itsoffset 0.200 -i FULLSET.wav \\
        -/filter_complex FILTER_SCRIPT \\
        -map [out] -map 8:a \\
        -pix_fmt yuv420p -c:v libx264 -preset fast -crf 23 \\
        -c:a aac -b:a 192k -shortest [-t TRIAL] \\
        BASE_temp.mp4

Exit codes: 0 = success, 1 = ffmpeg error, 2 = input missing, 3 = probe failed
"""

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile


def _probe(path: pathlib.Path, key: str) -> str:
    """Return a single stream or format field from ffprobe."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error',
         '-select_streams', 'v:0',
         '-show_entries', f'stream={key}:format={key}',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True,
    )
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ''


def _variable_scroll_y(strip_h: int, speed_px_s: float) -> str:
    """Build the crop-filter y expression for the sinusoidal looping scroll.

    Mirrors nofun/reel.py._variable_scroll_y() — keep in sync.
    """
    import math
    scroll_period   = 30.0
    scroll_variance = 0.5
    omega        = 2.0 * math.pi / scroll_period
    B            = scroll_variance * speed_px_s
    B_over_omega = B / omega
    inner = (
        f"{speed_px_s:.6f}*t"
        f"+{B_over_omega:.6f}*(1-cos({omega:.6f}*t))"
    )
    return f"mod({inner}\\,{strip_h})"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quads-dir',    required=True,  help='Directory containing UL/UR/LL/LR mp4 files')
    p.add_argument('--base',         required=True,  help='Stem shared by all quad files (e.g. 26-04-07_PRIZE)')
    p.add_argument('--audio-path',   required=True,  help='Path to the FULLSET WAV')
    p.add_argument('--dest-dir',     required=True,  help='Output directory')
    p.add_argument('--filter-script', default='',    help='Path to pre-written filter_complex script (skips Python filter build)')
    p.add_argument('--delay-ms',     type=float, default=200.0, help='Audio delay ms (default 200)')
    p.add_argument('--trial',        type=int,   default=0,     help='Encode only N seconds (0 = full)')
    p.add_argument('--seek',         type=float, default=0.0,   help='Start position in seconds')
    p.add_argument('--dry-run',      action='store_true', help='Print command without executing')
    args = p.parse_args()

    quads_dir  = pathlib.Path(args.quads_dir)
    dest_dir   = pathlib.Path(args.dest_dir)
    audio_path = pathlib.Path(args.audio_path)
    base       = args.base

    # Locate quad files
    quad_paths: dict[str, pathlib.Path] = {}
    for q in ('UL', 'UR', 'LL', 'LR'):
        candidate = quads_dir / f'{base}_{q}.mp4'
        if not candidate.exists():
            print(json.dumps({'status': 'error', 'reason': f'missing quad {q}', 'path': str(candidate)}))
            sys.exit(2)
        quad_paths[q] = candidate

    if not audio_path.exists():
        print(json.dumps({'status': 'error', 'reason': 'FULLSET WAV missing', 'path': str(audio_path)}))
        sys.exit(2)

    # Probe dimensions from UL quad
    ref = quad_paths['UL']
    try:
        w   = int(_probe(ref, 'width') or '0')
        h   = int(_probe(ref, 'height') or '0')
        dur = float(_probe(ref, 'duration') or '0')
    except (ValueError, TypeError):
        print(json.dumps({'status': 'error', 'reason': 'probe failed', 'path': str(ref)}))
        sys.exit(3)

    if not w or not h:
        print(json.dumps({'status': 'error', 'reason': 'could not probe dimensions', 'path': str(ref)}))
        sys.exit(3)

    # Build filter if no pre-written script supplied
    filt_path_to_delete: pathlib.Path | None = None
    if args.filter_script:
        filt_path = pathlib.Path(args.filter_script)
    else:
        secs_per_quad = 20.0
        strip_h  = h * 4
        out_h    = min(strip_h, int(w * 16 / 9) // 2 * 2)
        speed    = h / secs_per_quad
        y_expr   = _variable_scroll_y(strip_h, speed)
        top_in   = ''.join(f'[{i}:v]' for i in range(4))
        bot_in   = ''.join(f'[{i}:v]' for i in range(4, 8))
        filt = (
            f'{top_in}vstack=4[top];'
            f'{bot_in}vstack=4[bot];'
            f'[top][bot]vstack[loop];'
            f'[loop]crop=w={w}:h={out_h}:x=0:y={y_expr}[out]'
        )
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        filt_path = pathlib.Path(tmp.name)
        filt_path_to_delete = filt_path
        tmp.write(filt)
        tmp.close()

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / f'{base}_reel.mp4'
        temp     = dest_dir / f'{base}_reel_temp.mp4'

        seek_args = ['-ss', str(args.seek)] if args.seek else []
        quads_list = [quad_paths[q] for q in ('UL', 'UR', 'LL', 'LR')]

        cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
               '-progress', 'pipe:2', '-nostats']
        for q in quads_list:
            cmd += seek_args + ['-i', str(q)]
        for q in quads_list:
            cmd += seek_args + ['-i', str(q)]
        if args.delay_ms:
            cmd += ['-itsoffset', str(args.delay_ms / 1000.0)]
        cmd += seek_args + ['-i', str(audio_path)]
        cmd += [f'-/filter_complex', str(filt_path)]
        cmd += ['-map', '[out]', '-map', '8:a']
        cmd += ['-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'fast', '-crf', '23']
        cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
        if args.trial:
            cmd += ['-t', str(args.trial)]
        cmd += [str(temp)]

        if args.dry_run:
            print(json.dumps({
                'status':     'dry_run',
                'command':    cmd,
                'dimensions': {'w': w, 'h': h, 'duration': dur},
                'out_path':   str(out_path),
            }))
            return

        proc = subprocess.Popen(cmd)
        sys.stderr.write(f'ffmpeg_pid={proc.pid}\n')
        sys.stderr.flush()
        proc.wait()
        result_rc = proc.returncode
        # Heartbeat: write to stderr so the ScriptRunner stall-timer resets
        # after the ffmpeg encode finishes and before the file-rename step.
        sys.stderr.write('encode_done=1\n')
        sys.stderr.flush()
        if result_rc != 0:
            temp.unlink(missing_ok=True)
            print(json.dumps({'status': 'error', 'exit_code': result_rc}))
            sys.exit(1)

        temp.replace(out_path)
        sz = out_path.stat().st_size
        print(json.dumps({
            'status':   'ok',
            'out_path': str(out_path),
            'size':     sz,
            'dimensions': {'w': w, 'h': h, 'duration': dur},
        }))

    finally:
        if filt_path_to_delete:
            filt_path_to_delete.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
