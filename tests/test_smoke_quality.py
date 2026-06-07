"""Unit tests for scripts/smoke_quality.py pure functions.

Covers the parts that don't shell out: log-timeline parsing, output discovery,
metric flattening, and aggregation. The ffmpeg/ffprobe paths (ssim, probe,
cmd_reference) are exercised by the real smoke run, not here.
"""

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location(
    'smoke_quality', REPO / 'scripts' / 'smoke_quality.py'
)
assert _SPEC is not None and _SPEC.loader is not None
sq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sq)


# ---------------------------------------------------------------------------
# parse_timeline
# ---------------------------------------------------------------------------

_LOG = """\
[26-06-05T12:00:00] startup, watching VenueLighting
[26-06-05T12:00:10] DETECTED raw .mov 01-01-01_SMOKETEST.mov
[26-06-05T12:00:12] JobQueue enqueued 01-01-01_SMOKETEST
[26-06-05T12:00:20] CREATE CAM1 quadrant
[26-06-05T12:00:50] REEL 01-01-01_SMOKETEST start
[26-06-05T12:01:10] CREATE 12 clips
[26-06-05T12:01:30] ARCHIVE AUDIO 01-01-01_SMOKETEST
[26-06-05T12:02:00] no files pending; idle
"""


def test_parse_timeline_total_wall():
    t = sq.parse_timeline(_LOG)
    assert t['detect'] == '26-06-05T12:00:10'
    assert t['idle'] == '26-06-05T12:02:00'
    assert t['total_wall_s'] == 110.0


def test_parse_timeline_stage_deltas():
    t = sq.parse_timeline(_LOG)
    assert t['stages']['detect->enqueue'] == 2.0
    assert t['stages']['enqueue->quads'] == 8.0
    assert t['stages']['quads->reel'] == 30.0
    assert t['stages']['reel->clips'] == 20.0
    assert t['stages']['clips->audio'] == 20.0
    assert t['stages']['audio->idle'] == 30.0


def test_parse_timeline_no_detect_returns_empty():
    assert sq.parse_timeline('[26-06-05T12:00:00] just chatter\n') == {}


def test_parse_timeline_missing_milestones_absent():
    log = (
        '[26-06-05T12:00:10] DETECTED raw .mov x.mov\n'
        '[26-06-05T12:00:20] CREATE CAM1 quadrant\n'
    )
    t = sq.parse_timeline(log)
    assert 'idle' not in t
    assert 'total_wall_s' not in t
    assert t['stages'] == {'detect->quads': 10.0}


def test_parse_timeline_ignores_garbage_lines():
    log = (
        'not a log line at all\n'
        '[bad-timestamp] DETECTED raw .mov x.mov\n'
        '[26-06-05T12:00:10] DETECTED raw .mov x.mov\n'
        '[26-06-05T12:00:40] no files pending\n'
    )
    t = sq.parse_timeline(log)
    assert t['total_wall_s'] == 30.0


# ---------------------------------------------------------------------------
# discover_outputs
# ---------------------------------------------------------------------------

def _touch(p: pathlib.Path, size: int = 16) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'\0' * size)


def test_discover_outputs_full(tmp_path):
    perf = '01-01-01_SMOKETEST'
    videos = tmp_path / 'videos'
    clips = tmp_path / 'clips'
    audio = tmp_path / 'audio'
    for cam in sq.CAM_LABELS:
        _touch(videos / f'{perf}_{cam}.mp4')
    _touch(videos / f'{perf}_INSTAGRAM.mp4')
    _touch(audio / f'{perf}_AUDIO.mp3')
    _touch(audio / f'{perf}_MULTITRACK.zip')
    for i in range(3):
        _touch(clips / f'{perf}.1' / f'clip{i}.mp4', size=100)

    found = sq.discover_outputs(perf, videos, clips, audio)
    assert set(found) == {*sq.CAM_LABELS, 'INSTAGRAM', 'AUDIO_MP3',
                          'MULTITRACK_ZIP', 'clips'}
    assert found['clips']['count'] == 3
    assert found['clips']['total_size'] == 300
    assert found['clips']['sample'] is not None


def test_discover_outputs_perf_index_tolerance(tmp_path):
    # raw '01-01-01_SMOKETEST.1.mov' carries '.1' into the quads/reel; the audio
    # outputs drop it. A single perf stem must match both tiers.
    perf = '01-01-01_SMOKETEST'
    videos = tmp_path / 'videos'
    clips = tmp_path / 'clips'
    audio = tmp_path / 'audio'
    for cam in sq.CAM_LABELS:
        _touch(videos / f'{perf}.1_{cam}.mp4')
    _touch(videos / f'{perf}.1_INSTAGRAM.mp4')
    _touch(audio / f'{perf}_AUDIO.mp3')
    _touch(audio / f'{perf}_MULTITRACK.zip')
    for i in range(2):
        _touch(clips / f'{perf}.1' / f'clip{i}.mp4', size=100)

    found = sq.discover_outputs(perf, videos, clips, audio)
    assert set(found) == {*sq.CAM_LABELS, 'INSTAGRAM', 'AUDIO_MP3',
                          'MULTITRACK_ZIP', 'clips'}
    assert found['clips']['count'] == 2


def test_find_output_audio_label_not_matched_by_multitrack(tmp_path):
    # 'AUDIO.mp3' must not spuriously match '..._MULTITRACK_AUDIO.mp3'.
    perf = '01-01-01_SMOKETEST'
    _touch(tmp_path / f'{perf}_MULTITRACK_AUDIO.mp3')
    assert sq._find_output(tmp_path, perf, 'AUDIO.mp3') is None
    assert sq._find_output(tmp_path, perf, 'MULTITRACK_AUDIO.mp3') is not None


