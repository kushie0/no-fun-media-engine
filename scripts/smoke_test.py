#!/usr/bin/env python3
"""scripts/smoke_test.py — one-shot smoke run: clean -> stage -> wait -> analyze -> clean.

Single orchestration of the synthetic ``01-01-01_SMOKETEST`` delete-rebuild loop.
The engine must already be running; this script only moves files and watches disk:

  1. clean    wipe any prior SMOKETEST outputs so the run starts from zero
  2. stage    copy the trimmed raw .mov + 32 chan WAVs from the source build dir
              into VenueLighting, where the running engine detects + rebuilds them
  3. wait     tail the engine log until it reports all completion markers (quads +
              AUDIO.mp3 + reel + clips) or --timeout elapses
  4. analyze  probe outputs + SSIM + log timing -> one JSON record (smoke_quality)
  5. clean    wipe the outputs again; the engine drops its own now-stale DB entry
              on the next SCAN (media_engine._heal_stale_smoke_entries), so the
              next run rebuilds instead of skipping on presence

"wait" reads completion from the engine's own log rather than polling the NAS:
the log is on local disk and is always visible, whereas the NAS UNC paths are
invisible to an SSH-spawned child (the SMB session does not propagate). Tailing
the log also waits for the real finish events, so a busy MANUAL queue (a real-perf
REMASTER can hold the lane ~50 min) no longer false-times-out against a fixed poll
deadline. Pass the engine log via --log; analyze still reads the rebuilt files, so
run it in a session that can see the NAS (console, or after `net use`).

Run on prod, e.g.:
  net use \\\\192.168.0.232\\nofun-archive <pw> /user:alex
  python scripts/smoke_test.py --log C:/Users/NOFUNadmin/clips/convert_recent.log
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.smoke_quality import cmd_analyze  # noqa: E402

PERF          = '01-01-01_SMOKETEST'
EXPECTED_CLIPS = 12   # 120s / STEP_SECONDS=40 -> 3 per CAM x 4 CAMs


def clean(perf: str, *, nas: pathlib.Path, dmirror: pathlib.Path,
          clips: pathlib.Path, venue: pathlib.Path,
          onedrive: pathlib.Path) -> int:
    """Delete every artifact a smoke run can leave. Mirrors smoke_cleanup.ps1.

    Wipes both storage tiers: the NAS deliverable dirs AND their D: down-mirror
    copies (videos/audio/*_archive). The DB records quad paths on whichever tier
    the engine last wrote, so leaving the D: mirror behind means the heal sees the
    DB-referenced outputs still present and skips on presence instead of rebuilding.

    Leaves the source build dir untouched (it is never under any glob below).
    """
    globs = [
        (nas / 'videos',         f'{perf}*'),
        (nas / 'audio',          f'{perf}*'),
        (nas / 'video_archive',  f'{perf}*'),
        (nas / 'audio_archive',  f'{perf}*'),
        (dmirror / 'videos',         f'{perf}*'),
        (dmirror / 'audio',          f'{perf}*'),
        (dmirror / 'video_archive',  f'{perf}*'),
        (dmirror / 'audio_archive',  f'{perf}*'),
        (venue,                  f'{perf}*'),
        (venue / 'Audio',        f'{perf}*'),
        (clips,                  f'{perf}*'),
        (onedrive,               f'{perf}*'),
    ]
    removed = 0
    for root, pat in globs:
        if not root.is_dir():
            continue
        for p in root.glob(pat):
            print(f'  DEL  {p}')
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            removed += 1
    print(f'clean: {removed} item(s) removed')
    return removed


def stage(perf: str, *, source: pathlib.Path, venue: pathlib.Path) -> int:
    """Copy the trimmed raw .mov + chan WAVs from source build into VenueLighting.

    Two-phase to make the whole channel set appear atomically. The engine probes
    silence the moment it detects WAVs; if it sees a partial set (a slow copy still
    landing the active channels) it marks the perf ``audio_all_silent`` on the silent
    majority and drains the late-arriving active channels. So we copy every file to a
    ``.part`` sidecar first — the engine's watch only matches ``.mov``/``.wav``, so
    sidecars are invisible — then rename them all into their real names in one tight
    loop. Renames are metadata-only on the same volume, so the full set lands inside a
    single detection tick.
    """
    venue.mkdir(parents=True, exist_ok=True)
    srcs = sorted(p for p in source.glob(f'{perf}*')
                  if p.is_file() and p.suffix.lower() in ('.mov', '.wav'))
    if not srcs:
        print(f'stage: no source files matching {perf}* in {source}', file=sys.stderr)
        return 0
    parts = []
    for s in srcs:
        part = venue / (s.name + '.part')
        print(f'  STAGE {s.name}')
        shutil.copy2(s, part)
        parts.append((part, venue / s.name))
    for part, dst in parts:
        os.replace(part, dst)
    print(f'stage: {len(srcs)} file(s) copied into {venue}')
    return len(srcs)


def _markers_for(perf: str) -> dict[str, str]:
    """Log substrings that, once all present, prove the rebuild finished.

    The engine labels queue jobs ``<short-date> <band> <KIND>`` and writes the
    audio master as ``<perf>_AUDIO.mp3``; matching those exact shapes ties each
    marker to this perf's success (a REMASTER that fails logs ``no audio master
    produced`` instead, so it never trips the audio marker).
    """
    date_part, _, band = perf.partition('_')
    return {
        'quads': f'finish {date_part} {band} REENCODE',
        'audio': f'{perf}_AUDIO.mp3',
        'reel':  f'finish {date_part} {band} REEL',
        'clips': f'finish {date_part} {band} CLIPS',
    }


def _seen_markers(text: str, perf: str) -> set[str]:
    return {name for name, sub in _markers_for(perf).items() if sub in text}


def _terminal_failure(text: str, perf: str) -> bool:
    """True if the engine logged a non-retryable mastering failure for *perf*."""
    return any('no audio master produced' in ln and perf in ln
               for ln in text.splitlines())


def _read_log_tail(log_path: pathlib.Path, offset: int) -> bytes:
    """Return log bytes appended since *offset*; whole file if it rotated smaller."""
    size = log_path.stat().st_size
    start = offset if size >= offset else 0
    with log_path.open('rb') as f:
        f.seek(start)
        return f.read()


def wait_for_outputs(perf: str, *, log_path: pathlib.Path | None, log_offset: int,
                     timeout: float, poll: float) -> bool:
    """Tail the engine log until all completion markers for *perf* appear.

    Reads completion from the engine's own log (local disk, always visible) instead
    of polling the NAS (invisible to an SSH child). Waiting on the real finish
    events also absorbs arbitrary queue latency, so a busy MANUAL lane no longer
    false-times-out.
    """
    if log_path is None or not log_path.is_file():
        print('wait: --log is required for completion detection', file=sys.stderr)
        return False
    want = set(_markers_for(perf))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _read_log_tail(log_path, log_offset).decode('utf-8', 'replace')
        seen = _seen_markers(text, perf)
        if seen >= want:
            print('wait: all completion markers seen in log')
            return True
        if _terminal_failure(text, perf):
            print(f'wait: engine logged terminal mastering failure for {perf}',
                  file=sys.stderr)
            return False
        print(f'  wait: {" ".join(sorted(seen)) or "(none)"}  [{len(seen)}/{len(want)}]')
        time.sleep(poll)
    print('wait: TIMEOUT — engine did not finish in time', file=sys.stderr)
    return False


def _slice_log(log_path: pathlib.Path | None, offset: int) -> str | None:
    """Write the log bytes appended since *offset* to a temp file; return its path.

    Isolates one run's lines so parse_timeline doesn't anchor on a prior run.
    """
    if log_path is None or not log_path.is_file():
        return None
    from scripts.smoke_quality import RUNS_DIR
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / '_run.log'
    out.write_bytes(_read_log_tail(log_path, offset))
    return str(out)


def main(argv: list[str] | None = None) -> int:
    home = pathlib.Path.home()
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--perf', default=PERF)
    p.add_argument('--source',   default='D:/smoke_build',
                   help='build dir holding the trimmed raw .mov + chan WAVs')
    p.add_argument('--nas',      default='//192.168.0.232/nofun-archive',
                   help='NAS root (holds videos/, audio/, *_archive/)')
    p.add_argument('--dmirror',  default='D:/',
                   help='D: down-mirror root (holds videos/, audio/, *_archive/)')
    p.add_argument('--clips',    default='C:/clips')
    p.add_argument('--venue',    default=str(home / 'VenueLighting'))
    p.add_argument('--onedrive',
                   default=str(home / 'OneDrive - No Fun Troy LLC' / 'Multitracks'))
    p.add_argument('--reference', default='D:/smoke_build/reference',
                   help='reference-crop dir for SSIM (skip if absent)')
    p.add_argument('--log', help='engine log — gates completion + parses run timing')
    p.add_argument('--nas-user', default='nofunadmin',
                   help='SMB user for analyze NAS auth (SSH sessions can\'t see NAS otherwise)')
    p.add_argument('--nas-pass', default=None,
                   help='SMB password; falls back to $NOFUN_NAS_PASS')
    p.add_argument('--timeout', type=float, default=3600.0,
                   help='ceiling for queue latency; smoke shares the live MANUAL lane')
    p.add_argument('--poll',    type=float, default=10.0)
    p.add_argument('--keep', action='store_true',
                   help='skip the final cleanup (leave outputs for inspection)')
    args = p.parse_args(argv)

    nas      = pathlib.Path(args.nas)
    dmirror  = pathlib.Path(args.dmirror)
    videos   = nas / 'videos'
    audio    = nas / 'audio'
    clips    = pathlib.Path(args.clips)
    venue    = pathlib.Path(args.venue)
    source   = pathlib.Path(args.source)
    onedrive = pathlib.Path(args.onedrive)
    ref      = pathlib.Path(args.reference) if args.reference else None

    print('== clean (pre) ==')
    clean(args.perf, nas=nas, dmirror=dmirror, clips=clips, venue=venue,
          onedrive=onedrive)

    # Capture the log's size now so we can slice out only THIS run's lines for
    # timing: convert_recent.log is a rolling 48h log full of prior DETECTED
    # lines, and parse_timeline anchors on the first one it sees.
    log_path   = pathlib.Path(args.log) if args.log else None
    log_offset = log_path.stat().st_size if log_path and log_path.is_file() else 0

    print('== stage ==')
    if not stage(args.perf, source=source, venue=venue):
        return 2

    print('== wait ==')
    ok = wait_for_outputs(args.perf, log_path=log_path, log_offset=log_offset,
                          timeout=args.timeout, poll=args.poll)

    run_log = _slice_log(log_path, log_offset)

    print('== analyze ==')
    analyze_args = argparse.Namespace(
        perf=args.perf, videos=str(videos), clips=str(clips), audio=str(audio),
        reference=str(ref) if ref and ref.is_dir() else None, log=run_log,
        nas_user=args.nas_user, nas_pass=args.nas_pass)
    rc = cmd_analyze(analyze_args)

    if args.keep:
        print('== clean (post) skipped (--keep) ==')
    else:
        print('== clean (post) ==')
        clean(args.perf, nas=nas, dmirror=dmirror, clips=clips, venue=venue,
              onedrive=onedrive)
        print('engine will drop the stale smoke DB entry on its next SCAN.')

    return 0 if (ok and rc == 0) else 1


if __name__ == '__main__':
    raise SystemExit(main())
