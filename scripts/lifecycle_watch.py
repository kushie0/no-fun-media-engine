#!/usr/bin/env python3
"""scripts/lifecycle_watch.py — a quiet lifecycle log for the engine + streams.

Polls the process table every --interval seconds and appends ONE line to the
lifecycle log only when something *transitions*: the engine launching or closing
(tagged graceful vs crash) and stream workers coming up or going down. Silence
means nothing changed — the log stays empty until an actual event.

Two sources, each answering a question the other can't:
  - process table (psutil): liveness. Robust and wording-independent, and it's
    the only thing that sees a *silent death* — a crash/kill/tmux-drop where the
    engine never gets to log "exiting".
  - engine log markers: PAUSE/RESUME. Pause is internal engine state, invisible
    in the process table, so it's mirrored from the marker the engine already
    writes. (--engine-log; omit to skip pause-mirroring + graceful tagging.)

Run it as a long-lived process (scheduled task at logon, or a tmux window):
  uv run python scripts/lifecycle_watch.py \
      --log C:/Users/NOFUNadmin/clips/lifecycle.log \
      --engine-log C:/Users/NOFUNadmin/clips/convert_recent.log
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import time

import psutil

ENGINE_MARK = 'media_engine.py'
STREAM_MARKS = ('mpegts', 'pipe:1')

# Engine log substrings that mark a *clean* shutdown — their presence at the tail
# distinguishes a graceful close from a crash.
GRACEFUL_MARKS = ('exiting',)
# Pause/resume transitions the engine already writes. Matched on the specific
# wording so routine "Already paused" / "Could not move" noise is ignored.
PAUSE_MARKS = ('Paused at safe point', 'Hard stop complete')
RESUME_MARKS = ('Continuing processing',)


def engine_running(procs: list[tuple[str, list[str]]]) -> bool:
    """True if any process is the media engine.

    Matched as a *python interpreter* running media_engine.py — not merely a
    process whose argv mentions the string (a grep, ssh, or editor would). The
    argv element must end with the script name so a shell that quotes the whole
    command (which contains the path as a substring) doesn't false-positive.
    """
    return any(
        name.lower().startswith('python')
        and any(part.endswith(ENGINE_MARK) for part in cl)
        for name, cl in procs
    )


def stream_count(procs: list[tuple[str, list[str]]]) -> int:
    """Number of live stream-worker ffmpeg processes."""
    return sum(
        name.lower().startswith('ffmpeg') and all(m in cl for m in STREAM_MARKS)
        for name, cl in procs
    )


def diff_state(prev: tuple[bool, int] | None,
               cur: tuple[bool, int]) -> list[str]:
    """Edge-trigger: events for the change from *prev* to *cur* state.

    State is (engine_up, stream_count). On the first sample prev is None and we
    emit only what's already running, so the log opens with a baseline.
    """
    events: list[str] = []
    p_eng, p_str = (False, 0) if prev is None else prev
    c_eng, c_str = cur

    if c_eng and not p_eng:
        events.append('ENGINE LAUNCH')
    elif p_eng and not c_eng:
        events.append('ENGINE CLOSE')

    if c_str and not p_str:
        events.append(f'STREAMS UP ({c_str})')
    elif p_str and not c_str:
        events.append('STREAMS DOWN')
    elif p_str and c_str and p_str != c_str:
        events.append(f'STREAMS {p_str}->{c_str}')

    return events


def pause_events(new_text: str) -> list[str]:
    """ENGINE PAUSE / ENGINE RESUME events from newly-appended engine-log text."""
    events: list[str] = []
    for line in new_text.splitlines():
        if any(m in line for m in PAUSE_MARKS):
            events.append('ENGINE PAUSE')
        elif any(m in line for m in RESUME_MARKS):
            events.append('ENGINE RESUME')
    return events


def is_graceful_close(log_tail: str) -> bool:
    """True if the engine log's last few lines show a clean exit marker."""
    tail = '\n'.join(log_tail.splitlines()[-5:])
    return any(m in tail for m in GRACEFUL_MARKS)


def _procs() -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            name = proc.info['name'] or ''
            cl = proc.info['cmdline']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if cl:
            out.append((name, cl))
    return out


def _read_new(path: pathlib.Path | None, offset: int) -> tuple[str, int]:
    """Read bytes appended to *path* since *offset*; return (text, new_offset)."""
    if path is None or not path.is_file():
        return '', offset
    size = path.stat().st_size
    if size < offset:  # log rotated/truncated — restart from the top
        offset = 0
    with path.open('rb') as fh:
        fh.seek(offset)
        data = fh.read()
    return data.decode('utf-8', 'replace'), size


def _emit(log: pathlib.Path, msg: str) -> None:
    stamp = _dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    line = f'[{stamp}] {msg}'
    print(line, flush=True)
    with log.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def watch(log: pathlib.Path, engine_log: pathlib.Path | None,
          interval: float) -> None:
    prev: tuple[bool, int] | None = None
    offset = engine_log.stat().st_size if engine_log and engine_log.is_file() else 0
    while True:
        procs = _procs()
        cur = (engine_running(procs), stream_count(procs))
        new_text, offset = _read_new(engine_log, offset)

        for ev in diff_state(prev, cur):
            if ev == 'ENGINE CLOSE' and engine_log is not None:
                tail, _ = _read_new(engine_log, max(0, offset - 8192))
                ev += ' (graceful)' if is_graceful_close(tail) else ' (no clean exit — possible crash)'
            _emit(log, ev)
        for ev in pause_events(new_text):
            _emit(log, ev)

        prev = cur
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--log', required=True, help='lifecycle log to append transitions to')
    p.add_argument('--engine-log', help='engine convert_recent.log — for pause + graceful tagging')
    p.add_argument('--interval', type=float, default=5.0, help='poll seconds (default 5)')
    args = p.parse_args(argv)

    log = pathlib.Path(args.log)
    engine_log = pathlib.Path(args.engine_log) if args.engine_log else None
    _emit(log, f'WATCH START  (interval={args.interval:g}s)')
    try:
        watch(log, engine_log, args.interval)
    except KeyboardInterrupt:
        _emit(log, 'WATCH STOP')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
