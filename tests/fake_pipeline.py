"""Shared FakePipeline test stub — the single replacement for the per-file
_FakePipeline/_FakeAudio stubs (test_audio, test_video, test_cleanup,
test_menu_commands). New Pipeline fields land here, in one place.

A Pipeline subclass whose __init__ skips the real one (no path detection,
logging setup, or encoder probing): tmp_path-rooted production dir layout,
mocked logger + script runner (ok result by default), real DeleteQueue,
EncodingDB, and JobQueue.
"""

import pathlib
import queue
import threading
from typing import Any
from unittest.mock import MagicMock

from nofun.encoding_db import EncodingDB
from nofun.job_queue import JobQueue
from nofun.media_io import DeleteQueue
from nofun.script_runner import ScriptResult, ScriptRunner
from nofun.state import MenuMode, PauseState
from media_engine import Pipeline, _HelpState, _HOME_COMMANDS


class FakePipeline(Pipeline):
    """Pipeline with a side-effect-free __init__ wired to tmp_path."""

    # Typed as Any so MagicMock assignments and mock assertions pass Pyright
    logger:         Any
    delete_queue:   Any
    _pause_state:   Any
    _app:           Any
    _script_runner: Any

    def __init__(self, tmp_path: pathlib.Path) -> None:
        # Skip super().__init__() — set every attribute Pipeline uses directly
        self.directory        = tmp_path
        self.trial_run        = 0
        self.force            = False
        self.exit_on_complete = False
        self.skip_audio       = False
        self.gpu              = False
        self.cleanup_only     = False

        # Production dir names so tests can never assert a stale layout
        self.search_dir      = tmp_path
        self.vids_dest       = tmp_path / 'videos'
        self.clips_dest      = tmp_path / 'clips'
        self.audio_dest      = tmp_path / 'audio'
        self.video_archive   = tmp_path / 'video_archive'
        self.audio_archive   = tmp_path / 'audio_archive'
        self.sharepoint_dest = None
        self.script_dir      = tmp_path
        self.mount_d         = tmp_path
        self.inventory_csv     = tmp_path / 'file_inventory.csv'
        self.inventory_summary = tmp_path / 'inventory_summary.txt'

        self._encoding_db = EncodingDB(tmp_path / 'encoding_db.json')

        self.logger       = MagicMock()
        self.delete_queue = DeleteQueue()
        self.enc          = {
            'accel':    [],
            'enc_quad': ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18'],
            'enc_clip': ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23'],
        }

        self._known_files:    dict = {}
        self._file_sizes:     dict = {}
        self._pipeline_moved: queue.Queue[str] = queue.Queue()
        self._streams_active       = False
        self._stream_procs:   list = []
        self._app                  = None

        self._HOME_COMMANDS        = _HOME_COMMANDS
        self._active_menu          = MenuMode.NONE
        self._cleanup_findings: list = []
        self._status_entries: list = []
        self._show_groups:    list = []
        self._status_expanded_key  = None
        self._rename_state         = None
        self._remaster_state       = None
        self._rename_date          = None
        self._rename_band          = None
        self._rename_new_name      = None
        self._rename_thread        = None
        self._disk_c               = ''
        self._disk_d               = ''
        self._disk_n               = ''
        self._disk_sp              = ''
        self._stream_states:  list = []

        self._pause_state             = PauseState.RUNNING
        self._current_ffmpeg_procs:  dict = {}
        self._ffmpeg_procs_lock      = threading.Lock()
        self._cmd_queue              = None
        self._current_operation      = ''
        self._noproblem_active       = False
        self._override_time          = False
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

        self._script_runner = MagicMock()
        self._script_runner.run.return_value = ScriptResult(
            script='', exit_code=0, stdout_json={}, stderr_tail='', elapsed=0.0,
        )
        self._job_queue = JobQueue(MagicMock(spec=ScriptRunner), self.logger)

        for d in (self.vids_dest, self.clips_dest, self.audio_dest,
                  self.video_archive, self.audio_archive):
            d.mkdir(parents=True, exist_ok=True)

    # -- behaviour stubs shared by the old per-file fakes ------------------

    def _move_to_hard_paused(self, files) -> None:
        pass

    def _is_file_stable(self, path: pathlib.Path) -> bool:
        return True

    def _flush_commands(self) -> None:
        pass

    def _set_op(self, key: str, text: str) -> None:
        pass

    def _clear_op(self, key: str) -> None:
        pass

    def _set_ffmpeg_proc(self, key: str, proc) -> None:
        if proc is None:
            self._current_ffmpeg_procs.pop(key, None)
        else:
            self._current_ffmpeg_procs[key] = proc

