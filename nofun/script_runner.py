"""nofun/script_runner.py — Execute external scripts with progress tracking.

Equivalent bash:
    python scripts/encode_quads.py --source foo.mov --dest-dir ./videos/ \\
        --encoder hevc_amf --accel d3d11va

Each script is a standalone Python file in scripts/ that:
  - Takes CLI args (--key value)
  - Writes JSON to stdout (structured result)
  - Inherits stderr from the parent (ffmpeg progress goes to ScriptRunner)
  - Exits with a meaningful return code (0=ok, 1=error, 2=input missing)

ScriptRunner parses ffmpeg progress from stderr using the same byte-by-byte
approach as run_ffmpeg(), firing progress_cb for TUI updates.
"""

from __future__ import annotations

__all__ = ['ScriptRunner', 'ScriptJob', 'ScriptResult']

import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# If no stderr bytes arrive for this long, assume the subprocess hung and kill it.
_STALL_TIMEOUT_SEC = 120  # seconds of no output before killing a stalled process
_MAX_ATTEMPTS      = 2    # one restart on stall before giving up

# Same progress regex as media_io.py
_PROGRESS_RE = re.compile(
    r'frame=\s*(\d+)\s+fps=\s*([\d.]+)\s+.*'
    r'time=(\S+)\s+.*speed=\s*(\S+)'
)
_FFMPEG_SUPPRESS = (
    'VBAQ is not supported by cqp Rate Control Method, automatically disabled',
)


@dataclass
class ScriptResult:
    """Structured result from a script execution."""
    script:      str
    exit_code:   int
    stdout_json: dict           # parsed JSON from stdout (empty dict on parse failure)
    stderr_tail: str            # last 2KB of stderr (for error diagnosis)
    elapsed:     float          # wall-clock seconds
    killed:      bool = False   # True if terminated by PAUSE

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.killed


@dataclass
class ScriptJob:
    """One invocation of a script, with all args.

    Equivalent bash:
        python scripts/{script}.py --arg1 val1 --arg2 val2
    """
    script:   str                             # e.g. 'encode_quads'
    args:     dict                            # passed as --key value to the script
    job_id:   str         = ''                # unique ID (auto-generated if empty)
    label:    str         = ''                # human-readable description for logs
    depends:  list[str]   = field(default_factory=list)
    priority: int         = 50                # lower = sooner (within dependency order)

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = f'{self.script}_{uuid.uuid4().hex[:8]}'


