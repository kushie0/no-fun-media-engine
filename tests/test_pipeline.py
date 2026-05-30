"""Integration tests for media_engine.py.

Runs the pipeline on test_files/ in trial mode using the committed synthetic
source files (26-01-01_TestBand.mov / .wav).  Requires ffmpeg/ffprobe on PATH.

Run with:
    pytest tests/test_pipeline.py -v --integration
    pytest tests/test_pipeline.py -k "quadrant" --integration
    pytest tests/test_pipeline.py -k "audio" --integration
"""

import logging
import pathlib
import subprocess
import zipfile as zf
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO  = pathlib.Path(__file__).parent.parent
TF    = REPO / 'test_files'
TSECS = 10

COMMITTED_FILES = {'26-01-01_TestBand.mov', '26-01-01_TestBand.wav'}

pytestmark_video = pytest.mark.integration
pytestmark_audio = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe(path: pathlib.Path, entry: str, stream: str = 'v:0') -> str:
    return subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', stream,
         '-show_entries', f'stream={entry}', '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True,
    ).stdout.strip()


def _probe_format(path: pathlib.Path, entry: str) -> str:
    return subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', f'format={entry}',
         '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Quadrant tests
# ---------------------------------------------------------------------------

@pytestmark_video
def test_quadrants_exist(video_trial):
    """One UL/UR/LL/LR.mp4 produced per source .mov."""
    vids = video_trial / 'trial_runs' / 'videos'
    for mov in TF.glob('*.mov'):
        for q in ('UL', 'UR', 'LL', 'LR'):
            expected = vids / f'{mov.stem}_{q}.mp4'
            assert expected.exists(), f"Missing: {expected.name}"


@pytestmark_video
def test_quadrant_codec(video_trial):
    """All quadrant files are H.264."""
    vids = video_trial / 'trial_runs' / 'videos'
    for mp4 in vids.glob('*_UL.mp4'):
        codec = _probe(mp4, 'codec_name')
        assert codec == 'h264', f"{mp4.name}: expected h264, got {codec}"


@pytestmark_video
def test_quadrant_duration(video_trial):
    """Quadrant duration is within 1.5 s of trial length."""
    vids = video_trial / 'trial_runs' / 'videos'
    for mp4 in vids.glob('*_UL.mp4'):
        dur = float(_probe_format(mp4, 'duration') or '0')
        assert 0 < dur <= TSECS + 1.5, (
            f"{mp4.name}: duration {dur:.2f}s, expected >0 and <={TSECS + 1.5}s"
        )


@pytestmark_video
def test_quadrant_resolution(video_trial):
    """Each quadrant is exactly half the source width and height.

    We don't know the source resolution up front, but all four quadrants
    from the same source must be the same size as each other.
    """
    vids = video_trial / 'trial_runs' / 'videos'
    for mov in TF.glob('*.mov'):
        sizes = {}
        for q in ('UL', 'UR', 'LL', 'LR'):
            mp4 = vids / f'{mov.stem}_{q}.mp4'
            if not mp4.exists():
                continue
            w = _probe(mp4, 'width')
            h = _probe(mp4, 'height')
            sizes[q] = (w, h)
        if len(sizes) == 4:
            assert len(set(sizes.values())) == 1, (
                f"{mov.stem}: quadrants have inconsistent resolutions: {sizes}"
            )


@pytestmark_video
def test_no_temp_files_left(video_trial):
    """No _temp.mp4 files remain after successful encode."""
    vids = video_trial / 'trial_runs' / 'videos'
    temps = list(vids.glob('*_temp.mp4'))
    assert temps == [], f"Leftover temp files: {[t.name for t in temps]}"


# ---------------------------------------------------------------------------
# Clip tests
# ---------------------------------------------------------------------------

@pytestmark_video
def test_clips_exist(video_trial):
    """At least one clip produced per source .mov."""
    clips = video_trial / 'trial_runs' / 'clips'
    for mov in TF.glob('*.mov'):
        clip_dir = clips / mov.stem
        mp4s = list(clip_dir.glob('*.mp4')) if clip_dir.exists() else []
        assert mp4s, f"No clips found for {mov.name}"


