"""Unit tests for pipeline_utils.py"""

import io
import logging
import pathlib
import queue
import unittest.mock as mock

import pytest

from nofun.media_io import DeleteQueue, fmt_size, is_file_locked, run_ffmpeg
from nofun.paths    import detect_clips_root, detect_platform, detect_mounts


class TestDetectClipsRoot:
    def test_env_var_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv('CLIPS_ROOT', str(tmp_path / 'custom'))
        assert detect_clips_root(pathlib.Path('/d')) == tmp_path / 'custom'

    def test_default_falls_back_to_mount_d(self, monkeypatch):
        monkeypatch.delenv('CLIPS_ROOT', raising=False)
        assert detect_clips_root(pathlib.Path('/d')) == pathlib.Path('/d/clips')

    def test_empty_env_var_falls_back(self, monkeypatch):
        monkeypatch.setenv('CLIPS_ROOT', '')
        assert detect_clips_root(pathlib.Path('/d')) == pathlib.Path('/d/clips')


class TestFmtSize:
    def test_mb(self):
        assert "MB" in fmt_size(500 * 1024 * 1024)

    def test_gb(self):
        assert "GB" in fmt_size(2 * 1024 ** 3)

    def test_mb_value(self):
        # 512 MB
        result = fmt_size(512 * 1024 * 1024)
        assert result == "512 MB"

    def test_gb_value(self):
        result = fmt_size(2 * 1024 ** 3)
        assert result == "2.0 GB"

    def test_small_bytes_shows_mb(self):
        result = fmt_size(1)
        assert "MB" in result


class TestDeleteQueue:
    def test_execute_removes_file(self, tmp_path):
        f = tmp_path / "test.wav"
        f.write_bytes(b'\x00' * 1024)
        q = DeleteQueue()
        q.add(f, "unit test")
        logger = logging.getLogger('test')
        q.execute(logger)
        assert not f.exists()
        assert q.items == []

    def test_execute_skips_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.wav"
        q = DeleteQueue()
        q.add(f, "unit test missing")
        logger = logging.getLogger('test')
        q.execute(logger)  # should not raise
        assert q.items == []

    def test_add_logs_message(self, tmp_path, caplog):
        f = tmp_path / "x.wav"
        f.write_bytes(b'x')
        q = DeleteQueue()
        logger = logging.getLogger('test_add')
        with caplog.at_level(logging.INFO, logger='test_add'):
            q.add(f, "silent channel", logger=logger)
        assert "PENDING" in caplog.text
        assert "x.wav" in caplog.text

    def test_summary_groups_by_ext(self, tmp_path, capsys):
        f1 = tmp_path / "a.wav"
        f1.write_bytes(b'\x00' * 512)
        f2 = tmp_path / "b.wav"
        f2.write_bytes(b'\x00' * 512)
        q = DeleteQueue()
        q.add(f1, "silent channel")
        q.add(f2, "silent channel")
        q.show_summary()
        out = capsys.readouterr().out
        assert ".wav" in out
        assert "2" in out  # grouped count

    def test_summary_returns_false_when_empty(self):
        q = DeleteQueue()
        assert q.show_summary() is False

    def test_summary_returns_true_when_items(self, tmp_path, capsys):
        f = tmp_path / "c.wav"
        f.write_bytes(b'\x00')
        q = DeleteQueue()
        q.add(f, "reason")
        result = q.show_summary()
        assert result is True

    def test_clear(self, tmp_path):
        f = tmp_path / "d.wav"
        f.write_bytes(b'\x00')
        q = DeleteQueue()
        q.add(f, "r")
        q.clear()
        assert q.items == []

    def test_multiple_reasons_shown_separately(self, tmp_path, capsys):
        f1 = tmp_path / "e.wav"
        f1.write_bytes(b'\x00' * 100)
        f2 = tmp_path / "f.mov"
        f2.write_bytes(b'\x00' * 200)
        q = DeleteQueue()
        q.add(f1, "reason A")
        q.add(f2, "reason B")
        q.show_summary()
        out = capsys.readouterr().out
        assert "reason A" in out
        assert "reason B" in out

    def test_execute_updates_pipeline_moved(self, tmp_path):
        """Deleted paths must be added to pipeline_moved so the watchdog
        doesn't log REMOVED for files the pipeline itself deleted.

        Regression for log_bugs.md #2 — DELETEs triggered false REMOVED events.
        """
        moved: queue.Queue[str] = queue.Queue()
        f = tmp_path / 'gone.wav'
        f.write_bytes(b'x')
        logger = logging.getLogger('test')
        q = DeleteQueue()
        q.add(f, 'silent channel')

        q.execute(logger, pipeline_moved=moved)

        items: list[str] = []
        try:
            while True:
                items.append(moved.get_nowait())
        except queue.Empty:
            pass
        assert str(f) in items
        assert not f.exists()


