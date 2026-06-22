"""nofun/media_io.py — ANSI logging, ffmpeg wrappers, DeleteQueue, and stdin input."""

__all__ = [
    'setup_logging',
    'app_version',
    'ColorFormatter',
    'FileDetailFormatter',
    'run_ffmpeg',
    'probe_stream',
    'probe_format',
    'fmt_size',
    'format_eta',
    'compute_ffmpeg_eta',
    'get_open_processes',
    'is_file_locked',
    'is_cloud_only',
    'rename_cloud_file',
    'dehydrate_cloud_files',
    'DeleteQueue',
]

import logging
import os
import pathlib
import queue
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from nofun.log_handlers import RollingRecentHandler, RemoteRotatingHandler
from nofun.paths import is_windows, is_windows_native


# ---------------------------------------------------------------------------
# ANSI colour codes
# ---------------------------------------------------------------------------
# Work in Windows Terminal, VS Code terminal, macOS Terminal, and any
# xterm-compatible emulator.  Legacy conhost.exe needs os.system('') once
# to enable VT processing — done in setup_logging() on Windows.

_R      = '\033[0m'   # reset
_DIM    = '\033[2m'   # dimmed / muted
_B      = '\033[1m'   # bold
_RED    = '\033[31m'
_GREEN  = '\033[32m'
_YELLOW = '\033[33m'
_CYAN   = '\033[36m'

# Maps the first word/phrase of a log message to a colour.
# Order matters: more specific prefixes must appear before shorter ones.
_KEYWORD_COLORS: dict[str, str] = {
    'CREATE':     _GREEN,
    'MOVE':       _CYAN,
    'DELETE':     _B + _RED,
    'DETECTED':   _GREEN,
    'REMOVED':    _YELLOW,
    'RENAME':     _CYAN,
    'SPLITTING':  _DIM,
    'ENCODING':   _DIM,
    'ZIPPING':    _DIM,
    'PENDING':    _YELLOW,
    'NOTICE':     _YELLOW,
    'ERROR':      _B + _RED,
    'SKIP':       _DIM,
    'LOCKED':     _YELLOW,
    'REEL':       _CYAN,
    'MASTER':     _CYAN,
    'SHARE':      _GREEN,
    'ALIGN':      _DIM,
    'LOAD':       _DIM,
    'WRITE':      _DIM,
    'PAUSE':      _B + _YELLOW,
    'RESUME':     _GREEN,
    'Encoding':   _DIM,
    'Audio:':     _DIM,
    'Inventory:': _DIM,
    'Batch':      _DIM,
    'Pipeline':   _DIM,
    'Scheduled':  _DIM,
    'Deleted':    _DIM,
}

