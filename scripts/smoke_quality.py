#!/usr/bin/env python3
"""scripts/smoke_quality.py — quality + metadata + runtime analysis for a smoke run.

The smoke test deletes one synthetic performance's outputs and lets the engine
rebuild them. This tool captures, for each run, a single JSON record under
``scratch/smoke_runs/`` holding:

  * per-output ffprobe metadata (codec, resolution, fps, bitrate, duration, size)
  * quad SSIM vs a near-lossless reference crop (perceptual encode quality)
  * run timing parsed from the engine log (detect -> idle wall + per-stage deltas)
  * a structural verdict (all expected outputs present, none problematic)

Run a few smoke tests, then ``average`` over the JSON records for mean / median /
stdev of runtime and quality — the spread itself reveals any non-determinism.

Subcommands
-----------
  reference   build near-lossless MJPEG CAM reference crops from the source .mov
              (one-time per fixture; needed for the SSIM column)
  analyze     probe rebuilt outputs + SSIM + parse log timing -> one JSON record
  average     aggregate scratch/smoke_runs/*.json -> mean/median/stdev tables

Examples
--------
  # one-time, on prod, from the trimmed 120s source:
  python scripts/smoke_quality.py reference D:/smoke_build/01-01-01_SMOKETEST.mov \\
      --dest D:/smoke_build/reference

  # after a rebuild completes (prod paths shown). Over an SSH-key session the
  # NAS roots need explicit creds — set $NOFUN_NAS_PASS (user defaults to
  # nofunadmin); on the console session they're already authed, so omit it:
  python scripts/smoke_quality.py analyze 01-01-01_SMOKETEST \\
      --videos //192.168.0.232/nofun-archive/videos \\
      --clips  C:/clips \\
      --audio  //192.168.0.232/nofun-archive/audio \\
      --reference D:/smoke_build/reference \\
      --log "$LOCALAPPDATA/nofun/engine.log"

  # after several runs:
  python scripts/smoke_quality.py average
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys

# UL->CAM1, UR->CAM2, LL->CAM3, LR->CAM4 (see nofun/video.py CAM_LABELS).
CAM_LABELS = ('CAM1', 'CAM2', 'CAM3', 'CAM4')
_CAM_TO_QUAD = {'CAM1': 'ul', 'CAM2': 'ur', 'CAM3': 'll', 'CAM4': 'lr'}

REPO     = pathlib.Path(__file__).resolve().parent.parent
RUNS_DIR = REPO / 'scratch' / 'smoke_runs'

# Running this file directly puts scripts/ on sys.path, not the repo root, so the
# lazy `nofun.*` imports below fail. Put the repo root first.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Engine log line: "[26-06-05T12:12:43] DETECTED raw .mov ...".
_LOG_LINE = re.compile(r'^\[(\d\d-\d\d-\d\dT\d\d:\d\d:\d\d)\]\s+(.*)$')

# Ordered run milestones. Each stage's timestamp is the first matching line at or
# after detect; deltas are computed between consecutive milestones that fired.
_STAGE_MARKERS: list[tuple[str, re.Pattern]] = [
    ('detect',  re.compile(r'DETECTED .*\.mov', re.I)),
    ('enqueue', re.compile(r'JobQueue enqueued', re.I)),
    ('quads',   re.compile(r'CREATE CAM', re.I)),
    ('reel',    re.compile(r'REEL .*\bstart', re.I)),
    ('clips',   re.compile(r'CREATE \d+ clips', re.I)),
    ('audio',   re.compile(r'\b(ARCHIVE AUDIO|split_audio|REMASTER)\b', re.I)),
    ('idle',    re.compile(r'no files pending', re.I)),
]


# ---------------------------------------------------------------------------
# Probing / quality
# ---------------------------------------------------------------------------

def probe(path: pathlib.Path) -> dict:
    """ffprobe metadata for one file, via the engine's own probe_file."""
    from nofun.encoding_db import probe_file
    return probe_file(path)


def ssim(ref: pathlib.Path, test: pathlib.Path) -> float | None:
    """Average SSIM of ``test`` against near-lossless ``ref`` (None if unparsable).

    Mirrors tests/test_quality.py::_ssim — ffmpeg framesync aligns by PTS, so a
    differing frame rate is handled. The final All: value is the all-frame mean.
    """
    result = subprocess.run(
        ['ffmpeg', '-i', str(ref), '-i', str(test),
         '-filter_complex', '[0:v][1:v]ssim', '-f', 'null', '-'],
        capture_output=True, text=True,
    )
    matches = re.findall(r'All:([\d.]+)', result.stderr)
    return float(matches[-1]) if matches else None