class TestDetectPlatform:
    def test_returns_string(self):
        result = detect_platform()
        assert result in ('darwin', 'wsl', 'windows', 'gitbash', 'linux')


class TestDetectMounts:
    def test_returns_tuple_of_paths(self):
        c, d = detect_mounts()
        assert isinstance(c, pathlib.Path)
        assert isinstance(d, pathlib.Path)


class TestIsFileLocked:
    def test_unlocked_file_returns_false(self, tmp_path):
        f = tmp_path / "unlocked.wav"
        f.write_bytes(b'\x00' * 256)
        assert is_file_locked(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert is_file_locked(tmp_path / "nonexistent.wav") is False


# ---------------------------------------------------------------------------
# TestRunFfmpegParser — byte-by-byte \r/\n progress parser
# ---------------------------------------------------------------------------

_PROGRESS_LINE = (
    b'frame=  120 fps= 30 q=28.0 size=     512kB '
    b'time=00:00:04.00 speed=1.00x\r'
)


def _make_proc(stderr_bytes: bytes, returncode: int = 0) -> mock.MagicMock:
    """Return a MagicMock Popen whose stderr drains from stderr_bytes."""
    buf  = io.BytesIO(stderr_bytes)
    proc = mock.MagicMock()
    proc.stderr.read.side_effect = lambda n: buf.read(n)
    proc.wait.return_value       = returncode
    proc.__enter__.return_value  = proc
    proc.__exit__                = mock.MagicMock(return_value=False)
    return proc


class TestRunFfmpegParser:
    _log = logging.getLogger('test_ffmpeg')

    def test_progress_cb_called_with_parsed_values(self):
        proc  = _make_proc(_PROGRESS_LINE)
        calls: list = []
        with mock.patch('nofun.media_io.subprocess.Popen', return_value=proc):
            run_ffmpeg(['dummy'], self._log,
                       progress_cb=lambda f, fps, tc, s: calls.append((f, fps, tc, s)))
        assert len(calls) == 1
        # _PROGRESS_RE uses [\d:]+ so the fractional seconds are not captured
        assert calls[0] == ('120', '30', '00:00:04', '1.00x')

    def test_proc_cb_receives_popen_handle(self):
        proc     = _make_proc(b'')
        received: list = []
        with mock.patch('nofun.media_io.subprocess.Popen', return_value=proc):
            run_ffmpeg(['dummy'], self._log, proc_cb=lambda p: received.append(p))
        assert len(received) == 1
        assert received[0] is proc

    def test_return_value_matches_returncode(self):
        proc = _make_proc(b'', returncode=1)
        with mock.patch('nofun.media_io.subprocess.Popen', return_value=proc):
            result = run_ffmpeg(['dummy'], self._log)
        assert result == 1

    def test_zero_returncode_on_success(self):
        proc = _make_proc(b'', returncode=0)
        with mock.patch('nofun.media_io.subprocess.Popen', return_value=proc):
            result = run_ffmpeg(['dummy'], self._log)
        assert result == 0

    def test_no_progress_cb_does_not_raise(self, capsys):
        proc = _make_proc(_PROGRESS_LINE)
        with mock.patch('nofun.media_io.subprocess.Popen', return_value=proc):
            result = run_ffmpeg(['dummy'], self._log)
        assert result == 0