# Matches the four useful fields in an ffmpeg progress stats line, e.g.:
#   frame= 1847 fps= 62 q=-0.0 size=56832kB time=00:30:47.32 speed=2.11x
_PROGRESS_RE = re.compile(
    r'frame=\s*(\d+)'       # frame count
    r'.*?fps=\s*([\d.]+)'   # frames per second
    r'.*?time=([\d:]+)'     # encode position HH:MM:SS
    r'.*?speed=\s*(\S+)'    # speed multiplier e.g. "2.11x"
)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    """logging.Formatter that adds ANSI colour to console output.

    The [timestamp] bracket is dimmed so it recedes visually.
    The first keyword of the message body is coloured via _KEYWORD_COLORS.
    Applied only to the StreamHandler — the FileHandler stays plain text.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)

        # Dim the [timestamp] bracket
        if msg.startswith('['):
            close = msg.find(']')
            if close != -1:
                ts   = msg[:close + 1]
                body = msg[close + 1:]   # includes the leading space
                msg  = f"{_DIM}{ts}{_R}{body}"

        # Colour the first keyword in the message body
        raw = record.getMessage()
        for keyword, colour in _KEYWORD_COLORS.items():
            if raw.startswith(keyword):
                idx = msg.find(keyword)
                if idx != -1:
                    msg = msg[:idx] + colour + keyword + _R + msg[idx + len(keyword):]
                break

        return msg


class FileDetailFormatter(logging.Formatter):
    """Plain-text formatter for file handlers.

    If the LogRecord has any of the recognised detail attributes
    (set via extra={} on the log call), appends them on indented
    continuation lines — e.g.:

        [26-03-07T14:22:01] MOVE    foo.mov → archive/
                                     |  src=C:\\VenueLighting\\foo.mov
                                     |  dst=D:\\video_archive\\foo.mov  size=8721563648 (8.1 GB)
    """

    _DETAIL_KEYS = ('src', 'dst', 'path', 'size', 'mtime', 'codec', 'accel',
                    'channels', 'trial', 'detail')

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts = []
        for key in self._DETAIL_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                parts.append(f'{key}={val}')
        if not parts:
            return base
        indent = ' ' * 31   # align with message body after [ts] prefix
        extra  = f'\n{indent}|  ' + f'\n{indent}|  '.join(parts)
        return base + extra


def app_version(repo_dir: pathlib.Path | None = None) -> str:
    """Return 'branch@shortsha (YYYY-MM-DD HH:MM)' for the running checkout.

    Reads git directly so the value reflects the on-disk source — useful when
    a production install drifts from what main looks like. Returns
    'unknown' if git is unavailable (zip install, no .git, etc.). Result is
    cached at module level since the running process can't change branches.
    """
    global _APP_VERSION_CACHE
    if _APP_VERSION_CACHE is not None:
        return _APP_VERSION_CACHE
    cwd = repo_dir or pathlib.Path(__file__).parent.parent
    try:
        run = lambda *a: subprocess.run(
            ['git', '-C', str(cwd), *a],
            capture_output=True, text=True, timeout=2,
        )
        branch = run('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
        sha    = run('rev-parse', '--short', 'HEAD').stdout.strip()
        when   = run('show', '-s', '--format=%cs %ch', 'HEAD').stdout.strip()
        if not (branch and sha):
            _APP_VERSION_CACHE = 'unknown'
        else:
            _APP_VERSION_CACHE = f'{branch}@{sha} ({when})' if when else f'{branch}@{sha}'
    except (OSError, subprocess.TimeoutExpired):
        _APP_VERSION_CACHE = 'unknown'
    return _APP_VERSION_CACHE


_APP_VERSION_CACHE: str | None = None


def setup_logging(
    local_log:      pathlib.Path,
    remote_log_dir: pathlib.Path | None = None,
) -> logging.Logger:
    """Set up three handlers: console (INFO/colour), local rolling (DEBUG), remote rotating (DEBUG)."""
    fmt  = '[%(asctime)s] %(message)s'
    dfmt = '%y-%m-%dT%H:%M:%S'

    logger = logging.getLogger('media_engine')
    logger.setLevel(logging.DEBUG)
    # Remove any handlers added by previous calls (e.g. in tests)
    logger.handlers.clear()

    # Enable ANSI on legacy Windows conhost.exe; no-op on Windows Terminal / macOS
    if is_windows_native():
        os.system('')

    # 1. Console handler: INFO+, coloured output (in batch/batch mode; replaced by
    #    TextualLogHandler in watchdog/TUI mode).
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(ColorFormatter(fmt, dfmt))
    logger.addHandler(stream)

    # 2. Local rolling file: DEBUG+, plain text + detail lines, 48-hour window
    local_log.parent.mkdir(parents=True, exist_ok=True)
    local_fh = RollingRecentHandler(local_log)
    local_fh.setLevel(logging.DEBUG)
    local_fh.setFormatter(FileDetailFormatter(fmt, dfmt))
    logger.addHandler(local_fh)

    # 3. Remote rotating file: DEBUG+, plain text + detail lines, 800 KB rotation
    if remote_log_dir is not None:
        try:
            remote_fh = RemoteRotatingHandler(remote_log_dir)
            remote_fh.setLevel(logging.DEBUG)
            remote_fh.setFormatter(FileDetailFormatter(fmt, dfmt))
            logger.addHandler(remote_fh)
        except OSError as e:
            logger.warning(f"NOTICE  Remote log dir unavailable ({e}); logging locally only")

    return logger


# ---------------------------------------------------------------------------
# FFmpeg / FFprobe wrappers
# ---------------------------------------------------------------------------

# Benign encoder messages that clutter the log without signalling a real issue.
_FFMPEG_SUPPRESS = (
    'VBAQ is not supported by cqp Rate Control Method, automatically disabled',
)


def run_ffmpeg(
    args: list[str],
    logger: logging.Logger,
    label: str = '',
    progress_cb: Callable[[str, str, str, str], None] | None = None,
    proc_cb: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """Run ffmpeg with a live single-line progress display.

    Supports two progress sources — whichever is present in the ffmpeg args:

    * ``-stats``          — single \r-terminated line per update; parsed with
                            _PROGRESS_RE (frame/fps/time/speed on one line).
    * ``-progress pipe:2``— newline-terminated key=value blocks written to
                            stderr regardless of -loglevel.  Accumulates
                            frame/fps/out_time/speed and fires progress_cb
                            (or batch display) when the ``progress=`` marker
                            line appears.

    Both formats call ``progress_cb(frame, fps, tc, speed)`` if provided,
    otherwise write an in-place \\r\\033[2K line to stdout (batch mode).

    Returns ffmpeg exit code.
    """
    cmd = ['ffmpeg'] + args
    progress_active = False
    # Accumulator for -progress pipe:2 key=value blocks
    _kv: dict[str, str] = {}

    def _clear() -> None:
        sys.stdout.write('\r\033[2K')
        sys.stdout.flush()

    def _fire_progress(frame: str, fps: str, tc: str, speed: str) -> None:
        nonlocal progress_active
        if progress_cb:
            progress_cb(frame, fps, tc, speed)
        else:
            prefix = f'{_DIM}{label}  {_R}' if label else ''
            display = (
                f'  ◎  {prefix}'
                f'frame {_CYAN}{frame:>6}{_R}  '
                f'fps {fps:>5}  '
                f'{tc}  '
                f'{_YELLOW}{speed}{_R}'
            )
            sys.stdout.write('\r\033[2K' + display)
            sys.stdout.flush()
            progress_active = True

    # bufsize=0: unbuffered binary — required to read \r-terminated progress
    # lines before ffmpeg emits a \n.  text=True must NOT be set here.
    with subprocess.Popen(cmd, stderr=subprocess.PIPE, bufsize=0) as proc:
        if proc_cb:
            proc_cb(proc)
        assert proc.stderr is not None
        buf = bytearray()

        while True:
            byte = proc.stderr.read(1)
            if not byte:
                break

            if byte == b'\r' or byte == b'\n':
                line = buf.decode('utf-8', errors='replace').rstrip()
                buf.clear()

                if not line:
                    continue

                # -stats format: frame/fps/time/speed on one \r-terminated line
                m = _PROGRESS_RE.search(line)
                if m:
                    frame, fps, tc, speed = m.groups()
                    _fire_progress(frame, fps, tc, speed)
                    continue

                if byte == b'\n':
                    # -progress pipe:2 key=value line (no spaces in key name)
                    if '=' in line:
                        k, _, v = line.partition('=')
                        k = k.strip()
                        if k and all(c.isalnum() or c == '_' for c in k):
                            _kv[k] = v.strip()
                            if k == 'progress' and _kv.get('frame'):
                                # Full block received — fire callback
                                tc_raw = _kv.get('out_time', '00:00:00.000000')
                                tc = tc_raw.split('.')[0] if '.' in tc_raw else tc_raw
                                _fire_progress(
                                    _kv.get('frame', '0'),
                                    _kv.get('fps', '0'),
                                    tc,
                                    _kv.get('speed', '0x'),
                                )
                                _kv.clear()
                            continue  # don't echo key=value lines as messages

                # Real message (warning, error, etc.) — show in TUI and file
                # log.  Handled for both \n-terminated and Windows \r\n-
                # terminated lines (\r arrives first with the content; the
                # subsequent \n sees an empty buffer and is skipped above).
                if any(s in line for s in _FFMPEG_SUPPRESS):
                    continue
                if progress_active:
                    _clear()
                    progress_active = False
                tag = f'{label}  ' if label else ''
                logger.warning(f'{tag}ffmpeg: {line}')

            else:
                buf.extend(byte)

        # Flush any remaining bytes (last line sometimes lacks a terminator)
        if buf:
            line = buf.decode('utf-8', errors='replace').rstrip()
            if line and not _PROGRESS_RE.search(line) and not any(s in line for s in _FFMPEG_SUPPRESS):
                if progress_active:
                    _clear()
                    progress_active = False
                tag = f'{label}  ' if label else ''
                logger.warning(f'{tag}ffmpeg: {line}')

    if progress_active:
        _clear()

    return proc.wait()


def probe_stream(filepath: pathlib.Path, entry: str,
                 stream: str = 'v:0') -> str:
    """Extract a single stream metadata value via ffprobe. Returns '' on failure."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', stream,
         '-show_entries', f'stream={entry}', '-of', 'csv=p=0', str(filepath)],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def probe_format(filepath: pathlib.Path, entry: str) -> str:
    """Extract a format-level metadata value via ffprobe. Returns '' on failure."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', f'format={entry}',
         '-of', 'csv=p=0', str(filepath)],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def probe_total_frames(filepath: pathlib.Path, duration_s: float) -> int | None:
    """Estimate total frames as duration × source fps. Returns None when fps
    can't be probed or duration is zero. One ffprobe call."""
    if not duration_s or duration_s <= 0:
        return None
    fps_str = probe_stream(filepath, 'r_frame_rate')
    if not fps_str:
        return None
    try:
        num, den = fps_str.split('/') if '/' in fps_str else (fps_str, '1')
        src_fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return None
    return int(duration_s * src_fps) if src_fps > 0 else None


