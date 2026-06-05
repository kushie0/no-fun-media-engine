"""nofun/inventory.py — File scanner, date parser, classifier, state machine, dashboards."""

__all__ = [
    'scan_files',
    'extract_date_band',
    'extract_date_band_from_path',
    'short_date',
    'perf_key',
    'classify_file',
    'classify_location',
    'build_performance_states',
    'build_state_dashboard',
    'rows_from_db',
    'PerformanceState',
    'EXPIRE_AGE',
    'RAW_EXPIRE_AGE',
]

# Age thresholds (days from recording date)
EXPIRE_AGE     = 28   # cloud lease length; sync stops at this age, expiry deletes after
RAW_EXPIRE_AGE = 14   # delete local raw video + audio after this many days

import concurrent.futures
import dataclasses
import datetime
import os
import pathlib
import re
from typing import Iterator


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

RECORDER_PAT = re.compile(
    r'^R_(\d{4})(\d{2})(\d{2})-\d{6}(am|pm)?.*\.wav$', re.IGNORECASE
)
SHORT_DATE   = re.compile(r'^(\d{2})-(\d{1,2})-(\d{1,2})(.*)')
LONG_DATE    = re.compile(r'^(\d{4})(\d{2})(\d{2})(.*)')
JUNK_SUFFIX  = re.compile(
    r'(_chan[\d.]*|_ch\d+|Video\d*|Audio|h265|\.h265|\.mov|\.mp4|\.wav|\.zip|AUDIO|INSTAGRAM|MULTITRACK|reel|FULLSET|temp|\d+|[._\s]|CAM\d+|UL|UR|LL|LR)+$',
    re.IGNORECASE
)
LEAD_JUNK = re.compile(r'^[_\s]+')
INNER_WS  = re.compile(r'\s+')
QUAD_RE = re.compile(r'_(CAM[1-4])\.mp4$', re.IGNORECASE)
FULLSET_RE = re.compile(r'_AUDIO\b', re.IGNORECASE)
REEL_RE    = re.compile(r'_INSTAGRAM\.mp4$', re.IGNORECASE)

MEDIA_EXTS = {'.mov', '.mp4', '.wav', '.zip'}
_MEDIA_EXT_RAW = frozenset({'mov', 'mp4', 'wav', 'zip'})  # without dot, for scandir loop


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def _scan_root(root: pathlib.Path) -> list[dict]:
    """Walk one directory tree with os.scandir(); return matching file dicts.

    DirEntry.is_file() / is_dir() use the cached d_type from readdir() on
    Linux/macOS — no extra lstat() syscall per entry.  DirEntry.stat() is
    also cached on Windows (from FindNextFile).
    """
    media_exts = _MEDIA_EXT_RAW
    results: list[dict] = []
    stack = [str(root)]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            name = entry.name
                            dot  = name.rfind('.')
                            if dot != -1 and name[dot + 1:].lower() in media_exts:
                                st = entry.stat(follow_symlinks=False)
                                results.append({
                                    'fullpath': pathlib.Path(entry.path),
                                    'filename': name,
                                    'size':     st.st_size,
                                    'mtime':    datetime.datetime.fromtimestamp(st.st_mtime),
                                })
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    return results


def scan_files(search_paths: list[pathlib.Path],
               limit: int | None = None) -> Iterator[dict]:
    """Yield file metadata dicts; roots are scanned concurrently via threads."""
    roots = [p for p in search_paths if p.exists()]
    if not roots:
        return

    count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(roots), 4)) as pool:
        futures = [pool.submit(_scan_root, root) for root in roots]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch = future.result()
            except Exception:
                continue
            for meta in batch:
                yield meta
                count += 1
                if limit and count >= limit:
                    for f in futures:
                        f.cancel()
                    return


# ---------------------------------------------------------------------------
# Date / band extraction
# ---------------------------------------------------------------------------

