"""Read-only status of the external gtv clip-wall (scripts/streams/google_tv_run.ps1 → mediamtx :8656).

The engine does NOT own these processes — the gtv stack runs as its own scheduled tasks
(GoogleTVStreams / GoogleTVHeal). This module just OBSERVES it via psutil + the heal log, so the
STREAMS menu can show per-feed liveness + reception without coupling the engine to the stream
lifecycle. Every function degrades gracefully (empty / best-effort) rather than raising into the TUI.

Part of Phase 3 of docs/active/venue-av-target-architecture.md. `get_local_ip` lives here now that the
old `nofun.streams` StreamServer (the unused in-engine HTTP-TS streamer) has been retired.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import psutil

__all__ = ['GTV_RTSP_PORT', 'GTV_HEAL_LOG', 'get_local_ip', 'gtv_feeds_status']

GTV_RTSP_PORT = 8656
GTV_HEAL_LOG  = pathlib.Path(r'C:\Users\NOFUNadmin\clips\scratch\ndi\gtv_heal.log')


def get_local_ip() -> str:
    """Detect LAN IP: ipconfig.exe (Windows) → ip route (Linux) → ipconfig (macOS)."""
    if shutil.which('ipconfig.exe'):
        try:
            out = subprocess.run(['ipconfig.exe'], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if 'IPv4 Address' in line:
                    return line.split(':')[-1].strip().rstrip('\r')
        except Exception:
            pass
    if shutil.which('ip'):
        try:
            out = subprocess.run(['ip', 'route', 'get', '1'], capture_output=True, text=True, timeout=5).stdout
            parts = out.split()
            if 'src' in parts:
                return parts[parts.index('src') + 1]
        except Exception:
            pass
    if shutil.which('ipconfig'):
        for iface in ('en0', 'en1'):
            try:
                out = subprocess.run(['ipconfig', 'getifaddr', iface],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
                if out:
                    return out
            except Exception:
                pass
    return '127.0.0.1'

# Each gtv ffmpeg carries `-metadata comment=nofun_google_tv_gtvN`, so we can map a live publisher to
# its feed from the process cmdline. The heal log records assignments as `assigned <ip>:port -> /gtvN`.
_FEED_RE   = re.compile(r'nofun_google_tv_(gtv\d+)')
_ASSIGN_RE = re.compile(r'assigned (\d+\.\d+\.\d+\.\d+):\d+ -> /(gtv\d+)')


def _live_feeds() -> set[str]:
    """Feeds with a live publishing ffmpeg (identified by the per-feed metadata tag in its cmdline)."""
    feeds: set[str] = set()
    for p in psutil.process_iter(['name', 'cmdline']):
        if 'ffmpeg' not in (p.info.get('name') or '').lower():
            continue
        m = _FEED_RE.search(' '.join(p.info.get('cmdline') or []))
        if m:
            feeds.add(m.group(1))
    return feeds


def _reader_ips(port: int) -> set[str] | None:
    """LAN IPs with an ESTABLISHED TCP connection to the RTSP port (i.e. actually pulling a feed).

    Returns None if connections can't be read (no admin) so the caller can show '?' vs. a false 'no'.
    """
    try:
        return {
            c.raddr.ip
            for c in psutil.net_connections(kind='tcp')
            if (c.laddr and c.laddr.port == port and c.status == psutil.CONN_ESTABLISHED
                and c.raddr and str(c.raddr.ip).startswith('192.168.'))
        }
    except (psutil.AccessDenied, PermissionError):
        return None


def _assignments(log: pathlib.Path) -> dict[str, str]:
    """feed -> stick ip, from the heal log's `assigned <ip> -> /gtvN` lines (last one wins)."""
    out: dict[str, str] = {}
    try:
        for line in log.read_text(errors='ignore').splitlines():
            m = _ASSIGN_RE.search(line)
            if m:
                out[m.group(2)] = m.group(1)
    except OSError:
        pass
    return out


def gtv_feeds_status(port: int = GTV_RTSP_PORT, host: str | None = None,
                     log: pathlib.Path = GTV_HEAL_LOG) -> list[dict]:
    """One row per live gtv feed: {feed, url, live, stick, receiving}.

    Empty list when nothing is publishing (e.g. GoogleTVStreams stopped, or on a dev box). `receiving`
    is None when reader state is unreadable (no admin), else True/False for the feed's assigned stick.
    """
    host = host or get_local_ip()
    readers = _reader_ips(port)
    assigned = _assignments(log)
    rows: list[dict] = []
    for feed in sorted(_live_feeds()):
        stick = assigned.get(feed)
        rows.append({
            'feed': feed,
            'url': f'rtsp://{host}:{port}/{feed}',
            'live': True,
            'stick': stick or '(unassigned)',
            'receiving': None if readers is None else bool(stick and stick in readers),
        })
    return rows