# ---------------------------------------------------------------------------
# Size formatter
# ---------------------------------------------------------------------------

def fmt_size(b: int) -> str:
    """Format byte count as human-readable GB or MB string."""
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    return f"{b / 1_048_576:.0f} MB"


# ---------------------------------------------------------------------------
# ETA formatting (ffmpeg + ZIP progress)
# ---------------------------------------------------------------------------

def format_eta(seconds: float) -> str:
    """Format a non-negative duration as 'eta 5m 23s' or 'eta 12s'.

    Returns '' for non-positive, sub-second, or non-finite input.
    """
    if not seconds or seconds <= 0 or seconds == float('inf'):
        return ''
    if seconds < 1.0:
        return ''
    s = int(round(seconds))
    if s < 60:
        return f'eta {s}s'
    return f'eta {s // 60}m {s % 60:02d}s'


def _parse_tc_seconds(tc: str) -> float:
    """Parse 'HH:MM:SS' or 'HH:MM:SS.fff' to seconds. Returns 0.0 on failure."""
    try:
        parts = tc.split(':')
        if len(parts) != 3:
            return 0.0
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


def _parse_speed(speed: str) -> float:
    """Parse ffmpeg speed like '2.11x' to float. Returns 0.0 on failure."""
    try:
        return float(speed.rstrip('xX'))
    except (ValueError, AttributeError):
        return 0.0


