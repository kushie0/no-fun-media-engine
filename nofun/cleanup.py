"""nofun/cleanup.py — archive_or_dedup free function + CleanupMixin."""

import dataclasses
import datetime
import logging
import pathlib
import queue
import re
import shutil
import zipfile
from enum import Enum

from nofun.check_encoding import probe_video
from nofun.inventory import (
    EXPIRE_AGE,
    RAW_EXPIRE_AGE,
    build_performance_states,
    classify_file,
    classify_location,
    extract_date_band,
    perf_key,
    perf_output_name,
    scan_files,
)
from nofun.media_io import DeleteQueue, fmt_size
from nofun.video import CAM_LABELS


# ---------------------------------------------------------------------------
# archive_or_dedup outcome
# ---------------------------------------------------------------------------

import enum as _enum


class ArchiveOutcome(str, _enum.Enum):
    MOVED        = 'moved'
    DEDUPED      = 'deduped'
    LOCKED_SIZE  = 'locked_size'
    LOCKED_OSERR = 'locked_oserr'


# ---------------------------------------------------------------------------
# SharePoint folder naming
# ---------------------------------------------------------------------------

_BAND_SKIP = {'NOFUN', 'TBD', ''}

_SP_SYSTEM_FILES = {'_nofun_info.txt', 'desktop.ini', 'Thumbs.db', '.DS_Store'}

_DATE_PREFIX_RE = re.compile(r'^(?:\d{2}-\d{1,2}-\d{1,2}|\d{8})_')


def cloud_filename(src_name: str) -> str:
    """Strip the leading date prefix from a source filename for SharePoint upload.

    Source files are named ``YY-M-D_BAND_CAM1.mp4`` or ``YYYYMMDD_BAND_MULTITRACK.zip``; the
    date is redundant in the cloud because the parent folder already carries it.
    Idempotent — if no date prefix is present, returns src_name unchanged.
    """
    return _DATE_PREFIX_RE.sub('', src_name, count=1)


def plan_cloud_copy(dest_exists: bool, legacy_exists: bool,
                    legacy_dehydrated: bool, overwrite: bool) -> str:
    """Decide how to place a file in its SharePoint folder.

    Returns one of:
      'overwrite' — a cloud copy already exists and overwrite was requested (replace in place)
      'skip'      — cloud copy exists (and overwrite not requested), or only a dehydrated
                    dated placeholder exists (renaming it would force a download)
      'rename'    — a hydrated dated copy exists under the source name → rename in place
      'copy'      — nothing in the folder yet → copy from source

    Pure (no I/O) so the decision table can be unit-tested. Callers pass the results of
    ``dest.exists()`` / ``legacy.exists()`` / ``is_cloud_only(legacy)``.
    """
    if dest_exists:
        return 'overwrite' if overwrite else 'skip'
    if legacy_exists and legacy_dehydrated:
        return 'skip'
    if legacy_exists:
        return 'rename'
    return 'copy'


def expected_cloud_names(base: str, zip_src: pathlib.Path | None) -> list[str]:
    """Cloud filenames a performance will have once encoded: 4 quads + audio ZIP.

    base: source stem without the quad suffix (e.g. '26-05-24_BAND').
    zip_src: the real ZIP path if it already exists; otherwise the name is
             predicted from base (the date prefix is stripped either way).
    Used to mark not-yet-produced files as "processing…" in _nofun_info.txt.
    """
    names = [cloud_filename(f'{base}_{q}.mp4') for q in CAM_LABELS]
    names.append(cloud_filename(zip_src.name) if zip_src else cloud_filename(base) + '_MULTITRACK.zip')
    return names


def _band_token(raw_band: str) -> str:
    """Convert a raw band name to a folder-name token.

    Strips underscores and whitespace; if the result is >15 chars, uses a
    first-letter acronym derived from the original word boundaries instead.
    Whitespace must be stripped because SharePoint normalizes trailing spaces
    out of folder names, which broke the rename idempotency check.
    Examples:
      'MALL_GOTH'                        → 'MALLGOTH'
      'Sara Devoe'                       → 'SARADEVOE'
      'THEY_ARE_GUTTING_A_BODY_OF_WATER' → 'TAGABOW'
    """
    normalized = re.sub(r'[\s_]+', '', raw_band).upper()
    if len(normalized) > 15:
        return ''.join(w[0].upper() for w in re.split(r'[\s_]+', raw_band) if w)
    return normalized


def _tokens_from_folder_name(folder_name: str, date_prefix: str) -> list[str]:
    """Extract the band tokens already present in a SharePoint folder name.

    e.g. '26-04-07_PRIZE_MALLGOTH' with date_prefix '26-04-07'
         → ['PRIZE', 'MALLGOTH']
    Returns [] for bare date names like '26-04-07'.
    """
    n = len(date_prefix)
    if len(folder_name) > n and folder_name[:n] == date_prefix and folder_name[n] in ' _-':
        return [t for t in folder_name[n + 1:].split('_') if t]
    return []


