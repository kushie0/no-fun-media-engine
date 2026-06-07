"""REMASTER must find PerformanceState for canonical YY-MM-DD perf keys.

Regression for the date-format mismatch the smoke test
``remaster_perf_key_date_format_001`` guards: perf keys and _status_entries
keys are both YY-MM-DD since the DB migration, but _do_remaster_for_perf used
to expand date_str to YYYY-MM-DD ('20' + date) before comparing — so the match
never fired and REMASTER was silently skipped for every band. The fix
normalises with short_date() instead.
"""
import logging

from media_engine import Pipeline


class _PS:
    def __init__(self, date, band):
        self.date, self.band = date, band


def _make_pipeline(status_entries):
    """A Pipeline with __init__ bypassed, wired only for _do_remaster_for_perf."""
    p = object.__new__(Pipeline)
    p.logger = logging.getLogger('test_remaster')
    p._status_entries = status_entries
    p._rebuild_status_entries = lambda: True   # don't re-read the real DB
    p._remastered = []
    p._do_remaster_for_band = lambda d, ps: p._remastered.append((d, ps))
    return p


def test_remaster_matches_short_date_perf_key():
    ps = _PS('01-01-01', 'SMOKETEST')
    p = _make_pipeline([(('01-01-01', 'SMOKETEST'), ps)])
    p._do_remaster_for_perf('01-01-01_SMOKETEST')
    assert p._remastered == [('01-01-01', ps)]


def test_remaster_tolerates_long_date_in_perf():
    # If a long YYYY-MM-DD date leaks into perf, it must still match the
    # canonical YY-MM-DD status entry rather than silently miss.
    ps = _PS('01-01-01', 'SMOKETEST')
    p = _make_pipeline([(('01-01-01', 'SMOKETEST'), ps)])
    p._do_remaster_for_perf('2001-01-01_SMOKETEST')
    assert p._remastered == [('01-01-01', ps)]


def test_remaster_warns_when_no_state(caplog):
    p = _make_pipeline([])
    with caplog.at_level(logging.WARNING):
        p._do_remaster_for_perf('01-01-01_SMOKETEST')
    assert p._remastered == []
    assert 'no performance state found' in caplog.text