def compute_ffmpeg_eta(tc: str, speed: str, duration: float | None) -> str:
    """Compute 'eta Xm Ys' for an in-progress ffmpeg encode.

    Returns '' when duration is unknown, speed is zero/unparseable, or
    the encode is within 1 s of finishing.
    """
    if not duration or duration <= 0:
        return ''
    sp = _parse_speed(speed)
    if sp <= 0:
        return ''
    encoded = _parse_tc_seconds(tc)
    remaining_source = max(0.0, duration - encoded)
    return format_eta(remaining_source / sp)


# ---------------------------------------------------------------------------
# File-lock detection
# ---------------------------------------------------------------------------

def get_open_processes(path: pathlib.Path) -> list[str]:
    """Return the display names of processes that currently have *path* open.

    Uses the Windows Restart Manager API (rstrtmgr.dll), which enumerates
    open file handles regardless of the share flags the writer used.
    This is reliable for recording software like TouchDesigner that may open
    files with FILE_SHARE_READ|WRITE, which defeats a simple CreateFileW probe.

    Returns an empty list on non-Windows platforms or if the API call fails.
    Callers should treat an empty list as "no process holds the file open".
    """
    if not is_windows():
        return []
    if not path.exists():
        return []

    import ctypes
    import ctypes.wintypes as _wt

    _rm = ctypes.WinDLL('rstrtmgr')  # type: ignore[attr-defined]

    _CCH_RM_SESSION_KEY  = 32
    _CCH_RM_MAX_APP_NAME = 255
    _CCH_RM_MAX_SVC_NAME = 63

    class _RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [
            ('dwProcessId',      _wt.DWORD),
            ('ProcessStartTime', _wt.FILETIME),
        ]

    class _RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ('Process',             _RM_UNIQUE_PROCESS),
            ('strAppName',          ctypes.c_wchar * (_CCH_RM_MAX_APP_NAME + 1)),
            ('strServiceShortName', ctypes.c_wchar * (_CCH_RM_MAX_SVC_NAME + 1)),
            ('ApplicationType',     ctypes.c_int),
            ('AppStatus',           _wt.ULONG),
            ('TSSessionId',         _wt.DWORD),
            ('bRestartable',        _wt.BOOL),
        ]

    session_key    = (ctypes.c_wchar * (_CCH_RM_SESSION_KEY + 1))()
    session_handle = _wt.DWORD()

    if _rm.RmStartSession(ctypes.byref(session_handle), 0, session_key) != 0:
        return []

    try:
        files = (ctypes.c_wchar_p * 1)(str(path))
        if _rm.RmRegisterResources(
            session_handle, 1, files, 0, None, 0, None
        ) != 0:
            return []

        n_needed = ctypes.c_uint(0)
        n_info   = ctypes.c_uint(0)
        reboot   = _wt.DWORD(0)

        # First call: get the number of processes that have the file open
        _rm.RmGetList(
            session_handle,
            ctypes.byref(n_needed), ctypes.byref(n_info),
            None, ctypes.byref(reboot),
        )

        count = n_needed.value
        if count == 0:
            return []

        info_arr  = (_RM_PROCESS_INFO * count)()
        n_info    = ctypes.c_uint(count)

        # Second call: fill the process info array
        if _rm.RmGetList(
            session_handle,
            ctypes.byref(n_needed), ctypes.byref(n_info),
            info_arr, ctypes.byref(reboot),
        ) != 0:
            return []

        return [info_arr[i].strAppName for i in range(n_info.value)]
    finally:
        _rm.RmEndSession(session_handle)


