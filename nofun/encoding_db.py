"""nofun/encoding_db.py — Persistent encoding metadata database.

Stores probe results (codec, resolution, duration, etc.) for quadrant videos,
clips, and zipped audio, keyed by performance date/band. Written to
``encoding_db.json`` in the project directory.

Usage::

    db = EncodingDB(pathlib.Path('encoding_db.json'))
    db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', {
        'path': str(path), 'quadrant': 'UL',
        'size': 123456, 'mtime': 1743280251.0,
        'scanned': '2026-03-31T14:20:00',
        **probe_file(path),
    })
    db.save()
"""

__all__ = [
    'probe_file',
    'EncodingDB',
]

import datetime
import json
import logging
import pathlib
import statistics
import subprocess
import threading

_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def probe_file(path: pathlib.Path) -> dict:
    """Probe a video file with ffprobe and return a metadata dict.

    Returns keys: codec, profile, pix_fmt, resolution, fps,
    bitrate_kbps, duration, file_created, problematic.
    Missing values are omitted rather than set to None.
    """
    from nofun.check_encoding import is_problematic

    result = subprocess.run(
        ['ffprobe', '-v', 'error',
         '-select_streams', 'v:0',
         '-show_entries',
         'stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,bit_rate'
         ':format=duration,bit_rate,tags',
         '-of', 'json', str(path)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict = {}
    streams = data.get('streams', [])
    if streams:
        s = streams[0]
        out['codec']   = s.get('codec_name') or 'unknown'
        out['profile'] = s.get('profile')    or 'unknown'
        out['pix_fmt'] = s.get('pix_fmt')    or 'unknown'
        w, h = s.get('width'), s.get('height')
        if w and h:
            out['resolution'] = f'{w}x{h}'
        rfr = s.get('r_frame_rate', '')
        if '/' in rfr:
            n, d = rfr.split('/', 1)
            try:
                if int(d):
                    out['fps'] = round(int(n) / int(d), 3)
            except (ValueError, ZeroDivisionError):
                pass
        br = s.get('bit_rate')
        if br and str(br).lstrip('-').isdigit() and int(br) > 0:
            out['bitrate_kbps'] = int(br) // 1000

    fmt = data.get('format', {})
    dur = fmt.get('duration')
    if dur:
        try:
            out['duration'] = round(float(dur), 1)
        except ValueError:
            pass
    if 'bitrate_kbps' not in out:
        fbr = fmt.get('bit_rate')
        if fbr and str(fbr).lstrip('-').isdigit() and int(fbr) > 0:
            out['bitrate_kbps'] = int(fbr) // 1000

    tags = fmt.get('tags', {})
    ct = tags.get('creation_time') or tags.get('com.apple.quicktime.creationdate')
    if ct:
        out['file_created'] = ct[:19]

    out['problematic'] = is_problematic(
        out.get('profile', ''), out.get('pix_fmt', '')
    )
    return out


class EncodingDB:
    """Persistent JSON database of encoding metadata.

    Structure::

        {
          "schema": 1,
          "updated": "2026-03-31T14:22:00",
          "performances": {
            "2026-03-20": {
              "OTOBO": {
                "quadrant_video": [ {record}, ... ],
                "clips":          [ {record}, ... ],
                "zipped_audio":   [ {record}, ... ]
              }
            }
          }
        }

    Records are keyed by ``path`` within each category list.
    """

    @staticmethod
    def _norm(path: 'str | pathlib.Path') -> str:
        """Normalise a path to a case-folded, forward-slash string for comparisons.

        This makes lookup robust against backslash/forward-slash differences and
        Windows drive-letter casing (e.g. ``D:\\`` vs ``d:/``).
        """
        return str(path).replace('\\', '/').lower()

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._data: dict = {'schema': 3, 'updated': '', 'performances': {}}
        # Fast path-keyed index: _norm(path) -> record dict reference
        self._index: dict[str, dict] = {}
        self._lock = threading.Lock()
        if path.exists():
            self.load()

    def _rebuild_index(self) -> None:
        """Rebuild the in-memory path index from _data. Called after load/upsert."""
        self._index = {}
        for bands in self._data.get('performances', {}).values():
            for perf in bands.values():
                for category_records in perf.values():
                    if isinstance(category_records, list):
                        for rec in category_records:
                            if isinstance(rec, dict) and rec.get('path'):
                                self._index[self._norm(rec['path'])] = rec

    def load(self) -> None:
        """Load from disk, silently ignoring corrupt/missing files."""
        try:
            self._data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            pass
        schema = self._data.get('schema', 1)
        if schema < 2:
            n = self.migrate_clips_to_summary()
            self._data['schema'] = 2
            _logger.info(f"EncodingDB: schema 1 → 2 (converted {n} clip lists to clips_summary)")
            if n:
                self.save()
        if schema < 3:
            n = self.migrate_normalize_band_keys()
            self._data['schema'] = 3
            _logger.info(f"EncodingDB: schema 2 → 3 (normalized {n} band keys with spaces)")
            if n:
                self.save()
        self._rebuild_index()

    def save(self) -> None:
        """Write atomically via a .tmp file (thread-safe)."""
        with self._lock:
            self._data['updated'] = _now_iso()
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self._data, indent=2), encoding='utf-8')
            tmp.replace(self.path)

    def upsert(self, date: str, band: str, category: str, record: dict) -> None:
        """Add or replace a record. Keyed by ``record['path']`` within the category."""
        perfs = self._data.setdefault('performances', {})
        perf  = perfs.setdefault(date, {}).setdefault(band, {})
        entries: list[dict] = perf.setdefault(category, [])
        path     = record.get('path', '')
        path_key = self._norm(path)
        for i, entry in enumerate(entries):
            if self._norm(entry.get('path', '')) == path_key:
                entries[i] = record
                self._index[path_key] = record
                return
        entries.append(record)
        self._index[path_key] = record

    def get_performance(self, date: str, band: str) -> dict | None:
        """Return the perf dict for (date, band), or None."""
        return self._data.get('performances', {}).get(date, {}).get(band)

    def lookup(self, path: pathlib.Path) -> dict | None:
        """Find a record by file path. O(1) via the in-memory index."""
        return self._index.get(self._norm(path))

    def is_stale(self, record: dict, file_path: pathlib.Path) -> bool:
        """True if the file's mtime has changed since the record was written."""
        try:
            return abs(file_path.stat().st_mtime - record.get('mtime', 0)) > 1.0
        except OSError:
            return True

    def all_performances(self) -> list[tuple[str, str, dict]]:
        """Return [(date, band, perf_dict), ...] sorted newest-first."""
        result = []
        for date, bands in sorted(
            self._data.get('performances', {}).items(), reverse=True
        ):
            for band, perf in sorted(bands.items()):
                result.append((date, band, perf))
        return result

    def set_clips_summary(self, date: str, band: str, summary: dict) -> None:
        """Set the clips_summary dict for (date, band). Replaces existing summary."""
        perfs = self._data.setdefault('performances', {})
        perf  = perfs.setdefault(date, {}).setdefault(band, {})
        perf['clips_summary'] = summary

    def set_runtime_seconds(self, date: str, band: str, seconds: float) -> None:
        """Cache the source duration for a perf at the band-level dict.

        Stored as a sibling key to quadrant_video / clips_summary / zipped_audio.
        Idempotent — last write wins.
        """
        perfs = self._data.setdefault('performances', {})
        perf  = perfs.setdefault(date, {}).setdefault(band, {})
        perf['runtime_seconds'] = round(float(seconds), 1)

    @staticmethod
    def derive_runtime_seconds(perf: dict) -> float:
        """Compute runtime_seconds from a perf dict's quadrant_video records.

        All four quads come from the same source ffmpeg pass; max guards
        against partial-write corruption. Returns 0.0 if no record carries
        a usable duration.
        """
        quads = perf.get('quadrant_video') or []
        durs  = [q.get('duration', 0.0) for q in quads if isinstance(q, dict)]
        return max((d for d in durs if d and d > 0), default=0.0)

    def get_clips_summary(self, date: str, band: str) -> dict | None:
        """Return clips_summary for (date, band), or None."""
        return (self._data.get('performances', {})
                .get(date, {}).get(band, {})
                .get('clips_summary'))

    def migrate_clips_to_summary(self) -> int:
        """Convert every band's `clips` list → `clips_summary`. Returns count migrated.

        Idempotent: bands already on `clips_summary` are skipped.
        """
        migrated = 0
        for _date, bands in self._data.get('performances', {}).items():
            for _band, perf in bands.items():
                if 'clips_summary' in perf:
                    perf.pop('clips', None)
                    continue
                clips = perf.pop('clips', None)
                if not isinstance(clips, list) or not clips:
                    continue
                sizes    = [r.get('size', 0)           for r in clips]
                bitrates = [r.get('bitrate_kbps') or 0 for r in clips]
                durs     = [r.get('duration')    or 0.0 for r in clips]
                mtimes   = [r.get('mtime')       or 0.0 for r in clips]
                sample   = clips[0]
                dirs = {str(pathlib.Path(r['path']).parent)
                        for r in clips if r.get('path')}
                perf['clips_summary'] = {
                    'dir': dirs.pop() if len(dirs) == 1
                           else str(pathlib.Path(sample['path']).parent),
                    'count':              len(clips),
                    'codec':              sample.get('codec'),
                    'resolution':         sample.get('resolution'),
                    'fps':                sample.get('fps'),
                    'profile':            sample.get('profile'),
                    'pix_fmt':            sample.get('pix_fmt'),
                    'total_size':         sum(sizes),
                    'min_size':           min(sizes) if sizes else 0,
                    'max_size':           max(sizes) if sizes else 0,
                    'avg_size':           sum(sizes) // len(sizes) if sizes else 0,
                    'min_bitrate_kbps':   min(bitrates) if bitrates else 0,
                    'avg_bitrate_kbps':   sum(bitrates) // len(bitrates) if bitrates else 0,
                    'median_bitrate_kbps': int(statistics.median(bitrates)) if bitrates else 0,
                    'max_bitrate_kbps':   max(bitrates) if bitrates else 0,
                    'min_duration':       min(durs) if durs else 0.0,
                    'max_duration':       max(durs) if durs else 0.0,
                    'min_mtime':          min(mtimes) if mtimes else 0.0,
                    'max_mtime':          max(mtimes) if mtimes else 0.0,
                    'scanned':            sample.get('scanned', ''),
                }
                migrated += 1
        if migrated:
            self._rebuild_index()
        return migrated

    def migrate_normalize_band_keys(self) -> int:
        """Re-key every band entry whose key contains spaces, replacing spaces
        with underscores. Returns count of migrated entries. Idempotent."""
        migrated = 0
        for bands in self._data.get('performances', {}).values():
            for band_key in list(bands):
                if ' ' not in band_key:
                    continue
                new_key = band_key.replace(' ', '_')
                if new_key in bands:
                    bands.pop(band_key)
                else:
                    bands[new_key] = bands.pop(band_key)
                migrated += 1
        if migrated:
            self._rebuild_index()
        return migrated

    def set_inventory_scanned(self) -> None:
        """Record when the last full inventory scan ran."""
        self._data['inventory_scanned'] = _now_iso()

    def inventory_age_seconds(self) -> float:
        """Seconds since last full inventory scan, or inf if never scanned."""
        ts = self._data.get('inventory_scanned', '')
        if not ts:
            return float('inf')
        try:
            dt = datetime.datetime.fromisoformat(ts)
            return (datetime.datetime.now() - dt).total_seconds()
        except ValueError:
            return float('inf')

    def set_summary(
        self,
        perf_count:            int,
        type_counts:           dict,
        total_runtime_seconds: float = 0.0,
    ) -> None:
        """Cache inventory counts so startup can read them without iterating all records."""
        self._data['summary'] = {
            'perf_count':            perf_count,
            'type_counts':           dict(type_counts),
            'total_runtime_seconds': float(total_runtime_seconds),
            'updated':               _now_iso(),
        }

    def get_summary(self) -> dict:
        """Return cached inventory summary, or {} if never stored."""
        return self._data.get('summary', {})

    def rename_band(self, date_str: str, old_band: str, new_band: str) -> None:
        """Move all records from (date_str, old_band) to new_band, rewriting paths."""
        date_bands = self._data.get('performances', {}).get(date_str, {})
        if old_band not in date_bands:
            return
        perf = date_bands.pop(old_band)
        for records in perf.values():
            if isinstance(records, list):
                for rec in records:
                    if isinstance(rec.get('path'), str):
                        rec['path'] = rec['path'].replace(old_band, new_band)
            elif isinstance(records, dict) and 'dir' in records:  # clips_summary
                records['dir'] = records['dir'].replace(old_band, new_band)
        date_bands[new_band] = perf
        self._rebuild_index()

    def prune_orphaned_bands(self, valid_by_date: dict[str, set[str]]) -> int:
        """Remove (date, band) DB entries whose band no longer appears in a scan.

        Only touches dates present in *valid_by_date*; dates absent from the dict
        (e.g. because that drive wasn't mounted during the scan) are left intact.
        This makes BIGSCAN self-healing: stale entries from old band-name extraction
        logic are automatically cleaned up without a manual script.

        Returns the number of (date, band) entries removed.
        """
        pruned = 0
        perfs = self._data.get('performances', {})
        for date, valid_bands in valid_by_date.items():
            date_perfs = perfs.get(date, {})
            stale = [b for b in list(date_perfs) if b not in valid_bands]
            for band in stale:
                del date_perfs[band]
                pruned += 1
        if pruned:
            self._rebuild_index()
        return pruned

    def unscanned_paths(self, paths: list[pathlib.Path]) -> list[pathlib.Path]:
        """Return paths not yet in the DB or whose mtime has changed."""
        result = []
        for p in paths:
            rec = self.lookup(p)
            if rec is None or self.is_stale(rec, p):
                result.append(p)
        return result
