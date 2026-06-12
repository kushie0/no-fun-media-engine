"""media_engine.py — Main pipeline for concert recording processing.

Watches a source directory for .mov and .wav files and runs them through:
  1. VIDEO  — splits each .mov into 4 HEVC quadrant files (UL, UR, LL, LR)
  2. CLIPS  — exports 40-second 320×180 proxy clips per quadrant
  3. AUDIO  — splits 32-ch WAVs into mono channels, drops silent ones, ZIPs

Usage:
    python media_engine.py                  # watchdog mode
    python media_engine.py -d /some/folder  # batch mode, exit when done
    python media_engine.py -t 5             # trial: encode 5s clips only
"""

import atexit
import datetime
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile as _zipfile
from collections import defaultdict
from dataclasses import dataclass
import click

from nofun.audio import AudioMixin
from nofun.cleanup import CleanupMixin
from nofun.menu_inventory import InventoryMenuMixin
from nofun.menu_jobs import JobsMenuMixin
from nofun.menu_reprocess import ReprocessMenuMixin
from nofun.menu_streams import StreamsMenuMixin
from nofun.video import VideoMixin, build_encoder_config
from nofun.paths import (
    detect_clips_root,
    detect_media_root,
    detect_mounts,
    detect_platform,
    is_windows,
    nas_reachable,
)
from nofun.media_io import (
    DeleteQueue,
    app_version,
    dehydrate_cloud_files,
    fmt_size,
    is_cloud_only,
    is_file_locked,
    probe_format,
    probe_total_frames,
    setup_logging,
)
from nofun.cleanup import (
    canonical_sharepoint_name,
    cloud_filename,
    expected_cloud_names,
    make_sharepoint_folder_name,
    plan_cloud_copy,
    write_sharepoint_info,
)
from nofun.encoding_db import EncodingDB
from nofun.inventory import (
    EXPIRE_AGE, RAW_EXPIRE_AGE, D_BACKUP_AGE, extract_date_band, files_for_perf,
    perf_key, short_date, _status_label, _STATUS_ICON,
)
from nofun.job_manifest import JobManifest
from nofun.job_queue import JobCategory, JobQueue
from nofun.state import MenuMode, PauseState
from nofun.video import CAM_LABELS
from nofun.multiplex import Layout, detect_layout, route_by_layout
from nofun.backup_mirror import (
    mirror_files, find_expired, DELIVERABLE_EXTS, RAW_BACKUP_EXTS,
)
from nofun.streams import BASE_PORT, STREAM_COUNT, StreamServer, get_local_ip

# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

_LOCK_FILE = pathlib.Path(tempfile.gettempdir()) / 'nofun_media_engine.lock'

# Reserved band name for the synthetic smoke-test fixture. Performances under
# this band are throwaway and must never reach OneDrive — the manifest SYNC
# QUADS/AUDIO jobs would otherwise push them unconditionally.
SMOKE_TEST_BAND = 'SMOKETEST'


def _is_smoke_band(band: str) -> bool:
    return band.strip().upper() == SMOKE_TEST_BAND


def _acquire_lock() -> None:
    """Exit immediately if another instance is already running."""
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # signal 0: raises if process is gone
            print(f"\n  Media Engine is already running (PID {pid}). Exiting.\n")
            sys.exit(1)
        except (ValueError, OSError):
            _LOCK_FILE.unlink(missing_ok=True)  # stale lock from a crashed run
    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: _LOCK_FILE.unlink(missing_ok=True))


# Home command bar text — restored after exiting any interactive menu
_HOME_COMMANDS = (
    "Available commands:  NOPROBLEM / INVENTORY / JOBS / SCAN / PAUSE / [green]HELP[/green]"
)

# ---------------------------------------------------------------------------
# Help-overlay state — one per menu (HOME / INVENTORY / STREAMS / JOBS).
# `verbose` toggles brief↔detailed on each HELP press; `active` tracks whether
# the overlay is currently shown so HOME knows to dismiss it before unwinding
# the menu.
# ---------------------------------------------------------------------------