class ScriptRunner:
    """Execute a ScriptJob as a subprocess with progress monitoring.

    Designed as a drop-in replacement for the run_ffmpeg() call pattern:
    the caller provides progress_cb and proc_cb callbacks, and the runner
    handles stderr parsing and process lifecycle identically.
    """

    SCRIPTS_DIR = Path(__file__).parent.parent / 'scripts'

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._proc: subprocess.Popen | None = None
        self._tracked_ffmpeg_pid: int | None = None
        self.last_progress: dict[str, str] = {}

    @property
    def process(self) -> subprocess.Popen | None:
        """The currently running subprocess, if any."""
        return self._proc

    def run(
        self,
        job: ScriptJob,
        progress_cb:      Callable[[str, str, str, str], None] | None = None,
        proc_cb:          Callable[[subprocess.Popen], None] | None = None,
        clip_progress_cb: Callable[[int, int], None] | None = None,
    ) -> ScriptResult:
        """Run a script and return its structured result.

        Equivalent bash:
            python scripts/{job.script}.py --key1 val1 --key2 val2

        Progress is parsed from stderr (ffmpeg -stats format).
        Structured output is parsed from stdout as JSON.
        """
        script_path = self.SCRIPTS_DIR / f'{job.script}.py'
        if not script_path.exists():
            self.logger.error(f'Script not found: {script_path}')
            return ScriptResult(
                script=job.script, exit_code=127,
                stdout_json={'error': f'script not found: {script_path}'},
                stderr_tail='', elapsed=0.0,
            )

        cmd = [sys.executable, str(script_path)]
        for k, v in job.args.items():
            cli_key = k.replace('_', '-')
            if isinstance(v, bool):
                if v:
                    cmd.append(f'--{cli_key}')
            else:
                cmd += [f'--{cli_key}', str(v)]

        tag = job.label or job.script
        self.logger.debug(
            f'ScriptRunner: {tag}',
            extra={'tui': False},  # file-only log
        )

        overall_t0 = time.monotonic()
        _MAX_STDERR_TAIL = 2048

        stderr_tail_buf = bytearray()
        killed = False
        raw_stdout = b''
        exit_code = -1

        for attempt in range(_MAX_ATTEMPTS):
            stderr_tail_buf = bytearray()
            killed = False
            _kv: dict[str, str] = {}
            self._tracked_ffmpeg_pid = None
            self.last_progress = {}

            try:
                # bufsize=0: unbuffered binary, same as run_ffmpeg
                with subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                ) as proc:
                    self._proc = proc
                    if proc_cb:
                        proc_cb(proc)

                    assert proc.stderr is not None
                    buf = bytearray()

                    # Background thread feeds stderr bytes into a queue so
                    # the main loop can apply a stall timeout without
                    # blocking on read() forever.
                    _stderr_q: queue.Queue[bytes | None] = queue.Queue()
                    _stderr_pipe = proc.stderr

                    def _read_stderr() -> None:
                        try:
                            while True:
                                b = _stderr_pipe.read(1)
                                _stderr_q.put(b if b else None)
                                if not b:
                                    break
                        except OSError:
                            _stderr_q.put(None)

                    threading.Thread(target=_read_stderr, daemon=True,
                                     name='script-stderr-reader').start()

                    while True:
                        try:
                            byte = _stderr_q.get(timeout=_STALL_TIMEOUT_SEC)
                        except queue.Empty:
                            self.logger.warning(
                                f'{tag}  no output for {_STALL_TIMEOUT_SEC}s'
                                f' — killing stalled process'
                                + ('' if attempt + 1 >= _MAX_ATTEMPTS
                                   else ' (will retry)')
                            )
                            if self._tracked_ffmpeg_pid is not None:
                                try:
                                    _SIG_KILL = getattr(signal, 'SIGKILL', signal.SIGTERM)
                                    os.kill(self._tracked_ffmpeg_pid, _SIG_KILL)
                                except (ProcessLookupError, PermissionError):
                                    pass
                                self._tracked_ffmpeg_pid = None
                            proc.kill()
                            killed = True
                            break
                        if not byte:
                            break

                        # Keep tail for diagnostics
                        stderr_tail_buf.extend(byte)
                        if len(stderr_tail_buf) > _MAX_STDERR_TAIL:
                            stderr_tail_buf = stderr_tail_buf[-_MAX_STDERR_TAIL:]

                        if byte in (b'\r', b'\n'):
                            line = buf.decode('utf-8', errors='replace').rstrip()
                            buf.clear()
                            if not line:
                                continue

                            # -stats format
                            m = _PROGRESS_RE.search(line)
                            if m:
                                frame, fps, tc, speed = m.groups()
                                if progress_cb:
                                    progress_cb(frame, fps, tc, speed)
                                continue

                            # -progress pipe:2 key=value format
                            if '=' in line:
                                k, _, v = line.partition('=')
                                k = k.strip()
                                if k and all(c.isalnum() or c == '_' for c in k):
                                    if k == 'ffmpeg_pid':
                                        try:
                                            self._tracked_ffmpeg_pid = int(v.strip())
                                        except ValueError:
                                            pass
                                        continue
                                    if k == 'clip_progress':
                                        if clip_progress_cb:
                                            try:
                                                a, b = v.strip().split('/')
                                                clip_progress_cb(int(a), int(b))
                                            except (ValueError, TypeError):
                                                pass
                                        continue
                                    _kv[k] = v.strip()
                                    if k == 'progress' and _kv.get('frame'):
                                        tc_raw = _kv.get('out_time', '00:00:00.000000')
                                        tc = tc_raw.split('.')[0] if '.' in tc_raw else tc_raw
                                        self.last_progress = dict(_kv)
                                        self.last_progress['out_time'] = tc
                                        if progress_cb:
                                            progress_cb(
                                                _kv.get('frame', '0'),
                                                _kv.get('fps', '0'),
                                                tc,
                                                _kv.get('speed', '0x'),
                                            )
                                        _kv.clear()
                                    continue

                            # Real message (warning/error) — log it
                            if any(s in line for s in _FFMPEG_SUPPRESS):
                                continue
                            self.logger.warning(f'{tag}  ffmpeg: {line}')
                        else:
                            buf.extend(byte)

                    # Flush remaining
                    if buf:
                        line = buf.decode('utf-8', errors='replace').rstrip()
                        if line and not _PROGRESS_RE.search(line) and not any(s in line for s in _FFMPEG_SUPPRESS):
                            self.logger.warning(f'{tag}  ffmpeg: {line}')

                    # Read stdout before __exit__ closes it
                    assert proc.stdout is not None
                    raw_stdout = proc.stdout.read()
                    proc.wait()
                    exit_code = proc.returncode
                    self._tracked_ffmpeg_pid = None

            except Exception as exc:
                elapsed = time.monotonic() - overall_t0
                self.logger.error(f'ScriptRunner: {tag} crashed: {exc}')
                self._proc = None
                return ScriptResult(
                    script=job.script, exit_code=-1,
                    stdout_json={'error': str(exc)},
                    stderr_tail=stderr_tail_buf.decode('utf-8', errors='replace'),
                    elapsed=elapsed,
                )

            if not killed:
                break  # completed (success or ffmpeg error) — no retry needed

            if attempt + 1 < _MAX_ATTEMPTS:
                self.logger.warning(f'{tag}  retrying (attempt {attempt + 2}/{_MAX_ATTEMPTS})')

        elapsed = time.monotonic() - overall_t0
        self.logger.debug(
            f'ScriptRunner: {tag} done in {elapsed:.1f}s exit={exit_code}',
            extra={'tui': False},
        )

        # Parse JSON from stdout
        stdout_json: dict = {}
        if raw_stdout:
            try:
                stdout_json = json.loads(raw_stdout)
            except (json.JSONDecodeError, ValueError):
                stdout_json = {'raw': raw_stdout.decode('utf-8', errors='replace')}

        self._proc = None

        return ScriptResult(
            script=job.script,
            exit_code=exit_code,
            stdout_json=stdout_json,
            stderr_tail=stderr_tail_buf.decode('utf-8', errors='replace'),
            elapsed=elapsed,
            killed=killed,
        )

    def kill(self) -> None:
        """Kill the running script process (for PAUSE hard-stop).

        Kills the tracked ffmpeg grandchild first, then the script process,
        so no orphaned ffmpeg survives holding a GPU lock or temp-file write lock.
        """
        if self._tracked_ffmpeg_pid is not None:
            try:
                _SIG_KILL = getattr(signal, 'SIGKILL', signal.SIGTERM)
                os.kill(self._tracked_ffmpeg_pid, _SIG_KILL)
            except (ProcessLookupError, PermissionError):
                pass
            self._tracked_ffmpeg_pid = None
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
