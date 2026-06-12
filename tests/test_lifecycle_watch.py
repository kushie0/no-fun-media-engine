"""Unit tests for scripts/lifecycle_watch.py pure functions.

Covers the parts that don't touch the process table or sleep: process-signature
matching, edge-trigger diffing, pause-marker extraction, graceful detection, and
the rotation-tolerant log reader. The psutil polling loop is exercised live.
"""

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location(
    'lifecycle_watch', REPO / 'scripts' / 'lifecycle_watch.py'
)
assert _SPEC is not None and _SPEC.loader is not None
lw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lw)


# ---------------------------------------------------------------------------
# engine_running / stream_count — process-signature matching
# ---------------------------------------------------------------------------

def test_engine_running_matches_python_running_script():
    procs = [
        ('python.exe', ['C:/uv/python.exe', 'media_engine.py']),
        ('ffmpeg.exe', ['ffmpeg', '-i', 'x.mov']),
    ]
    assert lw.engine_running(procs) is True


def test_engine_running_matches_pathed_script():
    assert lw.engine_running([('python3', ['python3', '/opt/app/media_engine.py'])]) is True


def test_engine_running_absent():
    assert lw.engine_running([('ffmpeg.exe', ['ffmpeg', '-i', 'x.mov']),
                              ('explorer.exe', ['explorer.exe'])]) is False


def test_engine_running_ignores_shell_that_mentions_script():
    # a shell/grep/ssh whose argv merely CONTAINS the path must not match — this
    # is the false-positive that substring matching caused.
    procs = [
        ('bash', ['bash', '-c', 'python3 /tmp/media_engine.py & sleep 60']),
        ('grep', ['grep', 'media_engine.py']),
        ('vim', ['vim', 'media_engine.py']),
    ]
    assert lw.engine_running(procs) is False


def test_stream_count_matches_mpegts_pipe():
    procs = [
        ('ffmpeg.exe', ['ffmpeg', '-f', 'mpegts', 'pipe:1']),   # stream worker
        ('ffmpeg.exe', ['ffmpeg', '-f', 'mpegts', 'pipe:1']),   # stream worker
        ('ffmpeg.exe', ['ffmpeg', '-i', 'a.mov', 'out.mp4']),   # an encode, not a stream
        ('python.exe', ['python.exe', 'media_engine.py']),
    ]
    assert lw.stream_count(procs) == 2


def test_stream_count_zero_when_no_pipe():
    # mpegts present but not piped (e.g. file mux) must not count as a stream
    assert lw.stream_count([('ffmpeg.exe', ['ffmpeg', '-f', 'mpegts', 'out.ts'])]) == 0


def test_stream_count_ignores_non_ffmpeg_mentioning_args():
    # a shell whose argv contains both marks but isn't ffmpeg must not count
    assert lw.stream_count([('bash', ['bash', '-c', 'echo mpegts pipe:1'])]) == 0


# ---------------------------------------------------------------------------
# diff_state — edge triggering
# ---------------------------------------------------------------------------

def test_diff_first_sample_emits_baseline():
    assert lw.diff_state(None, (True, 2)) == ['ENGINE LAUNCH', 'STREAMS UP (2)']


def test_diff_no_change_is_silent():
    assert lw.diff_state((True, 2), (True, 2)) == []


def test_diff_engine_launch_and_close():
    assert lw.diff_state((False, 0), (True, 0)) == ['ENGINE LAUNCH']
    assert lw.diff_state((True, 0), (False, 0)) == ['ENGINE CLOSE']


def test_diff_streams_up_down_and_change():
    assert lw.diff_state((True, 0), (True, 3)) == ['STREAMS UP (3)']
    assert lw.diff_state((True, 3), (True, 0)) == ['STREAMS DOWN']
    assert lw.diff_state((True, 3), (True, 5)) == ['STREAMS 3->5']


def test_diff_combined_engine_and_streams():
    assert lw.diff_state((False, 0), (True, 4)) == ['ENGINE LAUNCH', 'STREAMS UP (4)']


# ---------------------------------------------------------------------------
# pause_events — mirrored from engine-log markers
# ---------------------------------------------------------------------------

def test_pause_events_detects_pause_and_resume():
    text = (
        '[26-06-06T12:00:00] PAUSE   Paused at safe point — type RESUME to continue\n'
        '[26-06-06T12:01:00] RESUME  Continuing processing\n'
    )
    assert lw.pause_events(text) == ['ENGINE PAUSE', 'ENGINE RESUME']


def test_pause_events_hard_stop_is_pause():
    text = '[26-06-06T12:00:00] PAUSE   Hard stop complete — type RESUME to continue\n'
    assert lw.pause_events(text) == ['ENGINE PAUSE']


def test_pause_events_ignores_noise():
    # routine PAUSE-prefixed lines that aren't real transitions
    text = (
        '[26-06-06T12:00:00] PAUSE   Already paused — type RESUME to continue\n'
        '[26-06-06T12:00:01] PAUSE   Could not move x.mov: busy\n'
        '[26-06-06T12:00:02] NOTICE  Not currently paused\n'
    )
    assert lw.pause_events(text) == []


# ---------------------------------------------------------------------------
# is_graceful_close
# ---------------------------------------------------------------------------

def test_graceful_close_true_on_exit_marker():
    tail = 'CREATE foo.mp4\nNo files found — exiting\n'
    assert lw.is_graceful_close(tail) is True


def test_graceful_close_false_when_exit_is_stale():
    # an old exit marker far above the last 5 lines must not count
    tail = 'exiting\n' + '\n'.join(f'JobQueue: finish job{i}' for i in range(8))
    assert lw.is_graceful_close(tail) is False


def test_graceful_close_false_without_marker():
    assert lw.is_graceful_close('CREATE a.mp4\nJobQueue: start REEL\n') is False


# ---------------------------------------------------------------------------
# _read_new — rotation tolerance
# ---------------------------------------------------------------------------

def test_read_new_appends_since_offset(tmp_path):
    p = tmp_path / 'engine.log'
    p.write_text('line1\n')
    text, off = lw._read_new(p, 0)
    assert text == 'line1\n'
    p.write_text('line1\nline2\n')
    text2, off2 = lw._read_new(p, off)
    assert text2 == 'line2\n'
    assert off2 > off


def test_read_new_resets_on_truncation(tmp_path):
    p = tmp_path / 'engine.log'
    p.write_text('a lot of content here\n')
    _, off = lw._read_new(p, 0)
    p.write_text('x\n')  # rotated/truncated — smaller than prior offset
    text, _ = lw._read_new(p, off)
    assert text == 'x\n'


def test_read_new_missing_path_is_noop():
    assert lw._read_new(None, 0) == ('', 0)