def _clean_band(raw: str) -> str:
    """Strip leading/trailing junk (quadrant labels, extensions, ch nums) from a band name."""
    stripped = LEAD_JUNK.sub('', raw)
    cleaned  = JUNK_SUFFIX.sub('', stripped)
    cleaned  = INNER_WS.sub('_', cleaned.strip())
    return cleaned if cleaned else 'TBD'


def extract_date_band(filename: str) -> tuple[str, str]:
    """Return (YY-MM-DD, band_name) parsed from filename, or ('TBD','TBD')."""
    if m := RECORDER_PAT.match(filename):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "Audio Recorder"
    if m := SHORT_DATE.match(filename):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:02d}-{mo:02d}-{d:02d}", _clean_band(m.group(4))
    if m := LONG_DATE.match(filename):
        # Long dates (YYYY-MM-DD) truncated to YY-MM-DD for consistency
        return f"{m.group(1)[2:]}-{m.group(2)}-{m.group(3)}", _clean_band(m.group(4))
    return 'TBD', 'TBD'


def extract_date_band_from_path(path: pathlib.Path) -> tuple[str, str]:
    """Like extract_date_band(path.stem), but falls back to the parent folder's
    YY-MM-DD prefix when the file itself has no date prefix.

    SharePoint cloud copies are named ``BAND_CAM1.mp4`` (no date) — the date lives
    in the parent folder name like ``26-05-17_BAND_OTHER/``.
    """
    date, band = extract_date_band(path.stem)
    if date != 'TBD':
        return date, band
    if m := SHORT_DATE.match(path.parent.name):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:02d}-{mo:02d}-{d:02d}", _clean_band(path.stem)
    return 'TBD', 'TBD'


def short_date(date: str) -> str:
    """Normalise any parsed date to YY-MM-DD. extract_date_band already returns
    this form; this only re-truncates a long YYYY-MM-DD that leaked in via the DB."""
    return date[2:] if len(date) == 10 and date.startswith('20') else date


def perf_key(date: str, band: str) -> str:
    """Canonical performance identity: '<YY-MM-DD>_<band>'. The single source of
    truth for the DB key, ZIP stem, reconciler key, and status map."""
    return f'{short_date(date)}_{band}'


def files_for_perf(
    directory: pathlib.Path, suffix: str, perf: str
) -> list[pathlib.Path]:
    """Files in ``directory`` ending with ``suffix`` whose normalised
    ``(date, band)`` identity equals ``perf``.

    A literal-prefix glob (``{perf}*{suffix}``) misses outputs whose on-disk band
    spelling differs from the canonical perf key — e.g. a multi-band recording
    encoded as ``26-05-13_B hvpie.25_CAM1.mp4`` (space + session suffix) when the
    perf key is ``26-05-13_B_hvpie``. Matching on the same normalisation the perf
    key itself uses tolerates that gap, so a perf→files lookup stays in sync with
    the audio pipeline (which already writes the normalised name)."""
    if not directory.is_dir():
        return []
    matches = []
    for f in directory.glob(f'*{suffix}'):
        date, band = extract_date_band(f.stem)
        if date != 'TBD' and perf_key(date, band) == perf:
            matches.append(f)
    return sorted(matches)


# ---------------------------------------------------------------------------
# File type classification
# ---------------------------------------------------------------------------

