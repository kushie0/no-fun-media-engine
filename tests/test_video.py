"""Unit tests for nofun/video.py (VideoMixin)."""

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from nofun.video import (
    CLIP_FILTER, MIN_QUAD, QUAD_FILTER, SINGLE_FILTER, STEP_SECONDS,
    build_encoder_config,
)
from nofun.script_runner import ScriptResult


from tests.fake_pipeline import FakePipeline as _FakePipeline


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_step_seconds(self):
        assert STEP_SECONDS == 40

    def test_quad_filter_splits_4(self):
        assert 'split=4' in QUAD_FILTER

    def test_quad_filter_has_four_corners(self):
        for corner in ('[ul]', '[ur]', '[ll]', '[lr]'):
            assert corner in QUAD_FILTER

    def test_clip_filter_resolution(self):
        assert 'scale=320:180' in CLIP_FILTER

    def test_clip_filter_fps(self):
        assert 'fps=30' in CLIP_FILTER

    def test_min_quad_has_required_encoders(self):
        assert 'h264_amf'          in MIN_QUAD
        assert 'h264_videotoolbox' in MIN_QUAD
        assert 'libx264'           in MIN_QUAD

    def test_min_quad_amf_floor_is_128_64(self):
        assert MIN_QUAD['h264_amf'] == (128, 64)


# ---------------------------------------------------------------------------
# TestEncodeQuadrants
# ---------------------------------------------------------------------------