def is_file_locked(path: pathlib.Path) -> bool:
    """Return True if *path* is locked (open exclusively) by another process.

    Uses CreateFileW on Windows/Git Bash and fcntl.flock on Unix/macOS.
    Returns False for missing files (treat as unlocked; watcher will skip).

    NOTE: This approach only detects *exclusive* locks.  Recording software
    like TouchDesigner may open files with FILE_SHARE_READ|WRITE, in which
    case CreateFileW succeeds even while the file is being written.
    Use get_open_processes() for reliable detection on Windows.
    """
    if not path.exists():
        return False
    if is_windows():
        import ctypes
        GENERIC_READ        = 0x80000000
        FILE_SHARE_NONE     = 0x00000000
        OPEN_EXISTING       = 3
        INVALID_HANDLE      = ctypes.c_void_p(-1).value
        handle = ctypes.windll.kernel32.CreateFileW(  # type: ignore[attr-defined]
            str(path), GENERIC_READ, FILE_SHARE_NONE,
            None, OPEN_EXISTING, 0, None,
        )
        if handle == INVALID_HANDLE:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return False
    else:
        import fcntl
        try:
            with path.open('rb') as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except OSError:
            return True


# ---------------------------------------------------------------------------
# OneDrive / Cloud Files helpers
# ---------------------------------------------------------------------------

# Windows FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS — set by OneDrive on cloud-only
# placeholders.  Checking this attribute does NOT trigger rehydration; only
# opening the file for reading does.
_CLOUD_RECALL_ATTR = 0x00400000


def is_cloud_only(path: pathlib.Path) -> bool:
    """Return True if *path* is a OneDrive cloud-only placeholder.

    Safe to call at any time — reads only file attributes, never file content.
    Always returns False on non-Windows platforms.
    """
    if not is_windows():
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        return attrs != 0xFFFFFFFF and bool(attrs & _CLOUD_RECALL_ATTR)
    except Exception:
        return False


