#!/usr/bin/env python3
"""Export proxy clips from quadrant MP4s using per-clip seek-based ffmpeg calls.

Replaces the `-f segment` muxer approach, which stalls consistently with
h264_amf after ~4 segments due to driver/muxer incompatibility.  Each clip
is a separate ffmpeg invocation with `-ss` + `-t`, so encoder state never
crosses segment boundaries.

Each clip is moved to its final destination immediately after it succeeds,
so a crash or kill only loses the clip that was mid-encode.

Exit codes:
    0 — all quadrants exported successfully
    1 — one or more quadrants failed
    2 — no quadrant files found

Stdout: JSON with per-quadrant results (moved_count per quad).
Stderr: ffmpeg progress (parsed by ScriptRunner).
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'json', str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)['format']['duration'])


def _export_quad_clips(
    quad_file: Path,
    temp_dir: Path,
    clips_dir: Path,
    base: str,
    quad: str,
    encoder_args: list[str],
    filter_str: str,
    step: int,
    dry_run: bool,
    start_n: int,
    n_clips: int,
    done_counter: list[int],
    done_lock: threading.Lock,
    total_clips: int,
) -> dict:
    """Export all clips for one quadrant.  Returns a per-quad result dict."""
    moved_count = 0
    any_failure = False
    dry_run_temps: list[str] = []

    for n in range(start_n, n_clips + 1):
        final = clips_dir / f'{base}_{quad}_{n}.mp4'
        if final.exists():
            # Already present — skip (idempotent backfill: only encode the gaps).
            with done_lock:
                done_counter[0] += 1
                done = done_counter[0]
            sys.stderr.write(f'clip_progress={done}/{total_clips}\n')
            sys.stderr.flush()
            continue
        start = (n - 1) * step
        out   = temp_dir / f'{base}_{quad}_temp_{n}.mp4'

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning', '-stats',
            '-ss', str(start), '-t', str(step),
            '-i', str(quad_file),
            '-vf', filter_str,
            '-color_range', '2', '-movflags', '+write_colr+faststart',
        ] + encoder_args + ['-an', str(out)]

        if dry_run:
            dry_run_temps.append(str(out))
            continue

        # Two attempts per clip; brief sleep before retry lets the AMF driver recover.
        # Per-clip timeout guards against ffmpeg hanging silently (h264_amf driver freeze).
        _CLIP_TIMEOUT = 90
        clip_ok = False
        for attempt in range(1, 3):
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
            sys.stderr.write(f'ffmpeg_pid={proc.pid}\n')
            sys.stderr.flush()

            def _relay(src=proc.stderr) -> None:
                try:
                    for chunk in iter(lambda: src.read(256), b''):
                        sys.stderr.buffer.write(chunk)
                        sys.stderr.flush()
                except OSError:
                    pass

            relay = threading.Thread(target=_relay, daemon=True)
            relay.start()
            timed_out = False
            try:
                proc.wait(timeout=_CLIP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                timed_out = True
            relay.join(timeout=2.0)

            if proc.returncode == 0:
                clip_ok = True
                break
            if attempt == 1:
                reason = f'timed out after {_CLIP_TIMEOUT}s' if timed_out else f'rc={proc.returncode}'
                sys.stderr.write(f'clip {n}/{n_clips} failed ({reason}), retrying\n')
                sys.stderr.flush()
                time.sleep(1.0)
        else:
            sys.stderr.write(f'clip {n}/{n_clips} failed after 2 attempts\n')
            sys.stderr.flush()
            any_failure = True

        with done_lock:
            done_counter[0] += 1
            done = done_counter[0]
        sys.stderr.write(f'clip_progress={done}/{total_clips}\n')
        sys.stderr.flush()

        if clip_ok and out.exists():
            final = clips_dir / f'{base}_{quad}_{n}.mp4'
            shutil.move(str(out), str(final))
            moved_count += 1

    if dry_run:
        return {'quad': quad, 'status': 'dry_run', 'temp_files': dry_run_temps}

    status = 'error' if any_failure else 'ok'
    return {'quad': quad, 'status': status, 'moved_count': moved_count}


def main() -> None:
    p = argparse.ArgumentParser(description='Export proxy clips from quadrant MP4s.')
    p.add_argument('--source-dir',     required=True, help='Dir containing quadrant MP4s')
    p.add_argument('--base',           required=True, help='Base name (stem)')
    p.add_argument('--temp-dir',       required=True, help='Dir for temp clip files')
    p.add_argument('--clips-dir',      required=True, help='Final destination dir for clips')
    p.add_argument('--encoder',        required=True, help='Encoder args as JSON list')
    p.add_argument('--filter',         required=True, help='Video filter string')
    p.add_argument('--step',           type=int, default=40, help='Segment duration in seconds')
    p.add_argument('--per-quad-start', default='{}',
                   help='JSON {quad: first_clip_n}; empty = all quads from 1')
    p.add_argument('--dry-run',        action='store_true', help='Print commands, do not execute')
    args = p.parse_args()

    source_dir = Path(args.source_dir)
    temp_dir   = Path(args.temp_dir)
    clips_dir  = Path(args.clips_dir)

    per_quad_start: dict[str, int] = {}
    try:
        per_quad_start = json.loads(args.per_quad_start)
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        encoder_args = json.loads(args.encoder)
    except (json.JSONDecodeError, ValueError):
        encoder_args = args.encoder.split()

    quad_tasks = [
        (source_dir / f'{args.base}_{q}.mp4', q)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4')
        if (source_dir / f'{args.base}_{q}.mp4').exists()
        and (not per_quad_start or q in per_quad_start)
    ]

    any_found = bool(quad_tasks)
    if not any_found:
        print(json.dumps({'status': 'error', 'reason': 'no quadrant files found'}))
        sys.exit(2)

    # Probe all durations upfront so we can report total clip count.
    quad_n_clips: dict[str, int] = {}
    for quad_file, quad in quad_tasks:
        try:
            dur = _probe_duration(quad_file)
            quad_n_clips[quad] = max(1, math.ceil(dur / args.step))
        except Exception:
            quad_n_clips[quad] = 1
    total_clips = sum(
        max(0, quad_n_clips[quad] - per_quad_start.get(quad, 1) + 1)
        for _, quad in quad_tasks
    )
    done_counter = [0]
    done_lock    = threading.Lock()

    quads_processed = []
    any_failure     = False

    # One quad at a time — concurrent h264_amf instances destabilise the AMD driver.
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {
            pool.submit(
                _export_quad_clips,
                quad_file, temp_dir, clips_dir, args.base, quad,
                encoder_args, args.filter, args.step, args.dry_run,
                per_quad_start.get(quad, 1), quad_n_clips[quad],
                done_counter, done_lock, total_clips,
            ): quad
            for quad_file, quad in quad_tasks
        }
        for fut in as_completed(futures):
            result = fut.result()
            quads_processed.append(result)
            if result['status'] == 'error':
                any_failure = True

    overall = 'ok' if not any_failure else 'partial_error'
    if args.dry_run:
        overall = 'dry_run'
    print(json.dumps({
        'status': overall,
        'quads':  quads_processed,
    }))
    sys.exit(1 if any_failure else 0)


if __name__ == '__main__':
    main()
