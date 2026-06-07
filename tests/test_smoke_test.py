"""Log-driven completion detection for the smoke harness (scripts/smoke_test.py).

These cover the marker matching that replaced NAS disk-polling: the harness now
decides "rebuild finished" by tailing the engine log, so the matchers must fire on
the real ``JobQueue: finish`` / ``WRITE _AUDIO.mp3`` line shapes and stay tied to
this perf (a failed REMASTER must NOT count as audio-complete).
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    'smoke_test', REPO / 'scripts' / 'smoke_test.py')
st = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(st)

PERF = '01-01-01_SMOKETEST'

# Real lines from a successful prod run (convert_recent.log, 26-06-06 14:xx).
_OK_LOG = """\
[26-06-06T14:05:03] CREATE  01-01-01_SMOKETEST_MULTITRACK.zip
[26-06-06T14:05:27] JobQueue: finish 01-01-01 SMOKETEST REENCODE  (done  35.4s)
[26-06-06T14:43:43] WRITE   01-01-01_SMOKETEST_AUDIO.mp3
[26-06-06T14:43:47] JobQueue: finish 01-01-01 SMOKETEST REMASTER  (done  18.4s)
[26-06-06T14:45:43] JobQueue: finish 01-01-01 SMOKETEST REEL  (done  1m55s)
[26-06-06T14:46:10] JobQueue: finish 01-01-01 SMOKETEST CLIPS  (done  2m21s)
"""


def test_all_markers_seen_on_success():
    assert st._seen_markers(_OK_LOG, PERF) == {'quads', 'audio', 'reel', 'clips'}


def test_partial_run_reports_subset():
    partial = '\n'.join(_OK_LOG.splitlines()[:2])  # zip + REENCODE only
    assert st._seen_markers(partial, PERF) == {'quads'}


def test_failed_remaster_does_not_count_as_audio():
    # A REMASTER that fails logs "no audio master produced" and never writes the
    # mp3 — the audio marker must stay clear so the harness doesn't false-pass.
    log = ("[26-06-06T14:41:49] REMASTER  01-01-01_SMOKETEST  no audio master "
           "produced (missing or unusable source WAVs in "
           "01-01-01_SMOKETEST_MULTITRACK.zip)\n")
    assert 'audio' not in st._seen_markers(log, PERF)
    assert st._terminal_failure(log, PERF)


def test_terminal_failure_is_perf_scoped():
    # Another band failing must not abort this perf's wait.
    log = "[26-06-06T14:41:49] REMASTER  26-05-25_Flatwounds  no audio master produced\n"
    assert not st._terminal_failure(log, PERF)


def test_wait_returns_true_when_log_complete(tmp_path):
    log = tmp_path / 'engine.log'
    log.write_text(_OK_LOG)
    assert st.wait_for_outputs(PERF, log_path=log, log_offset=0,
                               timeout=5, poll=0.01) is True


def test_wait_returns_false_without_log():
    assert st.wait_for_outputs(PERF, log_path=None, log_offset=0,
                               timeout=5, poll=0.01) is False


def test_wait_bails_fast_on_terminal_failure(tmp_path):
    log = tmp_path / 'engine.log'
    log.write_text("[26-06-06T14:41:49] REMASTER  01-01-01_SMOKETEST  "
                   "no audio master produced\n")
    assert st.wait_for_outputs(PERF, log_path=log, log_offset=0,
                               timeout=5, poll=0.01) is False