def make_sharepoint_folder_name(
    date_prefix: str,
    folder: pathlib.Path,
    new_band: str,
) -> str:
    """Return the folder name that should be used after uploading new_band.

    date_prefix: 'YY-MM-DD'  (e.g. '26-04-07')
    folder:      current path of the SharePoint date folder (may not exist yet)
    new_band:    raw band name from the pipeline (e.g. 'MALL_GOTH')

    Reads existing band tokens from the folder NAME (not file contents), so
    manually renamed folders like '26-04-07_MXLONELY_HALOBITE_MALLGOTH_PRIZE'
    are respected and never overwritten — only genuinely new bands are appended.

    Rules:
    - Ignores NoFun / TBD bands.
    - Strips underscores from new band; uses first-letter acronym if >15 chars.
    - Won't re-add a band already present (case-insensitive compare).
    - If resulting name would exceed 50 chars, collapses all tokens to their
      first letter.
    - Returns plain date_prefix if no valid bands exist.
    """
    norm_new = new_band.replace('_', '').upper()
    current_name = folder.name
    tokens = _tokens_from_folder_name(current_name, date_prefix)

    if norm_new not in _BAND_SKIP:
        existing_upper = {t.upper() for t in tokens}
        new_token = _band_token(new_band)
        if new_token.upper() not in existing_upper:
            tokens = tokens + [new_token]

    if not tokens:
        return date_prefix

    candidate = date_prefix + '_' + '_'.join(tokens)
    if len(candidate) <= 50:
        return candidate

    # Total too long — collapse every token to its first letter
    short_tokens = [t[:5].upper() for t in tokens if t]
    return date_prefix + '_' + '_'.join(short_tokens)