@pytestmark_video
def test_clip_resolution(video_trial):
    """All clips are 320×180."""
    clips = video_trial / 'trial_runs' / 'clips'
    for mp4 in clips.rglob('*.mp4'):
        w = _probe(mp4, 'width')
        h = _probe(mp4, 'height')
        assert w == '320' and h == '180', f"{mp4.name}: {w}×{h}"


@pytestmark_video
def test_clip_fps(video_trial):
    """All clips are 30 fps."""
    clips = video_trial / 'trial_runs' / 'clips'
    for mp4 in clips.rglob('*.mp4'):
        fps = _probe(mp4, 'r_frame_rate')
        assert fps == '30/1', f"{mp4.name}: fps {fps}"


@pytestmark_video
def test_clip_no_audio(video_trial):
    """Clips must have no audio stream."""
    clips = video_trial / 'trial_runs' / 'clips'
    for mp4 in clips.rglob('*.mp4'):
        streams = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=index', '-of', 'csv=p=0', str(mp4)],
            capture_output=True, text=True,
        ).stdout.strip()
        assert streams == '', f"{mp4.name} has audio"


@pytestmark_video
def test_clip_codec(video_trial):
    """All clips use H.264."""
    clips = video_trial / 'trial_runs' / 'clips'
    for mp4 in clips.rglob('*.mp4'):
        codec = _probe(mp4, 'codec_name')
        assert codec == 'h264', f"{mp4.name}: expected h264, got {codec}"


# ---------------------------------------------------------------------------
# Audio tests
# ---------------------------------------------------------------------------

@pytestmark_audio
def test_audio_zip_created(audio_trial):
    """At least one ZIP archive is created."""
    audio = audio_trial / 'trial_runs' / 'audio'
    zips = list(audio.glob('*.zip')) if audio.is_dir() else []
    assert zips, "No ZIP archive created"


@pytestmark_audio
def test_audio_zip_contains_wavs(audio_trial):
    """Each ZIP contains at least one .wav file."""
    audio = audio_trial / 'trial_runs' / 'audio'
    if not audio.is_dir():
        pytest.skip("No audio output directory")
    for archive in audio.glob('*.zip'):
        with zf.ZipFile(archive) as z:
            wavs = [n for n in z.namelist() if n.endswith('.wav')]
            assert wavs, f"{archive.name} has no .wav files"