def classify_file(filename: str, fullpath: pathlib.Path) -> str:
    """Map file extension + path to a human-readable type string."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    p   = fullpath.as_posix().lower()
    # REMASTER outputs — not raw source files; exclude from state machine
    if FULLSET_RE.search(filename):
        return 'fullset audio'
    if REEL_RE.search(filename):
        return 'reel video'
    if ext == 'zip':
        return 'zipped audio'
    if ext == 'wav':
        return 'audio'
    if ext == 'mov':
        return 'raw video'
    if ext in ('mp4', 'h265'):
        if '/clips/' in p or '/trial_runs/clips/' in p:
            return 'clip'
        if QUAD_RE.search(filename):
            return 'quadrant'
        if '/quadrants/' in p or '/trial_runs/quadrants/' in p:
            return 'quadrant'
        if '/trial_runs/videos/' in p:
            return 'quadrant' if QUAD_RE.search(filename) else 're-encoded'
        return 're-encoded'
    return 'unknown'


def classify_location(fullpath: pathlib.Path) -> str:
    """Return 'source', 'cloud', or 'archive' based on where the file lives."""
    p = fullpath.as_posix().lower()
    if 'venuelighting' in p:
        return 'source'
    if 'onedrive' in p:
        return 'cloud'
    return 'archive'


# ---------------------------------------------------------------------------
# Per-performance state machine
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PerformanceState:
    """Tracks where all files for a single (date, band) performance live."""
    date: str   # 'YYYY-MM-DD'
    band: str   # 'Band Name'

    mov_files:    list[pathlib.Path] = dataclasses.field(default_factory=list)
    quad_files:   list[pathlib.Path] = dataclasses.field(default_factory=list)
    wav_files:    list[pathlib.Path] = dataclasses.field(default_factory=list)
    zip_files:    list[pathlib.Path] = dataclasses.field(default_factory=list)
    raw_movs:     list[pathlib.Path] = dataclasses.field(default_factory=list)
    raw_wavs:     list[pathlib.Path] = dataclasses.field(default_factory=list)
    cloud_files:  list[pathlib.Path] = dataclasses.field(default_factory=list)
    clip_files:   list[pathlib.Path] = dataclasses.field(default_factory=list)
    fullset_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    reel_files:   list[pathlib.Path] = dataclasses.field(default_factory=list)
    duration_sec: float | None = None  # video duration in seconds (from encoding DB)

    @property
    def recording_date(self) -> datetime.date | None:
        try:
            parts = self.date.split('-')
            return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            return None

    @property
    def age_days(self) -> int | None:
        rd = self.recording_date
        return (datetime.date.today() - rd).days if rd else None

    @property
    def state(self) -> str:
        has_raw_mov   = bool(self.raw_movs)
        has_quads     = len(self.quad_files) >= 4
        has_mov       = bool(self.mov_files)
        has_local_zip = bool(self.zip_files)
        has_cloud_zip = any(f.suffix.lower() == '.zip' for f in self.cloud_files)
        has_wav       = bool(self.raw_wavs or self.wav_files)
        has_any_video = bool(self.raw_movs or self.mov_files or self.quad_files)

        # In-progress states (highest priority)
        if has_raw_mov and not has_quads:
            return 'DETECTED'
        if has_wav and not (has_local_zip or has_cloud_zip):
            return 'AUDIO_PENDING'

        # Content completeness — quads are sufficient, original .mov is optional
        video_ok = has_quads or has_mov
        audio_ok = has_local_zip or has_cloud_zip

        if has_any_video and not (video_ok and audio_ok):
            return 'INCOMPLETE'
        if not has_any_video and not audio_ok:
            return 'INCOMPLETE'

        # Fully archived — check cloud expiry
        age      = self.age_days
        in_cloud = bool(self.cloud_files)

        if in_cloud and age is not None and age > EXPIRE_AGE:
            return 'SHARE_EXPIRED'
        if in_cloud:
            return 'SHARED'
        if age is not None and age < EXPIRE_AGE:
            # Don't share default-named NoFun recordings under 1 minute
            if (self.band.lower() == 'nofun'
                    and self.duration_sec is not None
                    and self.duration_sec < 60):
                return 'COMPLETE'
            return 'SHARE_ELIGIBLE'
        return 'COMPLETE'

    @property
    def missing_components(self) -> list[str]:
        """Components expected for a complete performance that are absent."""
        missing = []
        if not self.raw_movs and not self.mov_files:
            missing.append('video raw')
        if len(self.quad_files) < 4:
            missing.append('quadrants')
        if not self.raw_wavs and not self.wav_files:
            missing.append('audio raw')
        if not self.zip_files:
            missing.append('audio zip')
        if not self.clip_files:
            missing.append('clips')
        if not any(f.suffix.lower() == '.mp4' for f in self.cloud_files):
            missing.append('cloud quadrants')
        if not any(f.suffix.lower() == '.zip' for f in self.cloud_files):
            missing.append('cloud zip')
        return missing

    @property
    def lifecycle_overdue(self) -> list[str]:
        """Lifecycle actions that should have happened already but haven't."""
        age = self.age_days
        if age is None:
            return []
        overdue = []
        if self.cloud_files and age > EXPIRE_AGE:
            overdue.append(f'cloud removal overdue ({age}d)')
        if (self.raw_movs or self.mov_files) and len(self.quad_files) >= 4 and age > RAW_EXPIRE_AGE:
            overdue.append(f'raw video overdue ({age}d)')
        if (self.raw_wavs or self.wav_files) and self.zip_files and age > RAW_EXPIRE_AGE:
            overdue.append(f'raw audio overdue ({age}d)')
        return overdue

    @property
    def actions(self) -> list[str]:
        s = self.state
        if s == 'SHARE_ELIGIBLE':  return ['upload to cloud']
        if s == 'SHARE_EXPIRED':   return ['auto-expired']
        if s == 'INCOMPLETE':      return ['investigate']
        return []