def canonical_sharepoint_name(date_prefix: str, bands: list[str]) -> str:
    """Compute the canonical SharePoint folder name for a date from all its bands.

    Unlike make_sharepoint_folder_name (which adds one band to an existing
    folder name), this builds from scratch using a deduplicated list of bands.
    Use this when you know all the bands for a date and want the authoritative
    target name — e.g. for reconciling folder names that may have accumulated
    duplicate tokens.

    date_prefix: 'YY-MM-DD'
    bands:       raw band names from inventory/pipeline (any order, may include
                 NOFUN/TBD — those are silently skipped)
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for band in bands:
        if band.replace('_', '').upper() in _BAND_SKIP:
            continue
        tok = _band_token(band)
        if tok.upper() not in seen:
            tokens.append(tok)
            seen.add(tok.upper())
    if not tokens:
        return date_prefix
    candidate = date_prefix + '_' + '_'.join(tokens)
    if len(candidate) <= 50:
        return candidate
    short_tokens = [t[:5].upper() for t in tokens if t]
    return date_prefix + '_' + '_'.join(short_tokens)


# ---------------------------------------------------------------------------
# SharePoint info file
# ---------------------------------------------------------------------------

_TS_INDENT  = '    '   # 4 spaces — timestamp lines under a file entry
_FILE_INDENT = '  '    # 2 spaces — file entry lines


def _fmt_ts(dt: datetime.datetime | None = None) -> str:
    """Format a datetime as 'Apr 7, 2026  3:45pm' (cross-platform, no %-d)."""
    if dt is None:
        dt = datetime.datetime.now()
    hour = dt.hour % 12 or 12
    ampm = 'am' if dt.hour < 12 else 'pm'
    return dt.strftime(f'%b {dt.day}, %Y  {hour}:%M{ampm}')


def _parse_file_timestamps(txt_path: pathlib.Path) -> dict[str, list[str]]:
    """Read existing _nofun_info.txt and extract {filename: [timestamp, ...]}."""
    if not txt_path.exists():
        return {}
    history: dict[str, list[str]] = {}
    current: str | None = None
    for line in txt_path.read_text(encoding='utf-8').splitlines():
        if line.startswith(_TS_INDENT) and not line.startswith(_TS_INDENT + ' '):
            # Timestamp line under the current file
            if current is not None:
                history.setdefault(current, []).append(line[len(_TS_INDENT):])
        elif line.startswith(_FILE_INDENT) and not line.startswith(_TS_INDENT):
            # File entry line — extract first token as filename
            parts = line.split()
            current = parts[0] if parts and '.' in parts[0] else None
    return history


def write_sharepoint_info(
    folder: pathlib.Path,
    media_files: list[pathlib.Path],
    expire_date: datetime.date,
    is_cleaned: bool = False,
    new_files: list[pathlib.Path] | None = None,
    expected_names: list[str] | None = None,
) -> None:
    """Update _nofun_info.txt in a SharePoint date folder.

    Preserves per-file upload history from previous versions of the file.
    new_files: files that were just uploaded/re-uploaded this run — they get
               a new timestamp appended.  Defaults to all media_files if the
               file is being created for the first time, or new_files if provided.
    expected_names: cloud filenames that *should* end up in this folder but
               haven't been produced/copied yet (e.g. quads still encoding).
               Each absent one is listed with a "processing…" marker so the
               folder is informative during recording/encoding. Markers sit in
               the size column (a file-entry line, never a timestamp sub-line),
               so they carry no history and the file converges to the normal
               present-only form once every expected file lands.
    """
    txt_path  = folder / '_nofun_info.txt'
    history   = _parse_file_timestamps(txt_path)
    now_str   = _fmt_ts()

    # Determine which files get a new timestamp stamped right now
    if new_files is None:
        stamp_set = {f.name for f in media_files}   # first write: stamp everything
    else:
        stamp_set = {f.name for f in new_files}

    # Append new timestamps into history
    for fname in stamp_set:
        existing = history.get(fname, [])
        label    = 'uploaded' if not existing else 're-uploaded'
        history.setdefault(fname, []).append(f'{label}  {now_str}')

    name = folder.name
    lines: list[str] = [
        'NO FUN TROY — MULTITRACK ARCHIVE',
        name,
        '',
    ]

    if is_cleaned:
        lines += [
            'the files are cleaned up — just ask no fun staff to reupload.',
            "(they'll hook you up, no prob)",
            '',
            'files that were here:',
        ]
        for f in sorted(media_files, key=lambda x: x.name):
            lines.append(f'{_FILE_INDENT}{f.name}')
            for ts in history.get(f.name, []):
                lines.append(f'{_TS_INDENT}{ts}')
    else:
        expire_str = expire_date.strftime(f'%b {expire_date.day}, %Y')
        lines += [
            f'expiry: {expire_date.isoformat()}',
            '',
            "get em while they're hot!!",
            f'files available until approx. {expire_str}',
            '',
        ]
        present    = {f.name: f for f in media_files}
        processing = sorted(
            n for n in (expected_names or [])
            if n not in present and n != '_nofun_info.txt'
        )
        if processing:
            lines += ['still processing — files appear here as they finish.', '']
        for name in sorted(set(present) | set(processing)):
            f = present.get(name)
            if f is None:
                lines.append(f'{_FILE_INDENT}{name:<52}  processing…')
                continue
            try:
                size = fmt_size(f.stat().st_size) if f.exists() else ''
                lines.append(f'{_FILE_INDENT}{name:<52}  {size}')
            except OSError:
                lines.append(f'{_FILE_INDENT}{name}')
            for ts in history.get(name, []):
                lines.append(f'{_TS_INDENT}{ts}')

    lines += ['', 'questions? hit up no fun staff.', '']
    txt_path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------------------
# Audit data types
# ---------------------------------------------------------------------------

class FindingKind(Enum):
    ORPHANED_TEMP     = 'orphaned_temp'
    REDUNDANT_SOURCE  = 'redundant_source'
    ORPHANED_CHANNELS = 'orphaned_channels'
    MISSING_CLIPS     = 'missing_clips'
    ORPHANED_CLIPS    = 'orphaned_clips'
    ARCHIVE_DEDUP     = 'archive_dedup'
    CLOUD_EXPIRED     = 'cloud_expired'
    RAW_VIDEO_EXPIRED = 'raw_video_expired'
    RAW_AUDIO_EXPIRED = 'raw_audio_expired'


@dataclasses.dataclass
class AuditFinding:
    kind:        FindingKind
    label:       str
    files:       list[pathlib.Path]
    action:      str                          # 'delete' | 'move' | 'reprocess'
    destination: pathlib.Path | None = None  # for 'move' actions
    size_bytes:  int = 0
    reason:      str = ''


def _folder_date_from_name(name: str) -> datetime.date | None:
    """Parse the YY-MM-DD prefix from a SharePoint date-folder name."""
    try:
        return datetime.datetime.strptime(name[:8], '%y-%m-%d').date()
    except ValueError:
        return None


def _read_expiry_date(folder: pathlib.Path) -> datetime.date | None:
    """Read the machine-readable expiry tag from _nofun_info.txt, or None.

    Returns None for folders without an info file (legacy) or with an info
    file that lacks/malforms the expiry tag (also legacy). Callers fall back
    to folder-name-based age in those cases.
    """
    txt = folder / '_nofun_info.txt'
    if not txt.exists():
        return None
    try:
        for line in txt.read_text(encoding='utf-8').splitlines():
            if line.startswith('expiry: '):
                return datetime.date.fromisoformat(line[8:].strip())
    except (OSError, ValueError):
        return None
    return None


def _safe_size(f: pathlib.Path) -> int:
    """Return file size without raising on missing files."""
    try:
        return f.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Free function
# ---------------------------------------------------------------------------

def archive_or_dedup(
    src: pathlib.Path,
    archive_dir: pathlib.Path,
    logger: logging.Logger,
    delete_queue: DeleteQueue,
    pipeline_moved: queue.Queue | None = None,
) -> ArchiveOutcome:
    """Move *src* to *archive_dir*, or queue it for deletion if a same-size
    copy already exists there.

    Returns an ArchiveOutcome so callers running batches can aggregate counts
    into a single summary log line instead of relying on per-file output.
    """
    src_size = src.stat().st_size
    existing = archive_dir / src.name
    if existing.exists() and existing.stat().st_size == src_size:
        delete_queue.add(
            src,
            f"duplicate, {fmt_size(src_size)} matches {archive_dir.name}/",
            logger,
        )
        return ArchiveOutcome.DEDUPED

    dst_path = archive_dir / src.name
    if dst_path.exists():
        # Different size on a stable source: either a stub leftover (archive
        # is bigger, source is junk) or an interrupted prior move (source is
        # bigger, archive is the partial). Recover either way instead of
        # looping forever.
        dst_size = dst_path.stat().st_size
        if dst_size > src_size:
            delete_queue.add(
                src,
                f"stub, source {fmt_size(src_size)} < archive {fmt_size(dst_size)}",
                logger,
            )
            return ArchiveOutcome.DEDUPED
        try:
            dst_path.unlink()
        except OSError as e:
            logger.info(f"LOCKED  {src.name} — couldn't unlink partial archive ({e})")
            return ArchiveOutcome.LOCKED_OSERR
        logger.info(
            f"REPLACE {src.name} — archive copy was partial "
            f"({fmt_size(dst_size)} → {fmt_size(src_size)}), re-archiving"
        )
        # fall through to the normal move below

    # Register intent BEFORE the (slow) filesystem op so a watchdog scan
    # mid-move doesn't see the source as "disappeared externally".
    if pipeline_moved is not None:
        pipeline_moved.put(str(src))
    try:
        shutil.move(str(src), str(archive_dir))
    except OSError as e:
        logger.info(
            f"LOCKED  {src.name} — move failed, will retry next loop ({e})"
        )
        return ArchiveOutcome.LOCKED_OSERR
    logger.info(
        f"MOVE    {src.name} → {archive_dir.name}/",
        extra={
            'src':  str(src),
            'dst':  str(archive_dir / src.name),
            'size': f"{src_size} ({fmt_size(src_size)})",
        },
    )
    return ArchiveOutcome.MOVED


# ---------------------------------------------------------------------------
# CleanupMixin
# ---------------------------------------------------------------------------

class CleanupMixin:
    """Provides _archive_or_dedup(), _cleanup_scan(), and
    _queue_expired_cloud_removals() for the Pipeline class."""

    # Attributes provided by Pipeline.__init__
    search_dir:       pathlib.Path
    audio_dest:       pathlib.Path
    audio_archive:    pathlib.Path
    video_archive:    pathlib.Path
    vids_dest:        pathlib.Path
    clips_dest:       pathlib.Path
    mount_d:          pathlib.Path
    sharepoint_dest:  pathlib.Path | None
    logger:           logging.Logger
    delete_queue:     DeleteQueue
    trial_run:        int
    force:            bool

    # -----------------------------------------------------------------------
    # Thin wrapper — keeps `self._archive_or_dedup(...)` working in callers
    # -----------------------------------------------------------------------

    def _archive_or_dedup(self, src: pathlib.Path, archive_dir: pathlib.Path,
                          *_extra_check_dirs: pathlib.Path) -> ArchiveOutcome:
        return archive_or_dedup(src, archive_dir,
                                logger=self.logger, delete_queue=self.delete_queue,
                                pipeline_moved=getattr(self, '_pipeline_moved', None))

    # -----------------------------------------------------------------------
    # Pipeline audit
    # -----------------------------------------------------------------------

    def _check_orphaned_temps(self) -> list[AuditFinding]:
        """Stray temp files left behind by interrupted encodes or trial runs."""
        findings = []
        scan_dirs = [self.search_dir]
        if hasattr(self, 'vids_dest') and self.vids_dest.is_dir():
            scan_dirs.append(self.vids_dest)
        for scan_dir in scan_dirs:
            for pattern in ('*_temp_*.mp4', '*_temp.mp4', '*_temp_*.wav', 'temp_trial_*'):
                for f in scan_dir.glob(pattern):
                    findings.append(AuditFinding(
                        kind=FindingKind.ORPHANED_TEMP,
                        label=f'stray temp: {f.name}',
                        files=[f], action='delete',
                        reason='stray temp file',
                        size_bytes=_safe_size(f),
                    ))
        return findings

    def _check_redundant_mov_sources(self) -> list[AuditFinding]:
        """Source .mov files whose all 4 quadrant outputs already exist."""
        findings = []
        for f in sorted(self.search_dir.glob('*.mov')):
            base = f.stem
            if all((self.vids_dest / f'{base}_{q}.mp4').exists()
                   for q in CAM_LABELS):
                findings.append(AuditFinding(
                    kind=FindingKind.REDUNDANT_SOURCE,
                    label=f'{base}: source .mov (quads complete)',
                    files=[f], action='move',
                    destination=self.video_archive,
                    reason='quadrants exist',
                    size_bytes=_safe_size(f),
                ))
        return findings

    def _check_redundant_wav_sources(self) -> list[AuditFinding]:
        """Original multi-channel WAVs whose _ch01.wav split already exists."""
        findings = []
        for f in sorted(self.search_dir.glob('*.wav')):
            if '_ch' in f.stem:
                continue
            if (f.parent / f'{f.stem}_ch01.wav').exists():
                findings.append(AuditFinding(
                    kind=FindingKind.REDUNDANT_SOURCE,
                    label=f'{f.stem}: original WAV (channels split)',
                    files=[f], action='delete',
                    reason='channel split exists',
                    size_bytes=_safe_size(f),
                ))
        return findings

    def _check_orphaned_channel_wavs(self) -> list[AuditFinding]:
        """Channel _ch??.wav files whose ZIP archive is already complete."""
        findings = []
        all_ch_wavs = sorted(self.search_dir.glob('*_ch??.wav'))
        if not all_ch_wavs:
            return findings
        groups = self._group_wav_files(all_ch_wavs)  # type: ignore[attr-defined]  # AudioMixin
        for group_key, ch_files in groups.items():
            if (self.audio_dest / f'{group_key}_MULTITRACK.zip').exists():
                total = sum(_safe_size(f) for f in ch_files)
                findings.append(AuditFinding(
                    kind=FindingKind.ORPHANED_CHANNELS,
                    label=f'{group_key} ({len(ch_files)} ch)',
                    files=ch_files, action='delete',
                    reason=f'zip exists: {group_key}_MULTITRACK.zip',
                    size_bytes=total,
                ))
        return findings

    def _check_orphaned_hardware_wavs(self) -> list[AuditFinding]:
        """Hardware _chan*.wav files whose ZIP archive is already complete."""
        findings = []
        all_hw_wavs = []
        for scan_dir in (self.search_dir, self.search_dir / 'Audio'):
            if not scan_dir.is_dir():
                continue
            for f in scan_dir.glob('*.wav'):
                if re.search(r'_chan[\d.]*$', f.stem, re.IGNORECASE):
                    all_hw_wavs.append(f)
        if not all_hw_wavs:
            return findings
        groups = self._group_wav_files(all_hw_wavs)  # type: ignore[attr-defined]  # AudioMixin
        db = getattr(self, '_encoding_db', None)
        for group_key, ch_files in groups.items():
            zip_exists = (self.audio_dest / f'{group_key}_MULTITRACK.zip').exists()
            all_silent = False
            if not zip_exists and db is not None:
                parts = group_key.split('_', 1)
                if len(parts) == 2:
                    perf_db = db.get_performance(parts[0], parts[1])
                    all_silent = bool(perf_db and perf_db.get('audio_all_silent'))
            if not (zip_exists or all_silent):
                continue
            reason = (f'zip exists: {group_key}_MULTITRACK.zip' if zip_exists else 'all-silent (DB)')
            total = sum(_safe_size(f) for f in ch_files)
            findings.append(AuditFinding(
                kind=FindingKind.ORPHANED_CHANNELS,
                label=f'{group_key} ({len(ch_files)} hw ch)',
                files=ch_files, action='move',
                destination=self.audio_archive,
                reason=reason,
                size_bytes=total,
            ))
        return findings

    def _check_missing_clips(self) -> list[AuditFinding]:
        """Performances with complete quadrants but missing or incomplete clip files."""
        findings = []
        now = datetime.datetime.now().timestamp()
        quad_bases = {f.stem[:-5] for f in self.vids_dest.glob('*_CAM1.mp4')}
        for base in sorted(quad_bases):
            ul_file = self.vids_dest / f'{base}_CAM1.mp4'
            try:
                if now - ul_file.stat().st_mtime < 3600:
                    continue
            except OSError:
                continue

            clips_dir = self.clips_dest / base
            quads_present = [q for q in CAM_LABELS
                             if (self.vids_dest / f'{base}_{q}.mp4').exists()]
            counts = [
                sum(1 for p in clips_dir.glob(f'{base}_{q}_*.mp4')
                    if p.stem.rsplit('_', 1)[-1].isdigit())
                if clips_dir.exists() else 0
                for q in quads_present
            ]

            # Flag if any quad has no clips, or quads have different clip counts
            if not counts or min(counts) == 0 or len(set(counts)) > 1:
                findings.append(AuditFinding(
                    kind=FindingKind.MISSING_CLIPS,
                    label=f'{base}: clips missing or incomplete',
                    files=[ul_file],
                    action='reprocess',
                    reason='clips not exported or incomplete',
                ))
        return findings

    def _check_orphaned_clip_dirs(self) -> list[AuditFinding]:
        """Clip directories with no corresponding quadrant files."""
        findings = []
        if not self.clips_dest.is_dir():
            return findings
        for clips_dir in sorted(d for d in self.clips_dest.iterdir() if d.is_dir()):
            base = clips_dir.name
            if not any(self.vids_dest.glob(f'{base}_*.mp4')):
                clip_files = list(clips_dir.rglob('*.mp4'))
                total = sum(_safe_size(f) for f in clip_files)
                findings.append(AuditFinding(
                    kind=FindingKind.ORPHANED_CLIPS,
                    label=f'{base}: clips dir (no parent quads)',
                    files=clip_files, action='delete',
                    reason='parent quadrants missing',
                    size_bytes=total,
                ))
        return findings

    def _check_archive_duplicates(self) -> list[AuditFinding]:
        """Archive files whose content already exists in a live output directory."""
        findings = []
        for arc_dir in (self.video_archive, self.audio_archive):
            if not arc_dir.is_dir():
                continue
            for f in sorted(f for f in arc_dir.iterdir() if f.is_file()):
                size = _safe_size(f)
                for live_dir in (self.vids_dest, self.audio_dest):
                    candidate = live_dir / f.name
                    if candidate.exists() and _safe_size(candidate) == size:
                        findings.append(AuditFinding(
                            kind=FindingKind.ARCHIVE_DEDUP,
                            label=f'{f.name}: archive copy (live copy in {live_dir.name}/)',
                            files=[f], action='delete',
                            reason=f'duplicate of {live_dir.name}/{f.name}',
                            size_bytes=size,
                        ))
                        break
        return findings

    def _check_expired_cloud_shares(self) -> list[AuditFinding]:
        """OneDrive folders past their machine-readable expiry date."""
        findings: list[AuditFinding] = []
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            return findings
        today = datetime.date.today()
        for date_dir in sorted(self.sharepoint_dest.iterdir()):
            if not date_dir.is_dir() or date_dir.name == 'archived':
                continue
            media_files = [
                f for f in date_dir.iterdir()
                if f.is_file() and f.name not in _SP_SYSTEM_FILES
            ]
            if not media_files:
                continue
            expiry = _read_expiry_date(date_dir)
            if expiry is None:
                folder_date = _folder_date_from_name(date_dir.name)
                if folder_date is None:
                    # Last-ditch fallback: newest file mtime
                    try:
                        newest_mtime = max(f.stat().st_mtime for f in media_files)
                    except OSError:
                        continue
                    folder_date = datetime.date.fromtimestamp(newest_mtime)
                expiry = folder_date + datetime.timedelta(days=EXPIRE_AGE)
            if today <= expiry:
                continue
            age_str = f"expired {(today - expiry).days}d ago"
            for f in media_files:
                findings.append(AuditFinding(
                    kind=FindingKind.CLOUD_EXPIRED,
                    label=f'{f.name}: cloud removal due ({age_str})',
                    files=[f], action='delete',
                    reason=f'cloud removal due ({age_str})',
                    size_bytes=_safe_size(f),
                ))
        return findings

    def _check_expired_raw_movs(self) -> list[AuditFinding]:
        """Raw .mov files in the archive that are >RAW_EXPIRE_AGE days old and encoded to quads."""
        findings = []
        if not self.video_archive.is_dir():
            return findings
        today = datetime.date.today()
        for mov in self.video_archive.glob('*.mov'):
            date_str, _ = extract_date_band(mov.stem)
            if date_str == 'TBD':
                continue
            try:
                parts = date_str.split('-')
                rec_date = datetime.date(2000 + int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                continue
            age = (today - rec_date).days
            if age <= RAW_EXPIRE_AGE:
                continue
            # Only flag if all 4 quads exist in vids_dest
            base = mov.stem
            quads = [self.vids_dest / f'{base}_{q}.mp4' for q in CAM_LABELS]
            if not all(q.exists() for q in quads):
                continue
            if not self._quads_verified(base):
                self.logger.warning(
                    f"EXPIRE  skipped {mov.name} — quadrant probe failed, raw file kept"
                )
                continue
            findings.append(AuditFinding(
                kind=FindingKind.RAW_VIDEO_EXPIRED,
                label=f'{mov.name}: raw video expired ({age}d, quads exist)',
                files=[mov], action='delete',
                reason=f'raw .mov expired ({age}d > {RAW_EXPIRE_AGE}d), quads on D: drive',
                size_bytes=_safe_size(mov),
            ))
        return findings

    def _check_expired_raw_wavs(self) -> list[AuditFinding]:
        """Raw .wav files in audio_archive >RAW_EXPIRE_AGE days old whose ZIP exists in audio_dest."""
        findings = []
        if not self.audio_archive.is_dir():
            return findings
        today = datetime.date.today()

        # Build set of ZIP perf keys (YY-MM-DD_BAND) matching extract_date_band output.
        zip_stems: set[str] = set()
        if self.audio_dest.is_dir():
            for zf in self.audio_dest.glob('*.zip'):
                zip_stems.add(zf.stem.removesuffix('_MULTITRACK'))

        # Group WAVs by (date, band), age-gated and ZIP-gated
        groups: dict[tuple[str, str], list[pathlib.Path]] = {}
        for wav in self.audio_archive.glob('*.wav'):
            date_str, band = extract_date_band(wav.stem)
            if date_str == 'TBD':
                continue
            try:
                parts = date_str.split('-')
                rec_date = datetime.date(2000 + int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                continue
            if (today - rec_date).days <= RAW_EXPIRE_AGE:
                continue
            if perf_key(date_str, band) not in zip_stems:
                continue
            groups.setdefault((date_str, band), []).append(wav)

        for (date_str, band), wavs in sorted(groups.items()):
            if not self._zip_verified(perf_key(date_str, band)):
                self.logger.warning(
                    f"EXPIRE  skipped raw WAVs for {date_str} {band} — ZIP probe failed"
                )
                continue
            total = sum(_safe_size(f) for f in wavs)
            findings.append(AuditFinding(
                kind=FindingKind.RAW_AUDIO_EXPIRED,
                label=f'{date_str} {band}: {len(wavs)} raw WAV(s) (ZIP exists)',
                files=sorted(wavs),
                action='delete',
                reason=f'raw WAVs expired (>{RAW_EXPIRE_AGE}d), ZIP on D: drive',
                size_bytes=total,
            ))
        return findings

    def _quads_verified(self, base: str) -> bool:
        """Return True if all 4 quadrant MP4s for *base* are probe-able.

        Uses probe_video() which reads only container headers (~0.1s per file).
        Returns False if any quadrant is missing or its codec probes as 'unknown'.
        """
        for q in CAM_LABELS:
            path = self.vids_dest / f'{base}_{q}.mp4'
            if not path.exists():
                return False
            codec, _, _ = probe_video(path)
            if codec == 'unknown':
                return False
        return True

    def _zip_verified(self, perf_key: str) -> bool:
        """Return True if the ZIP archive for *perf_key* is non-empty and readable.

        Checks: file exists, non-zero size, zipfile.namelist() returns ≥1 entry.
        BadZipFile and OSError both return False — raw file is kept.
        """
        zip_path = self.audio_dest / perf_output_name(perf_key, 'multitrack')
        if not zip_path.exists() or zip_path.stat().st_size == 0:
            return False
        try:
            with zipfile.ZipFile(zip_path) as zf:
                return len(zf.namelist()) > 0
        except (zipfile.BadZipFile, OSError):
            return False

    def _auto_expire_raw_files(self) -> None:
        """Delete raw .mov / .wav files in archive past RAW_EXPIRE_AGE once their
        derived outputs (quads / ZIP) exist and probe valid."""
        findings = (
            self._check_expired_raw_movs()
            + self._check_expired_raw_wavs()
        )
        if not findings:
            return
        self._apply_findings(findings)
        self.delete_queue.execute(
            self.logger, getattr(self, '_pipeline_moved', None)
        )

    def _archive_empty_cloud_folders(self) -> None:
        """Move drained SharePoint date folders into archived/.

        Two cases:
          A. Folder is already empty AND older than EXPIRE_AGE — was drained on
             a previous run, just move it.
          B. Folder was drained this run by the expiry sweep — move now.
        Empty folders younger than EXPIRE_AGE are left alone: they're created by
        _maybe_create_sharepoint_placeholder() while recordings are in progress
        and archiving them would silently break SYNC PERFORMANCES.
        """
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            return
        today = datetime.date.today()
        archive_dir = self.sharepoint_dest / 'archived'
        for date_dir in sorted(self.sharepoint_dest.iterdir()):
            if not date_dir.is_dir() or date_dir.name == 'archived':
                continue
            remaining = [
                f for f in date_dir.iterdir()
                if f.is_file() and f.name not in _SP_SYSTEM_FILES
            ]
            if remaining:
                continue
            expiry = _read_expiry_date(date_dir)
            if expiry is None:
                folder_date = _folder_date_from_name(date_dir.name)
                if folder_date is None or (today - folder_date).days <= EXPIRE_AGE:
                    continue
            elif today <= expiry:
                continue
            try:
                archive_dir.mkdir(exist_ok=True)
                date_dir.rename(archive_dir / date_dir.name)
                self.logger.info(f"CLOUDCLEAN: moved {date_dir.name}/ → archived/")
            except OSError as e:
                dest = archive_dir / date_dir.name
                if dest.exists():
                    # Stale copy already in archived/ (e.g. manually placed before this
                    # feature existed). Evict it — but only if it holds no real media —
                    # then retry the rename so the top-level item's SharePoint ID moves
                    # intact (shared links given to bands survive).
                    old_media = [
                        f for f in dest.iterdir()
                        if f.is_file() and f.name not in _SP_SYSTEM_FILES
                    ]
                    if not old_media:
                        try:
                            for f in list(dest.iterdir()):
                                f.unlink(missing_ok=True)
                            dest.rmdir()
                            date_dir.rename(archive_dir / date_dir.name)
                            self.logger.info(
                                f"CLOUDCLEAN: moved {date_dir.name}/ → archived/"
                                f" (replaced stale copy)"
                            )
                        except OSError:
                            pass
                else:
                    self.logger.warning(f"CLOUDCLEAN: could not archive {date_dir.name}/: {e}")

    def _auto_expire_cloud_shares(self) -> None:
        """Delete OneDrive files past their expiry, write cleaned manifests,
        archive empty folders. Driven entirely by _check_expired_cloud_shares
        findings + _apply_findings; this method just composes them."""
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            return
        findings = self._check_expired_cloud_shares()
        if findings:
            self._apply_findings(findings)
            # Group findings by folder for the cleaned-manifest write
            folders: dict[pathlib.Path, list[pathlib.Path]] = {}
            for finding in findings:
                for f in finding.files:
                    folders.setdefault(f.parent, []).append(f)
            self.delete_queue.execute(self.logger, getattr(self, '_pipeline_moved', None))
            for date_dir, files_snapshot in folders.items():
                try:
                    write_sharepoint_info(
                        date_dir, files_snapshot,
                        expire_date=datetime.date.today(), is_cleaned=True,
                        new_files=[],
                    )
                except OSError as e:
                    self.logger.warning(f"CLOUDCLEAN could not write info file: {e}")
        self._archive_empty_cloud_folders()

    def _dehydration_sweep(self) -> None:
        """Re-request dehydration for SharePoint files still physically present.

        Catches files OneDrive failed to dehydrate on the first pass — pinned by
        another app, paused sync, etc. is_cloud_only() reads attributes only and
        never triggers rehydration, so this sweep is safe to run frequently.
        """
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            return
        from nofun.media_io import is_cloud_only, dehydrate_cloud_files
        hydrated: list[pathlib.Path] = []
        for date_dir in self.sharepoint_dest.iterdir():
            if not date_dir.is_dir() or date_dir.name == 'archived':
                continue
            for f in date_dir.iterdir():
                if (f.is_file()
                        and f.name not in _SP_SYSTEM_FILES
                        and not is_cloud_only(f)):
                    hydrated.append(f)
        if hydrated:
            self.logger.info(
                f"DEHYDRATE: {len(hydrated)} file(s) still local — re-requesting"
            )
            dehydrate_cloud_files(hydrated, self.logger)

    def _audit_pipeline_state(self) -> list[AuditFinding]:
        """Run all audit checks and return combined findings."""
        findings: list[AuditFinding] = []
        findings += self._check_orphaned_temps()
        findings += self._check_redundant_mov_sources()
        findings += self._check_redundant_wav_sources()
        findings += self._check_orphaned_channel_wavs()
        findings += self._check_orphaned_hardware_wavs()
        findings += self._check_missing_clips()
        findings += self._check_orphaned_clip_dirs()
        findings += self._check_archive_duplicates()
        findings += self._check_expired_cloud_shares()
        findings += self._check_expired_raw_movs()
        findings += self._check_expired_raw_wavs()
        return findings

    # -----------------------------------------------------------------------
    # Cleanup scan (--cleanup mode)
    # -----------------------------------------------------------------------

    def _ensure_cloud_file_backed_up(self, f: pathlib.Path) -> None:
        """Copy a cloud file to the D: archive if no copy exists there yet.

        Pre-deletion safety net. Files in audio_dest or vids_dest are the
        authoritative archive — if a cloud file isn't already mirrored there
        when expiry runs, it gets rehydrated and copied first.
        """
        dest_dir = self.audio_dest if f.suffix.lower() == '.zip' else self.vids_dest
        if (self.vids_dest / f.name).exists() or (self.audio_dest / f.name).exists():
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(
                f"CLOUDCLEAN: rehydrating {f.name} before deletion (not in D: archive)"
            )
            shutil.copy2(f, dest_dir / f.name)
            self.logger.info(f"CLOUDCLEAN: backed up {f.name} → {dest_dir.name}/")
        except OSError as e:
            self.logger.warning(f"CLOUDCLEAN: could not back up {f.name}: {e}")

    def _apply_findings(self, findings: 'list[AuditFinding]') -> None:
        """Act on a list of audit findings: delete temps, archive redundant sources,
        queue deletions, or trigger clip re-export as appropriate."""
        for finding in findings:
            if finding.kind == FindingKind.ORPHANED_TEMP:
                for f in finding.files:
                    f.unlink(missing_ok=True)
                    self.logger.info(f"DELETE  {f.name}  (stray temp)")
            elif finding.kind == FindingKind.CLOUD_EXPIRED:
                for f in finding.files:
                    self._ensure_cloud_file_backed_up(f)
                    self.delete_queue.add(f, finding.reason, self.logger)
            elif finding.action == 'move' and finding.destination:
                for f in finding.files:
                    self._archive_or_dedup(f, finding.destination)
            elif finding.kind == FindingKind.ORPHANED_CLIPS:
                self.logger.warning(f"ORPHAN  {finding.label}  ({fmt_size(finding.size_bytes)})")
            elif finding.action == 'delete':
                for f in finding.files:
                    self.delete_queue.add(f, finding.reason, self.logger)
            elif finding.action == 'reprocess':
                base = finding.files[0].stem.rsplit('_', 1)[0]  # strip _CAM1
                self._export_clips(base)  # type: ignore[attr-defined]

    def _cleanup_scan(self) -> None:
        self.logger.info("=== Cleanup scan ===")
        self._apply_findings(self._audit_pipeline_state())
        self.logger.info("=== Cleanup scan complete ===")

