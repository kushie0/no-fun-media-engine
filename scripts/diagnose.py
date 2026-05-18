"""diagnose.py — Read-only production diagnostic for the NOFUN media engine.

Scans all known media directories and produces a plain-text report saved to
diagnostic_report.txt in the script directory.  NO files are modified,
deleted, or moved.  Safe to run on the production machine at any time.

Usage:
    python diagnose.py              # full scan
    python diagnose.py --quick      # skip per-file size stats (faster)

Output:
    diagnostic_report.txt           # copy to dev machine via git
"""

import datetime
import os
import pathlib
import platform
import subprocess
import sys

import click

from nofun.inventory import (
    build_performance_states,
    classify_file,
    classify_location,
    extract_date_band,
    scan_files,
)
from nofun.paths import detect_mounts, detect_platform


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sh(cmd: list[str]) -> str:
    """Run a command and return stripped stdout, or error message."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return f"(error: {e})"


def _dir_summary(path: pathlib.Path) -> str:
    """Return a one-line summary of a directory: exists? file count + size."""
    if not path.exists():
        return "NOT FOUND"
    if not path.is_dir():
        return "NOT A DIRECTORY"
    try:
        entries = list(path.iterdir())
        total   = sum(e.stat().st_size for e in entries if e.is_file())
        files   = sum(1 for e in entries if e.is_file())
        dirs    = sum(1 for e in entries if e.is_dir())
        gb      = total / 1_073_741_824
        return f"{files} files  {dirs} subdirs  {gb:.2f} GB (top-level only)"
    except PermissionError:
        return "PERMISSION DENIED"


def _list_dir(path: pathlib.Path, max_files: int = 40) -> list[str]:
    """Return sorted filenames in path (up to max_files), with truncation notice."""
    if not path.is_dir():
        return []
    try:
        names = sorted(e.name for e in path.iterdir() if e.is_file())
    except PermissionError:
        return ["(permission denied)"]
    if len(names) > max_files:
        shown = names[:max_files]
        shown.append(f"... ({len(names) - max_files} more files not shown)")
        return shown
    return names


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _section_system(lines: list[str]) -> None:
    lines += [
        "=" * 72,
        "  SYSTEM INFO",
        "=" * 72,
        f"  Date/time  : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Platform   : {detect_platform()}  ({sys.platform})",
        f"  Python     : {sys.version.split()[0]}",
        f"  OS release : {platform.uname().release}",
        f"  Machine    : {platform.machine()}",
        "",
    ]


def _section_paths(lines: list[str], mount_c: pathlib.Path,
                   mount_d: pathlib.Path) -> None:
    lines += [
        "=" * 72,
        "  DIRECTORY LAYOUT",
        "=" * 72,
    ]

    dirs_to_check = [
        # (label, path)
        ("C:\\VenueLighting",         mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun') / 'VenueLighting'),
        ("OneDrive\\Multitracks",     mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun')
                                      / 'OneDrive - No Fun Troy LLC' / 'Multitracks'),
        ("D:\\videos",                mount_d / 'videos'),
        ("D:\\audio",                 mount_d / 'audio'),
        ("D:\\clips",                 mount_d / 'clips'),
        # Legacy paths that should now be empty or gone:
        ("D:\\video_archive",          mount_d / 'video_archive'),
        ("D:\\audio_archive",          mount_d / 'audio_archive'),
    ]

    for label, path in dirs_to_check:
        lines.append(f"  {label}")
        lines.append(f"    {path}")
        lines.append(f"    {_dir_summary(path)}")
        if path.is_dir():
            for name in _list_dir(path, max_files=20):
                lines.append(f"      {name}")
        lines.append("")


def _section_onedrive_subfolders(lines: list[str], mount_c: pathlib.Path) -> None:
    od = (mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun')
          / 'OneDrive - No Fun Troy LLC' / 'Multitracks')
    if not od.is_dir():
        return
    lines += [
        "=" * 72,
        "  ONEDRIVE / MULTITRACKS SUBFOLDERS",
        "=" * 72,
    ]
    try:
        subdirs = sorted(d for d in od.iterdir() if d.is_dir())
    except PermissionError:
        lines.append("  (permission denied)")
        lines.append("")
        return
    for sub in subdirs:
        lines.append(f"  {sub.name}/")
        for name in _list_dir(sub, max_files=15):
            lines.append(f"    {name}")
    lines.append("")


def _section_performance_states(lines: list[str], mount_c: pathlib.Path,
                                 mount_d: pathlib.Path) -> None:
    lines += [
        "=" * 72,
        "  PERFORMANCE STATE SCAN",
        "=" * 72,
    ]

    search_paths = []
    for p in [
        mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun') / 'VenueLighting',
        mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun') / 'OneDrive - No Fun Troy LLC' / 'Multitracks',
        mount_d / 'videos',
        mount_d / 'audio',
        mount_d / 'clips',
        mount_d / 'audio_archive',
        mount_d / 'video_archive',
    ]:
        if p.exists():
            search_paths.append(p)

    if not search_paths:
        lines.append("  No known media directories found — nothing to scan.")
        lines.append("")
        return

    lines.append(f"  Scanning {len(search_paths)} directories...")

    rows: list[dict] = []
    for meta in scan_files(search_paths):
        date, band = extract_date_band(meta['filename'])
        ftype      = classify_file(meta['filename'], meta['fullpath'])
        loc        = classify_location(meta['fullpath'])
        rows.append({**meta, 'date': date, 'band': band, 'type': ftype,
                     'location': loc, 'size_gb': meta['size'] / 1_073_741_824})

    lines.append(f"  Found {len(rows)} media files")
    lines.append("")

    states = build_performance_states(rows)

    # Summary counts
    state_counts: dict[str, int] = {}
    for ps in states.values():
        s = ps.state
        state_counts[s] = state_counts.get(s, 0) + 1

    lines.append("  State breakdown:")
    for state, count in sorted(state_counts.items()):
        lines.append(f"    {state:<20} {count}")
    lines.append("")

    # Performances that need attention
    attention_states = {'DETECTED', 'AUDIO_PENDING', 'INCOMPLETE',
                        'SHARE_EXPIRED', 'SHARE_ELIGIBLE'}
    attention = [(k, ps) for k, ps in states.items() if ps.state in attention_states]

    if attention:
        lines.append("  Performances needing attention:")
        lines.append(f"  {'Date':<12} {'Band':<30} {'State':<20}  Notes")
        lines.append("  " + "-" * 70)
        for (date, band), ps in sorted(attention, key=lambda x: x[0], reverse=True):
            d = date[2:] if date.startswith('20') else date
            b = (band[:26] + '..') if len(band) > 28 else band
            notes = []
            if ps.raw_movs:   notes.append(f"{len(ps.raw_movs)} raw mov(s) in source")
            if not ps.quad_files and ps.mov_files:
                notes.append("quads missing")
            if ps.raw_wavs and not ps.zip_files:
                notes.append(f"{len(ps.raw_wavs)} unzipped wav(s)")
            if ps.cloud_files and ps.age_days and ps.age_days > 30:
                notes.append(f"cloud age {ps.age_days}d")
            lines.append(f"  {d:<12} {b:<30} {ps.state:<20}  {', '.join(notes)}")
        lines.append("")
    else:
        lines.append("  All performances are in good shape (no attention needed).")
        lines.append("")

    # Full table
    lines += [
        "  Full performance list:",
        f"  {'Date':<12} {'Band':<30} {'Mov':>3} {'Q':>3} {'Wav':>3} {'Zip':>3}"
        f"  {'Cloud':>8}   State",
        "  " + "-" * 70,
    ]
    clutter_keys = {
        k for k in states
        if k[0] == 'TBD' or k[1] in ('Audio Recorder', 'TBD') or k[1].startswith('R_')
    }
    for (date, band) in sorted(states.keys() - clutter_keys, reverse=True):
        ps = states[(date, band)]
        d  = date[2:] if date.startswith('20') else date
        b  = (band[:26] + '..') if len(band) > 28 else band
        mov  = str(len(ps.mov_files) + len(ps.raw_movs)) if (ps.mov_files or ps.raw_movs) else '-'
        quad = str(len(ps.quad_files)) if ps.quad_files else '-'
        wav  = str(len(ps.wav_files) + len(ps.raw_wavs)) if (ps.wav_files or ps.raw_wavs) else '-'
        zp   = str(len(ps.zip_files)) if ps.zip_files else '-'
        cloud_str = f'{ps.age_days}d' if ps.cloud_files and ps.age_days else (
            '?' if ps.cloud_files else '-'
        )
        lines.append(
            f"  {d:<12} {b:<30} {mov:>3} {quad:>3} {wav:>3} {zp:>3}"
            f"  {cloud_str:>8}   {ps.state}"
        )

    if clutter_keys:
        lines += ["", "  -- Unclassified --"]
        for (date, band) in sorted(clutter_keys):
            ps = states[(date, band)]
            d  = date[2:] if date.startswith('20') else date
            lines.append(f"  {d:<12} {band[:40]:<40}  {ps.state}")

    lines.append("")


def _section_naming_samples(lines: list[str], mount_c: pathlib.Path,
                             mount_d: pathlib.Path) -> None:
    """Sample filenames from each directory to spot naming convention drift."""
    lines += [
        "=" * 72,
        "  FILENAME SAMPLES (first 10 per directory)",
        "=" * 72,
    ]
    dirs = [
        ("VenueLighting",   mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun') / 'VenueLighting'),
        ("D:/videos",       mount_d / 'videos'),
        ("D:/audio",        mount_d / 'audio'),
    ]
    for label, path in dirs:
        if not path.is_dir():
            continue
        lines.append(f"  {label}/")
        for name in _list_dir(path, max_files=10):
            lines.append(f"    {name}")
        lines.append("")


def _section_disk_space(lines: list[str], mount_d: pathlib.Path) -> None:
    lines += [
        "=" * 72,
        "  DISK SPACE",
        "=" * 72,
    ]
    if sys.platform == 'win32' or 'MSYSTEM' in os.environ:
        result = _sh([
            'powershell', '-NoProfile', '-Command',
            'Get-PSDrive -PSProvider FileSystem | '
            'Select-Object Name,'
            '@{N="Used(GB)";E={if($_.Used){[math]::Round($_.Used/1GB,1)}else{"N/A"}}},'
            '@{N="Free(GB)";E={if($_.Free){[math]::Round($_.Free/1GB,1)}else{"N/A"}}} | '
            'Format-Table -AutoSize | Out-String',
        ])
        lines.append(result)
    else:
        result = _sh(['df', '-h', str(mount_d)])
        lines.append(result)
    lines.append("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option('--quick', is_flag=True,
              help='Skip per-file size stats (faster on large libraries).')
@click.option('--output', type=click.Path(), default=None,
              help='Output file path (default: diagnostic_report.txt next to script).')
def main(quick: bool, output: str | None) -> None:
    """Read-only production diagnostic — outputs diagnostic_report.txt."""
    script_dir  = pathlib.Path(__file__).parent
    output_path = pathlib.Path(output) if output else script_dir / 'diagnostic_report.txt'

    mount_c, mount_d = detect_mounts()

    click.echo(f"NOFUN Media Engine — Diagnostic scan")
    click.echo(f"  mount_c = {mount_c}")
    click.echo(f"  mount_d = {mount_d}")
    click.echo(f"  output  = {output_path}")
    click.echo("")

    lines: list[str] = [
        "NOFUN MEDIA ENGINE — DIAGNOSTIC REPORT",
        f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Script dir: {script_dir}",
        "",
    ]

    _section_system(lines)
    _section_paths(lines, mount_c, mount_d)
    _section_onedrive_subfolders(lines, mount_c)

    click.echo("Scanning media files (this may take a moment)...")
    _section_performance_states(lines, mount_c, mount_d)

    if not quick:
        _section_naming_samples(lines, mount_c, mount_d)

    _section_disk_space(lines, mount_d)

    lines += [
        "=" * 72,
        "  END OF REPORT",
        "=" * 72,
    ]

    report = '\n'.join(lines) + '\n'
    output_path.write_text(report, encoding='utf-8')

    click.echo(f"Report saved to: {output_path}")
    click.echo("Copy to dev machine: git add diagnostic_report.txt && git commit -m 'diag'")


if __name__ == '__main__':
    main()
