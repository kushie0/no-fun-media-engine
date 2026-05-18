"""nofun/log_handlers.py — Custom logging handlers for the pipeline.

Provides:
    RollingRecentHandler   — local file that always holds the last 48 hours
    RemoteRotatingHandler  — D:\\logs size-rotating log (800 KB per file)
"""

from __future__ import annotations

import datetime
import logging
import pathlib
import re
import string
import time


# ---------------------------------------------------------------------------
# Timestamp parser
# ---------------------------------------------------------------------------

_TS_RE       = re.compile(r'^\[(\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]')
_LOG_LETTERS = string.ascii_uppercase   # 'A' … 'Z' — used for log overflow suffixes


def _parse_log_ts(line: str) -> float | None:
    """Parse a [YY-MM-DDTHH:MM:SS] prefix and return a local epoch float, or None."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1), '%y-%m-%dT%H:%M:%S')
        return dt.timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# RollingRecentHandler — 48-hour rolling local log
# ---------------------------------------------------------------------------

class RollingRecentHandler(logging.FileHandler):
    """FileHandler that keeps at most WINDOW seconds of log history.

    On startup: reads the existing file and discards lines whose timestamp
    is older than WINDOW.  During a run: re-prunes every PRUNE_INTERVAL
    seconds (cheap: only if the file has grown since last check).

    Continuation lines (e.g. FileDetailFormatter '|  src=...' lines) carry
    no timestamp of their own; they inherit the timestamp of the preceding
    timestamped line and are pruned or kept with it.
    """

    WINDOW         = 48 * 3600   # 48 hours in seconds
    PRUNE_INTERVAL = 300         # re-scan at most every 5 minutes

    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, mode='a', encoding='utf-8', delay=False)
        self._last_prune: float = 0.0
        self._last_size:  int   = 0
        self._prune(path)        # trim stale entries on every startup

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        now = time.monotonic()
        if now - self._last_prune >= self.PRUNE_INTERVAL:
            path = pathlib.Path(self.baseFilename)
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
            if current_size != self._last_size:
                self._prune(path)
            self._last_prune = now
            self._last_size  = current_size

    def _prune(self, path: pathlib.Path) -> None:
        """Remove log lines older than WINDOW and rewrite the file in place."""
        if not path.exists() or path.stat().st_size == 0:
            return
        cutoff = time.time() - self.WINDOW
        try:
            raw = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return

        kept: list[str] = []
        cur_ts: float | None = None

        for line in raw.splitlines(keepends=True):
            ts = _parse_log_ts(line)
            if ts is not None:
                cur_ts = ts
            # Keep line if it has no own timestamp (continuation) or is within window
            if cur_ts is None or cur_ts >= cutoff:
                kept.append(line)

        if len(kept) < raw.count('\n'):
            # Only rewrite when we actually dropped something
            self.stream.close()
            path.write_text(''.join(kept), encoding='utf-8')
            self.stream = self._open()    # re-open in append mode


# ---------------------------------------------------------------------------
# RemoteRotatingHandler — D:\logs size-rotating log
# ---------------------------------------------------------------------------

class RemoteRotatingHandler(logging.FileHandler):
    """Rotates into a new log_YYMMDD[_N].txt once the current file reaches MIN_SIZE.

    The date in the filename is the date of the FIRST log entry written into
    that file — so a file started at 23:58 stays named for that day even if
    most of its entries land in the next calendar day.

    Overflow within the same date:  log_260307.txt → log_260307_1.txt → …
    """

    MIN_SIZE = 800_000   # 800 000 bytes ≈ 781 KiB

    def __init__(self, log_dir: pathlib.Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._find_active_file()
        super().__init__(path, mode='a', encoding='utf-8', delay=False)
        self._first_date: str | None = self._read_first_date(pathlib.Path(self.baseFilename))

    # ------------------------------------------------------------------
    # File-finding helpers
    # ------------------------------------------------------------------

    def _find_active_file(self) -> pathlib.Path:
        """Return an existing under-MIN_SIZE file, or start a fresh one."""
        candidates = sorted(self._log_dir.glob('log_*.txt'), reverse=True)
        for f in candidates:
            try:
                if f.stat().st_size < self.MIN_SIZE:
                    return f
            except OSError:
                pass
        return self._new_file_path()

    def _new_file_path(self) -> pathlib.Path:
        """Generate the next unused log_YYMMDD[_LETTER].txt path."""
        date_str = datetime.date.today().strftime('%y%m%d')
        # Prefer the date of the first entry already in the current file
        if hasattr(self, '_first_date') and self._first_date:
            date_str = self._first_date
        base = self._log_dir / f'log_{date_str}.txt'
        if not base.exists():
            return base
        for letter in _LOG_LETTERS:
            p = self._log_dir / f'log_{date_str}_{letter}.txt'
            if not p.exists():
                return p
        # Fallback: numeric suffix beyond 26 sessions (should never occur in practice).
        n = 1
        while True:
            p = self._log_dir / f'log_{date_str}_{n}.txt'
            if not p.exists():
                return p
            n += 1

    @staticmethod
    def _read_first_date(path: pathlib.Path) -> str | None:
        """Return 'YYMMDD' string from the first timestamped line in *path*, or None."""
        if not path.exists() or path.stat().st_size == 0:
            return None
        try:
            with path.open(encoding='utf-8', errors='replace') as f:
                for line in f:
                    ts = _parse_log_ts(line)
                    if ts is not None:
                        return datetime.datetime.fromtimestamp(ts).strftime('%y%m%d')
        except OSError:
            pass
        return None

    # ------------------------------------------------------------------
    # Emit with rotation check
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        path = pathlib.Path(self.baseFilename)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0

        if size >= self.MIN_SIZE:
            self._rotate()

        super().emit(record)

        # Capture the first-entry date from a newly opened file
        if self._first_date is None:
            self._first_date = self._read_first_date(pathlib.Path(self.baseFilename))

    def _rotate(self) -> None:
        """Flush, close, and open a fresh log file."""
        self.acquire()
        try:
            self.stream.flush()
            self.stream.close()
            self.baseFilename = str(self._new_file_path())
            self._first_date  = None
            self.stream       = self._open()
        finally:
            self.release()