@pytestmark_audio
def test_test_files_not_contaminated(audio_trial):  # noqa: ARG001
    """test_files/ must contain exactly the committed files after an audio trial run.

    Fails if audio_trial's temp-dir copy was bypassed or the pipeline wrote
    new files into the committed source directory.
    """
    actual = {f.name for f in TF.iterdir() if f.is_file()}
    assert actual == COMMITTED_FILES, (
        f"test_files/ contaminated — "
        f"extra: {actual - COMMITTED_FILES!r}  "
        f"missing: {COMMITTED_FILES - actual!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests — remaster status tracker (no ffmpeg required)
# ---------------------------------------------------------------------------

class _FakePipelineRemaster:
    """Minimal Pipeline stand-in for testing _do_remaster_for_band and _do_reel_for_perf."""
    logger:        Any
    _remaster_status: dict

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.audio_dest    = tmp_path / 'audio'
        self.vids_dest     = tmp_path / 'videos'
        self.audio_archive = tmp_path / 'audio_archive'
        self.sharepoint_dest = None
        self.logger        = logging.getLogger('test_remaster')
        self._remaster_status: dict[str, str] = {}
        self._script_runner = None
        self._app           = None
        for d in (self.audio_dest, self.vids_dest, self.audio_archive):
            d.mkdir(parents=True, exist_ok=True)

    def _set_op(self, key: str, text: str) -> None: ...
    def _clear_op(self, key: str) -> None: ...
    def _find_date_folder(self, *a: Any) -> None: return None
    def _show_status_list(self) -> None: ...
    _active_menu: Any = None


class TestRemasterStatusTracker:
    def _make(self, tmp_path: pathlib.Path) -> _FakePipelineRemaster:
        from media_engine import Pipeline
        fp = _FakePipelineRemaster(tmp_path)
        fp._do_remaster_for_band = Pipeline._do_remaster_for_band.__get__(fp)
        fp._do_reel_for_perf    = Pipeline._do_reel_for_perf.__get__(fp)
        return fp

    def test_no_zip_marks_status(self, tmp_path: pathlib.Path) -> None:
        from nofun.inventory import PerformanceState
        fp = self._make(tmp_path)
        ps = PerformanceState(date='2026-05-13', band='NoFun')
        ps.zip_files = []
        fp._do_remaster_for_band('2026-05-13', ps)
        assert fp._remaster_status.get('26-05-13_NoFun') == 'no_zip'

    def test_reel_logs_upstream_cause_when_no_zip(
        self, tmp_path: pathlib.Path, caplog: Any
    ) -> None:
        fp = self._make(tmp_path)
        fp._remaster_status['26-05-13_NoFun'] = 'no_zip'
        with caplog.at_level(logging.WARNING, logger='test_remaster'):
            fp._do_reel_for_perf('26-05-13_NoFun')
        warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any('upstream REMASTER had no ZIP' in m for m in warns)

    def test_reel_skip_with_unknown_status_still_logs(
        self, tmp_path: pathlib.Path, caplog: Any
    ) -> None:
        fp = self._make(tmp_path)
        with caplog.at_level(logging.WARNING, logger='test_remaster'):
            fp._do_reel_for_perf('26-05-13_Mystery')
        warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any('REMASTER status: unknown' in m for m in warns)

    def test_multitrack_zip_base_strips_suffix(self, tmp_path: pathlib.Path) -> None:
        # Regression: real ZIPs are named <perf>_MULTITRACK.zip; base must not
        # carry _MULTITRACK into the output filename so REEL can find _AUDIO.mp3.
        from nofun.inventory import PerformanceState
        fp = self._make(tmp_path)
        ps = PerformanceState(date='2026-05-28', band='Jermey_Gold')
        zip_path = tmp_path / 'audio' / '26-05-28_Jermey_Gold_MULTITRACK.zip'
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.touch()
        ps.zip_files = [zip_path]
        # Pre-create the canonical audio file so the existing_fullset path is taken
        # (avoids numpy/scipy dependency while still exercising the base derivation).
        canonical = fp.audio_dest / '26-05-28_Jermey_Gold_AUDIO.mp3'
        canonical.touch()
        fp._do_remaster_for_band('2026-05-28', ps)
        assert fp._remaster_status.get('26-05-28_Jermey_Gold') == 'ok'

    def test_zip_with_space_uses_canonical_underscore_base(self, tmp_path: pathlib.Path) -> None:
        # Regression: ZIPs can be named with a space ("26-05-13_Mall Goth_MULTITRACK.zip"),
        # but the canonical perf key (ps.band) uses underscores. base must follow the
        # perf key so the master is named "26-05-13_Mall_Goth_AUDIO.mp3" — the name REEL
        # and existing_fullset look up. (Deriving base from the ZIP stem produced a
        # space-named master and REEL skipped: "AUDIO not found".)
        from nofun.inventory import PerformanceState
        fp = self._make(tmp_path)
        ps = PerformanceState(date='2026-05-13', band='Mall_Goth')
        zip_path = tmp_path / 'audio' / '26-05-13_Mall Goth_MULTITRACK.zip'   # space!
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.touch()
        ps.zip_files = [zip_path]
        # Canonical underscore name present → existing_fullset path taken (no numpy).
        # If base were derived from the space-named ZIP, this lookup would miss and the
        # empty .touch()'d ZIP would fail extraction → status 'mastering_error'.
        (fp.audio_dest / '26-05-13_Mall_Goth_AUDIO.mp3').touch()
        fp._do_remaster_for_band('2026-05-13', ps)
        assert fp._remaster_status.get('26-05-13_Mall_Goth') == 'ok'

    def test_stale_zip_path_no_match_marks_no_zip(self, tmp_path: pathlib.Path) -> None:
        # Regression (Bug C): the encoding-DB path can be stale — e.g. it lost
        # the _MULTITRACK suffix ("26-05-04_FORCE_RAVE.zip") so the file does not
        # exist. With no real ZIP matching the perf key on disk, mark no_zip
        # rather than crashing with FileNotFoundError opening the dead path.
        from nofun.inventory import PerformanceState
        fp = self._make(tmp_path)
        ps = PerformanceState(date='2026-05-04', band='FORCE_RAVE')
        ps.zip_files = [fp.audio_dest / '26-05-04_FORCE_RAVE.zip']  # stale, doesn't exist
        fp._do_remaster_for_band('2026-05-04', ps)
        assert fp._remaster_status.get('26-05-04_FORCE_RAVE') == 'no_zip'

    def test_stale_zip_path_resolves_real_multitrack(
        self, tmp_path: pathlib.Path, caplog: Any
    ) -> None:
        # Regression (Bug C): stored path is stale, but the real
        # <perf>_MULTITRACK.zip is on disk — resolve to it instead of the dead
        # path. (Canonical AUDIO present so existing_fullset is taken, avoiding
        # the numpy/scipy mastering dependency; the resolve log proves the fix.)
        from nofun.inventory import PerformanceState
        fp = self._make(tmp_path)
        ps = PerformanceState(date='2026-05-04', band='FORCE_RAVE')
        ps.zip_files = [fp.audio_dest / '26-05-04_FORCE_RAVE.zip']         # stale path
        (fp.audio_dest / '26-05-04_FORCE_RAVE_MULTITRACK.zip').touch()     # real file present
        (fp.audio_dest / '26-05-04_FORCE_RAVE_AUDIO.mp3').touch()          # existing_fullset → ok
        with caplog.at_level(logging.INFO, logger='test_remaster'):
            fp._do_remaster_for_band('2026-05-04', ps)
        assert fp._remaster_status.get('26-05-04_FORCE_RAVE') == 'ok'
        assert any('resolved stale ZIP path' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Unit tests — _finish_incomplete_shows reconciler (no ffmpeg required)
# ---------------------------------------------------------------------------

class _FakeQueue:
    def __init__(self, active_keys: list[str] | None = None) -> None:
        self._active = [type('J', (), {'manifest_key': k})() for k in (active_keys or [])]

    def all_active(self) -> list:
        return self._active


class _FakePipelineFinish:
    """Minimal Pipeline stand-in for _finish_incomplete_shows (no ffmpeg/DB)."""

    def __init__(self, tmp_path: pathlib.Path, active_keys: list[str] | None = None) -> None:
        self.audio_dest = tmp_path / 'audio'
        self.vids_dest  = tmp_path / 'videos'
        for d in (self.audio_dest, self.vids_dest):
            d.mkdir(parents=True, exist_ok=True)
        self.logger          = logging.getLogger('test_finish')
        self._status_entries: list = []
        self._enqueued_keys: set   = set()
        self._remaster_status: dict = {}
        self._job_queue            = _FakeQueue(active_keys)
        self.remaster_calls: list  = []

    def _rebuild_status_entries(self) -> bool:
        # No-op for unit tests: _status_entries is set directly by each test.
        return bool(self._status_entries)

    def _enqueue_remaster(self, date: str, band: str | None = None,
                          reel_overwrite: bool = True, **_: Any) -> None:
        self.remaster_calls.append((date, band, reel_overwrite))


class TestFinishIncompleteShows:
    def _make(self, tmp_path: pathlib.Path, active_keys: list[str] | None = None):
        from media_engine import Pipeline
        fp = _FakePipelineFinish(tmp_path, active_keys)
        fp._finish_incomplete_shows = Pipeline._finish_incomplete_shows.__get__(fp)
        return fp

    @staticmethod
    def _recent_date(days_ago: int = 1) -> str:
        import datetime
        return f'{datetime.date.today() - datetime.timedelta(days=days_ago):%y-%m-%d}'

    @staticmethod
    def _ps(date: str, band: str):
        from nofun.inventory import PerformanceState
        ps = PerformanceState(date=date, band=band)
        ps.zip_files  = [pathlib.Path(f'{date}_{band}_MULTITRACK.zip')]
        ps.quad_files = [pathlib.Path(f'{date}_{band}_CAM{n}.mp4') for n in (1, 2, 3, 4)]
        return ps

    def test_missing_both_enqueues_with_overwrite_false(self, tmp_path):
        date = self._recent_date()
        fp = self._make(tmp_path)
        fp._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == [(date, 'Jermey_Gold', False)]

    def test_already_finished_skipped(self, tmp_path):
        date = self._recent_date()
        fp = self._make(tmp_path)
        perf = f'{date}_Jermey_Gold'
        (fp.audio_dest / f'{perf}_AUDIO.mp3').write_bytes(b'x')
        (fp.vids_dest / f'{perf}.0_INSTAGRAM.mp4').write_bytes(b'x')
        fp._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == []

    def test_missing_reel_only_enqueues(self, tmp_path):
        date = self._recent_date()
        fp = self._make(tmp_path)
        perf = f'{date}_Jermey_Gold'
        (fp.audio_dest / f'{perf}_AUDIO.mp3').write_bytes(b'x')  # master present, reel absent
        fp._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == [(date, 'Jermey_Gold', False)]

    def test_nofun_band_skipped(self, tmp_path):
        date = self._recent_date()
        fp = self._make(tmp_path)
        fp._status_entries = [((date, 'NOFUN'), self._ps(date, 'NOFUN'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == []

    def test_too_old_skipped(self, tmp_path):
        date = self._recent_date(days_ago=40)
        fp = self._make(tmp_path)
        fp._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == []

    def test_no_zip_or_few_quads_skipped(self, tmp_path):
        from nofun.inventory import PerformanceState
        date = self._recent_date()
        fp = self._make(tmp_path)
        no_zip = PerformanceState(date=date, band='Jermey_Gold')
        no_zip.zip_files  = []
        no_zip.quad_files = [pathlib.Path('a'), pathlib.Path('b'), pathlib.Path('c'), pathlib.Path('d')]
        few_quads = self._ps(date, 'Other')
        few_quads.quad_files = few_quads.quad_files[:2]
        fp._status_entries = [
            ((date, 'Jermey_Gold'), no_zip),
            ((date, 'Other'), few_quads),
        ]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == []

    def test_already_in_flight_skipped(self, tmp_path):
        date = self._recent_date()
        # active REMASTER manifest for this perf
        fp = self._make(tmp_path, active_keys=[f'{date}_Jermey_Gold_REMASTER'])
        fp._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp._finish_incomplete_shows()
        assert fp.remaster_calls == []

        # main manifest still enqueued
        fp2 = self._make(tmp_path)
        fp2._enqueued_keys = {f'{date}_Jermey_Gold'}
        fp2._status_entries = [((date, 'Jermey_Gold'), self._ps(date, 'Jermey_Gold'))]
        fp2._finish_incomplete_shows()
        assert fp2.remaster_calls == []

    def test_terminal_remaster_failure_not_requeued(self, tmp_path):
        # Regression (Bug B): a show whose REMASTER failed terminally this
        # session must not be re-queued every hour. The reconciler ran hourly
        # and piled up duplicate REMASTER+REEL jobs for shows that could never
        # complete (no usable ZIP / mastering crash), draining in a burst.
        date = self._recent_date()
        for status in ('mastering_error', 'no_zip', 'zip_empty'):
            fp = self._make(tmp_path)
            fp._status_entries = [((date, 'Flatwounds'), self._ps(date, 'Flatwounds'))]
            fp._remaster_status = {f'{date}_Flatwounds': status}
            fp._finish_incomplete_shows()
            assert fp.remaster_calls == [], f'should skip after {status}'