# ---------------------------------------------------------------------------
# Timeline parsing (pure — unit tested)
# ---------------------------------------------------------------------------

def _parse_ts(stamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(stamp, '%y-%m-%dT%H:%M:%S')


def parse_timeline(log_text: str) -> dict:
    """Extract run milestones from engine log text for a single run window.

    Returns ``{detect, idle, total_wall_s, stages: {a->b: seconds}, marks: {...}}``.
    ``detect`` anchors the window; ``idle`` is the first 'no files pending' after
    it. Missing milestones are simply absent. Empty dict if no detect line.
    """
    events: list[tuple[datetime.datetime, str]] = []
    for line in log_text.splitlines():
        m = _LOG_LINE.match(line.strip())
        if m:
            try:
                events.append((_parse_ts(m.group(1)), m.group(2)))
            except ValueError:
                continue

    detect_i = next(
        (i for i, (_, msg) in enumerate(events)
         if _STAGE_MARKERS[0][1].search(msg)),
        None,
    )
    if detect_i is None:
        return {}

    marks: dict[str, datetime.datetime] = {}
    for name, pat in _STAGE_MARKERS:
        ts = next(
            (t for t, msg in events[detect_i:] if pat.search(msg)),
            None,
        )
        if ts is not None:
            marks[name] = ts

    ordered = [(n, marks[n]) for n, _ in _STAGE_MARKERS if n in marks]
    stages = {
        f'{a}->{b}': (tb - ta).total_seconds()
        for (a, ta), (b, tb) in zip(ordered, ordered[1:])
    }

    out: dict = {'stages': stages,
                 'marks': {n: t.strftime('%y-%m-%dT%H:%M:%S') for n, t in marks.items()}}
    if 'detect' in marks:
        out['detect'] = marks['detect'].strftime('%y-%m-%dT%H:%M:%S')
    if 'idle' in marks:
        out['idle'] = marks['idle'].strftime('%y-%m-%dT%H:%M:%S')
        out['total_wall_s'] = (marks['idle'] - marks['detect']).total_seconds()
    return out


# ---------------------------------------------------------------------------
# NAS authentication (Windows SMB)
# ---------------------------------------------------------------------------
#
# `analyze` runs on prod over an SSH-key logon, which carries no password — so
# SMB pass-through and the DPAPI cred vault (cmdkey) both fail and the UNC video/
# audio roots read empty (false "missing"). Explicit `net use` with credentials
# is the only auth that works from such a session. See VenueLighting/setup_nas.txt.

def _unc_root(path: pathlib.Path) -> str | None:
    r"""Return the ``\\host\share`` root of a UNC path, else None.

    Accepts forward or back slashes (argparse hands us ``//host/share/...`` on
    posix and ``\\host\share\...`` on Windows for the same input).
    """
    s = str(path).replace('/', '\\')
    if not s.startswith('\\\\'):
        return None
    parts = [p for p in s.split('\\') if p]
    if len(parts) < 2:
        return None
    return f'\\\\{parts[0]}\\{parts[1]}'


@contextlib.contextmanager
def _nas_auth(roots: set[str], user: str, password: str | None):
    """`net use`-authenticate each UNC ``root`` for the block's duration.

    No-op without a password (caller relies on ambient/console auth) or off
    Windows. A root that's already connected makes `net use` fail; we leave it
    alone and only tear down the connections we actually created.
    """
    created: list[str] = []
    if password and sys.platform == 'win32':
        for root in sorted(roots):
            rc = subprocess.run(
                ['net', 'use', root, f'/user:{user}', password],
                capture_output=True, text=True,
            )
            if rc.returncode == 0:
                created.append(root)
    try:
        yield
    finally:
        for root in created:
            subprocess.run(['net', 'use', root, '/delete', '/y'],
                           capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Output discovery
# ---------------------------------------------------------------------------

def _find_output(root: pathlib.Path, perf: str, label: str) -> pathlib.Path | None:
    """Find ``<perf>[.<idx>]_<label>`` under ``root``, newest match wins.

    Tolerates an optional numeric performance index between the perf base and the
    label — the raw video '01-01-01_SMOKETEST.1.mov' carries its '.1' into the
    quads ('01-01-01_SMOKETEST.1_CAM1.mp4') while the audio outputs (grouped from
    differently-named WAVs) drop it. The index regex (``\\.\\d+``) is anchored so
    the 'AUDIO.mp3' label won't spuriously match 'MULTITRACK_AUDIO.mp3'.
    """
    pat = re.compile(rf'^{re.escape(perf)}(\.\d+)?_{re.escape(label)}$')
    matches = sorted(p for p in root.glob(f'{perf}*_{label}')
                     if p.is_file() and pat.fullmatch(p.name))
    return matches[-1] if matches else None


def discover_outputs(
    perf: str,
    videos: pathlib.Path,
    clips: pathlib.Path,
    audio: pathlib.Path,
) -> dict:
    """Locate every expected rebuilt artifact for ``perf`` across its roots."""
    found: dict = {}
    for cam in CAM_LABELS:
        p = _find_output(videos, perf, f'{cam}.mp4')
        if p is not None:
            found[cam] = p
    reel = _find_output(videos, perf, 'INSTAGRAM.mp4')
    if reel is not None:
        found['INSTAGRAM'] = reel
    for label in ('AUDIO.mp3', 'MULTITRACK_AUDIO.mp3'):
        mp3 = _find_output(audio, perf, label)
        if mp3 is not None:
            found['AUDIO_MP3'] = mp3
            break
    zp = _find_output(audio, perf, 'MULTITRACK.zip')
    if zp is not None:
        found['MULTITRACK_ZIP'] = zp

    clip_dir = next((d for d in (clips.glob(f'{perf}*'))
                     if d.is_dir()), None)
    if clip_dir is not None:
        clip_files = sorted(clip_dir.glob('*.mp4'))
        found['clips'] = {
            'dir': str(clip_dir),
            'count': len(clip_files),
            'total_size': sum(f.stat().st_size for f in clip_files),
            'sample': clip_files[0] if clip_files else None,
        }
    return found


# Outputs we expect a full rebuild to produce — drives the 'missing' verdict.
_EXPECTED = (*CAM_LABELS, 'INSTAGRAM', 'AUDIO_MP3', 'MULTITRACK_ZIP', 'clips')


def analyze_run(
    perf: str,
    videos: pathlib.Path,
    clips: pathlib.Path,
    audio: pathlib.Path,
    reference: pathlib.Path | None,
    log_text: str | None,
) -> dict:
    """Assemble one run record: per-output metadata + SSIM + timing + verdict."""
    found = discover_outputs(perf, videos, clips, audio)
    outputs: dict = {}
    problematic: list[str] = []

    for label, p in found.items():
        if label == 'clips':
            entry = {k: v for k, v in p.items() if k != 'sample'}
            sample = p.get('sample')
            if sample is not None:
                entry['probe'] = probe(sample)
            outputs['clips'] = entry
            continue
        entry = {'path': str(p), 'size': p.stat().st_size}
        if p.suffix.lower() in ('.mp4', '.mov'):
            entry['probe'] = probe(p)
            if entry['probe'].get('problematic'):
                problematic.append(label)
        if reference is not None and label in CAM_LABELS:
            ref = reference / f'{perf}_{label}.mov'
            if ref.is_file():
                entry['ssim'] = ssim(ref, p)
        outputs[label] = entry

    record: dict = {
        'schema':    1,
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'perf':      perf,
        'outputs':   outputs,
        'timing':    parse_timeline(log_text) if log_text else {},
        'verdict': {
            'missing':     [e for e in _EXPECTED if e not in outputs],
            'problematic': problematic,
        },
    }
    record['verdict']['pass'] = (
        not record['verdict']['missing'] and not problematic
    )
    return record


# ---------------------------------------------------------------------------
# Aggregation (pure — unit tested)
# ---------------------------------------------------------------------------

def flatten_metrics(record: dict) -> dict[str, float]:
    """Pull the comparable numeric metrics out of one run record."""
    m: dict[str, float] = {}
    timing = record.get('timing', {})
    if 'total_wall_s' in timing:
        m['total_wall_s'] = timing['total_wall_s']
    for stage, secs in timing.get('stages', {}).items():
        m[f'stage:{stage}'] = secs
    for label, entry in record.get('outputs', {}).items():
        if label == 'clips':
            if 'count' in entry:
                m['clips.count'] = entry['count']
            if 'total_size' in entry:
                m['clips.total_size'] = entry['total_size']
            continue
        if 'size' in entry:
            m[f'{label}.size'] = entry['size']
        if entry.get('ssim') is not None:
            m[f'{label}.ssim'] = entry['ssim']
    return m


def aggregate(records: list[dict]) -> dict[str, dict]:
    """mean/median/stdev/min/max per metric across run records."""
    series: dict[str, list[float]] = {}
    for rec in records:
        for k, v in flatten_metrics(rec).items():
            series.setdefault(k, []).append(v)

    stats: dict[str, dict] = {}
    for k, vals in series.items():
        stats[k] = {
            'n':      len(vals),
            'mean':   statistics.mean(vals),
            'median': statistics.median(vals),
            'stdev':  statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            'min':    min(vals),
            'max':    max(vals),
        }
    return stats


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_reference(args: argparse.Namespace) -> int:
    from nofun.video import QUAD_FILTER

    source = pathlib.Path(args.source)
    dest   = pathlib.Path(args.dest)
    if not source.is_file():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    perf = source.stem

    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(source),
           '-filter_complex', QUAD_FILTER]
    for cam in CAM_LABELS:
        cmd += ['-map', f'[{_CAM_TO_QUAD[cam]}]', '-c:v', 'mjpeg', '-q:v', '2',
                str(dest / f'{perf}_{cam}.mov')]
    print('building near-lossless reference crops ->', dest)
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        for cam in CAM_LABELS:
            print('  ', (dest / f'{perf}_{cam}.mov').name)
    return rc


def cmd_analyze(args: argparse.Namespace) -> int:
    log_text = None
    if args.log:
        log_text = pathlib.Path(args.log).read_text(errors='replace')

    videos = pathlib.Path(args.videos)
    audio = pathlib.Path(args.audio)
    password = args.nas_pass or os.environ.get('NOFUN_NAS_PASS')
    roots = {r for r in (_unc_root(videos), _unc_root(audio)) if r}

    with _nas_auth(roots, args.nas_user, password):
        record = analyze_run(
            perf=args.perf,
            videos=videos,
            clips=pathlib.Path(args.clips),
            audio=audio,
            reference=pathlib.Path(args.reference) if args.reference else None,
            log_text=log_text,
        )

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    out = RUNS_DIR / f'{stamp}_{args.perf}.json'
    out.write_text(json.dumps(record, indent=2))

    v = record['verdict']
    print(f"\nsmoke analyze: {args.perf}   {'PASS' if v['pass'] else 'FAIL'}")
    if v['missing']:
        print('  missing:    ', ', '.join(v['missing']))
    if v['problematic']:
        print('  problematic:', ', '.join(v['problematic']))
    for label in CAM_LABELS:
        e = record['outputs'].get(label)
        if e:
            s = e.get('ssim')
            print(f"  {label}: {e['size']:>12,} B  ssim={s if s is None else round(s, 4)}")
    if 'total_wall_s' in record['timing']:
        print(f"  wall: {record['timing']['total_wall_s']:.0f}s "
              f"({record['timing'].get('detect')} -> {record['timing'].get('idle')})")
    print('  ->', out)
    return 0 if v['pass'] else 1


def cmd_average(args: argparse.Namespace) -> int:
    files = sorted(RUNS_DIR.glob('*.json'))
    if args.perf:
        files = [f for f in files if f.stem.endswith(args.perf)]
    if not files:
        print(f"no run records in {RUNS_DIR}", file=sys.stderr)
        return 2
    records = [json.loads(f.read_text()) for f in files]
    stats = aggregate(records)

    print(f"\nsmoke average over {len(records)} run(s):\n")
    print(f"  {'metric':<28} {'n':>3} {'mean':>14} {'median':>14} {'stdev':>12}")
    for k in sorted(stats):
        s = stats[k]
        print(f"  {k:<28} {s['n']:>3} {s['mean']:>14,.2f} "
              f"{s['median']:>14,.2f} {s['stdev']:>12,.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    pr = sub.add_parser('reference', help='build near-lossless CAM reference crops')
    pr.add_argument('source', help='source .mov (the trimmed fixture)')
    pr.add_argument('--dest', required=True, help='output dir for reference crops')
    pr.set_defaults(func=cmd_reference)

    pa = sub.add_parser('analyze', help='probe rebuilt outputs + SSIM + timing')
    pa.add_argument('perf', help='performance stem, e.g. 01-01-01_SMOKETEST')
    pa.add_argument('--videos', required=True, help='dir holding _CAM*.mp4 + _INSTAGRAM.mp4')
    pa.add_argument('--clips',  required=True, help='clips root (holds <perf>.N/)')
    pa.add_argument('--audio',  required=True, help='dir holding _AUDIO.mp3 + _MULTITRACK.zip')
    pa.add_argument('--reference', help='reference-crop dir for SSIM (optional)')
    pa.add_argument('--log', help='engine log file to parse run timing from (optional)')
    pa.add_argument('--nas-user', default='nofunadmin',
                    help='SMB user for UNC video/audio roots (net use auth; default nofunadmin)')
    pa.add_argument('--nas-pass',
                    help='SMB password; falls back to $NOFUN_NAS_PASS. Needed to read NAS '
                         'outputs over an SSH-key session (no ambient SMB auth there)')
    pa.set_defaults(func=cmd_analyze)

    pv = sub.add_parser('average', help='aggregate scratch/smoke_runs/*.json')
    pv.add_argument('--perf', help='only records for this perf stem')
    pv.set_defaults(func=cmd_average)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