def rename_cloud_file(src: pathlib.Path, dst: pathlib.Path,
                      logger: logging.Logger | None = None) -> str:
    """Rename a SharePoint/OneDrive file in place without touching hydration.

    Renaming a Files-On-Demand placeholder forces OneDrive to materialize
    (download) the full file first, so dehydrated placeholders are left
    untouched — they expire on the cloud's own cadence and the new name arrives
    via the normal sync. Only hydrated copies are renamed in place (a metadata
    op, no bandwidth). Centralizes the guard that was inline in the sync flow so
    RENAME and SHARE share one hydration-safe path.

    Returns one of:
      'renamed'         — src was hydrated and renamed to dst
      'skip-exists'     — dst already present (nothing to do)
      'skip-dehydrated' — src is a cloud-only placeholder; left as-is
      'missing'         — src does not exist
    """
    if not src.exists():
        return 'missing'
    if dst.exists():
        return 'skip-exists'
    if is_cloud_only(src):
        if logger:
            logger.debug(f'CLOUD-RENAME: {src.name} is a placeholder — left to expire')
        return 'skip-dehydrated'
    src.rename(dst)
    return 'renamed'


def dehydrate_cloud_files(paths: list[pathlib.Path], logger: logging.Logger) -> None:
    """Mark files as unpinned so OneDrive will free their local disk space.

    Sets the OneDrive ``+U`` (unpinned) attribute via ``attrib``.  OneDrive
    dehydrates each file on its next sync cycle (typically within seconds).
    No-op on non-Windows platforms.  Best-effort: failures are logged at DEBUG.
    """
    if not is_windows() or not paths:
        return
    for path in paths:
        try:
            result = subprocess.run(
                ['attrib', '+U', '-P', str(path)],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                logger.debug(f'DEHYDRATE: attrib +U returned {result.returncode} for {path.name}')
            else:
                logger.debug(f'DEHYDRATE: queued for cloud-only → {path.name}')
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug(f'DEHYDRATE: skipped {path.name}: {exc}')


# ---------------------------------------------------------------------------
# Delete queue
# ---------------------------------------------------------------------------

@dataclass
class DeleteQueue:
    """Accumulates files for deferred deletion; requires explicit confirmation."""

    items: list[tuple[pathlib.Path, str]] = field(default_factory=list)

    def add(self, path: pathlib.Path, reason: str,
            logger: logging.Logger | None = None,
            tui: bool = True) -> None:
        self.items.append((pathlib.Path(path), reason))
        if logger:
            logger.info(
                f"PENDING {reason} — {pathlib.Path(path).name}",
                extra={'tui': tui},
            )

    def show_summary(self) -> bool:
        """Print a grouped summary table. Returns True if any items exist."""
        if not self.items:
            return False
        groups: dict[tuple, dict] = defaultdict(
            lambda: {'count': 0, 'bytes': 0})
        for path, reason in self.items:
            key = (path.suffix or 'unknown', reason)
            groups[key]['count'] += 1
            try:
                groups[key]['bytes'] += path.stat().st_size
            except OSError:
                pass

        print(f"\n  {'Type':<8} {'Count':>6} {'Size':>10}   Reason")
        print(f"  {'--------':<8} {'------':>6} {'----------':>10}   {'---'}")
        total = 0
        for (ext, reason), d in groups.items():
            print(
                f"  {ext:<8} {d['count']:>6} {fmt_size(d['bytes']):>10}   {reason}")
            total += d['bytes']
        print(f"  {'--------':<8} {'------':>6} {'----------':>10}")
        print(f"  {'TOTAL':<8} {len(self.items):>6} {fmt_size(total):>10}\n")
        return True

    def execute(
        self,
        logger: logging.Logger,
        pipeline_moved: 'queue.Queue[str] | None' = None,
    ) -> None:
        """Delete all queued files and clear the queue.

        If *pipeline_moved* is supplied, each path is registered in it before
        unlink so the watchdog's _detect_file_events() doesn't log REMOVED for
        files the pipeline itself deleted.
        """
        failed: list[tuple[pathlib.Path, str]] = []
        for path, reason in self.items:
            try:
                if not path.exists():
                    continue
                if pipeline_moved is not None:
                    pipeline_moved.put(str(path))
                path.unlink()
                logger.info(f"DELETE  {path.name}")
            except OSError as e:
                # Any per-file OSError (locked, or a NAS stat raising WinError 5
                # Access denied) must not kill the worker thread — leave it queued.
                logger.warning(f"DELETE  {path.name} — locked, will retry next loop ({e})")
                failed.append((path, reason))
        deleted = len(self.items) - len(failed)
        if deleted:
            logger.info(f"Deleted {deleted} file(s)")
        self.items[:] = failed

    def clear(self) -> None:
        self.items.clear()