def build_performance_states(rows: list[dict]) -> dict[tuple[str, str], PerformanceState]:
    """Build a {(date, band): PerformanceState} dict from inventory rows."""
    states: dict[tuple[str, str], PerformanceState] = {}

    for row in rows:
        key = (row['date'], row['band'])
        if key not in states:
            states[key] = PerformanceState(date=row['date'], band=row['band'])
        ps   = states[key]
        ftype = row['type']
        loc   = row.get('location', classify_location(row['fullpath']))
        path  = row['fullpath']

        dur = row.get('duration')
        if ftype == 'raw video' and loc == 'source':
            ps.raw_movs.append(path)
            if dur and (ps.duration_sec is None or dur > ps.duration_sec):
                ps.duration_sec = dur
        elif ftype == 'raw video':
            ps.mov_files.append(path)
            if dur and (ps.duration_sec is None or dur > ps.duration_sec):
                ps.duration_sec = dur
        elif ftype == 'quadrant' and loc == 'cloud':
            ps.cloud_files.append(path)
            if dur and (ps.duration_sec is None or dur > ps.duration_sec):
                ps.duration_sec = dur
        elif ftype == 'quadrant':
            ps.quad_files.append(path)
            if dur and (ps.duration_sec is None or dur > ps.duration_sec):
                ps.duration_sec = dur
        elif ftype == 'audio' and loc == 'source':
            ps.raw_wavs.append(path)
        elif ftype == 'audio':
            ps.wav_files.append(path)
        elif ftype == 'zipped audio' and loc == 'cloud':
            ps.cloud_files.append(path)
        elif ftype == 'zipped audio':
            ps.zip_files.append(path)
        elif ftype == 'clip':
            ps.clip_files.append(path)
        elif ftype == 'fullset audio':
            ps.fullset_files.append(path)
        elif ftype == 'reel video':
            ps.reel_files.append(path)

    return states


# ---------------------------------------------------------------------------
# Dashboard builders
# ---------------------------------------------------------------------------

_STATE_ICON = {
    'DETECTED':       '→',
    'AUDIO_PENDING':  '♪',
    'INCOMPLETE':     '⚠',
    'COMPLETE':       '✓',
    'SHARE_ELIGIBLE': '✓',
    'SHARED':         '☁',
    'SHARE_EXPIRED':  '✗',
}

_STATE_LABEL = {
    'DETECTED':       'pending encode',
    'AUDIO_PENDING':  'audio pending',
    'INCOMPLETE':     'INCOMPLETE',
    'COMPLETE':       'complete',
    'SHARE_ELIGIBLE': 'complete [upload?]',
    'SHARED':         'shared',
    'SHARE_EXPIRED':  'expired',
}


