#!/usr/bin/env python3
"""td-timeline.py — assemble a unified, chronological timeline around a TD hang.

The 2026-06-22 AppHang was reconstructed by hand from WER reports + engine logs
+ Defender status + recording state. This script does that automatically: given
a center time (or auto-detected from the latest TouchPlayer AppHang in WER), it
merges every relevant source into one sorted timeline so the *next* hang
documents its own surrounding conditions.

It is read-only — it only reads logs, WER reports, and the Windows event log.
Safe to run during a live show.

Sources merged (each is best-effort; missing ones are skipped with a note):
  - Engine logs        convert_recent.log + D:\\logs\\*  (lines: "[yy-mm-ddTHH:MM:SS] msg")
  - conn-watch log     output of td-conn-watch.ps1 (--conn-log)
  - WER reports        ReportArchive/ReportQueue Report.wer (AppHang/crash events)
  - Windows event log  System + Application, relevant providers, via Get-WinEvent

Examples
--------
  # Auto: center on the most recent TouchPlayer AppHang, +/-15 min
  python td-timeline.py

  # Explicit center time and window
  python td-timeline.py --at "2026-06-22 20:12" --window 20
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import platform
import re
import subprocess
import sys

# --- prod defaults (override via args) -------------------------------------
DEFAULT_ENGINE_LOGS = [
    pathlib.Path(r"C:\Users\NOFUNadmin\clips\convert_recent.log"),
    pathlib.Path(r"D:\logs"),
]
DEFAULT_CONN_LOG = pathlib.Path(r"D:\tmp\td_conn_watch.log")
WER_ROOTS = [
    pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "WER",
    pathlib.Path(r"C:\ProgramData\Microsoft\Windows\WER"),
]
# Event-log providers worth surfacing (noise-free allowlist).
EVENT_PROVIDERS = [
    "Application Hang",
    "Windows Error Reporting",
    "Microsoft-Windows-WER-SystemErrorReporting",
    "Application Error",
    "amdkmdag",
    "Microsoft-Windows-Kernel-Power",
    "Microsoft-Windows-Windows Defender",
]

# Engine-log line: "[26-06-22T20:12:01] message"
_ENGINE_RE = re.compile(r"^\[(\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]\s?(.*)$")
# conn-watch / generic ISO-ish line: "[2026-06-22T20:12:01] message"
_ISO_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]\s?(.*)$")


class Event:
    __slots__ = ("when", "source", "text")

    def __init__(self, when: dt.datetime, source: str, text: str) -> None:
        self.when = when
        self.source = source
        self.text = text


def _in_window(t: dt.datetime, lo: dt.datetime, hi: dt.datetime) -> bool:
    return lo <= t <= hi


# ---------------------------------------------------------------------------
# Source: engine logs
# ---------------------------------------------------------------------------

def _iter_log_files(paths: list[pathlib.Path]):
    for p in paths:
        if p.is_dir():
            yield from sorted(p.glob("*.log"))
            yield from sorted(p.glob("*.log.*"))
        elif p.is_file():
            yield p


def collect_engine(paths: list[pathlib.Path], lo: dt.datetime, hi: dt.datetime) -> list[Event]:
    out: list[Event] = []
    for f in _iter_log_files(paths):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _ENGINE_RE.match(line)
            if not m:
                continue
            try:
                when = dt.datetime.strptime(m.group(1), "%y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
            if _in_window(when, lo, hi):
                out.append(Event(when, f"engine:{f.name}", m.group(2)))
    return out


# ---------------------------------------------------------------------------
# Source: conn-watch log
# ---------------------------------------------------------------------------

def collect_conn(path: pathlib.Path, lo: dt.datetime, hi: dt.datetime) -> list[Event]:
    out: list[Event] = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = _ISO_RE.match(line)
        if not m:
            continue
        try:
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if _in_window(when, lo, hi):
            out.append(Event(when, "conn-watch", m.group(2)))
    return out


# ---------------------------------------------------------------------------
# Source: WER reports
# ---------------------------------------------------------------------------

def _parse_wer(report: pathlib.Path) -> dict[str, str]:
    """Report.wer is UTF-16LE key=value. Return a small dict of fields we care about."""
    fields: dict[str, str] = {}
    try:
        raw = report.read_text(encoding="utf-16", errors="replace")
    except (OSError, UnicodeError):
        try:
            raw = report.read_text(errors="replace")
        except OSError:
            return fields
    for line in raw.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in ("EventType", "AppName", "AppPath") or k.startswith("Sig") or k.startswith("DynamicSig"):
            fields[k] = v
    return fields


def collect_wer(roots: list[pathlib.Path], lo: dt.datetime, hi: dt.datetime,
                app_filter: str | None) -> list[Event]:
    out: list[Event] = []
    for root in roots:
        for sub in ("ReportArchive", "ReportQueue"):
            base = root / sub
            if not base.is_dir():
                continue
            for report in base.glob("*/Report.wer"):
                try:
                    mtime = dt.datetime.fromtimestamp(report.stat().st_mtime)
                except OSError:
                    continue
                if not _in_window(mtime, lo, hi):
                    continue
                f = _parse_wer(report)
                app = f.get("AppName", "") or f.get("Sig[0].Value", "")
                if app_filter and app_filter.lower() not in (app or "").lower() \
                        and app_filter.lower() not in report.parent.name.lower():
                    continue
                etype = f.get("EventType", "?")
                out.append(Event(mtime, "WER", f"{etype}  {app}  [{report.parent.name}]"))
    return out


def latest_apphang(roots: list[pathlib.Path], app_filter: str) -> dt.datetime | None:
    """Find the most recent AppHang WER report for the target app."""
    best: dt.datetime | None = None
    wide_lo = dt.datetime.now() - dt.timedelta(days=120)
    for ev in collect_wer(roots, wide_lo, dt.datetime.now(), app_filter):
        if "hang" in ev.text.lower() and (best is None or ev.when > best):
            best = ev.when
    return best


# ---------------------------------------------------------------------------
# Source: Windows event log (via Get-WinEvent)
# ---------------------------------------------------------------------------

def collect_events(lo: dt.datetime, hi: dt.datetime, providers: list[str]) -> list[Event]:
    if platform.system() != "Windows":  # Get-WinEvent is Windows-only
        return []
    start = lo.strftime("%Y-%m-%dT%H:%M:%S")
    end = hi.strftime("%Y-%m-%dT%H:%M:%S")
    ps = (
        "$f=@{LogName=@('System','Application');"
        f"StartTime=[datetime]'{start}';EndTime=[datetime]'{end}'}};"
        "Get-WinEvent -FilterHashtable $f -ErrorAction SilentlyContinue |"
        "Select-Object @{n='t';e={$_.TimeCreated.ToString('s')}},"
        "@{n='p';e={$_.ProviderName}},Id,LevelDisplayName,"
        "@{n='m';e={($_.Message -split \"`n\")[0]}} | ConvertTo-Json -Depth 3"
    )
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout) if res.stdout.strip() else []
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = [data]
    allow = {p.lower() for p in providers}
    out: list[Event] = []
    for row in data:
        prov = (row.get("p") or "")
        if allow and prov.lower() not in allow:
            continue
        try:
            when = dt.datetime.strptime(row["t"], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, ValueError):
            continue
        lvl = row.get("LevelDisplayName") or ""
        out.append(Event(when, f"evt:{prov}", f"[{lvl} id={row.get('Id')}] {row.get('m','')}"))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", help="center time 'YYYY-MM-DD HH:MM[:SS]' (default: auto from latest AppHang)")
    ap.add_argument("--window", type=float, default=15.0, help="minutes each side of center (default 15)")
    ap.add_argument("--app", default="TouchPlayer", help="app name to anchor auto-detect / filter WER (default TouchPlayer)")
    ap.add_argument("--engine-log", action="append", type=pathlib.Path,
                    help="engine log file or dir (repeatable; default convert_recent.log + D:\\logs)")
    ap.add_argument("--conn-log", type=pathlib.Path, default=DEFAULT_CONN_LOG)
    ap.add_argument("--no-events", action="store_true", help="skip the Windows event log query")
    args = ap.parse_args()

    if args.at:
        center = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                center = dt.datetime.strptime(args.at, fmt)
                break
            except ValueError:
                continue
        if center is None:
            print(f"could not parse --at {args.at!r}", file=sys.stderr)
            return 2
    else:
        center = latest_apphang(WER_ROOTS, args.app)
        if center is None:
            print("no AppHang found in WER and no --at given; "
                  "pass --at 'YYYY-MM-DD HH:MM'", file=sys.stderr)
            return 2
        print(f"auto-anchored on latest {args.app} AppHang: {center:%Y-%m-%d %H:%M:%S}")

    lo = center - dt.timedelta(minutes=args.window)
    hi = center + dt.timedelta(minutes=args.window)

    engine_paths = args.engine_log or DEFAULT_ENGINE_LOGS
    events: list[Event] = []
    events += collect_engine(engine_paths, lo, hi)
    events += collect_conn(args.conn_log, lo, hi)
    events += collect_wer(WER_ROOTS, lo, hi, args.app)
    if not args.no_events:
        events += collect_events(lo, hi, EVENT_PROVIDERS)

    events.sort(key=lambda e: e.when)

    print(f"\n=== timeline {lo:%Y-%m-%d %H:%M:%S} .. {hi:%H:%M:%S}  "
          f"(center {center:%H:%M:%S}, +/-{args.window:g}m) ===")
    if not events:
        print("(no events in window — check source paths; on non-Windows the "
              "event log / WER sources are unavailable)")
        return 0
    for e in events:
        mark = "  <<<" if abs((e.when - center).total_seconds()) <= 1 else ""
        print(f"{e.when:%H:%M:%S}  {e.source:<24.24}  {e.text}{mark}")
    print(f"\n{len(events)} events from "
          f"{len({e.source.split(':')[0] for e in events})} source kinds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
