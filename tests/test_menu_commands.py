"""Unit tests for _handle_command() routing and interactive menu entry/exit.

Tests STATUS menu routing and pipeline auto-execution
without running ffmpeg or touching the D: drive.
"""

import datetime
import logging
import pathlib
import queue
import threading
import time as _time
import unittest.mock
from unittest.mock import MagicMock

import pytest

from nofun.media_io import DeleteQueue
from nofun.state import MenuMode, PauseState
from media_engine import Pipeline, _HOME_COMMANDS


# ---------------------------------------------------------------------------
# Shared lightweight Pipeline stub
# ---------------------------------------------------------------------------

class _FakePipeline(Pipeline):
    """Pipeline subclass with a minimal __init__ for unit testing.

    Bypasses full Pipeline initialisation (path detection, logging setup,
    encoder config) and wires in a tmp_path-based layout instead.
    """

    def __init__(self, tmp_path: pathlib.Path) -> None:
        # Skip super().__init__() — set every attribute Pipeline uses directly
        self.directory        = tmp_path
        self.trial_run        = 0
        self.force            = False
        self.exit_on_complete = False
        self.skip_audio       = False
        self.gpu              = False
        self.cleanup_only     = False

        self.search_dir      = tmp_path
        self.vids_dest       = tmp_path / 'videos'
        self.clips_dest      = tmp_path / 'clips'
        self.audio_dest      = tmp_path / 'audio'
        self.video_archive   = tmp_path / 'video_archive'
        self.audio_archive   = tmp_path / 'audio_archive'
        self.sharepoint_dest = None
        self.script_dir      = tmp_path
        self.mount_d         = tmp_path
        self.inventory_summary = tmp_path / 'inventory_summary.txt'

        from nofun.encoding_db import EncodingDB
        self._encoding_db = EncodingDB(tmp_path / 'encoding_db.json')

        self.logger       = MagicMock(spec=logging.Logger)
        self.delete_queue = DeleteQueue()
        self.enc          = {'accel': [], 'enc_quad': [], 'enc_clip': []}

        self._known_files:    dict = {}
        self._file_sizes:     dict = {}
        self._pipeline_moved: queue.Queue[str] = queue.Queue()
        self._streams_active       = False
        self._stream_procs:   list = []
        self._app                  = None

        self._HOME_COMMANDS        = _HOME_COMMANDS
        self._active_menu          = MenuMode.NONE
        self._cleanup_findings:list = []
        self._status_entries: list = []
        self._show_groups:    list = []
        self._status_expanded_key  = None
        self._rename_state         = None
        self._rename_date          = None
        self._rename_band          = None
        self._rename_new_name      = None
        self._rename_thread        = None
        self._disk_c               = ''
        self._disk_d               = ''
        self._disk_sp              = ''
        self._stream_states:  list = []

        self._pause_state             = PauseState.RUNNING
        self._current_ffmpeg_procs:  dict = {}
        self._ffmpeg_procs_lock      = threading.Lock()
        self._cmd_queue              = None
        self._current_operation    = ''
        self._noproblem_active        = False
        self._override_time           = False
        from media_engine import _HelpState
        self._help: dict = {
            'home':      _HelpState(),
            'inventory': _HelpState(),
            'streams':   _HelpState(),
            'jobs':      _HelpState(),
        }
        self._jobs_selected_idx       = None
        self._manual_worker_running   = False
        self._manual_worker_thread    = None
        self._reprocess_candidates:   list = []
        self._reprocess_archived:     dict = {}
        self._enqueued_keys:          set             = set()
        self._enqueued_keys_lock:     threading.Lock  = threading.Lock()
        self._auto_remastered:        set  = set()
        self._sp_placeholder_done:    set  = set()
        self._reencode_fail_counts:   dict = {}
        self._reencode_parked:        set  = set()
        self._last_scheduled_enqueued: dict = {}
        self._last_scan_enqueued:      float = 0.0
        self._stream_server               = None

        from nofun.script_runner import ScriptRunner
        from nofun.job_queue import JobQueue
        _runner = MagicMock(spec=ScriptRunner)
        self._job_queue = JobQueue(_runner, self.logger)

        for d in (self.vids_dest, self.clips_dest, self.audio_dest,
                  self.video_archive, self.audio_archive):
            d.mkdir(parents=True, exist_ok=True)