def _render_state_dashboard(
    states: dict[tuple[str, str], PerformanceState],
    file_count: int,
) -> str:
    """Render the per-performance state dashboard."""
    SEP = '=' * 102
    DIV = '-' * 100

    ts = datetime.datetime.now().strftime('%y-%m-%d  %H:%M')
    lines = [
        SEP,
        f"  MEDIA INVENTORY                                                    {ts}",
        SEP,
        "",
        f"  {'Date':<12} {'Band':<28} {'Mov':>3} {'Q':>3} {'Clips':>5} {'MP3':>3} {'Reel':>4} "
        f"{'Wav':>3} {'Zip':>3} {'Cloud':>8}   State",
        "  " + DIV,
    ]

    clutter_keys = {
        k for k in states
        if k[0] == 'TBD' or k[1] in ('Audio Recorder', 'TBD') or k[1].startswith('R_')
    }
    main_keys = sorted(states.keys() - clutter_keys, reverse=True)

    total_gb = 0.0

    def _row(ps: PerformanceState) -> str:
        nonlocal total_gb
        all_files = (ps.mov_files + ps.quad_files + ps.wav_files +
                     ps.zip_files + ps.raw_movs + ps.raw_wavs + ps.cloud_files +
                     ps.clip_files + ps.fullset_files + ps.reel_files)
        gb = sum(f.stat().st_size for f in all_files if f.exists()) / 1_073_741_824
        total_gb += gb

        mov   = str(len(ps.mov_files) + len(ps.raw_movs)) if (ps.mov_files or ps.raw_movs) else '-'
        quad  = str(len(ps.quad_files)) if ps.quad_files else '-'
        clips = str(len(ps.clip_files)) if ps.clip_files else '-'
        mp3   = str(len(ps.fullset_files)) if ps.fullset_files else '-'
        reel  = str(len(ps.reel_files)) if ps.reel_files else '-'
        wav   = str(len(ps.wav_files) + len(ps.raw_wavs)) if (ps.wav_files or ps.raw_wavs) else '-'
        zp    = str(len(ps.zip_files)) if ps.zip_files else '-'

        age = ps.age_days
        cloud_str = '-'
        if ps.cloud_files:
            cloud_str = f'{age}d' if age is not None else '?d'

        state  = ps.state
        icon   = _STATE_ICON.get(state, '?')
        label  = _STATE_LABEL.get(state, state)

        b = (ps.band[:24] + '..') if len(ps.band) > 26 else ps.band
        # Strip 4-digit year prefix for display: '2026-02-07' → '26-02-07'
        d = short_date(ps.date)

        return (
            f"  {d:<12} {b:<28} {mov:>3} {quad:>3} {clips:>5} {mp3:>3} {reel:>4} "
            f"{wav:>3} {zp:>3} {cloud_str:>8}   {icon} {label}"
        )

    for key in main_keys:
        lines.append(_row(states[key]))

    if clutter_keys:
        lines += ["", f"  -- Unclassified / Unsorted {DIV[:67]}", ""]
        for key in sorted(clutter_keys):
            lines.append(_row(states[key]))

    expired_count  = sum(1 for ps in states.values() if ps.state == 'SHARE_EXPIRED')
    eligible_count = sum(1 for ps in states.values() if ps.state == 'SHARE_ELIGIBLE')

    lines += [
        "",
        "  " + DIV,
        f"  {len(main_keys)} performances  |  {total_gb:.1f} GB  |  {file_count} files indexed",
    ]
    if expired_count:
        lines.append(f"  ✗  {expired_count} cloud share(s) auto-expired and cleaned up")
    if eligible_count:
        lines.append(f"  ☁  {eligible_count} performance(s) eligible to upload to cloud")
    lines.append(SEP)

    return '\n'.join(lines)


def build_state_dashboard(rows: list[dict], file_count: int) -> str:
    """Build the per-performance state dashboard from inventory rows."""
    states = build_performance_states(rows)
    return _render_state_dashboard(states, file_count)


# ---------------------------------------------------------------------------
# Legacy dashboard (kept for backward compat / existing tests)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DB → rows bridge
# ---------------------------------------------------------------------------