class TestEncodeQuadrants:
    def test_deletes_temp_files_on_failure(self, tmp_path):
        """When the script returns nonzero, any partial temp files are removed."""
        fp = _FakePipeline(tmp_path)
        source = tmp_path / '26-01-01_TestBand.mov'
        source.write_bytes(b'\x00')

        base  = source.stem
        quads = ('CAM1', 'CAM2', 'CAM3', 'CAM4')
        for q in quads:
            (fp.vids_dest / f'{base}_{q}_temp.mp4').write_bytes(b'\x00')

        fp._script_runner.run.return_value = ScriptResult(
            script='encode_quads', exit_code=1,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )

        with patch('nofun.video.probe_stream', return_value='h264'):
            result = fp._encode_quadrants(source)

        assert result is False
        for q in quads:
            assert not (fp.vids_dest / f'{base}_{q}_temp.mp4').exists()

    def test_renames_temp_to_final_on_success(self, tmp_path):
        """When the script returns 0, _temp files are renamed to final names."""
        fp = _FakePipeline(tmp_path)
        source = tmp_path / '26-01-01_TestBand.mov'
        source.write_bytes(b'\x00')
        base  = source.stem
        quads = ('CAM1', 'CAM2', 'CAM3', 'CAM4')

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            for q in quads:
                (fp.vids_dest / f'{base}_{q}_temp.mp4').write_bytes(b'\x00')
            return ScriptResult(
                script='encode_quads', exit_code=0,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run

        with patch('nofun.video.probe_stream', return_value='h264'):
            result = fp._encode_quadrants(source)

        assert result is True
        for q in quads:
            assert (fp.vids_dest / f'{base}_{q}.mp4').exists()
            assert not (fp.vids_dest / f'{base}_{q}_temp.mp4').exists()

    def test_db_record_quads_writes_runtime_seconds(self, tmp_path):
        """After upserting all four quads, runtime_seconds is cached at band level."""
        from nofun.encoding_db import EncodingDB

        fp = _FakePipeline(tmp_path)
        fp._encoding_db = EncodingDB(tmp_path / 'encoding_db.json')
        base = '26-01-01_TestBand'
        dests = {q: fp.vids_dest / f'{base}_{q}.mp4' for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4')}
        for p in dests.values():
            p.write_bytes(b'\x00')

        with patch(
            'nofun.encoding_db.probe_file',
            return_value={'duration': 1500.0, 'codec': 'h264'},
        ):
            fp._db_record_quads(base, dests)

        perf = fp._encoding_db.get_performance('26-01-01', 'TestBand')
        assert perf is not None
        assert perf['runtime_seconds'] == 1500.0
        assert len(perf['quadrant_video']) == 4

    def test_progress_cb_forwards_source_duration_to_app(self, tmp_path):
        """_encode_quadrants probes source duration and threads it through to update_progress."""
        fp = _FakePipeline(tmp_path)
        fp._app = MagicMock()
        source = tmp_path / '26-01-01_TestBand.mov'
        source.write_bytes(b'\x00')

        captured = {}

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured['cb'] = progress_cb
            return ScriptResult(
                script='encode_quads', exit_code=1,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run

        with patch('nofun.video.probe_stream', return_value='h264'), \
             patch('nofun.video.probe_format', return_value='1234.5'):
            fp._encode_quadrants(source)

        assert captured['cb'] is not None, 'progress_cb must be passed to script runner'
        captured['cb']('100', '30', '00:00:10', '2.0x')

        fp._app.update_progress.assert_called_once()
        kwargs = fp._app.update_progress.call_args.kwargs
        assert kwargs.get('duration') == 1234.5


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TestEncodeQuadrantsPreFlight — dimension gate added to _encode_quadrants
# ---------------------------------------------------------------------------

class TestEncodeQuadrantsPreFlight:

    def _make_pipeline(self, tmp_path, encoder='libx264'):
        fp = _FakePipeline(tmp_path)
        fp.enc['enc_quad'] = ['-c:v', encoder, '-preset', 'ultrafast', '-crf', '18']
        # Fail the (mocked) encode so the pre-flight gate is all that runs
        fp._script_runner.run.return_value = ScriptResult(
            script='encode_quads', exit_code=1,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )
        return fp

    def test_returns_false_and_logs_alert_when_too_small_for_amf(self, tmp_path):
        """80×60 source → 40×30 quads, below h264_amf minimum (128×64)."""
        fp = self._make_pipeline(tmp_path, encoder='h264_amf')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        def _fake_probe(path, entry, stream='v:0'):
            return {'codec_name': 'h264', 'width': '160', 'height': '120'}.get(entry, '')

        with patch('nofun.video.probe_stream', side_effect=_fake_probe):
            result = fp._encode_quadrants(src)

        assert result is False
        fp.logger.error.assert_called_once()
        msg = fp.logger.error.call_args[0][0]
        assert 'ALERT' in msg
        assert '80×60' in msg
        assert 'h264_amf' in msg

    def test_returns_false_when_too_small_for_videotoolbox(self, tmp_path):
        """16×16 source → 8×8 quads, below h264_videotoolbox minimum (32×32)."""
        fp = self._make_pipeline(tmp_path, encoder='h264_videotoolbox')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        def _fake_probe(path, entry, stream='v:0'):
            return {'codec_name': 'mjpeg', 'width': '16', 'height': '16'}.get(entry, '')

        with patch('nofun.video.probe_stream', side_effect=_fake_probe):
            result = fp._encode_quadrants(src)

        assert result is False

    def test_proceeds_when_dimensions_meet_amf_minimum(self, tmp_path):
        """256×128 source → 128×64 quads, exactly at h264_amf minimum — allowed."""
        fp = self._make_pipeline(tmp_path, encoder='h264_amf')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        def _fake_probe(path, entry, stream='v:0'):
            return {'codec_name': 'h264', 'width': '256', 'height': '128'}.get(entry, '')

        with patch('nofun.video.probe_stream', side_effect=_fake_probe):
            fp._encode_quadrants(src)

        for call in fp.logger.error.call_args_list:
            assert 'ALERT' not in call[0][0]

    def test_proceeds_when_probe_fails(self, tmp_path):
        """If probe_stream returns '' (ffprobe failure), gate is skipped — no regression."""
        fp = self._make_pipeline(tmp_path, encoder='h264_amf')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        with patch('nofun.video.probe_stream', return_value=''):
            fp._encode_quadrants(src)

        for call in fp.logger.error.call_args_list:
            assert 'ALERT' not in call[0][0]

    def test_libx264_allows_very_small_source(self, tmp_path):
        """4×4 source → 2×2 quads, which meets libx264 minimum (2×2)."""
        fp = self._make_pipeline(tmp_path, encoder='libx264')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        def _fake_probe(path, entry, stream='v:0'):
            return {'codec_name': 'mjpeg', 'width': '4', 'height': '4'}.get(entry, '')

        with patch('nofun.video.probe_stream', side_effect=_fake_probe):
            fp._encode_quadrants(src)

        for call in fp.logger.error.call_args_list:
            assert 'ALERT' not in call[0][0]

    def test_unknown_encoder_uses_conservative_floor(self, tmp_path):
        """An unrecognised encoder falls back to the (2,2) default — does not block."""
        fp = self._make_pipeline(tmp_path, encoder='some_future_encoder')
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')

        def _fake_probe(path, entry, stream='v:0'):
            return {'codec_name': 'h264', 'width': '10', 'height': '10'}.get(entry, '')

        with patch('nofun.video.probe_stream', side_effect=_fake_probe):
            fp._encode_quadrants(src)

        for call in fp.logger.error.call_args_list:
            assert 'ALERT' not in call[0][0]


# Helpers shared by TestExportClips / TestProcessMov
# ---------------------------------------------------------------------------

def _make_quad(fp: _FakePipeline, base: str, quad: str) -> pathlib.Path:
    """Write a dummy quad MP4 to fp.vids_dest."""
    p = fp.vids_dest / f'{base}_{quad}.mp4'
    p.write_bytes(b'\x00' * 16)
    return p


# ---------------------------------------------------------------------------
# TestExportClips — segment-muxer clip export
# ---------------------------------------------------------------------------

class TestExportClips:
    _BASE = '26-01-01_TestBand'

    def test_calls_runner_with_segment_time(self, tmp_path):
        fp   = _FakePipeline(tmp_path)
        _make_quad(fp, self._BASE, 'CAM1')
        captured_jobs: list = []

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured_jobs.append(job)
            return ScriptResult(
                script='export_clips', exit_code=1,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run
        fp._export_clips(self._BASE)

        assert captured_jobs
        assert captured_jobs[0].args['step'] == STEP_SECONDS

    def test_clip_files_created_on_success(self, tmp_path):
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        _make_quad(fp, base, 'CAM1')

        clips_dir = fp.clips_dest / base

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            # Simulate script moving clips directly to final destination
            clips_dir.mkdir(parents=True, exist_ok=True)
            (clips_dir / f'{base}_CAM1_1.mp4').write_bytes(b'\x00')
            (clips_dir / f'{base}_CAM1_2.mp4').write_bytes(b'\x00')
            return ScriptResult(
                script='export_clips', exit_code=0,
                stdout_json={
                    'quads': [{'quad': 'CAM1', 'status': 'ok', 'moved_count': 2}],
                },
                stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run
        fp._export_clips(base)

        assert (clips_dir / f'{base}_CAM1_1.mp4').exists()
        assert (clips_dir / f'{base}_CAM1_2.mp4').exists()

    def test_temp_files_deleted_on_failure(self, tmp_path):
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        _make_quad(fp, base, 'CAM1')
        temp = fp.search_dir / f'{base}_CAM1_temp_1.mp4'

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            temp.write_bytes(b'\x00')
            return ScriptResult(
                script='export_clips', exit_code=1,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run
        fp._export_clips(base)

        assert not temp.exists()

    def test_skips_when_clips_already_exist(self, tmp_path):
        fp      = _FakePipeline(tmp_path)
        base    = self._BASE
        _make_quad(fp, base, 'CAM1')
        clips_dir = fp.clips_dest / base
        clips_dir.mkdir(parents=True)
        (clips_dir / f'{base}_CAM1_1.mp4').write_bytes(b'\x00')

        fp._export_clips(base)

        fp._script_runner.run.assert_not_called()

    def test_force_reruns_even_when_clips_exist(self, tmp_path):
        fp      = _FakePipeline(tmp_path)
        fp.force = True
        base    = self._BASE
        _make_quad(fp, base, 'CAM1')
        clips_dir = fp.clips_dest / base
        clips_dir.mkdir(parents=True)
        (clips_dir / f'{base}_CAM1_1.mp4').write_bytes(b'\x00')

        fp._script_runner.run.return_value = ScriptResult(
            script='export_clips', exit_code=1,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )
        fp._export_clips(base)

        fp._script_runner.run.assert_called_once()

    def test_runner_called_once_regardless_of_quad_count(self, tmp_path):
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        # Only CAM1 exists — runner still called exactly once (script handles discovery)
        _make_quad(fp, base, 'CAM1')

        fp._script_runner.run.return_value = ScriptResult(
            script='export_clips', exit_code=1,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )
        fp._export_clips(base)

        assert fp._script_runner.run.call_count == 1

    def test_resume_passes_per_quad_start(self, tmp_path):
        """Incomplete quad walks from clip 1 so the export script can backfill gaps."""
        import json
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        # CAM1 has 2 clips, CAM2 has 5 — CAM1 is behind; it should walk from 1
        # (the script skips clips already on disk), not resume past the count.
        for q in ('CAM1', 'CAM2'):
            _make_quad(fp, base, q)
        clips_dir = fp.clips_dest / base
        clips_dir.mkdir()
        for i in range(1, 3):
            (clips_dir / f'{base}_CAM1_{i}.mp4').write_bytes(b'\x00')
        for i in range(1, 6):
            (clips_dir / f'{base}_CAM2_{i}.mp4').write_bytes(b'\x00')

        captured: list = []
        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured.append(job.args)
            return ScriptResult(
                script='export_clips', exit_code=0,
                stdout_json={'quads': [{'quad': 'CAM1', 'status': 'ok', 'moved_count': 3}]},
                stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run
        fp._export_clips(base)

        assert captured
        pqs = json.loads(captured[0]['per_quad_start'])
        # CAM2 complete (top=5) → excluded; CAM1 incomplete → walks from 1, not 3.
        assert pqs == {'CAM1': 1}

    def test_resume_backfills_interior_gap(self, tmp_path):
        """A non-tail gap (missing clips 1-2, has 3-5) still walks from 1 so the
        gap gets backfilled — the old resume-by-count logic restarted past the
        count and never refilled early gaps."""
        import json
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        for q in ('CAM1', 'CAM2'):
            _make_quad(fp, base, q)
        clips_dir = fp.clips_dest / base
        clips_dir.mkdir()
        # CAM1 is missing clips 1 and 2 but has 3,4,5 (count=3, interior gap).
        for i in range(3, 6):
            (clips_dir / f'{base}_CAM1_{i}.mp4').write_bytes(b'\x00')
        for i in range(1, 6):
            (clips_dir / f'{base}_CAM2_{i}.mp4').write_bytes(b'\x00')

        captured: list = []
        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured.append(job.args)
            return ScriptResult(
                script='export_clips', exit_code=0,
                stdout_json={'quads': [{'quad': 'CAM1', 'status': 'ok', 'moved_count': 2}]},
                stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run
        fp._export_clips(base)

        assert captured
        pqs = json.loads(captured[0]['per_quad_start'])
        # Must be 1 (not 4): walking from 1 lets the script's skip-existing
        # backfill the missing 1-2 while skipping the present 3-5.
        assert pqs == {'CAM1': 1}

    def test_skip_when_all_quads_complete(self, tmp_path):
        """All quads at same count → skip without calling runner."""
        fp   = _FakePipeline(tmp_path)
        base = self._BASE
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            _make_quad(fp, base, q)
        clips_dir = fp.clips_dest / base
        clips_dir.mkdir()
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            for i in range(1, 6):
                (clips_dir / f'{base}_{q}_{i}.mp4').write_bytes(b'\x00')

        fp._export_clips(base)

        fp._script_runner.run.assert_not_called()


# ---------------------------------------------------------------------------
# TestProcessMov — orchestration of encode + export + archive
# ---------------------------------------------------------------------------

class TestProcessMov:
    _BASE = '26-01-01_TestBand'

    def _make_all_quads(self, fp: _FakePipeline) -> None:
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            _make_quad(fp, self._BASE, q)

    def test_skips_encode_when_all_quads_exist(self, tmp_path):
        fp     = _FakePipeline(tmp_path)
        source = tmp_path / f'{self._BASE}.mov'
        source.write_bytes(b'\x00')
        self._make_all_quads(fp)

        with patch.object(fp, '_encode_quadrants') as mock_enc, \
             patch.object(fp, '_export_clips'):
            fp._process_mov(source)

        mock_enc.assert_not_called()

    def test_export_clips_called_even_when_quads_already_existed(self, tmp_path):
        fp     = _FakePipeline(tmp_path)
        source = tmp_path / f'{self._BASE}.mov'
        source.write_bytes(b'\x00')
        self._make_all_quads(fp)

        with patch.object(fp, '_encode_quadrants', return_value=True), \
             patch.object(fp, '_export_clips') as mock_clips:
            fp._process_mov(source)

        mock_clips.assert_called_once_with(self._BASE)

    def test_returns_false_when_encode_fails(self, tmp_path):
        fp     = _FakePipeline(tmp_path)
        source = tmp_path / f'{self._BASE}.mov'
        source.write_bytes(b'\x00')

        with patch.object(fp, '_encode_quadrants', return_value=False), \
             patch.object(fp, '_export_clips') as mock_clips:
            result = fp._process_mov(source)

        assert result is False
        mock_clips.assert_not_called()

    def test_archives_source_on_real_drive(self, tmp_path):
        fp        = _FakePipeline(tmp_path)
        fp.mount_d = tmp_path  # simulate real D: drive
        source    = tmp_path / f'{self._BASE}.mov'
        source.write_bytes(b'\x00')

        with patch.object(fp, '_encode_quadrants', return_value=True), \
             patch.object(fp, '_export_clips'):
            fp._process_mov(source)

        assert not source.exists()
        assert (fp.video_archive / source.name).exists()

    def test_does_not_archive_source_in_trial_mode(self, tmp_path):
        fp         = _FakePipeline(tmp_path)
        fp.mount_d  = tmp_path
        fp.trial_run = 10
        source     = tmp_path / f'{self._BASE}.mov'
        source.write_bytes(b'\x00')

        with patch.object(fp, '_encode_quadrants', return_value=True), \
             patch.object(fp, '_export_clips'):
            fp._process_mov(source)

        assert source.exists()  # trial mode — source stays


# ---------------------------------------------------------------------------
# TestMjpegAccelBypass — hardware-accel skipped for MJPEG sources
# ---------------------------------------------------------------------------

class TestMjpegAccelBypass:
    _SOURCE_NAME = '26-01-01_TestBand.mov'

    def _source(self, fp: _FakePipeline) -> pathlib.Path:
        p = fp.search_dir / self._SOURCE_NAME
        p.write_bytes(b'\x00')
        return p

    def test_mjpeg_source_omits_accel_args(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        fp.enc['accel'] = ['-hwaccel', 'd3d11va']
        captured_jobs: list = []

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured_jobs.append(job)
            return ScriptResult(
                script='encode_quads', exit_code=1,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run

        with patch('nofun.video.probe_stream', return_value='mjpeg'):
            fp._encode_quadrants(self._source(fp))

        assert captured_jobs, '_script_runner.run was not called'
        assert captured_jobs[0].args['accel'] == 'none'

    def test_non_mjpeg_source_includes_accel_args(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        fp.enc['accel'] = ['-hwaccel', 'd3d11va']
        captured_jobs: list = []

        def _fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            captured_jobs.append(job)
            return ScriptResult(
                script='encode_quads', exit_code=1,
                stdout_json={}, stderr_tail='', elapsed=0.0,
            )

        fp._script_runner.run.side_effect = _fake_run

        with patch('nofun.video.probe_stream', return_value='h264'):
            fp._encode_quadrants(self._source(fp))

        assert captured_jobs[0].args['accel'] == 'd3d11va'

    def test_no_accel_configured_does_not_crash(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        # fp.enc['accel'] is already [] in the stub
        fp._script_runner.run.return_value = ScriptResult(
            script='encode_quads', exit_code=1,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )

        with patch('nofun.video.probe_stream', return_value='h264'):
            result = fp._encode_quadrants(self._source(fp))

        assert result is False


# ---------------------------------------------------------------------------
# TestBuildEncoderConfig — platform-based encoder selection
# ---------------------------------------------------------------------------

class TestBuildEncoderConfig:
    def test_darwin_uses_videotoolbox(self):
        with patch('nofun.video.detect_platform', return_value='darwin'):
            enc = build_encoder_config(gpu=False, trial_run=0)
        assert 'h264_videotoolbox' in enc['enc_quad']
        assert enc['accel'] == []

    def test_darwin_ignores_gpu_flag(self):
        # On macOS, gpu=True still gives videotoolbox (no d3d11va on macOS)
        with patch('nofun.video.detect_platform', return_value='darwin'):
            enc = build_encoder_config(gpu=True, trial_run=0)
        assert 'h264_videotoolbox' in enc['enc_quad']
        assert '-hwaccel' not in enc['accel']

    def test_windows_gpu_uses_amf(self):
        with patch('nofun.video.detect_platform', return_value='windows'):
            enc = build_encoder_config(gpu=True, trial_run=0)
        assert 'h264_amf' in enc['enc_quad']
        assert '-hwaccel' in enc['accel']

    def test_windows_no_gpu_uses_libx264(self):
        with patch('nofun.video.detect_platform', return_value='windows'):
            enc = build_encoder_config(gpu=False, trial_run=0)
        assert 'libx264' in enc['enc_quad']
        assert enc['accel'] == []

    def test_trial_mode_uses_ultrafast_for_cpu(self):
        with patch('nofun.video.detect_platform', return_value='windows'):
            enc = build_encoder_config(gpu=False, trial_run=10)
        assert 'ultrafast' in enc['enc_quad']

    def test_non_trial_uses_veryslow_for_cpu(self):
        with patch('nofun.video.detect_platform', return_value='windows'):
            enc = build_encoder_config(gpu=False, trial_run=0)
        assert 'veryslow' in enc['enc_quad']

    def test_result_has_required_keys(self):
        with patch('nofun.video.detect_platform', return_value='darwin'):
            enc = build_encoder_config()
        assert set(enc.keys()) == {'accel', 'enc_quad', 'enc_clip'}


# ---------------------------------------------------------------------------
# TestTranscodeSingle
# ---------------------------------------------------------------------------

@pytest.fixture
def fp(tmp_path):
    return _FakePipeline(tmp_path)


class TestTranscodeSingle:
    """_transcode_single: write {base}.mp4 atomically via a temp file."""

    def test_single_filter_even_dimensions(self):
        assert 'trunc(iw/2)*2' in SINGLE_FILTER
        assert 'trunc(ih/2)*2' in SINGLE_FILTER

    def test_single_filter_colorspace(self):
        assert 'out_range=limited' in SINGLE_FILTER
        assert 'format=yuv420p' in SINGLE_FILTER

    def test_creates_output_on_success(self, fp, tmp_path):
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')
        base = src.stem
        temp = fp.vids_dest / f'{base}_single_temp.mp4'

        temp.write_bytes(b'\x00')
        fp._script_runner.run.return_value = ScriptResult(
            'transcode_single', 0, {}, '', 0.0
        )
        result = fp._transcode_single(src)

        assert result is True
        assert (fp.vids_dest / f'{base}.mp4').exists()
        assert not temp.exists()   # temp renamed away

    def test_cleans_up_temp_on_failure(self, fp, tmp_path):
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')
        base = src.stem
        temp = fp.vids_dest / f'{base}_single_temp.mp4'

        temp.write_bytes(b'\x00')
        fp._script_runner.run.return_value = ScriptResult(
            'transcode_single', 1, {}, 'err', 0.0
        )
        result = fp._transcode_single(src)

        assert result is False
        assert not temp.exists()
        assert not (fp.vids_dest / f'{base}.mp4').exists()

    def test_no_output_on_failure_without_temp(self, fp, tmp_path):
        src = tmp_path / '26-04-11_ALTAR.mov'
        src.write_bytes(b'')
        fp._script_runner.run.return_value = ScriptResult(
            'transcode_single', 1, {}, 'err', 0.0
        )
        result = fp._transcode_single(src)
        assert result is False
        assert not (fp.vids_dest / '26-04-11_ALTAR.mp4').exists()

    def test_script_job_uses_single_filter(self, fp, tmp_path):
        src = tmp_path / '26-04-11_CAM2.mov'
        src.write_bytes(b'')
        base = src.stem
        temp = fp.vids_dest / f'{base}_single_temp.mp4'
        temp.write_bytes(b'\x00')
        fp._script_runner.run.return_value = ScriptResult(
            'transcode_single', 0, {}, '', 0.0
        )
        fp._transcode_single(src)

        call_args = fp._script_runner.run.call_args[0][0]
        assert call_args.args['filter'] == SINGLE_FILTER
        assert call_args.script == 'transcode_single'