def _seed_db(fp: '_FakePipeline', rows: list[dict]) -> None:
    """Upsert minimal inventory rows into fp._encoding_db."""
    _CATEGORY = {
        'quadrant':     'quadrant_video',
        'clip':         'clips',
        'zipped audio': 'zipped_audio',
        'raw video':    'raw_video',
        'audio':        'source_audio',
    }
    import time
    for row in rows:
        ftype    = row.get('type', 'quadrant')
        category = _CATEGORY.get(ftype, 'quadrant_video')
        fp._encoding_db.upsert(
            row.get('date', 'TBD'),
            row.get('band', 'TBD'),
            category,
            {
                'path':     str(row.get('fullpath', '/fake/file.mp4')),
                'size':     1000,
                'mtime':    time.time(),
                'type':     ftype,
                'location': row.get('location', 'archive'),
            },
        )
    fp._encoding_db.set_inventory_scanned()
    fp._encoding_db.save()


# ---------------------------------------------------------------------------
# TestDeleteQueueAutoExecute
# ---------------------------------------------------------------------------

class TestDeleteQueueAutoExecute:
    def test_queue_execute_removes_file(self, tmp_path: pathlib.Path) -> None:
        """DeleteQueue.execute() deletes files immediately (no YESPLEASE needed)."""
        fp = _FakePipeline(tmp_path)
        f  = tmp_path / 'old_source.mp4'
        f.write_bytes(b'\x00' * 10)
        fp.delete_queue.add(f, 'source after zip')
        fp.delete_queue.execute(fp.logger)
        assert not f.exists()
        assert fp.delete_queue.items == []

    def test_unknown_command_does_not_crash(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        # Neither of these is a valid command
        fp._handle_command('YESPLEASE', False)
        fp._handle_command('CLOUDCLEAN', False)


# ---------------------------------------------------------------------------
# TestInventoryMenuRouting
# ---------------------------------------------------------------------------

class TestInventoryMenuRouting:
    def _seed_recent(self, fp: _FakePipeline) -> None:
        today    = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        _seed_db(fp, [
            {
                'date': date_str, 'band': 'TestBand',
                'type': 'quadrant', 'location': 'archive',
                'fullpath': fp.vids_dest / 'foo_UL.mp4',
            },
        ])

    def _seed_two_perfs(self, fp: _FakePipeline) -> None:
        """DB with two distinct performances."""
        _seed_db(fp, [
            {
                'date': '2024-01-01', 'band': 'BandA',
                'type': 'quadrant', 'location': 'archive',
                'fullpath': fp.vids_dest / 'a_UL.mp4',
            },
            {
                'date': '2024-01-02', 'band': 'BandB',
                'type': 'quadrant', 'location': 'archive',
                'fullpath': fp.vids_dest / 'b_UL.mp4',
            },
        ])

    def test_inventory_command_enters_menu_with_data(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_recent(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        assert fp._active_menu == MenuMode.STATUS

    def test_inventory_no_data_does_not_enter_menu(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        assert fp._active_menu == MenuMode.NONE

    def test_home_exits_inventory_menu(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._active_menu    = MenuMode.STATUS
        fp._status_entries = []
        fp._handle_command('HOME', False)
        assert fp._active_menu == MenuMode.NONE

    def test_number_expands_key(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_two_perfs(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        assert fp._active_menu == MenuMode.STATUS
        fp._handle_command('1', False)
        assert fp._status_expanded_key is not None
        assert fp._status_expanded_key == fp._show_groups[0][0]

    def test_same_number_collapses(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_two_perfs(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        fp._handle_command('1', False)
        assert fp._status_expanded_key is not None
        fp._handle_command('1', False)
        assert fp._status_expanded_key is None

    def test_different_number_switches(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_two_perfs(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        fp._handle_command('1', False)
        fp._handle_command('2', False)
        assert fp._status_expanded_key == fp._show_groups[1][0]

    def test_home_clears_expanded_key(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_two_perfs(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        fp._handle_command('1', False)
        assert fp._status_expanded_key is not None
        fp._handle_command('HOME', False)
        assert fp._active_menu == MenuMode.NONE
        assert fp._status_expanded_key is None

    def test_bigscan_calls_run_scan_async(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_recent(fp)
        fp._app = MagicMock()
        fp._override_time = True
        fp._handle_command('INVENTORY', False)
        with unittest.mock.patch.object(fp, '_run_scan_async') as mock_scan:
            fp._handle_command('BIGSCAN', False)
        mock_scan.assert_called_once_with('BIGSCAN')

    def test_bigscan_time_gated(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        self._seed_recent(fp)
        fp._app = MagicMock()
        fp._handle_command('INVENTORY', False)
        with unittest.mock.patch('datetime.datetime') as mock_dt, \
             unittest.mock.patch.object(fp, '_run_scan_async') as mock_scan:
            mock_dt.now.return_value.hour = 17  # after 4pm
            fp._handle_command('BIGSCAN', False)
        mock_scan.assert_not_called()


# ---------------------------------------------------------------------------
# TestPauseResume — PAUSE / RESUME state machine
# ---------------------------------------------------------------------------

class TestPauseResume:
    def test_first_pause_sets_soft_pending(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._handle_command('PAUSE', False)
        assert fp._pause_state == PauseState.SOFT_PENDING

    def test_second_pause_stays_soft_pending(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Second PAUSE no longer hard-kills — use JOBS > CANCEL for that.
        fp = _FakePipeline(tmp_path)
        fp._pause_state = PauseState.SOFT_PENDING
        fp._handle_command('PAUSE', False)
        assert fp._pause_state == PauseState.SOFT_PENDING

    def test_second_pause_does_not_kill_proc(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._pause_state = PauseState.SOFT_PENDING
        mock_proc = MagicMock()
        fp._current_ffmpeg_procs['encode'] = mock_proc
        fp._handle_command('PAUSE', False)
        mock_proc.kill.assert_not_called()
        assert fp._pause_state == PauseState.SOFT_PENDING

    def test_pause_when_already_paused_stays_paused(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._pause_state = PauseState.PAUSED
        fp._handle_command('PAUSE', False)
        assert fp._pause_state == PauseState.PAUSED

    def test_resume_from_paused_sets_running(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._pause_state = PauseState.PAUSED
        fp._handle_command('RESUME', False)
        assert fp._pause_state == PauseState.RUNNING

    def test_resume_from_soft_pending_sets_running(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._pause_state = PauseState.SOFT_PENDING
        fp._handle_command('RESUME', False)
        assert fp._pause_state == PauseState.RUNNING

    def test_resume_when_already_running_is_noop(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        # _pause_state starts as RUNNING
        fp._handle_command('RESUME', False)
        assert fp._pause_state == PauseState.RUNNING


# ---------------------------------------------------------------------------
# TestNoproblem — NOPROBLEM double-command
# ---------------------------------------------------------------------------

class TestNoproblem:
    def test_first_noproblem_sets_override(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._handle_command('NOPROBLEM', False)
        assert fp._noproblem_active is True
        assert fp.force is False  # force is NOT set on the first press

    def test_first_noproblem_returns_true_for_override_time(
        self, tmp_path: pathlib.Path
    ) -> None:
        fp = _FakePipeline(tmp_path)
        result = fp._handle_command('NOPROBLEM', False)
        assert result is True

    def test_second_noproblem_sets_force(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._noproblem_active = True  # simulate first press already done
        fp._handle_command('NOPROBLEM', False)
        assert fp.force is True
        assert fp._noproblem_active is True   # bypass stays active so workers keep dispatching

    def test_midnight_reset_clears_all_three_flags(self, tmp_path: pathlib.Path) -> None:
        """After NOPROBLEM x 2 + midnight, force must clear alongside the other flags."""
        fp = _FakePipeline(tmp_path)
        fp._noproblem_active = True
        fp._override_time    = True
        fp.force             = True

        fp._reset_noproblem_flags_for_midnight()

        assert fp._noproblem_active is False
        assert fp._override_time    is False
        assert fp.force             is False


# ---------------------------------------------------------------------------
# TestIsFileStableBySize — macOS/Linux size-stability file gate
# ---------------------------------------------------------------------------

class TestIsFileStableBySize:
    def test_first_observation_returns_false(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        f = tmp_path / 'recording.mov'
        f.write_bytes(b'\x00' * 1024)
        assert fp._is_file_stable_by_size(f) is False

    def test_same_size_before_timeout_returns_false(
        self, tmp_path: pathlib.Path
    ) -> None:
        fp = _FakePipeline(tmp_path)
        f = tmp_path / 'recording.mov'
        f.write_bytes(b'\x00' * 1024)
        fp._is_file_stable_by_size(f)  # seeds the entry
        assert fp._is_file_stable_by_size(f) is False  # no time elapsed

    def test_same_size_after_timeout_returns_true(
        self, tmp_path: pathlib.Path
    ) -> None:
        fp = _FakePipeline(tmp_path)
        f = tmp_path / 'recording.mov'
        f.write_bytes(b'\x00' * 1024)
        size = f.stat().st_size
        # Inject a past timestamp that satisfies the stability window
        fp._file_sizes[str(f)] = (size, _time.monotonic() - fp._STABLE_SECS - 1)
        assert fp._is_file_stable_by_size(f) is True

    def test_size_change_resets_timer(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        f = tmp_path / 'recording.mov'
        f.write_bytes(b'\x00' * 1024)
        # Inject old timestamp that would make the 1024-byte file stable
        fp._file_sizes[str(f)] = (1024, _time.monotonic() - fp._STABLE_SECS - 1)
        # Overwrite with a different size — should reset the timer
        f.write_bytes(b'\x00' * 2048)
        assert fp._is_file_stable_by_size(f) is False
        assert fp._file_sizes[str(f)][0] == 2048

    def test_missing_file_returns_false(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        assert fp._is_file_stable_by_size(tmp_path / 'nonexistent.mov') is False


# ---------------------------------------------------------------------------
# TestJobsMenu
# ---------------------------------------------------------------------------

class TestJobsMenu:
    def test_jobs_command_enters_menu(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        fp._handle_command('JOBS', False)
        assert fp._active_menu == MenuMode.JOBS

    def test_home_exits_jobs_menu(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        fp._active_menu = MenuMode.JOBS
        fp._handle_command('HOME', False)
        assert fp._active_menu == MenuMode.NONE

    def test_jobs_no_app_logs_summary(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = None
        fp._handle_command('JOBS', False)
        fp.logger.info.assert_called()
        msg = fp.logger.info.call_args[0][0]
        assert 'JOBS' in msg
        assert 'pending' in msg

    def test_jobs_menu_routes_to_handler(self, tmp_path: pathlib.Path) -> None:
        """Commands typed while JOBS menu is active should route to _handle_jobs_command."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        fp._active_menu = MenuMode.JOBS
        # HOME inside the JOBS menu handler should close it
        fp._handle_command('HOME', False)
        assert fp._active_menu == MenuMode.NONE


# ---------------------------------------------------------------------------
# TestJobsCancel
# ---------------------------------------------------------------------------

class TestJobsCancel:
    def _enqueue_one(self, fp: _FakePipeline) -> str:
        """Enqueue a single no-op job and return its job_id."""
        from nofun.job_manifest import JobManifest, PipelineJob
        from nofun.job_queue import JobCategory
        job = PipelineJob(kind='encode_quads', label='test job')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: lambda: None},
        )
        fp._job_queue.enqueue(manifest, JobCategory.GPU_BOUND)
        return job.job_id

    def test_cancel_via_menu_command(self, tmp_path: pathlib.Path) -> None:
        """Select a job by number then CANCEL removes it from the queue."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._enqueue_one(fp)
        assert fp._job_queue.pending_count() == 1
        fp._active_menu = MenuMode.JOBS
        fp._handle_command('1', False)        # select job 1
        assert fp._jobs_selected_idx == 0
        fp._handle_command('CANCEL', False)   # cancel selected
        assert fp._job_queue.pending_count() == 0
        assert fp._jobs_selected_idx is None  # selection cleared

    def test_cancel_without_selection_does_nothing(self, tmp_path: pathlib.Path) -> None:
        """CANCEL with no job selected leaves the queue unchanged."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._enqueue_one(fp)
        fp._active_menu = MenuMode.JOBS
        fp._handle_command('CANCEL', False)
        assert fp._job_queue.pending_count() == 1

    def test_number_toggles_selection(self, tmp_path: pathlib.Path) -> None:
        """Typing the same number twice deselects."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._enqueue_one(fp)
        fp._active_menu = MenuMode.JOBS
        fp._handle_command('1', False)
        assert fp._jobs_selected_idx == 0
        fp._handle_command('1', False)
        assert fp._jobs_selected_idx is None

    def test_home_collapses_selection_before_exiting(self, tmp_path: pathlib.Path) -> None:
        """HOME with a selection deselects; second HOME exits."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._enqueue_one(fp)
        fp._active_menu = MenuMode.JOBS
        fp._handle_command('1', False)
        assert fp._jobs_selected_idx == 0
        fp._handle_command('HOME', False)
        assert fp._jobs_selected_idx is None
        assert fp._active_menu == MenuMode.JOBS  # still in menu
        fp._handle_command('HOME', False)
        assert fp._active_menu == MenuMode.NONE  # now exited


# ---------------------------------------------------------------------------
# TestManualQueue
# ---------------------------------------------------------------------------

class TestManualQueue:
    """REMASTER and REUPLOAD commands enqueue MANUAL jobs instead of raw threads."""

    def _make_ps(self, band: str, has_zip: bool = True, tmp_path=None):
        """Build a minimal PerformanceState stub."""
        from nofun.inventory import PerformanceState
        ps = PerformanceState.__new__(PerformanceState)
        ps.band = band
        ps.zip_files = [tmp_path / f'26-04-12_{band}.zip'] if has_zip and tmp_path else []
        return ps

    def _seed_status_entries(self, fp: _FakePipeline, date: str, bands: list, tmp_path) -> None:
        """Populate _status_entries the way _enter_status_menu does."""
        fp._status_entries = [
            ((date, 'show'), self._make_ps(b, tmp_path=tmp_path))
            for b in bands
        ]
        fp._status_expanded_key = date

    def test_remaster_enqueues_manual_job(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._seed_status_entries(fp, '2026-04-12', ['PRIZE'], tmp_path)
        fp._active_menu = MenuMode.STATUS

        fp._handle_command('REMASTER', False)

        # One REMASTER + one REEL (depends on it) per band
        assert fp._job_queue.pending_count() == 2
        scripts = {qj.job.kind for qj in fp._job_queue.all_active()}
        assert '_remaster' in scripts
        assert 'generate_reel' in scripts
        assert 'REMASTER' in fp._job_queue.all_active()[0].manifest_key

    def test_remaster_second_press_cancels_and_requeues(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._seed_status_entries(fp, '2026-04-12', ['PRIZE'], tmp_path)
        fp._active_menu = MenuMode.STATUS

        fp._handle_command('REMASTER', False)
        assert fp._job_queue.pending_count() == 2
        first_master = next(qj for qj in fp._job_queue.all_active()
                            if qj.job.kind == '_remaster')

        # Second press — should cancel first and re-enqueue with force
        fp._handle_command('REMASTER', False)
        assert fp._job_queue.pending_count() == 2
        second_master = next(qj for qj in fp._job_queue.all_active()
                             if qj.job.kind == '_remaster')
        assert second_master.job_id != first_master.job_id
        assert 'force' in second_master.job.label

    def test_remaster_no_expanded_key_does_not_enqueue(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        fp._active_menu = MenuMode.STATUS
        fp._status_expanded_key = None

        fp._handle_command('REMASTER', False)
        assert fp._job_queue.pending_count() == 0

    def test_reupload_enqueues_per_band(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._seed_status_entries(fp, '2026-04-12', ['PRIZE', 'ALTAR'], tmp_path)
        fp._active_menu = MenuMode.STATUS

        fp._handle_command('REUPLOAD', False)

        assert fp._job_queue.pending_count() == 2
        keys = {qj.manifest_key for qj in fp._job_queue.all_active()}
        assert any('PRIZE' in k for k in keys)
        assert any('ALTAR' in k for k in keys)

    def test_reupload_duplicate_skipped(self, tmp_path: pathlib.Path) -> None:
        """Second REUPLOAD for same band is silently skipped."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._seed_status_entries(fp, '2026-04-12', ['PRIZE'], tmp_path)
        fp._active_menu = MenuMode.STATUS

        fp._handle_command('REUPLOAD', False)
        fp._handle_command('REUPLOAD', False)  # duplicate

        assert fp._job_queue.pending_count() == 1

    def test_manual_jobs_use_manual_category(self, tmp_path: pathlib.Path) -> None:
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        self._seed_status_entries(fp, '2026-04-12', ['PRIZE'], tmp_path)
        fp._active_menu = MenuMode.STATUS

        fp._handle_command('REUPLOAD', False)

        from nofun.job_queue import JobCategory
        active = fp._job_queue.all_active()
        assert all(qj.category == JobCategory.MANUAL for qj in active)


# ---------------------------------------------------------------------------
# TestReprocessCommand
# ---------------------------------------------------------------------------

class TestReprocessCommand:
    def test_discover_archived_performances(self, tmp_path: pathlib.Path) -> None:
        """_cmd_reprocess finds MOVs and WAVs in archive dirs."""
        fp = _FakePipeline(tmp_path)
        (fp.video_archive / '26-04-07_PRIZE.mov').write_bytes(b'\x00' * 100)
        (fp.video_archive / '26-04-07_CLAY.mov').write_bytes(b'\x00' * 100)
        (fp.audio_archive / '26-04-07_PRIZE.wav').write_bytes(b'\x00' * 100)

        fp._cmd_reprocess()

        assert '26-04-07_PRIZE' in fp._reprocess_candidates
        assert '26-04-07_CLAY' in fp._reprocess_candidates
        archived = fp._reprocess_archived
        assert len(archived['26-04-07_PRIZE']['movs']) == 1
        assert len(archived['26-04-07_PRIZE']['wavs']) == 1

    def test_reprocess_empty_archive(self, tmp_path: pathlib.Path) -> None:
        """_cmd_reprocess logs a notice and returns when nothing is archived."""
        fp = _FakePipeline(tmp_path)
        fp._cmd_reprocess()
        fp.logger.info.assert_called_with("REPROCESS: no archived performances found")

    def test_staging_creates_symlinks(self, tmp_path: pathlib.Path) -> None:
        """REPROCESS creates symlinks in staging dir pointing to archived files."""
        probe = tmp_path / '_probe_src'
        probe.touch()
        try:
            (tmp_path / '_probe_link').symlink_to(probe)
        except OSError:
            pytest.skip('symlinks not supported on this platform')

        fp = _FakePipeline(tmp_path)
        original = fp.video_archive / '26-04-07_PRIZE.mov'
        original.write_bytes(b'\x00' * 100)
        fp._cmd_reprocess()

        # Manually trigger selection #1
        fp._handle_reprocess_command('1')

        staging = pathlib.Path(__file__).parent.parent / '_reprocess_staging' / '26-04-07_PRIZE'
        if staging.exists():
            link = staging / original.name
            assert link.exists()
            assert link.is_symlink()
            # Cleanup
            import shutil
            shutil.rmtree(staging.parent, ignore_errors=True)

    def test_reprocess_selection_out_of_range(self, tmp_path: pathlib.Path) -> None:
        """Out-of-range selection does not crash."""
        fp = _FakePipeline(tmp_path)
        (fp.video_archive / '26-04-07_PRIZE.mov').write_bytes(b'\x00' * 100)
        fp._cmd_reprocess()
        # Should not raise
        fp._handle_reprocess_command('999')
        fp._handle_reprocess_command('-1')

    def test_reprocess_home_cancels(self, tmp_path: pathlib.Path) -> None:
        """HOME while in REPROCESS menu clears the menu state."""
        fp = _FakePipeline(tmp_path)
        (fp.video_archive / '26-04-07_PRIZE.mov').write_bytes(b'\x00' * 100)
        fp._cmd_reprocess()
        assert fp._active_menu == MenuMode.REPROCESS

        fp._handle_reprocess_command('HOME')
        assert fp._active_menu == MenuMode.NONE


# ---------------------------------------------------------------------------
# TestAutoScan
# ---------------------------------------------------------------------------

import time as _time_module


class TestAutoScan:
    """Tests for hourly auto-SCAN enqueue logic in _maybe_enqueue_scheduled_tasks."""

    def _call_enqueue_scheduled(self, fp):
        """Call _maybe_enqueue_scheduled_tasks, stubbing out _run_scan."""
        fp._run_scan = MagicMock()
        fp._maybe_enqueue_scheduled_tasks()

    def test_auto_scan_enqueued_after_interval(self, tmp_path):
        """AUTO SCAN is enqueued when _last_scan_enqueued is more than 3600s ago."""
        fp = _FakePipeline(tmp_path)
        fp._last_scan_enqueued = _time_module.time() - 3601.0
        self._call_enqueue_scheduled(fp)
        labels = {qj.job.label for qj in fp._job_queue.all_active()}
        assert 'AUTO SCAN' in labels

    def test_auto_scan_not_duplicated_within_interval(self, tmp_path):
        """A second call within the hour does not enqueue another AUTO SCAN."""
        fp = _FakePipeline(tmp_path)
        fp._last_scan_enqueued = _time_module.time() - 3601.0
        self._call_enqueue_scheduled(fp)
        # Simulate time passing but still within 3600s of the enqueue
        fp._last_scan_enqueued = _time_module.time() - 10.0
        self._call_enqueue_scheduled(fp)
        scan_jobs = [qj for qj in fp._job_queue.all_active()
                     if qj.job.label == 'AUTO SCAN']
        assert len(scan_jobs) == 1

    def test_auto_scan_dedup_by_label_if_active(self, tmp_path):
        """AUTO SCAN not enqueued again if it is already in the active queue."""
        fp = _FakePipeline(tmp_path)
        # First enqueue goes through
        fp._last_scan_enqueued = _time_module.time() - 3601.0
        self._call_enqueue_scheduled(fp)
        # Reset timer so interval would allow another enqueue
        fp._last_scan_enqueued = _time_module.time() - 3601.0
        self._call_enqueue_scheduled(fp)
        scan_jobs = [qj for qj in fp._job_queue.all_active()
                     if qj.job.label == 'AUTO SCAN']
        assert len(scan_jobs) == 1


# ---------------------------------------------------------------------------
# TestSafeLinkAndStagingCleanup
# ---------------------------------------------------------------------------


class TestSafeLinkAndStagingCleanup:
    """Tests for REPROCESS _safe_link fallback and _reprocess_staging cleanup."""

    def test_safe_link_creates_symlink(self, tmp_path):
        """_safe_link creates a symlink when symlinks are supported."""
        probe = tmp_path / '_probe_src'
        probe.touch()
        try:
            (tmp_path / '_probe_link').symlink_to(probe)
        except OSError:
            pytest.skip('symlinks not supported on this platform')

        fp = _FakePipeline(tmp_path)
        src = tmp_path / 'source.mov'
        src.write_bytes(b'\x00' * 10)
        dst = tmp_path / 'link.mov'
        fp._safe_link(src, dst)
        assert dst.exists()
        assert dst.is_symlink()

    def test_safe_link_falls_back_to_copy_on_oserror(self, tmp_path, monkeypatch):
        """_safe_link copies the file when symlink_to raises OSError."""
        import pathlib as _pathlib
        fp = _FakePipeline(tmp_path)
        src = tmp_path / 'source.mov'
        src.write_bytes(b'\xDE\xAD\xBE\xEF' * 4)
        dst = tmp_path / 'link.mov'

        def _fail_symlink(self, *args, **kwargs):
            raise OSError("privilege not held")
        monkeypatch.setattr(_pathlib.Path, 'symlink_to', _fail_symlink)

        fp._safe_link(src, dst)
        assert dst.exists()
        assert not dst.is_symlink()
        assert dst.read_bytes() == src.read_bytes()

    def test_staging_cleanup_on_shutdown(self, tmp_path):
        """_cleanup() removes the _reprocess_staging/ directory tree."""
        import pathlib
        # Create a staging directory as if REPROCESS had run
        staging = pathlib.Path(__file__).parent.parent / '_reprocess_staging' / '26-04-07_PRIZE'
        staging.mkdir(parents=True, exist_ok=True)
        (staging / 'test.mov').write_bytes(b'\x00' * 10)
        try:
            fp = _FakePipeline(tmp_path)
            fp._cleanup()
            assert not staging.parent.exists()
        finally:
            # Safety cleanup in case the assertion fails
            import shutil
            staging_root = pathlib.Path(__file__).parent.parent / '_reprocess_staging'
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)


class TestHomeCommand:
    def test_home_at_top_level_no_op(self, tmp_path: pathlib.Path, caplog) -> None:
        """HOME at the top level (no active menu) should be a no-op.

        Regression for log_bugs.md #6 — HOME fell through to the catch-all
        handler and logged "Unknown command: 'HOME'" when typed outside
        a menu or help overlay.
        """
        fp = _FakePipeline(tmp_path)
        fp._active_menu = MenuMode.NONE

        import logging
        with caplog.at_level(logging.INFO):
            fp._handle_command('HOME', False)

        assert not any('Unknown command' in r.getMessage() for r in caplog.records)
