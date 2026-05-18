"""Unit tests for nofun/log_handlers.py and FileDetailFormatter."""
import logging
import pathlib

import pytest
from nofun.log_handlers import RollingRecentHandler, RemoteRotatingHandler


class TestRollingRecentHandler:
    def test_prunes_old_entries_on_init(self, tmp_path):
        log = tmp_path / 'recent.log'
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(hours=49)).strftime('%y-%m-%dT%H:%M:%S')
        new_ts = datetime.now().strftime('%y-%m-%dT%H:%M:%S')
        log.write_text(
            f"[{old_ts}] old entry\n[{new_ts}] new entry\n",
            encoding='utf-8',
        )
        h = RollingRecentHandler(log)
        h.close()
        text = log.read_text()
        assert 'old entry' not in text
        assert 'new entry' in text

    def test_appends_new_entries(self, tmp_path):
        log = tmp_path / 'recent.log'
        h = RollingRecentHandler(log)
        logger = logging.getLogger('test_rolling')
        logger.setLevel(logging.DEBUG)
        logger.handlers = [h]
        h.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', '%y-%m-%dT%H:%M:%S'))
        logger.info('hello')
        h.close()
        assert 'hello' in log.read_text()


class TestRemoteRotatingHandler:
    def _make_handler(self, log_dir: pathlib.Path) -> RemoteRotatingHandler:
        h = RemoteRotatingHandler(log_dir)
        h.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', '%y-%m-%dT%H:%M:%S'))
        return h

    def test_creates_log_dir(self, tmp_path):
        log_dir = tmp_path / 'subdir' / 'logs'
        h = self._make_handler(log_dir)
        h.close()
        assert log_dir.is_dir()

    def test_initial_file_has_log_prefix(self, tmp_path):
        h = self._make_handler(tmp_path)
        p = pathlib.Path(h.baseFilename)
        h.close()
        assert p.name.startswith('log_')
        assert p.suffix == '.txt'

    def test_rotates_at_min_size(self, tmp_path):
        h = self._make_handler(tmp_path)
        h.MIN_SIZE = 100   # tiny threshold for testing
        logger = logging.getLogger('test_rotate')
        logger.setLevel(logging.DEBUG)
        logger.handlers = [h]
        h.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', '%y-%m-%dT%H:%M:%S'))

        for _ in range(10):
            logger.info('x' * 20)

        h.close()
        files = list(tmp_path.glob('log_*.txt'))
        assert len(files) >= 2, f"Expected rotation, got only {[f.name for f in files]}"

    def test_overflow_naming(self, tmp_path):
        """Same-date overflow: log_YYMMDD.txt then log_YYMMDD_A.txt"""
        import datetime
        date_str = datetime.date.today().strftime('%y%m%d')
        base = tmp_path / f'log_{date_str}.txt'
        base.write_bytes(b'x' * RemoteRotatingHandler.MIN_SIZE)

        h = self._make_handler(tmp_path)
        h.close()
        p = pathlib.Path(h.baseFilename)
        assert p.name == f'log_{date_str}_A.txt'


class TestFileDetailFormatter:
    def test_appends_extra_fields(self):
        from nofun.media_io import FileDetailFormatter
        fmt = FileDetailFormatter('[%(asctime)s] %(message)s', '%y-%m-%dT%H:%M:%S')
        record = logging.LogRecord(
            name='test', level=logging.INFO,
            pathname='', lineno=0, msg='MOVE foo.mov → archive/',
            args=(), exc_info=None,
        )
        record.src  = '/source/foo.mov'   # type: ignore[attr-defined]
        record.dst  = '/archive/foo.mov'  # type: ignore[attr-defined]
        record.size = '1073741824 (1.0 GB)'  # type: ignore[attr-defined]
        out = fmt.format(record)
        assert 'src=/source/foo.mov' in out
        assert 'dst=/archive/foo.mov' in out
        assert 'size=1073741824' in out

    def test_no_extra_fields_unchanged(self):
        from nofun.media_io import FileDetailFormatter
        fmt = FileDetailFormatter('[%(asctime)s] %(message)s', '%y-%m-%dT%H:%M:%S')
        record = logging.LogRecord(
            name='test', level=logging.INFO,
            pathname='', lineno=0, msg='Pipeline started',
            args=(), exc_info=None,
        )
        out = fmt.format(record)
        assert '|  ' not in out
