"""REMASTER must find PerformanceState for canonical YY-MM-DD perf keys.

Regression for the date-format mismatch the smoke test
``remaster_perf_key_date_format_001`` guards: perf keys and _status_entries
keys are both YY-MM-DD since the DB migration, but _do_remaster_for_perf used
to expand date_str to YYYY-MM-DD ('20' + date) before comparing — so the match
never fired and REMASTER was silently skipped for every band. The fix
normalises with short_date() instead.
"""
import logging

from tests.fake_pipeline import FakePipeline


class _PS:
    def __init__(self, date, band):
        self.date, self.band = date, band


class _RemasterPipeline(FakePipeline):
    def __init__(self, tmp_path, status_entries):
        super().__init__(tmp_path)
        self.logger = logging.getLogger('test_remaster')
        self._status_entries = status_entries
        self._remastered = []

    def _rebuild_status_entries(self):
        return True

    def _do_remaster_for_band(self, date_str, perf_state):
        self._remastered.append((date_str, perf_state))


def _make_pipeline(tmp_path, status_entries):
    """A FakePipeline wired only for _do_remaster_for_perf."""
    return _RemasterPipeline(tmp_path, status_entries)


def test_remaster_matches_short_date_perf_key(tmp_path):
    ps = _PS('01-01-01', 'SMOKETEST')
    p = _make_pipeline(tmp_path, [(('01-01-01', 'SMOKETEST'), ps)])
    p._do_remaster_for_perf('01-01-01_SMOKETEST')
    assert p._remastered == [('01-01-01', ps)]


def test_remaster_tolerates_long_date_in_perf(tmp_path):
    # If a long YYYY-MM-DD date leaks into perf, it must still match the
    # canonical YY-MM-DD status entry rather than silently miss.
    ps = _PS('01-01-01', 'SMOKETEST')
    p = _make_pipeline(tmp_path, [(('01-01-01', 'SMOKETEST'), ps)])
    p._do_remaster_for_perf('2001-01-01_SMOKETEST')
    assert p._remastered == [('01-01-01', ps)]


def test_remaster_warns_when_no_state(tmp_path, caplog):
    p = _make_pipeline(tmp_path, [])
    with caplog.at_level(logging.WARNING):
        p._do_remaster_for_perf('01-01-01_SMOKETEST')
    assert p._remastered == []
    assert 'no performance state found' in caplog.text