@dataclass
class _HelpState:
    active:  bool = False
    verbose: bool = False

    def reset(self) -> None:
        self.active  = False
        self.verbose = False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline(VideoMixin, AudioMixin, CleanupMixin,
               InventoryMenuMixin, StreamsMenuMixin,
               JobsMenuMixin, ReprocessMenuMixin):
    # Runtime NAS→D: fallback: number of consecutive agreeing reachability
    # probes required before flipping media_root. At 60s/tick this is ≤120s of
    # hysteresis, so a brief NAS blip won't thrash the dest paths.
    _NAS_FLIP_TICKS = 2

    def __init__(
        self,
        directory:        pathlib.Path | None,
        trial_run:        int,
        exit_on_complete: bool,
        skip_audio:       bool,
        force:            bool,
        gpu:              bool,
        cleanup_only:     bool,
    ) -> None:
        self.directory        = directory
        self.trial_run        = trial_run
        self.exit_on_complete = exit_on_complete
        self.skip_audio       = skip_audio
        self.force            = force
        self.gpu              = gpu
        self.cleanup_only     = cleanup_only

        self.script_dir = pathlib.Path(__file__).parent
        self.mount_c, self.mount_d = detect_mounts()

        # Source directory to watch / process
        # SEARCH_DIR env var overrides the default VenueLighting path (local dev)
        _search_dir_env = os.environ.get('SEARCH_DIR')
        default_source = (pathlib.Path(_search_dir_env) if _search_dir_env
                          else self.mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun') / 'VenueLighting')
        self.search_dir: pathlib.Path = directory if directory else default_source

        # Destinations
        # clips_dest is separate (C:\clips SSD) and never follows media_root —
        # set once here and left untouched by any runtime NAS→D: re-point.
        prefix = 'trial_runs' if trial_run else ''
        if prefix:
            self.clips_dest = self.mount_d / prefix / 'clips'
            self._set_media_root(self.mount_d / prefix)
        else:
            self.clips_dest = detect_clips_root(self.mount_d)   # unchanged — C:\clips SSD
            self._set_media_root(detect_media_root(self.mount_d))

        for d in (self.vids_dest, self.clips_dest, self.audio_dest,
                  self.video_archive, self.audio_archive):
            d.mkdir(parents=True, exist_ok=True)

        # Runtime NAS→D: fallback debounce state (see _reconcile_media_root).
        # Each main-loop tick re-probes NAS reachability; flip media_root only
        # after _NAS_FLIP_TICKS consecutive agreeing probes so a flapping link
        # can't thrash the dest paths.
        self._nas_miss = 0
        self._nas_hit  = 0

        # SharePoint / OneDrive sync folder (copy-only; OneDrive syncs automatically)
        _od = (self.mount_c / 'Users' / (os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun')
               / 'OneDrive - No Fun Troy LLC' / 'Multitracks')
        self.sharepoint_dest: pathlib.Path | None = _od if _od.is_dir() else None

        # Logging — local rolling (48h) + remote rotating (800 KB)
        local_log      = self.script_dir / 'convert_recent.log'
        remote_log_dir = (self.mount_d / 'logs') if self.mount_d != pathlib.Path('.') else None
        self.logger    = setup_logging(local_log, remote_log_dir)

        # Console: brief summary; files: full detail
        version = app_version(self.script_dir)
        self.logger.info(
            f"Pipeline started  v={version}  ({detect_platform()}, D={self.mount_d.name}"
            + (f" trial={trial_run}s" if trial_run else "")
            + ")"
        )
        self.logger.debug(
            f"Pipeline started  v={version} plat={detect_platform()} "
            f"mount_c={self.mount_c} mount_d={self.mount_d} media_root={self.media_root} "
            f"trial={trial_run}s skip_audio={skip_audio}"
        )

        # Encoder config
        self.enc = build_encoder_config(gpu=self.gpu, trial_run=self.trial_run)

        # Script runner — delegates ffmpeg calls to standalone scripts in scripts/
        from nofun.script_runner import ScriptRunner
        self._script_runner = ScriptRunner(self.logger)

        # Job queue — schedules and dispatches all pipeline work
        from nofun.job_queue import JobQueue
        self._job_queue = JobQueue(self._script_runner, self.logger)

        # Inventory paths
        self.inventory_summary = self.script_dir / 'file_summary.txt'

        # Encoding metadata DB (persisted across runs)
        self._encoding_db = EncodingDB(self.script_dir / 'encoding_db.json')

        # Delete queue
        self.delete_queue = DeleteQueue()

        # File-event tracking (populated each loop iteration)
        self._known_files:    dict[str, tuple[int, float]] = {}
        self._pipeline_moved: queue.Queue[str]             = queue.Queue()

        # Tracks (date, band) pairs that have been bumped on the banner this
        # session, so encode retries don't double-increment the perf count.
        self._banner_bumped_perfs: set[tuple[str, str]] = set()

        # Cached total-ETA seconds, refreshed by the watchdog and read by
        # _format_status() for the "~N min remaining" status-line segment.
        self._cached_total_eta: float | None = None

        # Size-stability tracking: {path_str: (size, first_seen_at_that_size)}
        # A file must keep the same size for _STABLE_SECS before processing.
        self._file_sizes: dict[str, tuple[int, float]] = {}

        # First NOPROBLEM bypasses the time gate; second also enables force re-encode
        self._noproblem_active: bool = False
        # Stream server (None = not started)
        self._stream_server: StreamServer | None = None

        # TUI app reference — set by run_with_queue(); None in batch mode
        self._app = None

        # Home command bar text (exposed so CleanupMixin can restore it)
        self._HOME_COMMANDS = _HOME_COMMANDS

        # Tracks (date_str, band) pairs already auto-enqueued for REMASTER so
        # we don't re-enqueue them on every watchdog loop iteration.
        self._auto_remastered: set[tuple[str, str]] = set()

        # Interactive menu state (STATUS / STREAMS)
        self._active_menu:         MenuMode        = MenuMode.NONE
        self._status_entries:      list            = []
        self._show_groups:         list            = []   # [(date, show_name, [ps,...]), ...]
        self._disk_c:              str             = ''
        self._disk_d:              str             = ''
        self._disk_n:              str             = ''
        self._disk_sp:             str             = ''
        self._status_expanded_key: str | None      = None  # date string, or None
        # RENAME sub-state (active while _active_menu == MenuMode.STATUS)
        self._rename_state:    str | None            = None   # 'select'|'enter_name'|'confirm'
        self._rename_date:     str | None            = None
        self._rename_band:     str | None            = None
        self._rename_new_name: str | None            = None
        # REMASTER band-picker sub-state (active while _active_menu == MenuMode.STATUS)
        self._remaster_state:  str | None            = None   # 'select' | None
        # Pause state
        # _current_ffmpeg_procs: running ffmpeg handles keyed by slot ('encode', 'audio')
        # _cmd_queue:            reference to the TUI command queue (set by run_with_queue)
        self._pause_state:           PauseState                        = PauseState.RUNNING
        self._current_ffmpeg_procs:  dict[str, subprocess.Popen]       = {}
        self._ffmpeg_procs_lock:     threading.Lock                    = threading.Lock()
        self._cmd_queue:             queue.Queue | None                 = None
        # Mirrors the override_time local in run_with_queue/run so mid-operation
        # command flushes (_flush_commands) can update it from inside mixins
        self._override_time:        bool                       = False
        # HELP overlay state — one entry per menu (HOME / INVENTORY / STREAMS / JOBS)
        self._help: dict[str, _HelpState] = {
            'home':      _HelpState(),
            'inventory': _HelpState(),
            'streams':   _HelpState(),
            'jobs':      _HelpState(),
        }
        # Manual-job worker thread (dispatches JobCategory.MANUAL independently
        # of the watchdog loop so the TUI stays responsive during REMASTER/REUPLOAD)
        self._manual_worker_running: bool                    = False
        self._manual_worker_thread:  threading.Thread | None = None
        # GPU/CPU worker threads (dual-lane dispatch — Step 2)
        self._gpu_worker_running: bool                    = False
        self._gpu_worker_thread:  threading.Thread | None = None
        self._gpu_script_runner:  'ScriptRunner | None'   = None
        self._cpu_worker_running: bool                    = False
        self._cpu_worker_thread:  threading.Thread | None = None
        self._cpu_script_runner:  'ScriptRunner | None'   = None
        # Scheduled worker thread (dispatches JobCategory.SCHEDULED — SCAN/BIGSCAN/sync)
        self._scheduled_worker_running: bool                    = False
        self._scheduled_worker_thread:  threading.Thread | None = None
        self._scheduled_script_runner:  'ScriptRunner | None'   = None
        # Dedup: perf keys with active manifests; prevents re-enqueue each loop
        self._enqueued_keys:      set[str]       = set()
        self._enqueued_keys_lock: threading.Lock = threading.Lock()
        # Last process-gate block reason logged; dedups the gate-hold line so it
        # fires only on state change instead of every 15 s loop.
        self._last_gate_block:    str            = ''
        # Cache of content-detected mov layouts, keyed on str(path) →
        # ((path, mtime_ns, size), Layout); avoids re-probing on every loop.
        self._layout_cache: dict[str, tuple] = {}
        # Perfs where the DB audio_all_silent flag has already been logged this session
        self._audio_silent_notified: set[str] = set()
        # Per-perf REMASTER outcome; read by _do_reel_for_perf to compose upstream-cause warning.
        # Keys: '{YY-MM-DD}_{band}' (perf-key form). Values: 'ok'|'no_zip'|'zip_empty'|'mastering_error'.
        # GIL makes single-key dict set/get atomic; REMASTER writes once before REEL reads.
        self._remaster_status: dict[str, str] = {}
        # SharePoint placeholder folders already created this session
        self._sp_placeholder_done: set[tuple[str, str]] = set()
        # REPROCESS menu state
        self._reprocess_candidates: list[str] = []
        self._reprocess_archived:   dict      = {}

        # JOBS menu state
        self._jobs_selected_idx:  int | None = None  # index into all_active()
        # Scheduled task rate-limiting (label → last enqueue timestamp)
        self._last_scheduled_enqueued: dict[str, float] = {}
        self._last_scan_enqueued: float = 0.0

    # -----------------------------------------------------------------------
    # Encoder config
    # -----------------------------------------------------------------------

    _STABLE_SECS = 30  # seconds used by the size-stability fallback

    def _is_file_stable(self, path: pathlib.Path) -> bool:
        """Return True if *path* is not actively being written by another process.

        On Windows: queries the Restart Manager API via get_open_processes().
        This is reliable regardless of the share flags the recording software
        uses — TouchDesigner opens files with FILE_SHARE_READ|WRITE, which
        defeats a simple CreateFileW exclusive-open probe.

        On macOS / Linux: falls back to _is_file_stable_by_size(), which
        requires the file size to be unchanged for _STABLE_SECS seconds.
        """
        if is_windows():
            from nofun.media_io import get_open_processes
            try:
                return not bool(get_open_processes(path))
            except Exception:
                # Restart Manager unavailable — fall through to size check
                pass
        return self._is_file_stable_by_size(path)

    def _is_file_stable_by_size(self, path: pathlib.Path) -> bool:
        """Size-stability fallback: True if path size is unchanged for _STABLE_SECS.

        Used on macOS / Linux where the Restart Manager is not available.
        Also available as an alternative to the Restart Manager approach on
        Windows if needed (e.g. swap the call in _is_file_stable above).
        """
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        now = time.monotonic()
        stored = self._file_sizes.get(key)
        if stored is None or stored[0] != size:
            self._file_sizes[key] = (size, now)
            return False
        return (now - stored[1]) >= self._STABLE_SECS

    def _get_recording_files(self) -> list[pathlib.Path]:
        """Return .mov and .wav files in search_dir held open by another process.

        On Windows uses the Restart Manager (reliable regardless of share flags).
        On macOS falls back to _is_file_stable_by_size() — a file is considered
        'recording' until its size has been unchanged for _STABLE_SECS seconds.
        Original multi-channel WAVs and MOVs are checked; split _chNN.wav files
        created by this pipeline are excluded.
        """
        try:
            candidates = sorted(
                f for f in self.search_dir.iterdir()
                if f.suffix.lower() in ('.mov', '.wav')
                and not re.search(r'_ch\d+\.wav$', f.name, re.IGNORECASE)
                and f.is_file()
            )
        except OSError:
            return []
        return [f for f in candidates if not self._is_file_stable(f)]

    # -----------------------------------------------------------------------
    # Inventory helpers
    # -----------------------------------------------------------------------

    def _inventory_search_paths(self) -> list[pathlib.Path]:
        """Build the list of directories to scan for inventory."""
        paths: list[pathlib.Path] = []
        if self.mount_c != pathlib.Path('.'):
            paths.append(self.search_dir)           # VenueLighting source
            if self.sharepoint_dest:
                paths.append(self.sharepoint_dest)  # OneDrive Multitracks
        for p in (self.vids_dest, self.audio_dest, self.clips_dest,
                  self.video_archive, self.audio_archive):
            if p.is_dir():
                paths.append(p)
        return paths or [pathlib.Path('./VenueLighting')]

    def _run_inventory(self) -> None:
        from nofun.inventory import (
            build_performance_states,
            classify_file,
            classify_location,
            extract_date_band_from_path,
            scan_files,
            _render_state_dashboard,
        )
        from nofun.encoding_db import _now_iso

        # Types written to the encoding DB. Only real pipeline products go here.
        # Pipeline output types (fullset audio, reel video) are intentionally
        # absent so they never create phantom (date, band) keys in the DB.
        _INVENTORY_CATEGORY = {
            'raw video':    'raw_video',
            'quadrant':     'quadrant_video',
            'clip':         'clips',
            'audio':        'source_audio',
            'zipped audio': 'zipped_audio',
            're-encoded':   'quadrant_video',
        }

        search_paths = self._inventory_search_paths()
        if self._app:
            self._app.update_status('REBUILD  ·  scanning…')
        rows: list[dict] = []
        for meta in scan_files(search_paths):
            date, band = extract_date_band_from_path(meta['fullpath'])
            ftype    = classify_file(meta['filename'], meta['fullpath'])
            location = classify_location(meta['fullpath'])
            rows.append({
                **meta,
                'date':     date,
                'band':     band,
                'type':     ftype,
                'location': location,
                'size_gb':  meta['size'] / 1_073_741_824,
            })

            if date != 'TBD' and ftype in _INVENTORY_CATEGORY:
                category = _INVENTORY_CATEGORY[ftype]
                if category != 'clips':
                    self._encoding_db.upsert(date, band, category, {
                        'path':     str(meta['fullpath']),
                        'size':     meta['size'],
                        'mtime':    meta['mtime'].timestamp(),
                        'type':     ftype,
                        'location': location,
                        'scanned':  _now_iso(),
                    })

            if self._app:
                self._app.update_status(f'REBUILD  ·  {len(rows)} files indexed…')

        # Build one clips_summary per (date, band) from file-scan rows.
        clip_rows: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            if row['type'] == 'clip' and row['date'] != 'TBD':
                clip_rows.setdefault((row['date'], row['band']), []).append(row)
        for (date, band), entries in clip_rows.items():
            sizes  = [e['size']               for e in entries]
            mtimes = [e['mtime'].timestamp()  for e in entries]
            existing = self._encoding_db.get_clips_summary(date, band) or {}
            summary: dict = {
                'dir':        str(entries[0]['fullpath'].parent),
                'count':      len(entries),
                'total_size': sum(sizes),
                'min_size':   min(sizes),
                'max_size':   max(sizes),
                'avg_size':   sum(sizes) // len(sizes),
                'min_mtime':  min(mtimes),
                'max_mtime':  max(mtimes),
                'scanned':    _now_iso(),
            }
            for k in ('codec', 'resolution', 'fps', 'profile', 'pix_fmt',
                      'min_bitrate_kbps', 'avg_bitrate_kbps',
                      'median_bitrate_kbps', 'max_bitrate_kbps',
                      'min_duration', 'max_duration'):
                if k in existing:
                    summary[k] = existing[k]
            self._encoding_db.set_clips_summary(date, band, summary)

        # Prune stale (date, band) entries for dates where we have real scan data.
        # Only dates present in valid_by_date are touched; unmounted drives are safe.
        valid_by_date: dict[str, set] = {}
        for row in rows:
            if row['date'] != 'TBD' and row['type'] in _INVENTORY_CATEGORY:
                valid_by_date.setdefault(row['date'], set()).add(row['band'])
        pruned = self._encoding_db.prune_orphaned_bands(valid_by_date)
        if pruned:
            noun = 'entry' if pruned == 1 else 'entries'
            self.logger.info(f'REBUILD  pruned {pruned} stale band {noun} from encoding DB')

        self._encoding_db.set_inventory_scanned()

        states    = build_performance_states(rows)
        dashboard = _render_state_dashboard(states, len(rows))
        self.inventory_summary.write_text(dashboard, encoding='utf-8')

        perf_count = sum(
            1 for k in states
            if k[0] != 'TBD' and k[1] not in ('Audio Recorder', 'TBD')
        )
        type_counts: dict[str, int] = {}
        for row in rows:
            t = row.get('type', 'unknown')
            type_counts[t] = type_counts.get(t, 0) + 1

        total_rt = sum(
            perf.get('runtime_seconds', 0.0)
            for bands in self._encoding_db._data.get('performances', {}).values()
            for perf in bands.values()
            if isinstance(perf, dict)
        )
        self._encoding_db.set_summary(
            perf_count, type_counts, total_runtime_seconds=total_rt,
        )
        self._encoding_db.save()

        self.logger.info(
            f"Inventory: {perf_count} performances · {len(rows)} files indexed"
        )
        if self._app:
            ts = datetime.datetime.now().isoformat(timespec='seconds')
            self._app.update_inventory_stats(
                type_counts, ts, perf_count,
                total_runtime_seconds=total_rt,
            )
            self._app.update_status('')
        self._collect_header_stats()

    def _display_dashboard(self) -> None:
        if self.inventory_summary.exists():
            print()
            print(self.inventory_summary.read_text(encoding='utf-8'))
            print()
        else:
            print("No inventory dashboard found. Run INVENTORY to generate one.")

    def _collect_header_stats(self) -> None:
        """Compute free-space strings for C, D, and SharePoint. Stored on self for INVENTORY display."""
        if not hasattr(self, 'mount_c'):
            return

        def _h(gb: float) -> str:
            return f"{gb / 1024:.1f}TB" if gb >= 1024 else f"{int(gb)}GB"

        def _fmt_disk(path: pathlib.Path) -> str:
            try:
                u = shutil.disk_usage(path)
                free_gb = u.free / 1_073_741_824
                pct     = int(u.free / u.total * 100)
                return f"{pct}% free ({_h(free_gb)})"
            except OSError:
                return '?'

        _SP_TOTAL_GB = 1024.0  # 1 TB plan

        def _sp_size(path: pathlib.Path | None) -> str:
            if not path or not path.is_dir():
                return ''
            used = 0
            try:
                for entry in path.rglob('*'):
                    if entry.is_file(follow_symlinks=False):
                        used += entry.stat().st_size
            except OSError:
                pass
            free_gb = _SP_TOTAL_GB - used / 1_073_741_824
            pct     = int(free_gb / _SP_TOTAL_GB * 100)
            return f"SP: {pct}% free ({_h(free_gb)})"

        self._disk_c  = f"C: {_fmt_disk(self.mount_c)}" if self.mount_c != pathlib.Path('.') else ''
        self._disk_d  = f"D: {_fmt_disk(self.mount_d)}" if self.mount_d.is_dir() else ''
        self._disk_n  = (f"NAS: {_fmt_disk(self.media_root)}"
                         if self.media_root != self.mount_d else '')
        self._disk_sp = _sp_size(self.sharepoint_dest)

    def _maybe_refresh_inventory(self) -> None:
        inv_age = self._encoding_db.inventory_age_seconds()
        if inv_age < 86400:
            if self._app:
                summary = self._encoding_db.get_summary()
                if summary:
                    ts = summary.get('updated', '')
                    self._app.update_inventory_stats(
                        summary.get('type_counts', {}),
                        ts,
                        summary.get('perf_count', -1),
                        total_runtime_seconds=summary.get('total_runtime_seconds', 0.0),
                    )
        else:
            self.logger.info("Rebuilding inventory (cache stale)...")
            self._run_inventory()
            self.logger.info("Inventory rebuild complete")

    def _heal_stale_smoke_entries(self) -> bool:
        """Drop reserved smoke-band DB entries whose outputs are gone from disk.

        prune_orphaned_bands deliberately preserves dates with zero disk presence
        so an unmounted drive can't wipe the DB — which would otherwise keep a
        cleaned smoke entry forever and make the engine skip-on-presence on the
        next re-stage. The smoke fixture is synthetic and disposable, so it gets
        no such protection: if its recorded quads are gone, drop the entry so a
        re-stage rebuilds cleanly. Returns True if anything was dropped.
        """
        dropped = False
        for date, band, perf in self._encoding_db.all_performances():
            if not _is_smoke_band(band):
                continue
            quads = perf.get('quadrant_video') or []
            paths = [q['path'] for q in quads
                     if isinstance(q, dict) and q.get('path')]
            if paths and not all(pathlib.Path(p).exists() for p in paths):
                if self._encoding_db.drop_performance(date, band):
                    self.logger.info(
                        f"SCAN: cleared stale smoke entry {date}_{band} (outputs gone)")
                    dropped = True
        return dropped

    # -----------------------------------------------------------------------
    # SharePoint sync
    # -----------------------------------------------------------------------

    @staticmethod
    def _find_date_folder(root: pathlib.Path, date_prefix: str) -> pathlib.Path | None:
        """Return the first subfolder of *root* whose name starts with *date_prefix*.

        Accepts any single non-digit separator after the prefix (space, underscore,
        hyphen, dot, …) so manually renamed folders like '26-04-07 Troy Show' or
        '26-04-07_PRIZE_MALLGOTH' are all matched.  Returns None if not found.
        """
        n = len(date_prefix)
        try:
            return next(
                f for f in root.iterdir()
                if f.is_dir()
                and f.name[:n] == date_prefix
                and (len(f.name) == n or not f.name[n].isdigit())
            )
        except StopIteration:
            return None

    def _sync_eligible_performances(self) -> None:
        """Copy completed performances to SharePoint if their cloud lease has time
        left (`age < EXPIRE_AGE`) and they're not already in cloud.

        All bands from the same date share one subfolder named "YY-MM-DD" (user can rename
        it freely; the pipeline matches by the leading date prefix). Copies:
          - the 4 quadrant MP4s from vids_dest/
          - the matching audio ZIP from audio_dest/ (matched by date+band, not exact filename,
            so date-format differences between quad names and zip names are tolerated)
        Skips a specific performance only if its CAM1 quad file already exists in the folder.
        """
        if not self.sharepoint_dest or not self.sharepoint_dest.is_dir():
            return
        if not self.vids_dest.is_dir():
            return

        from nofun.inventory import extract_date_band

        # Build a lookup of all ZIPs in audio_dest keyed by (canonical_date, band)
        zip_by_perf: dict[tuple[str, str], pathlib.Path] = {}
        if self.audio_dest.is_dir():
            for zf in self.audio_dest.glob('*.zip'):
                z_date, z_band = extract_date_band(zf.stem)
                if z_date != 'TBD':
                    zip_by_perf[(z_date, z_band)] = zf

        # Collect all (date_prefix, date_str, band, rec_date, ul_file) tuples — used
        # both for file copying and for the post-loop folder rename reconciliation.
        eligible: list[tuple[str, str, str, datetime.date, pathlib.Path]] = []
        for ul_file in sorted(self.vids_dest.glob('*_CAM1.mp4')):
            base = ul_file.stem[:-5]
            date_str, band = extract_date_band(base)
            if date_str == 'TBD':
                continue
            if _is_smoke_band(band):
                continue
            try:
                y, mo, d = date_str.split('-')
                rec_date = datetime.date(2000 + int(y), int(mo), int(d))
            except (ValueError, AttributeError):
                continue
            if (datetime.date.today() - rec_date).days >= EXPIRE_AGE:
                continue
            date_prefix = date_str
            eligible.append((date_prefix, date_str, band, rec_date, ul_file))

        # Cloud filenames each date folder should eventually hold (all bands).
        # Passed to write_sharepoint_info so files not yet copied keep showing
        # as "processing…" through partial syncs; once all land the marker set
        # is empty and the info file matches the normal present-only form.
        expected_by_prefix: dict[str, set[str]] = {}
        for e_prefix, e_date, e_band, _, e_ul in eligible:
            names = expected_by_prefix.setdefault(e_prefix, set())
            names.update(expected_cloud_names(e_ul.stem[:-5], zip_by_perf.get((e_date, e_band))))

        # --- File copy pass ---
        for date_prefix, date_str, band, rec_date, ul_file in eligible:
            base = ul_file.stem[:-5]
            dest = (
                self._find_date_folder(self.sharepoint_dest, date_prefix)
                or self.sharepoint_dest / date_prefix
            )

            zip_src = zip_by_perf.get((date_str, band))

            ul_done  = dest.exists() and (dest / cloud_filename(ul_file.name)).exists()
            zip_done = zip_src is None or (dest.exists() and (dest / cloud_filename(zip_src.name)).exists())
            if ul_done and zip_done:
                continue

            quads = [] if ul_done else [
                q for q in
                [self.vids_dest / f'{base}_{q}.mp4' for q in CAM_LABELS]
                if q.exists()
            ]
            if not quads and zip_done:
                continue

            dest.mkdir(exist_ok=True)
            newly_copied: list[pathlib.Path] = []
            if quads:
                self.logger.info(f"SHARE   {base} → {dest.name}")
                for quad in quads:
                    cloud_dst = dest / cloud_filename(quad.name)
                    if cloud_dst.exists():
                        continue
                    legacy = dest / quad.name
                    if legacy.exists() and is_cloud_only(legacy):
                        # Renaming a Files-On-Demand placeholder forces OneDrive
                        # to materialize (download) the full file first.  Leave
                        # old dated copies alone — they expire on the cloud's
                        # normal cadence; new ones land under the stripped name.
                        continue
                    if legacy.exists():
                        # Migration / manual-rename case: existing dated copy in
                        # cloud has same content; rename in place (no bandwidth)
                        # instead of re-uploading from source.
                        legacy.rename(cloud_dst)
                    else:
                        shutil.copy(quad, cloud_dst)
                    newly_copied.append(cloud_dst)
            if zip_src and zip_src.exists() and not zip_done:
                cloud_dst = dest / cloud_filename(zip_src.name)
                legacy   = dest / zip_src.name
                if not cloud_dst.exists():
                    if legacy.exists() and is_cloud_only(legacy):
                        pass  # dehydrated — skip rename to avoid forced download
                    elif legacy.exists():
                        legacy.rename(cloud_dst)
                        newly_copied.append(cloud_dst)
                    else:
                        self.logger.info(f"SHARE   {base} ZIP → {dest.name}")
                        shutil.copy(zip_src, cloud_dst)
                        newly_copied.append(cloud_dst)

            try:
                expire_date = rec_date + datetime.timedelta(days=EXPIRE_AGE)
                media_in_dest = [
                    f for f in dest.iterdir()
                    if f.is_file() and f.name != '_nofun_info.txt'
                ]
                write_sharepoint_info(
                    dest, media_in_dest,
                    expire_date=expire_date,
                    new_files=newly_copied,
                    expected_names=sorted(expected_by_prefix.get(date_prefix, set())),
                )
            except OSError as e:
                self.logger.warning(f"SHARE   could not write info file: {e}")

            # Write cloud file records into the encoding DB so inventory
            # shows SHARED immediately without needing a manual REBUILD.
            from nofun.encoding_db import _now_iso
            final_dest = dest
            for f in newly_copied:
                cloud_path = final_dest / f.name
                # date+band come from the outer loop — cloud filenames have the
                # date stripped, so extract_date_band(f.stem) would return TBD.
                db_d, db_b = date_str, band
                cat = 'zipped_audio' if cloud_path.suffix.lower() == '.zip' else 'quadrant_video'
                try:
                    stat = cloud_path.stat()
                    self._encoding_db.upsert(db_d, db_b, cat, {
                        'path':     str(cloud_path),
                        'size':     stat.st_size,
                        'mtime':    stat.st_mtime,
                        'type':     'zipped audio' if cat == 'zipped_audio' else 'quadrant',
                        'location': 'cloud',
                    })
                except OSError:
                    pass
            if newly_copied:
                self._encoding_db.save()
            dehydrate_cloud_files(newly_copied, self.logger)

        # --- Folder rename pass ---
        # Compute the canonical folder name for each date from ALL its bands at once
        # (not incrementally per band), so duplicate tokens can never accumulate and
        # existing corrupt names (e.g. "26-03-14_A_C_T_A_C_T") are self-healed.
        bands_by_date: dict[str, list[str]] = {}
        for date_prefix, _, band, _, _ in eligible:
            bands_by_date.setdefault(date_prefix, [])
            if band not in bands_by_date[date_prefix]:
                bands_by_date[date_prefix].append(band)

        for date_prefix, bands in bands_by_date.items():
            folder = self._find_date_folder(self.sharepoint_dest, date_prefix)
            if not folder:
                continue
            target = canonical_sharepoint_name(date_prefix, bands)
            if target != folder.name:
                try:
                    folder.rename(folder.parent / target)
                    self.logger.info(f"SHARE   renamed folder → {target}")
                except OSError as e:
                    self.logger.warning(f"SHARE   could not rename folder: {e}")

    # -----------------------------------------------------------------------
    # Manual re-upload (INVENTORY menu REUPLOAD command)
    # -----------------------------------------------------------------------

    def _reupload_performance(self, date_str: str, band: str) -> None:
        """Re-copy a performance's quads and ZIP to SharePoint and reset the 28-day clock.

        Handles folders that were moved to archived/ — moves them back first.
        """
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            self.logger.info("REUPLOAD: SharePoint folder not accessible")
            return

        try:
            parts = date_str.split('-')
            datetime.date(2000 + int(parts[0]), int(parts[1]), int(parts[2]))  # validate
        except (ValueError, IndexError):
            self.logger.info(f"REUPLOAD: could not parse date {date_str!r}")
            return

        date_prefix = date_str

        dest = self._find_date_folder(self.sharepoint_dest, date_prefix)

        # Check archived/ if not found at top level
        if dest is None:
            archived_root = self.sharepoint_dest / 'archived'
            if archived_root.is_dir():
                archived = self._find_date_folder(archived_root, date_prefix)
                if archived is not None:
                    new_dest = self.sharepoint_dest / archived.name
                    try:
                        archived.rename(new_dest)
                        dest = new_dest
                        self.logger.info(f"REUPLOAD: moved {dest.name}/ back from archived/")
                    except OSError as e:
                        self.logger.warning(f"REUPLOAD: could not move from archived/: {e}")
                        return

        if dest is None:
            dest = self.sharepoint_dest / date_prefix

        dest.mkdir(exist_ok=True)

        from nofun.inventory import extract_date_band
        uploaded: list[pathlib.Path] = []

        def _copy(src: pathlib.Path, dst: pathlib.Path) -> None:
            if self._app:
                self._app.update_status(f"REUPLOAD  ·  copying {src.name}…")
            shutil.copy(src, dst)

        # Quads — all performances (e.g. .1, .2) for this band on this date
        for ul_file in sorted(self.vids_dest.glob('*_CAM1.mp4')):
            base      = ul_file.stem[:-5]   # strip _CAM1
            d, b      = extract_date_band(base)
            if d != date_str or b != band:
                continue
            for q in CAM_LABELS:
                quad = self.vids_dest / f'{base}_{q}.mp4'
                if quad.exists():
                    cloud_dst = dest / cloud_filename(quad.name)
                    _copy(quad, cloud_dst)
                    uploaded.append(cloud_dst)
            self.logger.info(f"REUPLOAD: {base} → {dest.name}/")

        # ZIP
        if self.audio_dest.is_dir():
            for zf in self.audio_dest.glob('*.zip'):
                zd, zb = extract_date_band(zf.stem)
                if zd == date_str and zb == band:
                    cloud_dst = dest / cloud_filename(zf.name)
                    _copy(zf, cloud_dst)
                    uploaded.append(cloud_dst)
                    self.logger.info(f"REUPLOAD: {zf.name} → {dest.name}/")
                    break

        if not uploaded:
            self.logger.info(f"REUPLOAD: no files found for {band} on {date_str}")
            return

        # Update info txt: stamp re-uploaded files, preserve prior history
        expire_date = datetime.date.today() + datetime.timedelta(days=EXPIRE_AGE)
        expire_str  = expire_date.strftime(f'%b {expire_date.day}, %Y')
        try:
            media_in_dest = [
                f for f in dest.iterdir()
                if f.is_file() and f.name != '_nofun_info.txt'
            ]
            write_sharepoint_info(
                dest, media_in_dest,
                expire_date=expire_date,
                new_files=uploaded,
            )
        except OSError as e:
            self.logger.warning(f"REUPLOAD: could not write info file: {e}")

        # Rename folder to include this band (and any others already there)
        final_dest = dest
        try:
            new_name = make_sharepoint_folder_name(date_prefix, dest, band)
            if new_name != dest.name:
                new_dest = dest.parent / new_name
                dest.rename(new_dest)
                final_dest = new_dest
                self.logger.info(f"REUPLOAD: renamed folder → {new_name}")
        except OSError as e:
            self.logger.warning(f"REUPLOAD: could not rename folder: {e}")

        # Write cloud file records into the encoding DB
        from nofun.encoding_db import _now_iso
        for f in uploaded:
            cloud_path = final_dest / f.name
            # date+band come from the function args — cloud filenames have the
            # date stripped, so extract_date_band(f.stem) would return TBD.
            d, b = date_str, band
            cat = 'zipped_audio' if cloud_path.suffix.lower() == '.zip' else 'quadrant_video'
            try:
                stat = cloud_path.stat()
                self._encoding_db.upsert(d, b, cat, {
                    'path':     str(cloud_path),
                    'size':     stat.st_size,
                    'mtime':    stat.st_mtime,
                    'type':     'zipped audio' if cat == 'zipped_audio' else 'quadrant',
                    'location': 'cloud',
                })
            except OSError:
                pass
        if uploaded:
            self._encoding_db.save()
        dehydrate_cloud_files(uploaded, self.logger)

        if self._app:
            self._app.update_status('')
        self.logger.info(
            f"REUPLOAD: {len(uploaded)} file(s) uploaded — good until {expire_str}"
        )

    def _do_reupload(self, date_str: str, band: str) -> None:
        """Body of a REUPLOAD job — called by the manual worker thread."""
        self._reupload_performance(date_str, band)
        if self._active_menu == MenuMode.STATUS:
            self._show_status_list()

    def _enqueue_reupload(self, date_str: str, band: str) -> None:
        """Enqueue a REUPLOAD job for *band* on *date_str* as a MANUAL queue entry."""
        from nofun.job_manifest import JobManifest, PipelineJob
        manifest_key = perf_key(date_str, band) + '_REUPLOAD'
        # Skip if already pending or running for this band
        active_keys = {qj.manifest_key for qj in self._job_queue.all_active()}
        if manifest_key in active_keys:
            self.logger.info(f"NOTICE  REUPLOAD already queued for {band}")
            return
        job = PipelineJob(kind='_reupload', label=f'REUPLOAD {band}', priority=5)
        manifest = JobManifest(
            performance_key=manifest_key,
            jobs=[job],
            python_fns={job.job_id: lambda d=date_str, b=band: self._do_reupload(d, b)},
        )
        self._job_queue.enqueue(manifest, JobCategory.MANUAL)
        self.logger.info(f"REUPLOAD  queued for {band} on {date_str}")

    # -----------------------------------------------------------------------
    # Remaster
    # -----------------------------------------------------------------------

    def _do_remaster_for_band(self, date_str: str, ps, trial_seconds: int = 0, force: bool = False) -> None:
        """Perform mastering + reel for a single band (one MANUAL queue job).

        ps is a PerformanceState captured at enqueue time.
        force=False: skip ZIP extraction if an AUDIO MP3 already exists.
        force=True: redo everything from scratch.
        """
        import tempfile, zipfile as _zf
        from nofun.mastering import generate_masters
        from nofun.reel import generate_reel
        import nofun.mastering as _mastering

        # Windows: initialise COM so VST3 plugin loading works in this thread.
        _com_inited = False
        if is_windows():
            try:
                import ctypes
                hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
                _com_inited = hr in (0, 1)  # S_OK or S_FALSE (already inited)
            except Exception:
                pass

        # Only reset the attempted flag if the previous load failed (cache is None).
        # Reusing a successful load across bands is safe — MANUAL jobs run on the
        # same persistent worker thread. Resetting when a prior thread is still
        # running load_plugin causes two concurrent VST3 calls → native crash.
        if _mastering._ott_plugin_cache is None:
            _mastering._ott_plugin_attempted = False

        # Perf key used to record outcome for _do_reel_for_perf (YY-MM-DD_Band form).
        short = short_date(date_str)
        perf  = perf_key(date_str, ps.band)

        if not ps.zip_files:
            self.logger.info(
                f"REMASTER  no ZIP found for {ps.band} — skipping  "
                f"(downstream REEL will skip until AUDIO produces {perf}.zip)"
            )
            self._remaster_status[perf] = 'no_zip'
            return

        zip_path = ps.zip_files[0]
        if not zip_path.exists():
            # The stored path (from the encoding DB) can be stale: missing the
            # _MULTITRACK suffix, or carrying space/underscore drift between the
            # band name and the on-disk filename. Resolve the real ZIP by
            # matching the canonical perf key against normalised candidate stems.
            def _norm(stem: str) -> str:
                return stem.replace(' ', '_').replace('_MULTITRACK', '')
            cands = sorted(
                (z for z in self.audio_dest.glob(f'{short}_*.zip')
                 if _norm(z.stem) == perf),
                key=lambda z: '_MULTITRACK' not in z.stem,  # prefer multitrack ZIP
            )
            if not cands:
                self.logger.warning(
                    f"REMASTER  ZIP path stale, no on-disk match for {perf}: {zip_path.name}"
                )
                self._remaster_status[perf] = 'no_zip'
                self._clear_op('remaster')
                return
            self.logger.info(f"REMASTER  resolved stale ZIP path → {cands[0].name}")
            zip_path = cands[0]
        # Derive the master basename from the canonical perf key (YY-MM-DD_Band,
        # underscores), NOT the ZIP filename — ZIP stems carry _MULTITRACK and can
        # contain spaces (e.g. "26-05-13_Mall Goth_MULTITRACK.zip"), which would
        # name the master differently from what REEL/existing_fullset look up.
        base     = perf   # e.g. 26-04-11_ALTAR

        self.logger.info(f"REMASTER  {base}")
        self._set_op('remaster', f'REMASTER  {base}')

        _TEST_SEEK = 900.0  # 15 min in
        existing_fullset = (
            self.audio_dest / f'{base}_AUDIO.mp3'
            if (self.audio_dest / f'{base}_AUDIO.mp3').exists()
            else None
        )
        if existing_fullset and not force:
            self.logger.info(f"REMASTER  {base}  using existing AUDIO (skipping ZIP extract)")
            results = [existing_fullset]
        else:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = pathlib.Path(tmp)
                    with _zf.ZipFile(zip_path) as zf:
                        zf.extractall(tmp_path)
                    wav_files = sorted([*tmp_path.glob('*.wav'), *tmp_path.glob('*.flac')])
                    if not wav_files:
                        self.logger.warning(f"REMASTER  ZIP contains no channel audio: {zip_path.name}")
                        self._remaster_status[perf] = 'zip_empty'
                        self._clear_op('remaster')
                        return
                    clip_arg = (_TEST_SEEK, _TEST_SEEK + trial_seconds) if trial_seconds else None
                    results = generate_masters(wav_files, base, self.audio_dest,
                                               self.logger, selected_only=True,
                                               clip=clip_arg,
                                               script_runner=self._script_runner,
                                               denoise_room=True, dyers=True)
            except Exception as exc:
                self.logger.error(f"REMASTER  {base}  mastering failed: {exc!r}", exc_info=exc)
                self._remaster_status[perf] = 'mastering_error'
                self._clear_op('remaster')
                return

        # Mastering can return cleanly having written nothing — e.g. the ZIP's
        # WAVs were all silent/missing so every channel combo was skipped. Treat
        # "no audio file on disk" as terminal, not success: otherwise the show
        # records 'ok', its REEL skips with "AUDIO not found", and the hourly
        # reconciler re-queues it forever. Cleared on restart, so a genuine
        # retry (once the WAVs exist) needs an engine bounce.
        results = [p for p in results if p.exists() and p.stat().st_size > 0]
        if not results:
            self.logger.warning(
                f"REMASTER  {base}  no audio master produced "
                f"(missing or unusable source WAVs in {zip_path.name})"
            )
            self._remaster_status[perf] = 'no_audio'
            self._clear_op('remaster')
            return

        # Copy AUDIO to SharePoint date folder. The throwaway smoke band must
        # never reach OneDrive — this copy bypasses _sync_perf_files (and its
        # guard), so it needs its own band check.
        if (self.sharepoint_dest and self.sharepoint_dest.is_dir()
                and not _is_smoke_band(ps.band)):
            from nofun.inventory import extract_date_band as _edb
            for wav_path in results:
                wav_date, _ = _edb(wav_path.stem)
                if wav_date != 'TBD':
                    wav_prefix = wav_date   # already YY-MM-DD
                    sp_wav = (
                        self._find_date_folder(self.sharepoint_dest, wav_prefix)
                        or self.sharepoint_dest / wav_prefix
                    )
                    sp_wav.mkdir(exist_ok=True)
                    # strip _MULTITRACK so the remaster overwrites the original
                    # <BAND>_AUDIO.mp3 instead of landing beside it
                    cloud_name = cloud_filename(wav_path.name).replace('_MULTITRACK', '')
                    sp_dst = sp_wav / cloud_name
                    shutil.copy(wav_path, sp_dst)
                    self.logger.info(f"SHARE   {wav_path.name} → {sp_wav.name}")
                    dehydrate_cloud_files([sp_dst], self.logger)

        self.logger.info(f"REMASTER  {base}  done")
        self._remaster_status[perf] = 'ok'
        self._clear_op('remaster')
        if self._active_menu == MenuMode.STATUS:
            self._show_status_list()

        if _com_inited:
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    def _enqueue_remaster(self, date_str: str, trial_seconds: int = 0, force: bool = False,
                          band: str | None = None, reel_overwrite: bool = True) -> None:
        """Enqueue one REMASTER + REEL job pair per band for *date_str*.

        Each band gets a REMASTER (MANUAL) job followed by a REEL (GPU_BOUND)
        job that depends on it, so both appear individually in the JOBS menu.
        When *band* is given, only that band is enqueued and the manifest key is
        YY-MM-DD_<band>_REMASTER; otherwise all bands run under YY-MM-DD_REMASTER.
        """
        from nofun.job_manifest import JobManifest, PipelineJob
        short = short_date(date_str)
        bands = [
            ps for (d, _), ps in self._status_entries
            if d == date_str and ps.band not in ('NOFUN', 'TBD', '')
        ]
        if band is not None:
            bands = [ps for ps in bands if ps.band == band]
        if not bands:
            self.logger.info(f"REMASTER  no bands found for {date_str}")
            return

        jobs: list = []
        python_fns: dict = {}
        cat_map: dict = {}
        for ps in bands:
            master_label = f"{short} {ps.band} REMASTER"
            if trial_seconds:
                master_label += f" (trial {trial_seconds}s)"
            if force:
                master_label += " (force)"
            master_job = PipelineJob(kind='_remaster', label=master_label, priority=5)
            jobs.append(master_job)
            python_fns[master_job.job_id] = (
                lambda _ps=ps, _d=date_str, _t=trial_seconds, _f=force:
                    self._do_remaster_for_band(_d, _ps, _t, _f)
            )
            cat_map[master_job.job_id] = JobCategory.MANUAL

            perf = perf_key(date_str, ps.band)
            reel_job = PipelineJob(
                kind='generate_reel',
                label=f"{short} {ps.band} REEL",
                priority=6,
                depends=[master_job.job_id],
            )
            jobs.append(reel_job)
            python_fns[reel_job.job_id] = lambda p=perf, _o=reel_overwrite: self._do_reel_for_perf(p, overwrite=_o)
            cat_map[reel_job.job_id] = JobCategory.MANUAL  # user-initiated, no time gate

        manifest_key = perf_key(date_str, band) + '_REMASTER' if band else f"{short}_REMASTER"
        manifest = JobManifest(performance_key=manifest_key, jobs=jobs, python_fns=python_fns)
        self._job_queue.enqueue(manifest, JobCategory.MANUAL, category_map=cat_map)
        if trial_seconds:
            self.logger.info(f"REMASTER  queued {len(bands)} band(s) (trial {trial_seconds}s)")
        elif force:
            self.logger.info(f"REMASTER  queued {len(bands)} band(s) — forcing from scratch")
        else:
            self.logger.info(
                f"REMASTER  queued {len(bands)} band(s)"
                " — type REMASTER again to restart from scratch"
            )

    def _finish_incomplete_shows(self) -> None:
        """Reconciler: queue REMASTER+REEL for recent shows that have local quads
        + ZIP but are missing the AUDIO master or INSTAGRAM reel on disk.

        Recovers shows interrupted after quads+ZIP but before master/reel (e.g. an
        engine restart) — the watchdog never re-detects them once raw sources are
        swept, and the per-perf manifest gates master/reel behind fresh
        encode/audio jobs. Master/reel presence is read from disk because the
        encoding DB intentionally does not track those output types.
        """
        # Rebuild from the encoding DB ourselves — don't depend on the STATUS
        # menu having been opened. Otherwise _status_entries stays empty on a
        # fresh engine and this reconciler silently no-ops forever.
        self._rebuild_status_entries()
        if not self._status_entries:
            return
        today  = datetime.date.today()
        queued = 0
        for (date, band), ps in self._status_entries:
            if band in ('NOFUN', 'TBD', ''):
                continue
            if not (ps.zip_files and len(ps.quad_files) >= 4):
                continue
            short = short_date(date)
            try:
                y, mo, d = short.split('-')
                rec_date = datetime.date(2000 + int(y), int(mo), int(d))
            except (ValueError, AttributeError):
                continue
            if (today - rec_date).days > EXPIRE_AGE:
                continue
            perf    = perf_key(date, band)
            audio   = self.audio_dest / f'{perf}_AUDIO.mp3'
            reel_ok = bool(files_for_perf(self.vids_dest, '_INSTAGRAM.mp4', perf))
            if audio.exists() and reel_ok:
                continue
            # Ask the live queue, not a pre-loop snapshot: two status rows can
            # normalise to the same perf, so the second must see the first's
            # just-enqueued REMASTER (which a snapshot taken before the loop
            # misses). _enqueued_keys still guards an in-flight full pipeline.
            rk = perf_key(date, band) + '_REMASTER'
            if any(qj.manifest_key == rk for qj in self._job_queue.all_active()) \
                    or perf in self._enqueued_keys:
                continue
            if self._remaster_status.get(perf) in ('mastering_error', 'no_zip', 'zip_empty', 'no_audio'):
                # A prior attempt this session failed terminally (no usable ZIP,
                # or mastering crashed) — re-queuing hourly just spams the queue
                # and log. Cleared on restart, so a genuine retry needs a bounce.
                continue
            miss = ' '.join(filter(None, [
                '' if audio.exists() else 'AUDIO',
                '' if reel_ok else 'REEL',
            ]))
            self.logger.info(f'FINISH  {perf} missing {miss} — queuing REMASTER+REEL')
            self._enqueue_remaster(date, band=band, reel_overwrite=False)
            queued += 1
        if queued:
            self.logger.info(f'FINISH  queued {queued} incomplete show(s)')

    def _perf_age_days(self, path: pathlib.Path) -> int | None:
        """Days since the show date parsed from *path*'s filename, or None if undated."""
        date_str, _ = extract_date_band(path.stem)
        if date_str == 'TBD':
            return None
        try:
            y, mo, d = date_str.split('-')
            rec = datetime.date(2000 + int(y), int(mo), int(d))
        except (ValueError, TypeError):
            return None
        return (datetime.date.today() - rec).days

    def _within_backup_window(self, path: pathlib.Path) -> bool:
        """True if *path* is recent enough to belong in the D: raw-backup window."""
        age = self._perf_age_days(path)
        return age is not None and age <= D_BACKUP_AGE

    def _mirror_raws_to_backup(self) -> None:
        """Mirror raw originals (.mov + _MULTITRACK.zip) from the NAS down to local D:.

        D: is a rolling D_BACKUP_AGE-day backup of the *inputs* — if the NAS dies,
        every deliverable can be regenerated from these (the encode is
        deterministic). Deliverables themselves are NOT backed up: that would be
        redundant storage of recoverable data. The window gate keeps the mirror
        from re-copying a raw the expiry just aged out (the .zip lives on N:
        forever, so without the gate it would thrash). No-op unless the NAS is the
        active root — on D: fallback or a trial run there is nothing to back up.
        """
        if self.media_root == self.mount_d:
            return
        pairs = [
            (self.video_archive, self.mount_d / 'video_archive'),  # .mov
            (self.audio_dest,    self.mount_d / 'audio'),          # .zip only (not .mp3)
        ]
        copied, skipped = mirror_files(pairs, RAW_BACKUP_EXTS,
                                       include=self._within_backup_window)
        if copied:
            self.logger.info(
                f'BACKUP MIRROR  {copied} raw(s) → D: ({skipped} already current)')

    def _expire_d_backup_raws(self) -> None:
        """Delete D: raw-backup files older than the D_BACKUP_AGE window.

        Shares its age boundary with `_mirror_raws_to_backup` so an expired file
        is not re-mirrored. No-op when D: is the live primary (NAS down) — those
        are real outputs, not backups. Undated files (age None) are never expired.
        """
        if self.media_root == self.mount_d:
            return
        pairs = [
            (self.mount_d / 'video_archive', ('.mov',)),
            (self.mount_d / 'audio',         ('.zip',)),
        ]
        stale = find_expired(
            pairs, lambda p: (self._perf_age_days(p) or 0) > D_BACKUP_AGE)
        removed = 0
        for f in stale:
            try:
                f.unlink()
                removed += 1
                self.logger.info(f'EXPIRE D BACKUP  {f.name}  (>{D_BACKUP_AGE}d)')
            except OSError as e:
                self.logger.warning(f'EXPIRE D BACKUP  could not remove {f.name}: {e}')
        if removed:
            self.logger.info(f'EXPIRE D BACKUP  {removed} raw(s) removed (>{D_BACKUP_AGE}d)')

    # -----------------------------------------------------------------------
    # Trial summary
    # -----------------------------------------------------------------------

    def _print_trial_summary(self) -> None:
        real_vids  = self.mount_d / 'videos'
        real_clips = self.mount_d / 'clips'
        real_audio = self.mount_d / 'audio'

        print("\n=== Real run would produce: ===")

        vid_lines = []
        for f in sorted(self.search_dir.glob('*.mov')):
            base = f.stem
            vid_lines.append(f"    {base}_CAM1/CAM2/CAM3/CAM4.mp4  (4 quadrants)")
            if self.mount_d != pathlib.Path('.'):
                vid_lines.append(f"    {f.name}  (original, moved here)")
        if vid_lines:
            print(f"  {real_vids}/")
            for l in vid_lines:
                print(l)

        clip_lines = []
        for d in sorted(self.clips_dest.iterdir()) if self.clips_dest.is_dir() else []:
            if d.is_dir():
                count = sum(1 for _ in d.glob('*.mp4'))
                clip_lines.append(f"    {d.name}/  ({count} proxy clips, 320×180 30fps HEVC)")
        if clip_lines:
            print(f"  {real_clips}/")
            for l in clip_lines:
                print(l)

        audio_lines = []
        if self.audio_dest.is_dir():
            for f in sorted(self.audio_dest.glob('*.zip')):
                with _zipfile.ZipFile(f) as z:
                    ch_count = sum(1 for n in z.namelist()
                                   if n.endswith(('.wav', '.flac')))
                audio_lines.append(f"    {f.name}  ({ch_count} channels)")
        if audio_lines:
            print(f"  {real_audio}/")
            for l in audio_lines:
                print(l)
        print()

    # -----------------------------------------------------------------------
    # Startup summary
    # -----------------------------------------------------------------------

    def _show_startup_summary(self) -> None:
        """Print the last 20 performance rows from file_summary.txt, if available."""
        if not self.inventory_summary.exists():
            return
        try:
            text = self.inventory_summary.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return

        # Collect performance rows: start with two spaces + YY-MM-DD (2-digit year)
        perf_lines = [ln for ln in text.splitlines()
                      if re.match(r'  \d{2}-\d{2}-\d{2}\s', ln)]
        if not perf_lines:
            return

        recent = perf_lines[:20]  # already sorted date-desc by inventory generator

        # Print a minimal header + the rows
        header = (
            f"  {'Date':<12} {'Band':<28} {'Mov':>3} {'Q':>3} {'Wav':>3} {'Zip':>3} "
            f"{'Cloud':>8}   State"
        )
        sep = "  " + "-" * 88
        print()
        print("  Recent performances (last 20):")
        print(header)
        print(sep)
        for ln in recent:
            print(ln)
        print()

    # -----------------------------------------------------------------------
    # Command handling
    # -----------------------------------------------------------------------

    def _toggle_streams(self) -> None:
        """Start or stop ffmpeg HTTP streams (toggle)."""
        if self._stream_server and self._stream_server.running:
            self._stream_server.stop()
            self._stream_server = None
            self.logger.info("Streams stopped")
        else:
            self._stream_server = StreamServer(self.clips_dest, BASE_PORT, STREAM_COUNT)
            self._stream_server.start()

    def _run_scan(self, scope: str = 'SCAN') -> None:
        """Probe files and write results to the encoding DB.

        scope:
          'SCAN'    — new + stale files (quads, zips, raw, clips, SharePoint)
          'BIGSCAN' — every file unconditionally, 8 parallel threads,
                      then runs full _run_inventory() to update file_summary.txt
        """
        from nofun.encoding_db import probe_file, _now_iso
        from nofun.inventory import extract_date_band_from_path

        paths: list[pathlib.Path] = []

        # Quads, raw video, audio ZIPs — always included
        if self.vids_dest.is_dir():
            paths += sorted(p for p in self.vids_dest.glob('*.mp4') if p.is_file())
        if self.search_dir.is_dir():
            paths += sorted(p for p in self.search_dir.glob('*.mov') if p.is_file())
        if self.audio_dest.is_dir():
            paths += sorted(p for p in self.audio_dest.glob('*.zip') if p.is_file())

        # SharePoint cloud copies — MP4s and ZIPs in date subfolders (skip archived/)
        if self.sharepoint_dest and self.sharepoint_dest.is_dir():
            for sub in sorted(self.sharepoint_dest.iterdir()):
                if not sub.is_dir() or sub.name == 'archived':
                    continue
                paths += sorted(
                    p for p in sub.iterdir()
                    if p.is_file() and p.suffix.lower() in ('.mp4', '.zip')
                )

        # Clips
        if self.clips_dest.is_dir():
            for d in sorted(self.clips_dest.iterdir()):
                if not d.is_dir():
                    continue
                paths += sorted(p for p in d.glob('*.mp4') if p.is_file())

        # Backfill per-perf runtime_seconds + summary total — runs every scan,
        # even when no new files need probing, so old DB entries grow the field
        # over time and the banner's archive-hours stat stays current.
        backfill_dirty = False
        for date, bands in self._encoding_db._data.get('performances', {}).items():
            for band, perf in bands.items():
                if not isinstance(perf, dict):
                    continue
                rs = self._encoding_db.derive_runtime_seconds(perf)
                if rs > 0 and perf.get('runtime_seconds') != round(rs, 1):
                    self._encoding_db.set_runtime_seconds(date, band, rs)
                    backfill_dirty = True
        if backfill_dirty or 'total_runtime_seconds' not in self._encoding_db.get_summary():
            total_rt = sum(
                perf.get('runtime_seconds', 0.0)
                for bands in self._encoding_db._data.get('performances', {}).values()
                for perf in bands.values()
                if isinstance(perf, dict)
            )
            summary = self._encoding_db._data.setdefault('summary', {})
            summary['total_runtime_seconds'] = total_rt
            backfill_dirty = True

        # Reserved smoke fixture self-heals: a cleaned (outputs-gone) entry is
        # dropped so the next re-stage rebuilds instead of skip-on-presence.
        if self._heal_stale_smoke_entries():
            backfill_dirty = True

        # SCAN: new + stale; BIGSCAN: everything
        if self._app:
            self._app.update_status(f"{scope}  ·  checking {len(paths)} file(s)…")
        if scope == 'SCAN':
            to_probe = self._encoding_db.unscanned_paths(paths)
        else:  # BIGSCAN
            to_probe = paths
        total = len(to_probe)
        if not total:
            if backfill_dirty:
                self._encoding_db.save()
            if self._app:
                self._app.update_status('')
            self.logger.info(f"{scope}: all files already up to date")
            return

        import concurrent.futures
        import zipfile as _zf
        import statistics as _statistics

        # BIGSCAN runs ffprobe calls in parallel (I/O-bound); others stay serial
        # to avoid hammering the drive during an active encode.
        workers = 8 if scope == 'BIGSCAN' else 1

        if scope == 'BIGSCAN':
            self.logger.info(f"BIGSCAN: probing {total} file(s) (full re-scan, {workers} threads)...")
        else:
            self.logger.info(f"SCAN: probing {total} file(s) needing fresh probe...")
        problems: list[str] = []
        done_count = 0
        db_lock    = threading.Lock()
        clip_buckets: dict[tuple[str, str], list[pathlib.Path]] = {}

        def _probe_one(p: pathlib.Path) -> None:
            nonlocal done_count
            path_str = p.as_posix().lower()
            ext      = p.suffix.lower()
            if ext == '.zip':
                category = 'zipped_audio'
            elif ext == '.mov':
                category = 'raw_video'
            elif '/clips/' in path_str:
                category = 'clips'
            else:
                category = 'quadrant_video'

            date_str, band = extract_date_band_from_path(p)
            if date_str == 'TBD':
                with db_lock:
                    done_count += 1
                return

            def _update_progress() -> None:
                n   = done_count
                pct = (n * 100) // total
                if self._app:
                    self._app.update_status(f"{scope}  ·  {n}/{total} ({pct}%)")
                else:
                    self.logger.info(f"{scope}: {n}/{total}  {p.name}")

            if category == 'clips':
                with db_lock:
                    clip_buckets.setdefault((date_str, band), []).append(p)
                    done_count += 1
                _update_progress()
                return

            # Skip SharePoint/OneDrive files — their DB records are written at
            # sync time; re-probing would trigger placeholder hydration (slow download).
            _sp = self.sharepoint_dest
            if _sp and _sp.is_dir():
                try:
                    _is_sp = p.is_relative_to(_sp)
                except (TypeError, ValueError):
                    _is_sp = False
                if _is_sp:
                    with db_lock:
                        done_count += 1
                    _update_progress()
                    return

            try:
                rec: dict = {
                    'path':    str(p),
                    'size':    p.stat().st_size,
                    'mtime':   p.stat().st_mtime,
                    'scanned': _now_iso(),
                }
                if ext == '.zip':
                    try:
                        with _zf.ZipFile(p) as zf:
                            rec['channel_count'] = sum(
                                1 for nm in zf.namelist()
                                if nm.lower().endswith(('.wav', '.flac'))
                            )
                    except Exception:
                        pass
                    with db_lock:
                        self._encoding_db.upsert(date_str, band, category, rec)
                        done_count += 1
                    _update_progress()
                    return

                elif category == 'quadrant_video':
                    quad_label = None
                    for q in CAM_LABELS:
                        if p.stem.endswith(f'_{q}'):
                            quad_label = q
                            break
                    rec['quadrant'] = quad_label or p.stem[-4:]

                    # Non-CAM1 quads: copy metadata from CAM1 if already probed
                    if quad_label and quad_label != 'CAM1':
                        ul_path = p.parent / (p.stem[:-len(quad_label)] + f'CAM1{p.suffix}')
                        ul_rec  = self._encoding_db.lookup(ul_path)
                        if ul_rec and ul_rec.get('codec') and not self._encoding_db.is_stale(ul_rec, ul_path):
                            for field in ('codec', 'resolution', 'duration',
                                          'bitrate_kbps', 'problematic', 'color_space'):
                                if field in ul_rec:
                                    rec[field] = ul_rec[field]
                            with db_lock:
                                self._encoding_db.upsert(date_str, band, category, rec)
                                done_count += 1
                            _update_progress()
                            return

                    info = probe_file(p)
                    rec.update(info)

                else:
                    info = probe_file(p)
                    rec.update(info)

                with db_lock:
                    self._encoding_db.upsert(date_str, band, category, rec)
                    if rec.get('problematic'):
                        problems.append(p.name)
                    done_count += 1
                _update_progress()

            except FileNotFoundError:
                self.logger.debug(f"SCAN: {p.name} not found, skipping")
                with db_lock:
                    done_count += 1
                _update_progress()
            except Exception as exc:
                self.logger.warning(f"SCAN: probe failed for {p.name}: {exc}")
                with db_lock:
                    done_count += 1
                _update_progress()

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_probe_one, to_probe))

        # Build one clips_summary per (date, band) from collected clip paths.
        for (date_str, band), clip_paths in clip_buckets.items():
            try:
                clip_paths_sorted = sorted(clip_paths)
                sample_info = probe_file(clip_paths_sorted[0])
                stats_list  = [p.stat() for p in clip_paths_sorted]
                sizes       = [s.st_size  for s in stats_list]
                mtimes      = [s.st_mtime for s in stats_list]
                bitrates    = [sample_info.get('bitrate_kbps') or 0] * len(clip_paths_sorted)
                durs        = [sample_info.get('duration')     or 0.0] * len(clip_paths_sorted)
                summary = {
                    'dir':                str(clip_paths_sorted[0].parent),
                    'count':              len(clip_paths_sorted),
                    'codec':              sample_info.get('codec'),
                    'resolution':         sample_info.get('resolution'),
                    'fps':                sample_info.get('fps'),
                    'profile':            sample_info.get('profile'),
                    'pix_fmt':            sample_info.get('pix_fmt'),
                    'total_size':         sum(sizes),
                    'min_size':           min(sizes),
                    'max_size':           max(sizes),
                    'avg_size':           sum(sizes) // len(sizes),
                    'min_bitrate_kbps':   min(bitrates),
                    'avg_bitrate_kbps':   sum(bitrates) // len(bitrates),
                    'median_bitrate_kbps': int(_statistics.median(bitrates)),
                    'max_bitrate_kbps':   max(bitrates),
                    'min_duration':       min(durs),
                    'max_duration':       max(durs),
                    'min_mtime':          min(mtimes),
                    'max_mtime':          max(mtimes),
                    'scanned':            _now_iso(),
                }
                self._encoding_db.set_clips_summary(date_str, band, summary)
            except Exception as exc:
                self.logger.warning(f"SCAN: clips_summary failed for {date_str}/{band}: {exc}")

        # Newly-probed quadrant_video records may have changed durations;
        # re-derive runtime_seconds for affected perfs and re-roll the summary
        # total. Idempotent — no-op if nothing drifted.
        for date, bands in self._encoding_db._data.get('performances', {}).items():
            for band, perf in bands.items():
                if not isinstance(perf, dict):
                    continue
                rs = self._encoding_db.derive_runtime_seconds(perf)
                if rs > 0 and perf.get('runtime_seconds') != round(rs, 1):
                    self._encoding_db.set_runtime_seconds(date, band, rs)
        total_rt = sum(
            perf.get('runtime_seconds', 0.0)
            for bands in self._encoding_db._data.get('performances', {}).values()
            for perf in bands.values()
            if isinstance(perf, dict)
        )
        summary = self._encoding_db._data.setdefault('summary', {})
        summary['total_runtime_seconds'] = total_rt

        self._encoding_db.save()
        if self._app:
            self._app.update_status('')
        self.logger.info(f"{scope}: {total} file(s) scanned")
        self._collect_header_stats()
        if problems:
            self.logger.info(f"{scope}: {len(problems)} PROBLEMATIC file(s) (Main10/10-bit):")
            for name in problems:
                self.logger.info(f"          {name}")
        else:
            self.logger.info(f"{scope}: no encoding issues found")

        # BIGSCAN also runs full inventory to update file_summary.txt and
        # location classification (replaces REBUILD).
        if scope == 'BIGSCAN':
            self._run_inventory()

    def _run_scan_async(self, scope: str) -> None:
        """Enqueue a SCAN or BIGSCAN job in the SCHEDULED lane.

        Appears in the JOBS menu and is dispatched by the scheduled worker thread.
        Deduplicates: a second SCAN/BIGSCAN call while one is active is ignored.
        """
        from nofun.job_manifest import JobManifest, PipelineJob

        active = self._job_queue.all_active()
        if any(qj.manifest_key.startswith('_scan_') for qj in active):
            self.logger.info("NOTICE  A scan is already running — please wait")
            return

        perf_key = f'_scan_{scope}'
        job_id   = f'scan_{scope.lower()}_job'
        job = PipelineJob(
            kind='_scan',
            job_id=job_id,
            label=f'{scope}: probe files',
            priority=10,
        )
        manifest = JobManifest(
            performance_key=perf_key,
            jobs=[job],
            python_fns={job_id: lambda s=scope: self._run_scan(s)},
        )
        self._job_queue.enqueue(manifest, JobCategory.SCHEDULED)
        self.logger.info(f"{scope}: queued — open JOBS to monitor progress")

    # ------------------------------------------------------------------
    # INVENTORY interactive menu
    # ------------------------------------------------------------------

    def _rebuild_status_entries(self) -> bool:
        """Re-read encoding DB and rebuild _status_entries + _show_groups.

        Returns False if the DB is empty.
        """
        from collections import defaultdict
        from nofun.inventory import build_performance_states, rows_from_db

        rows = rows_from_db(self._encoding_db)
        if not rows:
            return False
        states = build_performance_states(rows)
        self._status_entries = [
            (k, v) for k, v in sorted(states.items(), reverse=True)
            if k[1] not in ('Audio Recorder', 'TBD')
            and v.recording_date is not None
        ]

        # Group by date for show-level display
        by_date: dict[str, list] = defaultdict(list)
        for (date, band), ps in self._status_entries:
            by_date[date].append(ps)

        self._show_groups = []
        for date in sorted(by_date.keys(), reverse=True):
            perf_list = by_date[date]
            date_prefix = date   # already YY-MM-DD
            # Build display name directly from band names — no SharePoint logic.
            # SP folder operations (REUPLOAD) do their own naming independently.
            bands = [ps.band for ps in perf_list
                     if ps.band not in ('NOFUN', 'TBD', '')]
            display_name = date_prefix + ('_' + '_'.join(bands) if bands else '')
            self._show_groups.append((date, display_name, perf_list))

        return True

    def _show_status_report(self) -> None:
        """Log performance status for the last 7 days."""
        from nofun.inventory import build_performance_states, rows_from_db

        rows = rows_from_db(self._encoding_db)
        if not rows:
            self.logger.info("INVENTORY  No data found — type REBUILD first")
            return

        states = build_performance_states(rows)
        cutoff = datetime.date.today() - datetime.timedelta(days=7)

        recent = {
            k: v for k, v in states.items()
            if k[1] not in ('Audio Recorder', 'TBD') and v.recording_date is not None and v.recording_date >= cutoff
        }

        recording = self._get_recording_files()
        if recording:
            stems = list(dict.fromkeys(f.stem for f in recording))
            self.logger.info(f"● RECORDING  {'  '.join(stems)}")

        self.logger.info("STATUS  ── last 7 days ───────────────────────────────────")
        if not recent:
            self.logger.info("  no performances in the last 7 days")
        else:
            for (date, band), ps in sorted(recent.items(), reverse=True):
                label, colour = _status_label(ps)
                icon = _STATUS_ICON.get(label, '?')
                age  = ps.age_days
                b    = (band[:24] + '..') if len(band) > 26 else band
                self.logger.info(
                    f"  {date}  {b:<26}  [{colour}]{icon} {label}[/{colour}]"
                    f"  [dim]{age}d ago[/dim]"
                )
            self.logger.info(f"  ── {len(recent)} performance(s)")

    # ------------------------------------------------------------------
    # HELP overlay — shared builder used by both home and inventory HELP
    # ------------------------------------------------------------------

    # Each entry: (command, brief_one_liner, [detailed_lines...])
    _HOME_HELP: list[tuple[str, str, list[str]]] = [
        ('NOPROBLEM', 'Process now — bypass the 4pm–midnight no-processing window (again = force re-encode)', [
            'Heavy jobs run only midnight–4pm; the pipeline deliberately does NOT',
            'process between 4 pm and midnight, to avoid interfering with shows.',
            'NOPROBLEM lifts that block immediately so jobs dispatch right now.',
            'Second NOPROBLEM also sets force=True: normally the pipeline skips a',
            '.mov whose four quadrant .mp4s already exist in vids_dest; force clears',
            'that check and re-encodes from scratch. Both flags reset at midnight.',
        ]),
        ('INVENTORY', 'Show all performances in a scrollable overlay', [
            'Opens the INVENTORY menu. Reads encoding_db.json and groups files',
            'by (date, band) into PerformanceState rows (COMPLETE / SHARED / etc.).',
            'Type a row number to expand per-file ffprobe detail.',
            'SCAN probes new + stale files (fast, any time).',
            'BIGSCAN probes everything in parallel (8 threads) then runs a full',
            'filesystem re-index; time-gated to before 4pm (NOPROBLEM to override).',
        ]),
        ('JOBS',      'Show the job queue — pending, running, and recent history', [
            'Opens the JOBS menu. Type a number to select a job and see details.',
            'CANCEL stops the selected job (pending → removed; running → killed).',
            'History is read from the last two log files.',
        ]),
        ('STREAMS',   'Open the live-streams menu (start/stop per-quad VLC streams)', [
            'Opens the STREAMS menu. Lists each quadrant stream slot and its',
            'current state. Used to preview live or recently encoded performances',
            'without leaving the engine.',
        ]),
        ('PAUSE',     'Pause before the next encode job starts', [
            'Sets _pause_state → SOFT_PENDING. Pipeline finishes the current job',
            'then stops. To stop the running job immediately, open JOBS and type',
            'CANCEL on the highlighted running row.',
            'RESUME from any pause state → RUNNING, restores command bar.',
        ]),
        ('RESUME',    'Resume processing from any pause state', [
            'Sets _pause_state = RUNNING regardless of current state (SOFT_PENDING,',
            'HARD_PENDING, or PAUSED). No-op if already RUNNING. Restores the',
            'home command bar text.',
        ]),
        ('HELP',      'Show this help (again for technical detail)', [
            'First HELP: brief one-liner overlay (this view with details).',
            'HELP again while overlay is open: toggle between brief and detailed.',
            'HOME dismisses the overlay.',
        ]),
        ('TEST',      'Reshow any smoke tests not yet marked as passed', [
            'Pops up OS dialogs for every test in nofun/smoke_tests.py TESTS',
            'that has not been marked passed on this machine.',
            'OK marks a test passed (gone forever); Cancel defers to next run.',
        ]),
        ('TUTORIAL',  'A 7-step guided tour of the interface', [
            'Shows 7 OS dialogs walking through the interface.',
            'Cancel at any step exits early. Always reruns on demand.',
        ]),
    ]

    def _build_help_rows(
        self,
        entries: 'list[tuple[str, str, list[str]]]',
        verbose: bool,
    ) -> 'list':
        """Build MenuRow list for a help overlay (brief or detailed)."""
        from nofun.tui import MenuRow
        rows: list[MenuRow] = []
        for cmd_text, brief, details in entries:
            rows.append(MenuRow(index=None, text=f'  [bold]{cmd_text}[/bold]'))
            if verbose:
                for line in details:
                    rows.append(MenuRow(index=None, text=f'    {line}', dim=True))
            else:
                rows.append(MenuRow(index=None, text=f'    {brief}', dim=True))
            rows.append(MenuRow(index=None, text='', dim=True))
        return rows

    def _show_help(self) -> None:
        """Show home-level HELP as an overlay. Toggles brief↔detailed on each call."""
        state = self._help['home']
        if self._app:
            verbose  = state.verbose
            rows     = self._build_help_rows(self._HOME_HELP, verbose)
            subtitle = 'detailed — HELP to toggle' if verbose else 'brief — HELP for detail'
            if state.active:
                self._app.update_menu('HELP', subtitle, rows, subtitle)
            else:
                self._app.show_menu('HELP', subtitle, rows, subtitle)
            self._app.update_command_bar(
                '[green]HELP[/green] to toggle detail  /  [yellow]HOME[/yellow] to close'
            )
            state.active  = True
            state.verbose = not verbose
        else:
            tag = '(detailed)' if state.verbose else '(brief — run again for detail)'
            entries = self._HOME_HELP
            self.logger.info(f"Commands {tag}:")
            for cmd_text, brief, details in entries:
                if state.verbose:
                    self.logger.info(f"  {cmd_text}")
                    for line in details:
                        self.logger.info(f"    {line}")
                else:
                    self.logger.info(f"  {cmd_text:<12} — {brief}")
            state.verbose = not state.verbose

    # -----------------------------------------------------------------------
    # Pause helpers
    # -----------------------------------------------------------------------

    def _move_to_hard_paused(self, files: list[pathlib.Path]) -> None:
        """Move partial output files to mount_d/hard_paused/ after a hard stop."""
        hard_paused_dir = self.mount_d / 'hard_paused'
        hard_paused_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.exists():
                dest = hard_paused_dir / f.name
                try:
                    shutil.move(str(f), str(dest))
                    self.logger.info(
                        f"PAUSE   Partial file preserved → hard_paused/{f.name}",
                        extra={'src': str(f), 'dst': str(dest)},
                    )
                except Exception as exc:
                    self.logger.warning(f"PAUSE   Could not move {f.name}: {exc}")

    def immediate_home(self) -> None:
        """Close the active menu overlay immediately — safe to call from the TUI thread.

        The pipeline worker may be blocked inside ffmpeg (ScriptRunner) and
        unable to drain cmd_queue for minutes.  This method lets the TUI apply
        the visible side-effects of HOME right away so the overlay doesn't stay
        open until the encode finishes.

        Only handles the simple exit case (nothing selected, no help overlay).
        Complex sub-states (job selected, help open) are left for the normal
        queue path so _show_jobs_list() / _show_streams_list() can redraw them.
        """
        from nofun.state import MenuMode
        if self._active_menu == MenuMode.NONE:
            return
        if self._active_menu == MenuMode.JOBS:
            if self._help['jobs'].active or self._jobs_selected_idx is not None:
                return  # queue path will handle collapse/dismiss-help
            self._active_menu = MenuMode.NONE
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)
        elif self._active_menu == MenuMode.STATUS:
            if self._help['inventory'].active or self._rename_state is not None:
                return  # queue path will handle help dismiss / rename cancel
            self._active_menu         = MenuMode.NONE
            self._status_expanded_key = None
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)

    def _flush_commands(self) -> None:
        """Drain any pending commands from the queue without blocking.

        Called at natural yield points (between zip groups, between .mov files)
        so interactive commands (INVENTORY, PAUSE, …) are processed
        promptly even while long operations are running.
        """
        if self._cmd_queue is None:
            return
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
                if cmd:
                    self._handle_command(cmd, self._override_time)
            except queue.Empty:
                break

    def _handle_command(self, cmd: str, override_time: bool) -> bool:
        """Handle a user-typed command. Returns new override_time value."""
        # PAUSE and RESUME are handled globally — before any menu routing —
        # so they work even while a menu is active.
        if cmd in ('PAUSE', 'RESUME'):
            match cmd:
                case 'PAUSE':
                    match self._pause_state:
                        case PauseState.PAUSED:
                            self.logger.info("PAUSE   Already paused — type RESUME to continue")
                        case PauseState.SOFT_PENDING:
                            self.logger.info(
                                "PAUSE   Already pausing — open JOBS to cancel the running job"
                            )
                        case _:
                            # First PAUSE — wait for current job to finish
                            self._pause_state = PauseState.SOFT_PENDING
                            self._job_queue.pause()
                            self.logger.info(
                                "PAUSE   Will pause after current job completes"
                                "  — open JOBS to cancel it now"
                            )
                            if self._app:
                                self._app.update_command_bar(
                                    "Pausing after current job…"
                                    "  |  JOBS to cancel it now"
                                    "  |  RESUME to cancel pause"
                                )
                case 'RESUME':
                    if self._pause_state != PauseState.RUNNING:
                        self._pause_state = PauseState.RUNNING
                        self._job_queue.resume()
                        self.logger.info("RESUME  Continuing processing")
                        if self._app:
                            self._app.update_command_bar(self._HOME_COMMANDS)
                    else:
                        self.logger.info("NOTICE  Not currently paused")
            return override_time

        # Route to active menu handler
        # HOME at top level dismisses any open help overlay
        if cmd == 'HOME' and self._help['home'].active:
            self._help['home'].reset()
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(_HOME_COMMANDS)
            return override_time

        match self._active_menu:
            case MenuMode.STATUS:
                self._handle_status_command(cmd)
                return override_time
            case MenuMode.STREAMS:
                self._handle_stream_command(cmd)
                return override_time
            case MenuMode.JOBS:
                self._handle_jobs_command(cmd)
                return override_time
            case MenuMode.REPROCESS:
                self._handle_reprocess_command(cmd)
                return override_time

        match cmd.strip().upper():
            case 'NOPROBLEM':
                if self._noproblem_active:
                    self.force = True
                    self.logger.info("Force re-encode enabled — re-encoding all files now")
                else:
                    override_time = True
                    self._override_time = True
                    self._noproblem_active = True
                    self.logger.info(
                        "Processing pause bypassed — processing now"
                        "  (type NOPROBLEM again to force re-encode all files)"
                    )
            case 'INVENTORY':
                if self._app:
                    self._enter_status_menu()
                else:
                    self._show_status_report()
            case 'JOBS':
                if self._app:
                    self._enter_jobs_menu()
                else:
                    s = self._job_queue.summary()
                    self.logger.info(
                        f"JOBS  pending={s['pending']} running={s['running']}"
                        f" done={s['done']} failed={s['failed']}"
                    )
            case 'STREAMS':
                self._enter_streams_menu()
            case 'SCAN':
                self._run_scan_async('SCAN')
            case 'BIGSCAN':
                self._run_scan_async('BIGSCAN')
            case 'REPROCESS':
                self._cmd_reprocess()
            case 'TEST':
                from nofun.smoke_tests import run_smoke_tests as _rsts
                import threading as _t
                def _run_test(script_dir=self.script_dir):
                    n = _rsts(script_dir)
                    if n == 0:
                        self.logger.info("TEST  All smoke tests already passed — nothing to show")
                _t.Thread(target=_run_test, daemon=True, name='smoke_tests').start()
            case 'TUTORIAL':
                from nofun.smoke_tests import run_tutorial as _rt
                import threading as _t
                _t.Thread(target=_rt, daemon=True, name='tutorial').start()
            case 'HELP':
                self._show_help()
            case 'HOME':
                # Top-level HOME is a no-op — already home. The help-overlay
                # dismiss case is handled above; menu-exit is routed by the
                # _active_menu match before we reach here.
                pass
            case '':
                pass
            case _:
                self.logger.info(f"NOTICE  Unknown command: {cmd!r}  (type HELP)")
        return override_time

    # -----------------------------------------------------------------------
    # Cleanup on exit
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Manual-job worker
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Manual-job worker
    # -----------------------------------------------------------------------

    def _start_manual_worker(self) -> None:
        """Daemon thread that dispatches MANUAL-category jobs (REMASTER, REUPLOAD).

        Runs separately from the watchdog loop so the TUI stays responsive
        (HOME, menus, etc.) while a long mastering or reel job is in progress.

        Respects PAUSE: stops dispatching new jobs when _pause_state is not
        RUNNING, so the first PAUSE press freezes the queue between bands.
        """
        self._manual_worker_running = True

        def _worker() -> None:
            while self._manual_worker_running:
                if self._pause_state != PauseState.RUNNING:
                    time.sleep(0.5)
                    continue
                if not self._noproblem_active and not self._job_queue.is_within_schedule(JobCategory.MANUAL):
                    time.sleep(5.0)
                    continue
                result = self._job_queue.dispatch_one(JobCategory.MANUAL)
                if result is None:
                    time.sleep(0.5)
                else:
                    if self._active_menu == MenuMode.JOBS:
                        self._show_jobs_list()
                    elif self._active_menu == MenuMode.STATUS:
                        self._rebuild_status_entries()
                        self._show_status_list()

        self._manual_worker_thread = threading.Thread(
            target=_worker, daemon=True, name='manual_worker',
        )
        self._manual_worker_thread.start()

    def _start_workers(self) -> None:
        """Start GPU, CPU, and manual worker daemon threads."""
        self._start_manual_worker()

        if not self._gpu_worker_running:
            from nofun.script_runner import ScriptRunner as _SR
            self._gpu_script_runner = _SR(self.logger)
            self._gpu_worker_running = True
            self._gpu_worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(JobCategory.GPU_BOUND, 'gpu'),
                daemon=True,
                name='gpu-worker',
            )
            self._gpu_worker_thread.start()

        if not self._cpu_worker_running:
            from nofun.script_runner import ScriptRunner as _SR
            self._cpu_script_runner = _SR(self.logger)
            self._cpu_worker_running = True
            self._cpu_worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(JobCategory.CPU_BOUND, 'cpu'),
                daemon=True,
                name='cpu-worker',
            )
            self._cpu_worker_thread.start()

        if not self._scheduled_worker_running:
            self._scheduled_script_runner = None  # python_fn jobs don't need a runner
            self._scheduled_worker_running = True
            self._scheduled_worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(JobCategory.SCHEDULED, 'scheduled'),
                daemon=True,
                name='scheduled-worker',
            )
            self._scheduled_worker_thread.start()

    def _worker_loop(self, category: 'JobCategory', name: str) -> None:
        """Generic worker: dispatch one job at a time from the given category."""
        runner = getattr(self, f'_{name}_script_runner')
        while getattr(self, f'_{name}_worker_running'):
            if self._pause_state != PauseState.RUNNING:
                time.sleep(0.5)
                continue
            if not self._noproblem_active and not self._job_queue.is_within_schedule(category):
                time.sleep(5.0)
                continue
            result = self._job_queue.dispatch_one(category, runner=runner)
            if result is None:
                time.sleep(1.0)
            else:
                self._on_job_complete(name)

    def _on_job_complete(self, lane: str) -> None:
        """Post-job housekeeping called by a worker after each dispatch."""
        if self._stream_server:
            self._stream_server.refresh_clips()
        if self._active_menu == MenuMode.JOBS:
            self._show_jobs_list()
        elif self._active_menu == MenuMode.STATUS:
            self._rebuild_status_entries()
            self._show_status_list()
        # Prune _enqueued_keys for fully-completed manifests
        active_keys = {qj.manifest_key for qj in self._job_queue.all_active()}
        with self._enqueued_keys_lock:
            self._enqueued_keys &= active_keys
        if lane in ('gpu', 'cpu') and self._job_queue.pending_count() == 0:
            self._maybe_refresh_inventory()

    def _kill_worker_runners(self) -> None:
        """Kill the GPU and CPU worker ScriptRunners (for hard stop / JOBS CANCEL)."""
        for runner in (self._gpu_script_runner, self._cpu_script_runner):
            if runner is not None:
                runner.kill()

    # -----------------------------------------------------------------------
    # Cleanup on exit
    # -----------------------------------------------------------------------

    def _cleanup(self) -> None:
        self._manual_worker_running    = False
        self._gpu_worker_running       = False
        self._cpu_worker_running       = False
        self._scheduled_worker_running = False
        # Remove any orphaned temp_trial_ files
        if self.search_dir.exists():
            for f in self.search_dir.glob('temp_trial_*'):
                f.unlink(missing_ok=True)
        # Remove REPROCESS staging directories (symlinks or copies of archived files)
        staging_root = pathlib.Path(__file__).parent / '_reprocess_staging'
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        # Stop streams cleanly so downstream clients (TouchDesigner) get a clean
        # disconnect rather than a broken pipe / stalled connection.
        if self._stream_server and self._stream_server.running:
            self._stream_server.stop()
            self._stream_server = None

    # -----------------------------------------------------------------------
    # File-event detection
    # -----------------------------------------------------------------------

    def _detect_file_events(
        self,
        mov_files: list[pathlib.Path],
        wav_files: list[pathlib.Path],
    ) -> None:
        """Compare current file list to previous scan; log DETECTED / REMOVED events."""
        current: dict[str, tuple[int, float]] = {}
        for f in mov_files + wav_files:
            try:
                st = f.stat()
                current[str(f)] = (st.st_size, st.st_mtime)
            except OSError:
                continue

        # Newly appeared files
        for path_str, (size, mtime) in current.items():
            if path_str not in self._known_files:
                p  = pathlib.Path(path_str)
                mt = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%dT%H:%M:%S')
                self.logger.info(
                    f"DETECTED  {p.name}  ({fmt_size(size)})",
                    extra={'path': path_str, 'size': size, 'mtime': mt},
                )

        # Drain pipeline_moved queue into a local set for this pass.
        suppressed: set[str] = set()
        try:
            while True:
                suppressed.add(self._pipeline_moved.get_nowait())
        except queue.Empty:
            pass

        # Files that vanished without our pipeline moving them.
        # MOV disappearances are unexpected — log individually.
        # WAV disappearances are routine (recording software managing its own
        # files during active recording) — batch into a single count.
        removed_wavs: list[str] = []
        for path_str in self._known_files:
            if path_str not in current and path_str not in suppressed:
                p = pathlib.Path(path_str)
                if p.suffix.lower() == '.wav':
                    removed_wavs.append(p.name)
                else:
                    self.logger.info(
                        f"REMOVED   {p.name}  (disappeared externally)",
                        extra={'path': path_str},
                    )
        if removed_wavs:
            self.logger.info(
                f"REMOVED   {len(removed_wavs)} .wav file(s) disappeared externally",
            )

        self._known_files = current

    def _maybe_create_sharepoint_placeholder(
        self, all_movs: list[pathlib.Path]
    ) -> None:
        """Create (or rename) the SharePoint date folder as soon as a .mov is
        detected in search_dir — even while the file is still being recorded.

        Groups all .movs by date and computes the canonical folder name from ALL
        bands at once, so the name is always correct even when multiple bands are
        detected simultaneously or on app restart.
        """
        if not (self.sharepoint_dest and self.sharepoint_dest.is_dir()):
            return

        from nofun.inventory import extract_date_band

        # Group all .movs by date, collecting new (not-yet-done) bands only
        new_by_date: dict[str, tuple[str, list[str]]] = {}  # date_prefix → (date_str, [bands])
        for mov in all_movs:
            date_str, band = extract_date_band(mov.stem)
            if date_str == 'TBD' or not band or band.upper() in ('NOFUN', 'TBD'):
                continue
            if _is_smoke_band(band):
                continue
            if (date_str, band) in self._sp_placeholder_done:
                continue
            try:
                y, mo, d = date_str.split('-')
                datetime.date(2000 + int(y), int(mo), int(d))   # validate
                date_prefix = date_str
            except (ValueError, AttributeError):
                continue
            if date_prefix not in new_by_date:
                new_by_date[date_prefix] = (date_str, [])
            if band not in new_by_date[date_prefix][1]:
                new_by_date[date_prefix][1].append(band)

        for date_prefix, (date_str, new_bands) in new_by_date.items():
            # Collect ALL bands for this date (already-done + newly detected)
            all_bands = [
                b for (ds, b) in self._sp_placeholder_done
                if ds == date_str
            ] + new_bands

            dest = (
                self._find_date_folder(self.sharepoint_dest, date_prefix)
                or self.sharepoint_dest / date_prefix
            )
            try:
                dest.mkdir(exist_ok=True)
                target = canonical_sharepoint_name(date_prefix, all_bands)
                if target != dest.name:
                    dest.rename(dest.parent / target)
                    dest = dest.parent / target
                    self.logger.info(
                        f"SHARE   placeholder → {target}"
                    )
                else:
                    self.logger.info(
                        f"SHARE   placeholder ready: {dest.name}"
                    )
            except OSError as e:
                self.logger.warning(f"SHARE   could not create placeholder folder: {e}")
                continue

            # Write an in-progress info file so the folder is informative while
            # the band is still recording / encoding. Lists the cloud filenames
            # we expect (4 quads + audio ZIP per band) with a "processing…"
            # marker; SYNC overwrites this with the final form as files land.
            y, mo, d  = date_str.split('-')  # validated when new_by_date was built
            rec_date  = datetime.date(2000 + int(y), int(mo), int(d))
            expected: set[str] = set()
            for mov in all_movs:
                m_date, m_band = extract_date_band(mov.stem)
                if m_date != date_str or not m_band or m_band.upper() in ('NOFUN', 'TBD'):
                    continue
                expected.update(expected_cloud_names(mov.stem, None))
            if expected:
                try:
                    media_in_dest = [
                        f for f in dest.iterdir()
                        if f.is_file() and f.name != '_nofun_info.txt'
                    ]
                    write_sharepoint_info(
                        dest, media_in_dest,
                        expire_date=rec_date + datetime.timedelta(days=EXPIRE_AGE),
                        new_files=[],
                        expected_names=sorted(expected),
                    )
                except OSError as e:
                    self.logger.warning(f"SHARE   could not write placeholder info file: {e}")

            for band in new_bands:
                self._sp_placeholder_done.add((date_str, band))

    # -----------------------------------------------------------------------
    # Status formatting (used by both TUI and non-TUI status display)
    # -----------------------------------------------------------------------

    def _set_op(self, key: str, text: str) -> None:
        """Set a named concurrent-operation slot in the status bar."""
        if self._app:
            self._app.set_op(key, text)

    def _clear_op(self, key: str) -> None:
        """Remove a named operation slot from the status bar."""
        if self._app:
            self._app.clear_op(key)

    def _set_ffmpeg_proc(self, key: str, proc: subprocess.Popen | None) -> None:
        """Register or deregister an ffmpeg Popen handle under *key* (thread-safe).

        GPU_LANE_OVERLAP warning catches any future path that calls _export_clips
        outside the gpu-worker lane — the scheduled-worker did this until May 2026.
        """
        with self._ffmpeg_procs_lock:
            if proc is None:
                self._current_ffmpeg_procs.pop(key, None)
            else:
                existing = self._current_ffmpeg_procs.get(key)
                if existing is not None:
                    self.logger.warning(
                        f"GPU_LANE_OVERLAP slot={key} "
                        f"thread={threading.current_thread().name} "
                        f"existing_pid={existing.pid} new_pid={proc.pid}"
                    )
                self._current_ffmpeg_procs[key] = proc

    def _kill_all_ffmpeg_procs(self) -> None:
        """Kill every tracked ffmpeg process (called on hard pause)."""
        with self._ffmpeg_procs_lock:
            procs = list(self._current_ffmpeg_procs.values())
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    def _reset_noproblem_flags_for_midnight(self) -> None:
        """Clear NOPROBLEM bypass+force flags; called on watchdog hour rollover to 00:00."""
        self._override_time     = False
        self._noproblem_active  = False
        self.force              = False

    def _format_status(self, mov_files: list, wav_files: list, can_process: bool) -> str:
        """Return a Rich markup string for the idle portion of the StatusBar.

        Layout: ``[time]  N× .mov  ·  N× .wav  ·  J jobs  ·  ~M min remaining``
        plus pause-state / scheduling prefixes when active. Recording stems and
        per-op names now live in the progress rows above the bar.
        """
        ts    = datetime.datetime.now().strftime('%H:%M:%S')
        parts: list[str] = []

        if mov_files:
            parts.append(f"[cyan]{len(mov_files)}×[/cyan] .mov")
        if wav_files:
            parts.append(f"[cyan]{len(wav_files)}×[/cyan] .wav")
        if not mov_files and not wav_files:
            parts.append("[dim]no files pending[/dim]")

        recording = self._get_recording_files()
        if recording:
            stems = list(dict.fromkeys(f.stem for f in recording))
            parts.append(f"[yellow]recording  {'  '.join(stems)}[/yellow]")

        summary = self._job_queue.summary()
        n_jobs  = summary.get('running', 0) + summary.get('pending', 0)
        if n_jobs:
            parts.append(f"[dim]{n_jobs} jobs[/dim]")

        eta_s = self._cached_total_eta
        if eta_s and eta_s > 60:
            parts.append(f"[dim]~{int(eta_s // 60)} min remaining[/dim]")

        match self._pause_state:
            case PauseState.PAUSED:
                parts.append("[bold red]PAUSED[/bold red]")
            case PauseState.SOFT_PENDING | PauseState.HARD_PENDING:
                parts.append("[yellow]pausing after current job[/yellow]")
        if not can_process and self._pause_state == PauseState.RUNNING:
            parts.append("[yellow]paused until midnight[/yellow]")
        if (not self._job_queue.is_within_schedule(JobCategory.MANUAL)
                and self._job_queue.pending_count(JobCategory.MANUAL)):
            parts.append("[yellow]jobs queued · runs at midnight[/yellow]")
        if self._stream_server and self._stream_server.running:
            parts.append("[green]streams live[/green]")
        sep = "  [dim]·[/dim]  "
        return f"[dim]\\[{ts}][/dim]  {sep.join(parts)}"

    # Per-kind wall-time estimates (seconds), calibrated from 2026-05-22 show
    _KIND_WALL_S: dict[str, float] = {
        'encode_quads':     780,   # ~13 min for ~45-min source at 3.5x
        'transcode_single': 780,
        'generate_reel':    540,   # ~9 min for ~45-min source at 5.5x
        'export_clips':      30,
        'split_audio':       15,
        '_archive_audio':   210,   # ~3.5 min per ZIP group
        '_remaster':        120,
        '_sync_quads':       90,
        '_sync_audio':       60,
        '_sync_reel':        30,
        '_reupload':         60,
        '_scan':             30,
        '_cleanup_execute':  30,
        '_rename':            5,
    }
    _UNKNOWN_KIND_WALL_S = 60.0   # fallback when a new job kind appears

    def _estimate_total_eta(self) -> float | None:
        """Rough wall-time remaining to drain the active job queue.

        Sums per-job wall estimates grouped by JobCategory; returns the
        max lane since GPU, CPU, and SCHEDULED jobs dispatch in parallel.
        None when the queue is idle.
        """
        active = self._job_queue.all_active()
        if not active:
            return None
        lanes: dict = {}
        for qj in active:
            cost = self._KIND_WALL_S.get(qj.job.kind, self._UNKNOWN_KIND_WALL_S)
            lanes[qj.category] = lanes.get(qj.category, 0.0) + cost
        total = max(lanes.values(), default=0.0)
        return total if total > 0 else None

    # -----------------------------------------------------------------------
    # Per-band processing helpers (called concurrently from band loop)
    # -----------------------------------------------------------------------

    def _drain_all_silent_perfs(
        self,
        perf_sd: dict,
        perf_au: dict,
    ) -> None:
        """Archive _chan*.wav files for perfs already recorded as all-silent in the DB.

        Called once per watchdog loop before job manifests are built.  Handles
        the case where an external process restores files to VenueLighting/ after
        we archived them: we move/delete them cheaply (no ffmpeg probing) and
        remove the perf key from perf_sd/perf_au so no AUDIO job is created.
        Logs a notice the first time per session; stays silent on subsequent loops.
        """
        db = self._encoding_db
        for mapping in (perf_sd, perf_au):
            for perf_key in list(mapping.keys()):
                parts = perf_key.split('_', 1)
                date_full = ('20' + parts[0]) if (len(parts[0]) == 8 and parts[0][2] == '-') else parts[0]
                band = parts[1]
                perf_db = db.get_performance(date_full, band)
                silent_recs = perf_db and perf_db.get('audio_all_silent')
                if not silent_recs:
                    continue
                # Completeness guard: the all-silent verdict is only trustworthy
                # if it was computed on the full channel set. If more channels are
                # present now than were probed when the flag was written, it was a
                # partial probe (e.g. active channels still landing) — let the
                # audio pipeline re-probe rather than draining them unheard. The
                # mtime guard below can't catch this: copy/restore preserves source
                # mtime, so a freshly-arrived channel looks older than the flag.
                n_probed = silent_recs[0].get('n_channels')
                present = [f for f in mapping[perf_key] if f.exists()]
                if n_probed is not None and len(present) > n_probed:
                    continue
                # FS-first: if any file is newer than when we probed silence,
                # let the audio pipeline re-probe rather than blindly archiving.
                updated_ts = silent_recs[0].get('updated', '')
                if updated_ts:
                    try:
                        silent_dt = datetime.datetime.fromisoformat(updated_ts)
                        if any(
                            datetime.datetime.fromtimestamp(f.stat().st_mtime) > silent_dt
                            for f in mapping[perf_key]
                            if f.exists()
                        ):
                            continue
                    except (ValueError, OSError):
                        pass
                zip_path  = self.audio_dest / f'{perf_key}.zip'
                zip_exists = zip_path.exists()
                self.logger.debug(
                    f"Audio drain: {perf_key}"
                    f"  zip={'exists' if zip_exists else 'absent'}"
                    f"  files={len(mapping[perf_key])}"
                )
                files = mapping.pop(perf_key)
                for f in files:
                    if not f.exists():
                        continue
                    self.logger.debug(f"Audio drain: archiving {f.name}")
                    if zip_exists:
                        try:
                            f.unlink()
                            self.logger.info(f"DELETE  {f.name}  (all-silent hw WAV, ZIP exists)")
                        except OSError as e:
                            self.logger.warning(f"LOCKED  {f.name} — cannot delete, will retry ({e})")
                    else:
                        self._archive_or_dedup(f, self.audio_archive)
                if perf_key not in self._audio_silent_notified:
                    self.logger.info(
                        f"Audio: {parts[0]} {band} — all channels silent (DB); "
                        "archiving restored files, skipping AUDIO job"
                    )
                    self._audio_silent_notified.add(perf_key)

    def _process_perf_audio(
        self,
        perf: str,
        ch_files: list,
        sd_files: list,
        au_files: list,
    ) -> None:
        """Run the full audio pipeline for one performance in a thread."""
        if ch_files:
            for gk, gf in self._group_wav_files(ch_files).items():
                if self._pause_state == PauseState.HARD_PENDING:
                    break
                self._zip_wav_group(
                    gk, gf, zip_dest=self.audio_dest,
                    trim_dir=self.search_dir, on_success_real_drive='delete',
                )

        if sd_files and self._pause_state not in (PauseState.SOFT_PENDING, PauseState.HARD_PENDING):
            self._process_audio_group(perf, sd_files, self.search_dir)

        if au_files and self._pause_state not in (PauseState.SOFT_PENDING, PauseState.HARD_PENDING):
            self._process_audio_group(perf, au_files, self.search_dir / 'Audio')

    def _archive_audio_batch(self, hw_files: list) -> None:
        """Run _archive_or_dedup per file; emit one INFO summary instead of per-file LOCKED lines."""
        from nofun.cleanup import ArchiveOutcome
        counts: dict[ArchiveOutcome, int] = {o: 0 for o in ArchiveOutcome}
        for f in hw_files:
            counts[self._archive_or_dedup(f, self.audio_archive)] += 1

        parts: list[str] = []
        if counts[ArchiveOutcome.MOVED]:
            parts.append(f"{counts[ArchiveOutcome.MOVED]} archived")
        if counts[ArchiveOutcome.DEDUPED]:
            parts.append(f"{counts[ArchiveOutcome.DEDUPED]} dropped")
        if counts[ArchiveOutcome.LOCKED_SIZE]:
            parts.append(f"{counts[ArchiveOutcome.LOCKED_SIZE]} still growing")
        if counts[ArchiveOutcome.LOCKED_OSERR]:
            parts.append(f"{counts[ArchiveOutcome.LOCKED_OSERR]} retry next loop")
        if parts:
            self.logger.info(f"ARCHIVE AUDIO  {', '.join(parts)}")

    def _process_perf_video(
        self,
        perf: str,
        mov_list: list,
        skip_clips: bool = False,
    ) -> None:
        """Run the full video pipeline for one performance in a thread."""
        for mov in mov_list:
            ok = self._process_mov(mov, skip_clips=skip_clips)
            if not ok:
                self.logger.warning(
                    f"ALERT   {mov.stem} — encode failed; job marked failed in queue"
                )

    # -----------------------------------------------------------------------
    # Job manifest builder
    # -----------------------------------------------------------------------

    def _do_remaster_for_perf(self, perf: str) -> None:
        """Look up current PerformanceState for perf and call _do_remaster_for_band.

        Used as a python_fn closure in lifecycle manifests so that REMASTER runs
        with up-to-date file state rather than a snapshot captured at enqueue time.
        """
        self._rebuild_status_entries()
        parts = perf.split('_', 1)
        if len(parts) != 2:
            self.logger.warning(f"REMASTER  cannot parse perf key: {perf!r}")
            return
        date_str, band = parts[0], parts[1]
        # Both perf keys and _status_entries keys are canonical YY-MM-DD since the
        # DB migration re-keyed long dates; normalise in case a long date leaks in
        # via perf so the comparison doesn't silently miss (the old code expanded
        # to YYYY-MM-DD and never matched, skipping REMASTER for every band).
        date_str = short_date(date_str)
        for (d, b), ps in self._status_entries:
            if d == date_str and b == band:
                self._do_remaster_for_band(d, ps)
                return
        self.logger.warning(f"REMASTER  no performance state found for {perf}")

    def _export_clips_for_perf(self, perf: str) -> None:
        """Export proxy clips for all quad files belonging to perf."""
        parts = perf.split('_', 1)
        prefix = parts[0]  # date portion e.g. "26-04-11"
        for ul_file in sorted(self.vids_dest.glob(f'{prefix}*_CAM1.mp4')):
            base = ul_file.stem[:-5]  # strip "_CAM1"
            if self._pause_state in (PauseState.SOFT_PENDING, PauseState.HARD_PENDING):
                break
            self._export_clips(base)

    def _sync_perf_files(
        self,
        perf: str,
        files: list[pathlib.Path],
        overwrite: bool = False,
    ) -> None:
        """Copy files to the SharePoint date folder for perf. No-op if SP not mounted.

        overwrite=False (default): skip files already present in the cloud and skip
        dehydrated dated placeholders — used by the normal lifecycle sync.
        overwrite=True: replace an existing cloud copy in place (then re-dehydrate) —
        used by REMASTER so a rebuilt reel supersedes the stale one.
        """
        if not self.sharepoint_dest or not self.sharepoint_dest.is_dir():
            return
        if not files:
            return
        parts = perf.split('_', 1)
        date_str = parts[0]
        if _is_smoke_band(parts[1] if len(parts) > 1 else ''):
            return
        try:
            d_parts = date_str.split('-')
            if date_str.startswith('20'):
                date_prefix = f'{d_parts[0][2:]}-{d_parts[1]}-{d_parts[2]}'
            else:
                date_prefix = date_str
        except (IndexError, ValueError):
            date_prefix = date_str
        sp_folder = (
            self._find_date_folder(self.sharepoint_dest, date_prefix)
            or self.sharepoint_dest / date_prefix
        )
        sp_folder.mkdir(exist_ok=True)
        for f in files:
            dest   = sp_folder / cloud_filename(f.name)
            legacy = sp_folder / f.name
            action = plan_cloud_copy(
                dest.exists(),
                legacy.exists(),
                legacy.exists() and is_cloud_only(legacy),
                overwrite,
            )
            if action == 'skip':
                # Already in cloud (and not overwriting), or only a dehydrated dated
                # placeholder exists — renaming that would force a download.
                continue
            if action == 'rename':
                # Existing hydrated dated copy → rename in place instead of re-upload.
                legacy.rename(dest)
                self.logger.info(f"SHARE   renamed in cloud: {f.name} → {dest.name}")
            elif action == 'overwrite':
                # REMASTER: replace the stale cloud copy with the rebuilt file.
                shutil.copy(f, dest)
                self.logger.info(f"SHARE   replaced in cloud: {dest.name}")
                dehydrate_cloud_files([dest], self.logger)
            else:  # 'copy'
                shutil.copy(f, dest)
                self.logger.info(f"SHARE   {f.name} → {sp_folder.name}")

    def _sync_perf_quads(self, perf: str) -> None:
        """Copy finished quad MP4s for perf to SharePoint."""
        parts = perf.split('_', 1)
        prefix = parts[0]
        quad_files = [
            f for q in CAM_LABELS
            for f in sorted(self.vids_dest.glob(f'{prefix}*_{q}.mp4'))
        ]
        if not quad_files:
            return
        self.logger.info(f"SYNC    {perf}  quads ({len(quad_files)} files) → OneDrive")
        self._sync_perf_files(perf, quad_files)
        self.logger.info(f"SYNC    {perf}  quads done")

    def _sync_perf_audio(self, perf: str) -> None:
        """Copy the ZIP archive for perf to SharePoint."""
        from nofun.inventory import extract_date_band
        parts = perf.split('_', 1)
        date_str = parts[0]
        band = parts[1] if len(parts) > 1 else perf
        zip_files = [
            zf for zf in self.audio_dest.glob('*.zip')
            if extract_date_band(zf.stem) == (date_str, band)
        ] if self.audio_dest.is_dir() else []
        if not zip_files:
            return
        self.logger.info(f"SYNC    {perf}  audio ({len(zip_files)} files) → OneDrive")
        self._sync_perf_files(perf, zip_files)
        self.logger.info(f"SYNC    {perf}  audio done")

    def _do_reel_for_perf(self, perf: str, overwrite: bool = False) -> None:
        """Render one reel per quad set for perf using the shared AUDIO MP3.

        A band can have multiple sessions on the same date (multiple quad sets);
        each gets its own reel file named after the quad base (e.g. *10.0_INSTAGRAM.mp4).

        overwrite=True (REMASTER) replaces an existing cloud reel; the default
        leaves the lifecycle sync's skip-if-exists behavior intact.
        """
        from nofun.reel import generate_reel
        base    = perf  # perf key matches zip/audio base name
        fullset = self.audio_dest / f'{base}_AUDIO.mp3'
        if not fullset.exists():
            upstream = self._remaster_status.get(perf)
            if upstream == 'no_zip':
                self.logger.warning(
                    f"REEL  skipped for {base} — upstream REMASTER had no ZIP "
                    f"(AUDIO job hasn't produced {base}_MULTITRACK.zip yet)"
                )
            elif upstream == 'zip_empty':
                self.logger.warning(
                    f"REEL  skipped for {base} — upstream REMASTER found an "
                    f"empty ZIP (no channel audio inside {base}_MULTITRACK.zip)"
                )
            elif upstream == 'mastering_error':
                self.logger.warning(
                    f"REEL  skipped for {base} — upstream REMASTER errored "
                    f"(see prior REMASTER {base}  mastering failed: …)"
                )
            else:
                self.logger.warning(
                    f"REEL  skipped — AUDIO not found: {fullset.name} "
                    f"(upstream REMASTER status: {upstream or 'unknown'})"
                )
            return

        # Collect all CAM1 quad files; each represents a separate session.
        # Match on normalised perf identity, not a literal prefix, so quads
        # encoded under a non-canonical band spelling (e.g. a space + session
        # suffix) are still found — see files_for_perf.
        ul_candidates = files_for_perf(self.vids_dest, '_CAM1.mp4', base)
        if not ul_candidates:
            self.logger.warning(f"REEL  skipped for {base} — no CAM1 quad found")
            return

        for ul in ul_candidates:
            real_base = ul.stem[:-5]  # strip trailing "_CAM1"
            quad_map  = {q: self.vids_dest / f'{real_base}_{q}.mp4'
                         for q in CAM_LABELS}
            missing = [q for q, p in quad_map.items() if not p.exists()]
            if missing:
                self.logger.warning(
                    f"REEL  skipped for {real_base} — missing quad(s): {', '.join(missing)}"
                )
                continue
            out = self.vids_dest / f'{real_base}_INSTAGRAM.mp4'
            self.logger.info(f"REEL  {out.stem}")
            self._set_op('remaster', f'REEL  {out.stem}')
            _HB_INTERVAL = 60.0
            _last_hb: list[float] = [time.monotonic()]
            try:
                reel_dur = float(probe_format(ul, 'duration') or 0.0)
            except (ValueError, TypeError):
                reel_dur = 0.0
            reel_frames = probe_total_frames(ul, reel_dur)
            _, reel_band = extract_date_band(real_base)

            def _reel_progress(frame: str, fps: str, tc: str, speed: str,
                               _out=out, _last=_last_hb, _dur=reel_dur,
                               _tf=reel_frames, _band=reel_band) -> None:
                if self._app:
                    self._app.update_progress(
                        frame, fps, tc, speed,
                        duration=_dur, job_label='reel',
                        band=_band, total_frames=_tf,
                    )
                now = time.monotonic()
                if now - _last[0] >= _HB_INTERVAL:
                    self.logger.debug(
                        f"REEL    {_out.stem}  ♥  "
                        f"(frame {frame}, {fps} fps, {tc} encoded, {speed})"
                    )
                    _last[0] = now

            ok = generate_reel(quad_map, fullset, out, self.logger, self.enc,
                               trial_run=self.trial_run,
                               seek=0.0,
                               progress_cb=_reel_progress,
                               proc_cb=lambda p: self._set_ffmpeg_proc('reel', p),
                               script_runner=self._script_runner)
            self.logger.debug(f'REEL  generate_reel returned ok={ok}', extra={'tui': False})
            self._set_ffmpeg_proc('reel', None)
            if self._app:
                self._app.clear_row('progress')
            self._clear_op('remaster')
            if ok and out.exists():
                self._sync_perf_files(perf, [out], overwrite=overwrite)

    def _sync_perf_reel(self, perf: str) -> None:
        """Copy the Instagram reel MP4(s) for perf to SharePoint."""
        reel_files = list(self.vids_dest.glob(f'{perf}*_INSTAGRAM.mp4'))
        self._sync_perf_files(perf, reel_files)

    def _detect_layout_cached(self, mov: pathlib.Path) -> 'Layout':
        """Content-detect a mov's camera layout, cached on (path, mtime, size).

        Short-circuits to UNKNOWN when outputs already exist — routing is moot
        for an already-encoded mov, and this bounds the ffmpeg cost to genuinely
        new, unprocessed arrivals.
        """
        base = mov.stem
        if (self.vids_dest / f'{base}.mp4').exists() or all(
            (self.vids_dest / f'{base}_{q}.mp4').exists() for q in CAM_LABELS
        ):
            return Layout.UNKNOWN
        try:
            st = mov.stat()
            ckey = (str(mov), st.st_mtime_ns, st.st_size)
        except OSError:
            return Layout.UNKNOWN
        cached = self._layout_cache.get(str(mov))
        if cached is not None and cached[0] == ckey:
            return cached[1]
        layout = detect_layout(mov, logger=self.logger)
        self._layout_cache[str(mov)] = (ckey, layout)
        return layout

    def _route_movs_by_layout(
        self,
        perf_mov: 'dict[str, list[pathlib.Path]]',
        perf_singles: 'dict[str, list[pathlib.Path]]',
    ) -> None:
        """Move main-path movs that content-detect as non-quad into the singles map."""
        route_by_layout(perf_mov, perf_singles, self._detect_layout_cached, self.logger)

    def _build_full_manifest(
        self,
        perf: str,
        mov_list: list[pathlib.Path],
        ch_files: list[pathlib.Path],
        sd_files: list[pathlib.Path],
        au_files: list[pathlib.Path],
        singles_list: 'list[pathlib.Path] | None' = None,
    ) -> 'tuple[JobManifest, dict[str, JobCategory]]':
        """Build the full 8-job lifecycle manifest for one performance.

        Returns (manifest, category_map) where category_map maps job_id → JobCategory.

        Dependency graph:
            encode_quads (GPU, p=10) ──┬──▶ export_clips (GPU, p=30)
                                       ├──▶ sync_quads   (SCHED, p=40)
                                       └──┐
            split_audio  (CPU, p=10) ─────┼──▶ sync_audio  (SCHED, p=40)
                                          └──▶ remaster    (MANUAL, p=50)
                                                   └──▶ generate_reel (GPU, p=60)
                                                               └──▶ sync_reel (SCHED, p=70)
        """
        from nofun.job_manifest import JobManifest
        from nofun.job_manifest import PipelineJob as _ScriptJob

        _parts = perf.split('_', 1)
        date   = _parts[0]
        band   = _parts[1] if len(_parts) > 1 else perf

        jobs: list       = []
        python_fns: dict = {}
        cat_map: dict    = {}

        # --- 0. Singles pass-through transcode jobs (GPU_BOUND, priority=10) ---
        for mov in (singles_list or []):
            base  = mov.stem
            _dest = self.vids_dest / f'{base}.mp4'
            if _dest.exists() and not self.force:
                continue
            job = _ScriptJob(
                kind='transcode_single',
                label=f'{date} {band} SINGLE',
                priority=10,
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda m=mov: self._transcode_single(m)
            cat_map[job.job_id] = JobCategory.GPU_BOUND

        # --- 1. Video encode jobs (GPU_BOUND, priority=10) ---
        encode_ids: list[str] = []
        for mov in mov_list:
            base = mov.stem
            _quads = [self.vids_dest / f'{base}_{q}.mp4' for q in CAM_LABELS]
            if all(p.exists() for p in _quads) and not self.force:
                self.logger.info(
                    f'{perf}: encode_quads skipped — all 4 quads present at '
                    f'{self.vids_dest}')
                continue
            job = _ScriptJob(
                kind='encode_quads',
                label=f'{date} {band} REENCODE',
                priority=10,
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda m=mov: self._process_perf_video(
                perf, [m], skip_clips=True
            )
            cat_map[job.job_id] = JobCategory.GPU_BOUND
            encode_ids.append(job.job_id)

        # --- 2. Audio processing job (CPU_BOUND, priority=10) ---
        audio_ids: list[str] = []
        if ch_files or sd_files or au_files:
            _zip_path = self.audio_dest / f'{perf}_MULTITRACK.zip'
            if not (_zip_path.exists() and not self.force):
                ch, sd, au = list(ch_files), list(sd_files), list(au_files)
                job = _ScriptJob(
                    kind='split_audio',
                    label=f'{date} {band} AUDIO',
                    priority=10,
                )
                jobs.append(job)
                python_fns[job.job_id] = lambda: self._process_perf_audio(perf, ch, sd, au)
                cat_map[job.job_id] = JobCategory.CPU_BOUND
                audio_ids.append(job.job_id)
            elif sd_files or au_files:
                # ZIP already exists but hardware source files remain (sd/au WAVs need
                # a probe to know they're single-channel, so can't be swept pre-manifest).
                _hw_files = list(sd_files) + list(au_files)
                job = _ScriptJob(
                    kind='_archive_audio',
                    label=f'{date} {band} ARCHIVE AUDIO',
                    priority=80,
                )
                jobs.append(job)
                python_fns[job.job_id] = lambda hw=_hw_files: \
                    self._archive_audio_batch(hw)
                cat_map[job.job_id] = JobCategory.SCHEDULED

        # --- 3. Export clips (GPU_BOUND, priority=30, depends on encode) ---
        if encode_ids:
            # Clips are "done" only when every mov already has a clips dir with
            # outputs. Do NOT filter movs by whether their quads exist yet: on a
            # combined rebuild the quads are absent at build time, which would make
            # this generator empty and `all([])` vacuously True — silently skipping
            # clips. Ordering is handled by depends=encode_ids, not by this check.
            _clips_done = bool(mov_list) and all(
                (self.clips_dest / mov.stem).is_dir()
                and any((self.clips_dest / mov.stem).glob('*.mp4'))
                for mov in mov_list
            )
            if not (_clips_done and not self.force):
                job = _ScriptJob(
                    kind='export_clips',
                    label=f'{date} {band} CLIPS',
                    priority=30,
                    depends=encode_ids,
                )
                jobs.append(job)
                _clip_bases = [m.stem for m in mov_list]  # actual stems, not perf string
                def _clips_fn(bases=_clip_bases) -> None:
                    for b in bases:
                        if self._pause_state in (
                            PauseState.SOFT_PENDING, PauseState.HARD_PENDING
                        ):
                            break
                        self._export_clips(b)
                python_fns[job.job_id] = _clips_fn
                cat_map[job.job_id] = JobCategory.GPU_BOUND

        # --- 4. Sync quads to SharePoint (SCHEDULED, priority=40, depends on encode) ---
        if encode_ids:
            job = _ScriptJob(
                kind='_sync_quads',
                label=f'{date} {band} SYNC QUADS',
                priority=40,
                depends=encode_ids,
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda p=perf: self._sync_perf_quads(p)
            cat_map[job.job_id] = JobCategory.SCHEDULED

        # --- 5. Sync audio ZIP to SharePoint (SCHEDULED, priority=40, depends on audio) ---
        if audio_ids:
            job = _ScriptJob(
                kind='_sync_audio',
                label=f'{date} {band} SYNC AUDIO',
                priority=40,
                depends=audio_ids,
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda p=perf: self._sync_perf_audio(p)
            cat_map[job.job_id] = JobCategory.SCHEDULED

        # --- 6. Remaster (MANUAL, priority=50, depends on encode + audio) ---
        remaster_deps = encode_ids + audio_ids
        master_id: str | None = None
        _fullset = self.audio_dest / f'{perf}_AUDIO.mp3'  # also needed by step 7
        if remaster_deps:
            if not (_fullset.exists() and not self.force):
                job = _ScriptJob(
                    kind='_remaster',
                    label=f'{date} {band} REMASTER',
                    priority=50,
                    depends=remaster_deps,
                )
                jobs.append(job)
                python_fns[job.job_id] = lambda p=perf: self._do_remaster_for_perf(p)
                cat_map[job.job_id] = JobCategory.MANUAL
                master_id = job.job_id

        # --- 7. Generate reel (MANUAL, priority=60, depends on remaster) ---
        # MANUAL so it runs back-to-back with REMASTER in the same worker,
        # completing the band fully before the next band's REMASTER starts.
        # Matches the _enqueue_remaster() path which also uses MANUAL.
        #
        # Also handles the MJPEG late-MOV case: when AUDIO MP3 already exists but
        # quads are still pending (encode_ids non-empty), REEL depends on
        # encode_ids so it runs after REENCODE rather than being dropped silently.
        reel_id: str | None = None
        _fullset_ready = master_id is not None or _fullset.exists()
        _reel_exists = (
            any(self.vids_dest.glob(f'{perf}*_INSTAGRAM.mp4')) and not self.force
        )
        if _fullset_ready and not _reel_exists:
            reel_deps = [master_id] if master_id is not None else encode_ids
            job = _ScriptJob(
                kind='generate_reel',
                label=f'{date} {band} REEL',
                priority=60,
                depends=reel_deps,
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda p=perf: self._do_reel_for_perf(p)
            cat_map[job.job_id] = JobCategory.MANUAL
            reel_id = job.job_id

        # --- 8. Sync reel to SharePoint (SCHEDULED, priority=70, depends on reel) ---
        if reel_id is not None:
            job = _ScriptJob(
                kind='_sync_reel',
                label=f'{date} {band} SYNC REEL',
                priority=70,
                depends=[reel_id],
            )
            jobs.append(job)
            python_fns[job.job_id] = lambda p=perf: self._sync_perf_reel(p)
            cat_map[job.job_id] = JobCategory.SCHEDULED

        manifest = JobManifest(performance_key=perf, jobs=jobs, python_fns=python_fns)
        return manifest, cat_map

    def _build_auto_cleanup_manifest(self) -> 'tuple[JobManifest, dict[str, JobCategory]]':
        """Build an 11-job cleanup manifest: one job per audit check, one for deletes."""
        from nofun.job_manifest import JobManifest
        from nofun.job_manifest import PipelineJob as _ScriptJob

        # gpu=True: step calls _export_clips via _apply_findings, so it must run on
        # the gpu-worker to serialise with REENCODE jobs; scheduled-worker bypass
        # caused concurrent h264_amf processes (observed May 2026).
        steps = [
            ('_cleanup_temps',   'CLEANUP: orphaned temps',        self._check_orphaned_temps,          False),
            ('_cleanup_movs',    'CLEANUP: redundant MOV sources',  self._check_redundant_mov_sources,   False),
            ('_cleanup_wavs',    'CLEANUP: redundant WAV sources',  self._check_redundant_wav_sources,   False),
            ('_cleanup_chanwav', 'CLEANUP: orphaned channel WAVs',  self._check_orphaned_channel_wavs,   False),
            ('_cleanup_hwwav',   'CLEANUP: orphaned hw WAVs',       self._check_orphaned_hardware_wavs,  False),
            ('_cleanup_clips',   'CLEANUP: missing clips',          self._check_missing_clips,           True),
            ('_cleanup_clipdir', 'CLEANUP: orphaned clip dirs',     self._check_orphaned_clip_dirs,      False),
            ('_cleanup_dups',    'CLEANUP: archive duplicates',     self._check_archive_duplicates,      False),
            ('_cleanup_cloud',   'CLEANUP: expired cloud shares',   self._check_expired_cloud_shares,    False),
            ('_cleanup_rawmov',  'CLEANUP: expired raw MOVs',       self._check_expired_raw_movs,        False),
            ('_cleanup_rawwav',  'CLEANUP: expired raw WAVs',       self._check_expired_raw_wavs,        False),
        ]

        jobs: list[_ScriptJob]        = []
        python_fns: dict              = {}
        cat_map: dict[str, JobCategory] = {}
        prev_id: 'str | None'         = None

        for kind, label, check_fn, gpu in steps:
            job = _ScriptJob(
                kind=kind, label=label, priority=80,
                depends=[prev_id] if prev_id else [],
            )
            python_fns[job.job_id] = lambda fn=check_fn: self._apply_findings(fn())
            if gpu:
                cat_map[job.job_id] = JobCategory.GPU_BOUND
            jobs.append(job)
            prev_id = job.job_id

        final = _ScriptJob(
            kind='_cleanup_execute', label='CLEANUP: apply deletes',
            priority=80,
            depends=[prev_id] if prev_id else [],
        )
        python_fns[final.job_id] = lambda: self.delete_queue.execute(self.logger, self._pipeline_moved)
        jobs.append(final)

        return JobManifest(
            performance_key='_scheduled__auto_cleanup',
            jobs=jobs,
            python_fns=python_fns,
        ), cat_map

    def _maybe_enqueue_scheduled_tasks(self) -> None:
        """Enqueue housekeeping jobs at most once per interval (deduped by label + timestamp).

        Intervals:
          SYNC PERFORMANCES   — 900s   (15 min)
          EXPIRE CLOUD SHARES — 3600s  (1 hr)
          EXPIRE RAW FILES    — 3600s  (1 hr)
          DEHYDRATE SWEEP     — 14400s (4 hr)
          FINISH INCOMPLETE   — 3600s  (1 hr)
          BACKUP MIRROR       — 3600s  (1 hr) — N: raw .mov/.zip → local D: (180-day backup)
          EXPIRE D BACKUP     — 86400s (24 hr) — delete D: raws older than the 180-day window
          AUTO SCAN           — 3600s  (1 hr)
          AUTO CLEANUP        — 21600s (6 hr) — multi-job manifest, deduped by manifest key
        """
        from nofun.job_manifest import JobManifest, PipelineJob as _ScriptJob

        active_labels = {qj.job.label    for qj in self._job_queue.all_active()}
        active_keys   = {qj.manifest_key for qj in self._job_queue.all_active()}
        now = time.time()

        tasks = [
            ('SYNC PERFORMANCES',   '_sync',        self._sync_eligible_performances,  900.0),
            ('EXPIRE CLOUD SHARES', '_expire',       self._auto_expire_cloud_shares,   3600.0),
            ('EXPIRE RAW FILES',    '_expire_raw',   self._auto_expire_raw_files,      3600.0),
            ('DEHYDRATE SWEEP',     '_dehydrate',    self._dehydration_sweep,         14400.0),
            ('FINISH INCOMPLETE',   '_finish',       self._finish_incomplete_shows,    3600.0),
            ('BACKUP MIRROR',       '_backup_mirror', self._mirror_raws_to_backup,       3600.0),
            ('EXPIRE D BACKUP',     '_expire_d_raw', self._expire_d_backup_raws,       86400.0),
        ]
        for label, kind, fn, interval in tasks:
            last = self._last_scheduled_enqueued.get(label, 0.0)
            if label not in active_labels and now - last >= interval:
                job = _ScriptJob(kind=kind, label=label, priority=80)
                m = JobManifest(
                    performance_key=f'_scheduled_{kind}',
                    jobs=[job],
                    python_fns={job.job_id: fn},
                )
                self._job_queue.enqueue(m, JobCategory.SCHEDULED)
                self._last_scheduled_enqueued[label] = now

        # AUTO CLEANUP: multi-job manifest — dedup by manifest_key, not label
        _CLEANUP_KEY = '_scheduled__auto_cleanup'
        last_cleanup = self._last_scheduled_enqueued.get('AUTO CLEANUP', 0.0)
        if _CLEANUP_KEY not in active_keys and now - last_cleanup >= 21600.0:
            manifest, cat_map = self._build_auto_cleanup_manifest()
            self._job_queue.enqueue(manifest, JobCategory.SCHEDULED, category_map=cat_map)
            self._last_scheduled_enqueued['AUTO CLEANUP'] = now

        scan_label = 'AUTO SCAN'
        if (now - self._last_scan_enqueued >= 3600.0
                and scan_label not in active_labels):
            j = _ScriptJob(kind='_scan', label=scan_label, priority=100)
            m = JobManifest(
                performance_key='_scheduled_scan',
                jobs=[j],
                python_fns={j.job_id: lambda: self._run_scan('SCAN')},
            )
            self._job_queue.enqueue(m, JobCategory.SCHEDULED)
            self._last_scan_enqueued = now

    # -----------------------------------------------------------------------
    # TUI watchdog loop (called by MediaEngineApp worker thread)
    # -----------------------------------------------------------------------

    def run_with_queue(self, cmd_queue: 'queue.Queue[str]', app=None) -> None:
        """Watchdog loop for TUI mode — input comes from cmd_queue.

        Same logic as run() but:
          - TUI startup/teardown handled by MediaEngineApp (not here)
          - Status updates posted to app.update_status()
          - Progress callbacks passed through run_ffmpeg() via self._app
        """
        import atexit as _atexit
        _atexit.register(self._cleanup)

        # Register OS-level signals so closing the terminal window stops streams
        # cleanly before TouchDesigner sees a broken connection.
        # signal.signal() must be called from the main thread; run_with_queue is
        # called from a Textual worker thread, so we skip signal registration here
        # and rely on MediaEngineApp.on_unmount() instead (covers all TUI exit paths).
        # The SIGHUP / CTRL_CLOSE_EVENT handlers below are for batch / non-TUI mode.

        self._app       = app
        self._cmd_queue = cmd_queue
        self._start_workers()
        if app:
            app.update_status('Starting up…')
            app.update_command_bar(_HOME_COMMANDS)
            pass  # smoke tests run on-demand via TEST command only
        self._show_startup_summary()
        # Populate home header immediately from cached DB summary — no scan needed
        if app:
            summary = self._encoding_db.get_summary()
            if summary:
                ts = summary.get('updated', '')
                app.update_inventory_stats(
                    summary.get('type_counts', {}),
                    ts,
                    summary.get('perf_count', -1),
                    total_runtime_seconds=summary.get('total_runtime_seconds', 0.0),
                )
        override_time = False
        self._override_time = False

        while True:
            # Re-check NAS reachability and re-point media_root before anything
            # globs sources or builds manifests this tick — so a mid-run NAS drop
            # routes new outputs to D: (and a return routes them back to N:).
            self._reconcile_media_root()

            mov_files = sorted(self.search_dir.glob('*.mov'))
            wav_files = sorted(self.search_dir.glob('*.wav'))

            self._maybe_create_sharepoint_placeholder(mov_files)

            # Detect presence against the raw glob — a file held open by ffmpeg
            # (mid-encode) shows up in glob but fails _is_file_stable(); detecting
            # against the filtered list would fire a false REMOVED every encode.
            self._detect_file_events(mov_files, wav_files)

            # Skip files whose size has changed since the last loop — still
            # being written.  _is_file_stable() tracks size across iterations;
            # a file must be unchanged for _STABLE_SECS (30 s) before processing.
            mov_files = [f for f in mov_files if self._is_file_stable(f)]
            wav_files = [f for f in wav_files if self._is_file_stable(f)]

            can_process = True
            # Sync: _flush_commands may have updated _override_time mid-operation
            override_time = self._override_time
            hour = datetime.datetime.now().hour
            if hour == 0:
                self._reset_noproblem_flags_for_midnight()
                override_time = False
            if not override_time and not self._job_queue.is_within_schedule(JobCategory.GPU_BOUND):
                can_process = False
            # Suppress processing while any interactive menu or pause is active
            if self._active_menu != MenuMode.NONE or self._pause_state == PauseState.PAUSED:
                can_process = False

            # Name why the build block is skipped while raw inputs wait, so a
            # silent gate (menu open, pause, off-window) is visible in the log.
            if not can_process and (mov_files or wav_files):
                if self._pause_state == PauseState.PAUSED:
                    _block = 'PAUSED'
                elif self._active_menu != MenuMode.NONE:
                    _block = f'menu open ({self._active_menu.name})'
                else:
                    _block = 'outside GPU schedule window'
                _reason = f'{_block}: {len(mov_files)} mov + {len(wav_files)} wav waiting'
                if _reason != self._last_gate_block:
                    self.logger.info(f'PROCESS GATE holding — {_reason}')
                    self._last_gate_block = _reason
            elif can_process and self._last_gate_block:
                self._last_gate_block = ''

            _in_menu = (self._active_menu != MenuMode.NONE)
            if app and not _in_menu:
                self._cached_total_eta = self._estimate_total_eta()
                app.update_status(self._format_status(mov_files, wav_files, can_process))

            if can_process:
                if not self.skip_audio:
                    # Multi-ch raw WAVs: split upfront (produces _ch??.wav files)
                    if wav_files:
                        self._split_multichannel_wavs(wav_files)

                # Build per-performance maps (oldest-first processing order)
                perf_ch: dict[str, list[pathlib.Path]] = defaultdict(list)
                perf_sd: dict[str, list[pathlib.Path]] = {}
                perf_au: dict[str, list[pathlib.Path]] = {}
                perf_mov: dict[str, list[pathlib.Path]] = defaultdict(list)
                perf_singles: dict[str, list[pathlib.Path]] = defaultdict(list)

                if not self.skip_audio:
                    _dq = {item[0] for item in self.delete_queue.items}
                    for f in sorted(self.search_dir.glob('*.wav')):
                        if not self._CH_WAV.search(f.name) or f in _dq:
                            continue
                        _ds, _bd = extract_date_band(f.stem)
                        if _ds != 'TBD' and (self.audio_dest / f'{_ds}_{_bd}.zip').exists():
                            self.delete_queue.add(f, "channel WAV already zipped", self.logger, tui=False)
                        else:
                            perf_ch[self._perf_key(f)].append(f)
                    perf_sd = self._collect_chan_candidates(self.search_dir, exclude_split=True)
                    perf_au = self._collect_chan_candidates(self.search_dir / 'Audio')
                    self._drain_all_silent_perfs(perf_sd, perf_au)

                for mov in mov_files:
                    perf_mov[self._perf_key(mov)].append(mov)

                _singles_dir = self.search_dir / 'Singles'
                if _singles_dir.is_dir():
                    for mov in sorted(_singles_dir.glob('*.mov')):
                        perf_singles[self._perf_key(mov)].append(mov)

                # Content-route main-path movs: a 1×1 source detected here is
                # diverted to single-transcode instead of being quad-split into
                # four garbage cameras.  Singles/ folder entries are untouched.
                self._route_movs_by_layout(perf_mov, perf_singles)

                all_perfs = sorted(
                    set(list(perf_ch) + list(perf_sd) + list(perf_au)
                        + list(perf_mov) + list(perf_singles))
                )

                # Build manifests for all performances and enqueue them.
                # _enqueued_keys deduplication prevents re-enqueueing the same
                # perf each 15-second loop while workers are still dispatching.
                for perf in all_perfs:
                    ch_files = perf_ch.get(perf, []) if not self.skip_audio else []
                    sd_files = perf_sd.get(perf, []) if not self.skip_audio else []
                    au_files = perf_au.get(perf, []) if not self.skip_audio else []
                    mov_list = perf_mov.get(perf, [])
                    _singles  = perf_singles.get(perf, [])
                    if not (ch_files or sd_files or au_files or mov_list or _singles):
                        continue
                    with self._enqueued_keys_lock:
                        if perf in self._enqueued_keys:
                            if any(qj.manifest_key == perf
                                   for qj in self._job_queue.all_active()):
                                self.logger.debug(
                                    f'{perf}: skip rebuild — manifest still active')
                                continue
                            self._enqueued_keys.discard(perf)
                    manifest, cat_map = self._build_full_manifest(
                        perf, mov_list, ch_files, sd_files, au_files,
                        singles_list=_singles,
                    )
                    if manifest.jobs:
                        self._job_queue.enqueue(
                            manifest, JobCategory.MANUAL, category_map=cat_map
                        )
                        with self._enqueued_keys_lock:
                            self._enqueued_keys.add(perf)
                    elif mov_list or _singles:
                        # A raw mov was present but produced no jobs — every
                        # lifecycle step decided it had nothing to do.  Name it
                        # so a stuck perf isn't silently dropped each loop.
                        self.logger.info(
                            f'{perf}: 0 jobs built despite '
                            f'{len(mov_list)} mov + {len(_singles)} single(s) '
                            f'present — all lifecycle steps skipped')

            if self.delete_queue.items:
                self.delete_queue.execute(self.logger, self._pipeline_moved)
            if can_process:
                if self._pause_state == PauseState.RUNNING:
                    self._maybe_refresh_inventory()

            # SharePoint sync and cloud expiry are dispatched as SCHEDULED jobs
            # so they appear in the JOBS menu and don't block the watchdog loop.
            if self._pause_state != PauseState.PAUSED:
                self._maybe_enqueue_scheduled_tasks()

            # Update status bar queue info and inventory panel health indicator.
            if app:
                from scripts import SCRIPT_REGISTRY as _REG
                s = self._job_queue.summary()
                q_parts: list[str] = []
                if s['running']:
                    q_parts.append(f"{s['running']} running")
                if s['pending']:
                    q_parts.append(f"{s['pending']} pending")
                nxt = self._job_queue.next_runnable()
                if nxt:
                    nxt_lbl = _REG.get(nxt.job.kind, {}).get('label', nxt.job.kind)
                    q_parts.append(f"next: {nxt_lbl}")
                app.update_queue_info(' · '.join(q_parts) if q_parts else '')
                if s['running'] or s['pending']:
                    app.update_queue_health(
                        f"⚙ {s['running']} running · {s['pending']} queued"
                    )
                else:
                    app.update_queue_health('')

            # Surface what shows are being streamed (only when the in-built
            # StreamServer is running; the external start-streams script is
            # invisible to the engine and stays blank).
            if app:
                if self._stream_server and self._stream_server.running:
                    try:
                        statuses = self._stream_server.status()
                    except Exception:
                        statuses = []
                    seen: set[str] = set()
                    bands: list[str] = []
                    for st in statuses:
                        clip_name = st.get('clip', '')
                        if not clip_name or clip_name == '(none)':
                            continue
                        stem = clip_name.rsplit('.', 1)[0]
                        _date, band = extract_date_band(stem)
                        if band and band != 'TBD' and band not in seen:
                            seen.add(band)
                            bands.append(band)
                    app.update_streams_text(' · '.join(bands))
                else:
                    app.update_streams_text('')

            # Transition to paused state after a soft or hard stop.
            # SOFT_PENDING waits until workers finish their current job before
            # declaring PAUSED — prevents a premature "PAUSED" message while an
            # encode is still in progress.
            match self._pause_state:
                case PauseState.HARD_PENDING:
                    self._pause_state = PauseState.PAUSED
                    self.logger.info("PAUSE   Hard stop complete — type RESUME to continue")
                    if self._app:
                        self._app.update_command_bar(
                            "HARD PAUSED  —  RESUME to continue"
                        )
                case PauseState.SOFT_PENDING:
                    _running = [qj for qj in self._job_queue.all_active()
                                if qj.status == 'running']
                    if not _running:
                        self._pause_state = PauseState.PAUSED
                        self.logger.info("PAUSE   Paused at safe point — type RESUME to continue")
                        if self._app:
                            self._app.update_command_bar(
                                "PAUSED  —  type RESUME to continue"
                            )

            try:
                cmd = cmd_queue.get(timeout=15.0)
                if cmd:
                    override_time = self._handle_command(cmd, override_time)
                    self._override_time = override_time
            except queue.Empty:
                pass

        self.logger.info("Pipeline stopped")  # noqa: unreachable (loop exits via sys.exit)

    # -----------------------------------------------------------------------
    # Runtime media-root fallback (NAS↔D:)
    # -----------------------------------------------------------------------

    def _set_media_root(self, root: pathlib.Path) -> None:
        """Point media_root and its four derived dests at ``root`` atomically.

        Every NAS↔D: re-point goes through here so the five attributes can never
        drift out of sync. ``clips_dest`` is deliberately NOT touched — clips live
        on the C: SSD and never follow the NAS.
        """
        self.media_root    = root
        self.vids_dest     = root / 'videos'
        self.audio_dest    = root / 'audio'
        self.video_archive = root / 'video_archive'
        self.audio_archive = root / 'audio_archive'
        for d in (self.vids_dest, self.audio_dest,
                  self.video_archive, self.audio_archive):
            d.mkdir(parents=True, exist_ok=True)

    def _reconcile_media_root(self) -> None:
        """Re-probe NAS reachability each loop tick; re-point media_root on change.

        Debounced: only flip after _NAS_FLIP_TICKS consecutive agreeing probes so
        a flapping link can't thrash the dest paths. No-op for trial runs or when
        NAS_ROOT is unset (no NAS configured — already on local D:).
        """
        nas = os.environ.get('NAS_ROOT')
        if not nas or self.trial_run:
            return
        nas_root = pathlib.Path(nas)
        up = nas_reachable(nas_root)
        on_nas = (self.media_root != self.mount_d)
        if on_nas and not up:                       # NAS just went away
            self._nas_miss += 1
            self._nas_hit = 0
            if self._nas_miss >= self._NAS_FLIP_TICKS:
                self.logger.warning(
                    f'NAS unreachable — FALLING BACK to {self.mount_d}')
                self._set_media_root(self.mount_d)
                self._nas_miss = 0
        elif (not on_nas) and up:                   # NAS came back
            self._nas_hit += 1
            self._nas_miss = 0
            if self._nas_hit >= self._NAS_FLIP_TICKS:
                self.logger.warning(
                    f'NAS back — RESUMING {nas_root}; reconciling D:→N:')
                self._set_media_root(nas_root)
                self._nas_hit = 0
                self._enqueue_failback_reconcile(nas_root)
        else:                                       # steady state — reset counters
            self._nas_miss = 0
            self._nas_hit = 0

    def _enqueue_failback_reconcile(self, nas_root: pathlib.Path) -> None:
        """Catch up deliverables written to D: during a NAS outage, back up to N:.

        Inverse of _mirror_raws_to_backup: copy-only D:→N: so anything produced
        while the NAS was down lands on the primary, while N:-only files survive
        untouched. Deliverables only (.mp4/.mp3) — the heavy raw tiers written to
        D: during the outage are reconciled separately. Runs on the scheduled
        worker so it never blocks the main loop.
        """
        pairs = [
            (self.mount_d / 'videos',        nas_root / 'videos'),
            (self.mount_d / 'audio',         nas_root / 'audio'),
            (self.mount_d / 'video_archive', nas_root / 'video_archive'),
            (self.mount_d / 'audio_archive', nas_root / 'audio_archive'),
        ]

        def _reconcile() -> None:
            copied, skipped = mirror_files(pairs, DELIVERABLE_EXTS)
            self.logger.info(
                f'FAILBACK RECONCILE  {copied} deliverable(s) D:→N: '
                f'({skipped} already current)')

        threading.Thread(target=_reconcile, daemon=True,
                         name='failback-reconcile').start()

    # -----------------------------------------------------------------------
    # Main run loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        atexit.register(self._cleanup)
        self._show_startup_summary()
        def _exit_handler(*_: object) -> None:
            self._cleanup()
            sys.exit(0)

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT,  _exit_handler)
            signal.signal(signal.SIGTERM, _exit_handler)
            if hasattr(signal, 'SIGHUP'):                       # Unix: terminal window closed
                signal.signal(signal.SIGHUP, _exit_handler)    # type: ignore[attr-defined]
            if hasattr(signal, 'CTRL_CLOSE_EVENT'):             # Windows: X button on terminal
                signal.signal(signal.CTRL_CLOSE_EVENT, _exit_handler)  # type: ignore[attr-defined]

        # Cleanup-only mode
        if self.cleanup_only:
            self._cleanup_scan()
            self.delete_queue.execute(self.logger, self._pipeline_moved)
            self.logger.info("Pipeline stopped")
            return

        self._start_workers()
        if self.directory:
            self._noproblem_active = True   # batch mode: bypass time gate
        override_time = False

        while True:
            # Re-check NAS reachability and re-point media_root before anything
            # globs sources or builds manifests this tick — so a mid-run NAS drop
            # routes new outputs to D: (and a return routes them back to N:).
            self._reconcile_media_root()

            mov_files = sorted(self.search_dir.glob('*.mov'))
            wav_files = sorted(self.search_dir.glob('*.wav'))

            self._maybe_create_sharepoint_placeholder(mov_files)

            # Detect presence against the raw glob; see watchdog-mode comment above.
            self._detect_file_events(mov_files, wav_files)

            # Skip files still being written (watchdog mode only — size must be
            # unchanged for _STABLE_SECS before processing).
            # In batch mode (-d) the user explicitly points to a ready directory.
            if not self.directory:
                mov_files = [f for f in mov_files if self._is_file_stable(f)]
                wav_files = [f for f in wav_files if self._is_file_stable(f)]

            # Batch-mode exit condition
            if (self.directory or self.exit_on_complete) \
                    and not mov_files and not wav_files:
                self.logger.info("No files found — exiting")
                break

            # Time gate (watchdog mode only)
            can_process = True
            if not self.directory:
                hour = datetime.datetime.now().hour
                if hour == 0:
                    override_time = False
                    self._noproblem_active = False
                if hour >= 16 and not override_time:
                    can_process = False

            if can_process:
                if not self.skip_audio:
                    # Multi-ch raw WAVs: split upfront (produces _ch??.wav files)
                    if wav_files:
                        self._split_multichannel_wavs(wav_files)

                # Build per-performance maps (oldest-first processing order)
                perf_ch: dict[str, list[pathlib.Path]] = defaultdict(list)
                perf_sd: dict[str, list[pathlib.Path]] = {}
                perf_au: dict[str, list[pathlib.Path]] = {}
                perf_mov: dict[str, list[pathlib.Path]] = defaultdict(list)
                perf_singles: dict[str, list[pathlib.Path]] = defaultdict(list)

                if not self.skip_audio:
                    _dq = {item[0] for item in self.delete_queue.items}
                    for f in sorted(self.search_dir.glob('*.wav')):
                        if not self._CH_WAV.search(f.name) or f in _dq:
                            continue
                        _ds, _bd = extract_date_band(f.stem)
                        if _ds != 'TBD' and (self.audio_dest / f'{_ds}_{_bd}.zip').exists():
                            self.delete_queue.add(f, "channel WAV already zipped", self.logger, tui=False)
                        else:
                            perf_ch[self._perf_key(f)].append(f)
                    perf_sd = self._collect_chan_candidates(self.search_dir, exclude_split=True)
                    perf_au = self._collect_chan_candidates(self.search_dir / 'Audio')
                    self._drain_all_silent_perfs(perf_sd, perf_au)

                for mov in mov_files:
                    perf_mov[self._perf_key(mov)].append(mov)

                _singles_dir = self.search_dir / 'Singles'
                if _singles_dir.is_dir():
                    for mov in sorted(_singles_dir.glob('*.mov')):
                        perf_singles[self._perf_key(mov)].append(mov)

                # Content-route main-path movs: a 1×1 source detected here is
                # diverted to single-transcode instead of being quad-split into
                # four garbage cameras.  Singles/ folder entries are untouched.
                self._route_movs_by_layout(perf_mov, perf_singles)

                all_perfs = sorted(
                    set(list(perf_ch) + list(perf_sd) + list(perf_au)
                        + list(perf_mov) + list(perf_singles))
                )

                for perf in all_perfs:
                    ch_files = perf_ch.get(perf, []) if not self.skip_audio else []
                    sd_files = perf_sd.get(perf, []) if not self.skip_audio else []
                    au_files = perf_au.get(perf, []) if not self.skip_audio else []
                    _singles  = perf_singles.get(perf, [])
                    # In batch mode, run the interactive rename prompt before processing
                    raw_movs = perf_mov.get(perf, [])
                    mov_list = [self._prompt_rename_nofun(m) for m in raw_movs]

                    if not (ch_files or sd_files or au_files or mov_list or _singles):
                        continue
                    with self._enqueued_keys_lock:
                        if perf in self._enqueued_keys:
                            if any(qj.manifest_key == perf
                                   for qj in self._job_queue.all_active()):
                                continue
                            self._enqueued_keys.discard(perf)
                    manifest, cat_map = self._build_full_manifest(
                        perf, mov_list, ch_files, sd_files, au_files,
                        singles_list=_singles,
                    )
                    if manifest.jobs:
                        self._job_queue.enqueue(
                            manifest, JobCategory.MANUAL, category_map=cat_map
                        )
                        with self._enqueued_keys_lock:
                            self._enqueued_keys.add(perf)

                if self.directory:
                    # In batch mode, run SharePoint sync inline rather than as a
                    # scheduled queue job — avoids blocking the drain on inventory
                    # scans and cloud-expiry sweeps.
                    self._sync_eligible_performances()
                else:
                    self._maybe_enqueue_scheduled_tasks()
                self.delete_queue.execute(self.logger, self._pipeline_moved)

                if self.directory:
                    # Drain all enqueued work before declaring batch complete
                    self._job_queue.wait_drain()
                    if self.trial_run:
                        self._print_trial_summary()
                    self.logger.info("Batch complete")
                    break

                if self.exit_on_complete:
                    if (self._job_queue.pending_count() == 0
                            and self._job_queue.running_job() is None):
                        self.logger.info("Nothing processed — exiting")
                        break

            # Batch mode: wait before re-scanning for new files
            time.sleep(60)

        self.logger.info("Pipeline stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option('-d', '--directory',        type=click.Path(), default=None,
              help='Source directory (batch mode: process once then exit).')
@click.option('-t', '--trial-run',        type=int,          default=0, metavar='N',
              help='Encode only N seconds (trial mode).')
@click.option('-e', '--exit-on-complete', is_flag=True,
              help='Exit when no files remain (watchdog mode).')
@click.option('-s', '--skip-audio',       is_flag=True,
              help='Skip WAV splitting and ZIP archiving.')
@click.option('-f', '--force',            is_flag=True,
              help='Overwrite existing outputs.')
@click.option('--no-gpu',                 is_flag=True,
              help='Use CPU encoder (libx265) instead of GPU.')
@click.option('--cleanup',   'cleanup_only', is_flag=True,
              help='Scan for redundant source files and queue them for deletion.')
def main(directory: str | None, trial_run: int, exit_on_complete: bool,
         skip_audio: bool, force: bool, no_gpu: bool, cleanup_only: bool) -> None:
    """Concert recording pipeline: quadrants → clips → audio ZIPs."""
    # Lock prevents duplicate watchdog instances; batch mode (-d) is fine to run in parallel.
    if directory is None:
        _acquire_lock()
    pipeline = Pipeline(
        directory        = pathlib.Path(directory) if directory else None,
        trial_run        = trial_run,
        exit_on_complete = exit_on_complete,
        skip_audio       = skip_audio,
        force            = force,
        gpu              = not no_gpu,
        cleanup_only     = cleanup_only,
    )
    if directory is None and not cleanup_only:
        from nofun.tui import MediaEngineApp
        MediaEngineApp(pipeline).run()
    else:
        pipeline.run()


if __name__ == '__main__':
    main()