# Maps encoding_db category names to the type strings used by build_performance_states()
_CATEGORY_TO_TYPE: dict[str, str] = {
    'quadrant_video': 'quadrant',
    'clips':          'clip',
    'zipped_audio':   'zipped audio',
    'raw_video':      'raw video',
    'source_audio':   'audio',
}


def rows_from_db(db) -> list[dict]:
    """Convert EncodingDB records to the rows format for build_performance_states().

    Each returned dict has: date, band, type, location, fullpath, filename,
    size, size_gb, mtime — the same keys produced by scan_files() + classify_*().
    Records without a stored ``type`` field fall back to the category mapping;
    records without a ``location`` field fall back to classify_location().
    """
    rows: list[dict] = []
    for date, band, perf in db.all_performances():
        cs = perf.get('clips_summary')
        if isinstance(cs, dict) and cs.get('count') and pathlib.Path(cs.get('dir', '')).is_dir():
            rows.append({
                'date':     date,
                'band':     band,
                'type':     'clip',
                'location': 'archive',
                'fullpath': pathlib.Path(cs['dir']),
                'filename': '',
                'size':     cs.get('total_size', 0),
                'size_gb':  cs.get('total_size', 0) / 1_073_741_824,
                'mtime':    datetime.datetime.now(),
            })

        for category, records in perf.items():
            default_type = _CATEGORY_TO_TYPE.get(category)
            if default_type is None or not isinstance(records, list):
                continue
            for rec in records:
                path_str = rec.get('path', '')
                if not path_str:
                    continue
                p     = pathlib.Path(path_str)
                ftype = rec.get('type') or default_type
                loc   = rec.get('location') or classify_location(p)
                raw_mtime = rec.get('mtime', 0)
                mtime_dt  = (
                    datetime.datetime.fromtimestamp(raw_mtime)
                    if isinstance(raw_mtime, (int, float))
                    else datetime.datetime.now()
                )
                size = rec.get('size', 0)
                row: dict = {
                    'date':     date,
                    'band':     band,
                    'type':     ftype,
                    'location': loc,
                    'fullpath': p,
                    'filename': p.name,
                    'size':     size,
                    'size_gb':  size / 1_073_741_824,
                    'mtime':    mtime_dt,
                }
                dur = rec.get('duration')
                if dur is not None:
                    row['duration'] = dur
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Status display helpers (used by menu_inventory mixin + media_engine)
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    'NEW':                      '→',
    'INCOMPLETE VIDEO':         '⚠',
    'INCOMPLETE AUDIO':         '⚠',
    'INCOMPLETE VIDEO + AUDIO': '⚠',
    'SHARED':                   '☁',
    'UNSHARED':                 '✓',
}


def _status_label(ps: object) -> tuple[str, str]:
    """Map a PerformanceState to a (user-facing label, Rich colour) pair."""
    has_quads = len(ps.quad_files) >= 4  # type: ignore[attr-defined]
    has_mov   = bool(ps.mov_files)  # type: ignore[attr-defined]
    has_zip   = bool(ps.zip_files) or any(  # type: ignore[attr-defined]
        f.suffix.lower() == '.zip' for f in ps.cloud_files)  # type: ignore[attr-defined]
    has_wav   = bool(ps.raw_wavs or ps.wav_files)  # type: ignore[attr-defined]
    has_raw   = bool(ps.raw_movs)  # type: ignore[attr-defined]
    in_cloud  = bool(ps.cloud_files)  # type: ignore[attr-defined]
    video_ok  = has_quads or has_mov
    audio_ok  = has_zip

    if has_raw and not has_quads:
        return 'NEW', 'yellow'
    if not video_ok and not audio_ok:
        return 'INCOMPLETE VIDEO + AUDIO', 'bold red'
    if not video_ok:
        return 'INCOMPLETE VIDEO', 'bold red'
    if not audio_ok and has_wav:
        return 'INCOMPLETE AUDIO', 'yellow'
    if in_cloud:
        return 'SHARED', 'green'
    return 'UNSHARED', 'cyan'