def test_discover_outputs_multitrack_mp3_variant(tmp_path):
    perf = '01-01-01_SMOKETEST'
    videos = tmp_path / 'v'
    clips = tmp_path / 'c'
    audio = tmp_path / 'a'
    _touch(audio / f'{perf}_MULTITRACK_AUDIO.mp3')
    found = sq.discover_outputs(perf, videos, clips, audio)
    assert 'AUDIO_MP3' in found


def test_discover_outputs_missing_is_absent(tmp_path):
    found = sq.discover_outputs('01-01-01_SMOKETEST',
                                tmp_path / 'v', tmp_path / 'c', tmp_path / 'a')
    assert found == {}


# ---------------------------------------------------------------------------
# _unc_root / _nas_auth
# ---------------------------------------------------------------------------

def test_unc_root_from_string_forms():
    assert sq._unc_root(r'\\host\share\a\b') == r'\\host\share'
    assert sq._unc_root('//host/share/a/b') == r'\\host\share'
    assert sq._unc_root(pathlib.PurePosixPath('//192.168.0.232/nofun-archive/videos')) \
        == r'\\192.168.0.232\nofun-archive'


def test_unc_root_local_path_is_none():
    assert sq._unc_root(pathlib.PurePath('D:/clips')) is None
    assert sq._unc_root('/var/data') is None
    assert sq._unc_root(r'\\host') is None  # host only, no share


def test_nas_auth_noop_without_password(monkeypatch):
    calls = []
    monkeypatch.setattr(sq.sys, 'platform', 'win32')
    monkeypatch.setattr(sq.subprocess, 'run',
                        lambda *a, **k: calls.append(a) or _Ok())
    with sq._nas_auth({r'\\h\s'}, 'nofunadmin', None):
        pass
    assert calls == []  # no creds -> never shells out


def test_nas_auth_noop_off_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(sq.sys, 'platform', 'darwin')
    monkeypatch.setattr(sq.subprocess, 'run',
                        lambda *a, **k: calls.append(a) or _Ok())
    with sq._nas_auth({r'\\h\s'}, 'nofunadmin', 'pw'):
        pass
    assert calls == []


def test_nas_auth_connects_and_tears_down(monkeypatch):
    cmds = []

    def fake_run(argv, *a, **k):
        cmds.append(argv)
        return _Ok()

    monkeypatch.setattr(sq.sys, 'platform', 'win32')
    monkeypatch.setattr(sq.subprocess, 'run', fake_run)
    with sq._nas_auth({r'\\h\s'}, 'nofunadmin', 'pw'):
        pass
    assert cmds[0] == ['net', 'use', r'\\h\s', '/user:nofunadmin', 'pw']
    assert cmds[1] == ['net', 'use', r'\\h\s', '/delete', '/y']


def test_nas_auth_skips_teardown_when_connect_fails(monkeypatch):
    cmds = []

    def fake_run(argv, *a, **k):
        cmds.append(argv)
        return _Fail()  # already-connected root -> net use returns nonzero

    monkeypatch.setattr(sq.sys, 'platform', 'win32')
    monkeypatch.setattr(sq.subprocess, 'run', fake_run)
    with sq._nas_auth({r'\\h\s'}, 'nofunadmin', 'pw'):
        pass
    assert cmds == [['net', 'use', r'\\h\s', '/user:nofunadmin', 'pw']]


class _Ok:
    returncode = 0


class _Fail:
    returncode = 2


# ---------------------------------------------------------------------------
# flatten_metrics / aggregate
# ---------------------------------------------------------------------------

def _record(wall, cam1_size, cam1_ssim, clip_count):
    return {
        'timing': {
            'total_wall_s': wall,
            'stages': {'detect->quads': 5.0},
        },
        'outputs': {
            'CAM1': {'size': cam1_size, 'ssim': cam1_ssim},
            'clips': {'count': clip_count, 'total_size': clip_count * 10},
        },
    }


def test_flatten_metrics():
    m = sq.flatten_metrics(_record(100.0, 5000, 0.98, 12))
    assert m['total_wall_s'] == 100.0
    assert m['stage:detect->quads'] == 5.0
    assert m['CAM1.size'] == 5000
    assert m['CAM1.ssim'] == 0.98
    assert m['clips.count'] == 12
    assert m['clips.total_size'] == 120


def test_flatten_metrics_skips_none_ssim():
    m = sq.flatten_metrics({'outputs': {'CAM1': {'size': 1, 'ssim': None}}})
    assert 'CAM1.size' in m
    assert 'CAM1.ssim' not in m


def test_aggregate_stats():
    recs = [_record(100.0, 5000, 0.98, 12),
            _record(110.0, 5200, 0.96, 12)]
    stats = sq.aggregate(recs)
    assert stats['total_wall_s']['n'] == 2
    assert stats['total_wall_s']['mean'] == 105.0
    assert stats['total_wall_s']['median'] == 105.0
    assert stats['total_wall_s']['min'] == 100.0
    assert stats['total_wall_s']['max'] == 110.0
    assert stats['total_wall_s']['stdev'] == pytest.approx(5.0)
    assert stats['clips.count']['stdev'] == 0.0


def test_aggregate_single_record_zero_stdev():
    stats = sq.aggregate([_record(100.0, 5000, 0.98, 12)])
    assert stats['total_wall_s']['stdev'] == 0.0
    assert stats['total_wall_s']['n'] == 1
