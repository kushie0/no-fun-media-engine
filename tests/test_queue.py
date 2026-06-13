"""Queue-layer tests (Layer Q).

Tests the JobQueue, dependency logic, pause/resume, schedule gate, manifest
idempotency, multi-performance isolation, and full synthetic batch run.
All tests run in the default pass (no flag needed) using python_fn callables
instead of real ffmpeg — fast, deterministic, sub-second per test.

Import helpers from conftest via the conftest module path.
"""

import logging
import pathlib
import sys
import threading
import time

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from nofun.audio import chan_wav_name
from nofun.inventory import perf_output_name
from nofun.job_queue   import JobCategory, JobQueue, _ENCODE_END_HOUR
from nofun.job_manifest import JobManifest, PipelineJob
from nofun.script_runner import ScriptRunner, ScriptResult
from nofun.video import CAM_LABELS, quad_temp_name

logger = logging.getLogger('test_queue')


# ---------------------------------------------------------------------------
# Helpers (local copy to avoid conftest import headaches)
# ---------------------------------------------------------------------------

def wait_idle(job_queue: JobQueue, timeout: float = 5.0) -> bool:
    return job_queue.wait_drain(timeout)


def ok_result(script: str = '') -> ScriptResult:
    return ScriptResult(script=script, exit_code=0, stdout_json={}, stderr_tail='', elapsed=0.0)


def fail_result(script: str = '') -> ScriptResult:
    return ScriptResult(script=script, exit_code=1, stdout_json={}, stderr_tail='', elapsed=0.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def q(tmp_path):
    runner = ScriptRunner(logger)
    return JobQueue(runner=runner, logger=logger)


# ---------------------------------------------------------------------------
# TestJobQueueDirect — tests JobQueue in isolation with python_fn callables
# ---------------------------------------------------------------------------

class TestJobQueueDirect:

    def test_dependency_ordering(self, q):
        """Job B (depends on A) is not runnable until A completes."""
        order = []
        job_a = PipelineJob(kind='_a', label='A', priority=10)
        job_b = PipelineJob(kind='_b', label='B', priority=30,
                          depends=[job_a.job_id])

        manifest = JobManifest(
            performance_key='26-04-11_ALTAR',
            jobs=[job_a, job_b],
            python_fns={
                job_a.job_id: lambda: order.append('a'),
                job_b.job_id: lambda: order.append('b'),
            },
        )
        q.enqueue(manifest, JobCategory.GPU_BOUND)

        # B must not be next — only A is runnable
        assert q.next_runnable(JobCategory.GPU_BOUND).job_id == job_a.job_id

        q.dispatch_one(JobCategory.GPU_BOUND)   # runs A

        # Now B is runnable
        assert q.next_runnable(JobCategory.GPU_BOUND).job_id == job_b.job_id

        q.dispatch_one(JobCategory.GPU_BOUND)   # runs B

        assert order == ['a', 'b']

    def test_failed_job_blocks_downstream(self, q):
        """A failed job does NOT satisfy its dependents (unlike cancelled)."""
        job_a = PipelineJob(kind='_a', label='encode', priority=10)
        job_b = PipelineJob(kind='_b', label='clips', priority=30,
                          depends=[job_a.job_id])

        manifest = JobManifest(
            performance_key='26-04-11_ALTAR',
            jobs=[job_a, job_b],
            python_fns={
                job_a.job_id: lambda: (_ for _ in ()).throw(RuntimeError('encode crashed')),
                job_b.job_id: lambda: None,
            },
        )
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        q.dispatch_one(JobCategory.GPU_BOUND)   # A → fails

        assert q.recent_history()[-1].status == 'failed'

        # B still pending; A is failed so B's dep is unsatisfied
        assert q.next_runnable(JobCategory.GPU_BOUND) is None

    def test_permission_error_schedules_retry(self, q):
        """PermissionError on first call: job returns to pending with retry_after set."""
        attempt = [0]

        def _flaky():
            attempt[0] += 1
            if attempt[0] == 1:
                raise PermissionError('file locked')

        job = PipelineJob(kind='_lock', label='flaky')
        manifest = JobManifest('26-04-11_ALTAR', [job], {job.job_id: _flaky})
        q.enqueue(manifest, JobCategory.MANUAL)

        result = q.dispatch_one(JobCategory.MANUAL)
        assert result is None                         # retry scheduled, not done

        active = q.all_active()
        assert len(active) == 1
        assert active[0].retry_count == 1
        assert active[0].retry_after is not None      # backoff set
        assert active[0].status == 'pending'

    def test_permission_error_exhaustion_marks_failed(self, q):
        """After max_retries PermissionErrors, job is marked failed."""
        def _always_locked():
            raise PermissionError('forever locked')

        job = PipelineJob(kind='_lock', label='locked')
        manifest = JobManifest('26-04-11_ALTAR', [job], {job.job_id: _always_locked})
        q.enqueue(manifest, JobCategory.MANUAL)

        qj = q.all_active()[0]
        qj.max_retries = 2

        # Exhaust retries by clearing retry_after between attempts
        for _ in range(3):
            qj.retry_after = None
            q.dispatch_one(JobCategory.MANUAL)

        assert q.recent_history()[-1].status == 'failed'

    def test_pause_blocks_new_dispatch(self, q):
        """After pause(), next_runnable() returns None regardless of pending jobs."""
        job = PipelineJob(kind='_a', label='work')
        manifest = JobManifest('26-04-11_ALTAR', [job], {job.job_id: lambda: None})
        q.enqueue(manifest, JobCategory.GPU_BOUND)

        q.pause()
        assert q.next_runnable(JobCategory.GPU_BOUND) is None

    def test_resume_unblocks_dispatch(self, q):
        """After resume(), pending jobs become runnable again."""
        job = PipelineJob(kind='_a', label='work')
        manifest = JobManifest('26-04-11_ALTAR', [job], {job.job_id: lambda: None})
        q.enqueue(manifest, JobCategory.GPU_BOUND)

        q.pause()
        q.resume()
        assert q.next_runnable(JobCategory.GPU_BOUND) is not None

    def test_cancelled_dep_satisfies_downstream(self, q):
        """Cancelled jobs DO satisfy their dependents (they're gone, not failed)."""
        job_a = PipelineJob(kind='_a', label='encode', priority=10)
        job_b = PipelineJob(kind='_b', label='clips', priority=30,
                          depends=[job_a.job_id])

        manifest = JobManifest(
            performance_key='26-04-11_ALTAR',
            jobs=[job_a, job_b],
            python_fns={
                job_a.job_id: lambda: None,
                job_b.job_id: lambda: None,
            },
        )
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        q.cancel(job_a.job_id)

        # B should now be runnable since A was cancelled (not failed)
        runnable = q.next_runnable(JobCategory.GPU_BOUND)
        assert runnable is not None
        assert runnable.job_id == job_b.job_id

    def test_schedule_gate_blocks_outside_window(self, q):
        """GPU_BOUND jobs are not runnable at or beyond _ENCODE_END_HOUR."""
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=_ENCODE_END_HOUR)
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=_ENCODE_END_HOUR + 1)
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=23)

    def test_schedule_gate_allows_inside_window(self, q):
        """GPU_BOUND jobs are runnable within the 00:00–_ENCODE_END_HOUR window."""
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=0)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=8)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=_ENCODE_END_HOUR - 1)

    def test_schedule_gate_scheduled_category_always_runs(self, q):
        """SCHEDULED category is always allowed (not gated by window)."""
        for hour in (0, 8, 16, 17, 23):
            assert q.is_within_schedule(JobCategory.SCHEDULED, hour=hour), (
                f'SCHEDULED should be allowed at hour {hour}'
            )

    def test_summary_counts(self, q):
        """summary() accurately reflects pending, done, and failed counts."""
        job_ok   = PipelineJob(kind='_ok',   label='ok')
        job_fail = PipelineJob(kind='_fail', label='fail')

        q.enqueue(JobManifest('p1', [job_ok],   {job_ok.job_id:   lambda: None}),
                  JobCategory.GPU_BOUND)
        q.enqueue(JobManifest('p2', [job_fail], {job_fail.job_id: lambda: (_ for _ in ()).throw(RuntimeError('boom'))}),
                  JobCategory.GPU_BOUND)

        assert q.summary()['pending'] == 2

        q.dispatch_one(JobCategory.GPU_BOUND)
        q.dispatch_one(JobCategory.GPU_BOUND)

        s = q.summary()
        assert s['pending'] == 0
        assert s['done'] == 1
        assert s['failed'] == 1

    def test_enqueue_multiple_manifests(self, q):
        """Two independent manifests each run to completion without interference."""
        results = []
        for band in ('ALTAR', 'PRIZE'):
            job = PipelineJob(kind='_enc', label=f'{band} encode', priority=10)
            q.enqueue(
                JobManifest(f'26-04-11_{band}', [job],
                            {job.job_id: lambda b=band: results.append(b)}),
                JobCategory.GPU_BOUND,
            )

        q.dispatch_one(JobCategory.GPU_BOUND)
        q.dispatch_one(JobCategory.GPU_BOUND)

        assert set(results) == {'ALTAR', 'PRIZE'}

    def test_wait_drain_returns_immediately_on_empty_queue(self, q):
        """wait_drain() returns True instantly when no jobs are queued."""
        assert q.wait_drain(timeout=0.1) is True

    def test_wait_drain_blocks_until_job_completes(self, q):
        """wait_drain() blocks while a job runs and returns True after it finishes."""
        gate = threading.Event()
        done = []

        def _blocking_job():
            gate.wait()
            done.append(True)

        job = PipelineJob(kind='_block', label='block')
        manifest = JobManifest('perf', [job], {job.job_id: _blocking_job})
        q.enqueue(manifest, JobCategory.GPU_BOUND)

        t = threading.Thread(
            target=q.dispatch_one, args=(JobCategory.GPU_BOUND,), daemon=True
        )
        t.start()

        time.sleep(0.02)    # give dispatch_one time to enter the job
        assert not q.wait_drain(timeout=0.01), (
            'wait_drain should block while job is running'
        )

        gate.set()
        assert q.wait_drain(timeout=2.0), (
            'wait_drain should return True after job finishes'
        )
        assert done == [True]


# ---------------------------------------------------------------------------
# TestBuildManifestIdempotency — _build_full_manifest skips complete outputs
# ---------------------------------------------------------------------------

class TestBuildManifestIdempotency:

    PERF = '26-04-11_ALTAR'

    def _mov(self, pipeline):
        mov = pipeline.search_dir / '26-04-11_ALTAR.mov'
        mov.write_bytes(b'')
        return [mov]

    def test_encode_skipped_when_quads_exist(self, pipeline):
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, [], [], [])
        assert 'encode_quads' not in [j.kind for j in manifest.jobs]

    def test_audio_skipped_when_zip_exists(self, pipeline):
        movs = self._mov(pipeline)
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        assert 'split_audio' not in [j.kind for j in manifest.jobs]

    def test_clips_skipped_when_clips_exist(self, pipeline):
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
        clips_dir = pipeline.clips_dest / base
        clips_dir.mkdir(parents=True)
        (clips_dir / f'{base}_{CAM_LABELS[0]}_1.mp4').write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, [], [], [])
        assert 'export_clips' not in [j.kind for j in manifest.jobs]

    def test_remaster_skipped_when_fullset_exists(self, pipeline):
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'audio')).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        assert '_remaster' not in [j.kind for j in manifest.jobs]

    def test_reel_skipped_when_reel_exists(self, pipeline):
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'audio')).write_bytes(b'')
        (pipeline.vids_dest / perf_output_name(self.PERF, 'reel')).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        assert 'generate_reel' not in [j.kind for j in manifest.jobs]

    def test_reel_queued_when_fullset_exists_and_encode_pending(self, pipeline):
        """REEL waits on REENCODE when FULLSET exists but quads don't (MJPEG late-MOV case)."""
        movs = self._mov(pipeline)
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'audio')).write_bytes(b'')
        # quads intentionally absent

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        scripts = [j.kind for j in manifest.jobs]

        assert 'generate_reel' in scripts
        assert '_remaster' not in scripts

        reel_job    = next(j for j in manifest.jobs if j.kind == 'generate_reel')
        encode_job  = next(j for j in manifest.jobs if j.kind == 'encode_quads')
        assert encode_job.job_id in reel_job.depends

    def test_reel_queued_standalone_when_fullset_and_quads_exist(self, pipeline):
        """REEL runs immediately (no deps) when FULLSET and quads already exist but no reel."""
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'audio')).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        scripts = [j.kind for j in manifest.jobs]

        assert 'generate_reel' in scripts
        assert '_remaster' not in scripts

        reel_job = next(j for j in manifest.jobs if j.kind == 'generate_reel')
        assert reel_job.depends == []

    def test_reel_not_suppressed_by_other_band_reel(self, pipeline):
        """A reel for a different band on the same date does not block this band's reel."""
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
        ch = [pipeline.search_dir / '26-04-11_ALTAR_ch01.wav']
        ch[0].write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'multitrack')).write_bytes(b'')
        (pipeline.audio_dest / perf_output_name(self.PERF, 'audio')).write_bytes(b'')
        # A reel for a different band on the same date
        (pipeline.vids_dest / perf_output_name('26-04-11_PRIZE', 'reel')).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, ch, [], [])
        assert 'generate_reel' in [j.kind for j in manifest.jobs]

    def test_reel_not_queued_without_fullset_or_remaster(self, pipeline):
        """No REEL job when there is no FULLSET and nothing pending that would produce one."""
        manifest, _ = pipeline._build_full_manifest(self.PERF, [], [], [], [])
        assert 'generate_reel' not in [j.kind for j in manifest.jobs]

    def test_force_overrides_encode_skip(self, pipeline):
        pipeline.force = True
        movs = self._mov(pipeline)
        base = movs[0].stem
        for q in CAM_LABELS:
            (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(self.PERF, movs, [], [], [])
        assert 'encode_quads' in [j.kind for j in manifest.jobs]

    def test_empty_mov_list_produces_no_encode_job(self, pipeline):
        manifest, _ = pipeline._build_full_manifest(self.PERF, [], [], [], [])
        assert 'encode_quads' not in [j.kind for j in manifest.jobs]

    def test_single_skipped_when_output_exists(self, pipeline):
        """_build_full_manifest skips a single whose output .mp4 already exists."""
        singles_dir = pipeline.search_dir / 'Singles'
        singles_dir.mkdir()
        single_mov = singles_dir / '26-04-11_CAM2.mov'
        single_mov.write_bytes(b'')
        (pipeline.vids_dest / '26-04-11_CAM2.mp4').write_bytes(b'')

        manifest, _ = pipeline._build_full_manifest(
            self.PERF, [], [], [], [],
            singles_list=[single_mov],
        )
        assert 'transcode_single' not in [j.kind for j in manifest.jobs]


# ---------------------------------------------------------------------------
# TestMultiPerformance — two performances run independently through workers
# ---------------------------------------------------------------------------

class TestMultiPerformance:

    def test_two_perfs_both_complete(self, pipeline):
        """Two independent performances each produce their quad files."""
        results = []

        def make_encode_fn(base):
            def _fn():
                for q in CAM_LABELS:
                    (pipeline.vids_dest / perf_output_name(base, 'quad', q)).write_bytes(b'')
                results.append(base)
            return _fn

        for band in ('ALTAR', 'PRIZE'):
            base = f'26-04-11_{band}'
            job = PipelineJob(kind='encode_quads', label=f'{base} ENCODE',
                            priority=10)
            manifest = JobManifest(
                performance_key=f'26-04-11_{band}',
                jobs=[job],
                python_fns={job.job_id: make_encode_fn(base)},
            )
            pipeline._job_queue.enqueue(manifest, JobCategory.GPU_BOUND)

        pipeline._noproblem_active = True
        pipeline._start_workers()

        assert wait_idle(pipeline._job_queue, timeout=5.0), 'Queue did not drain in 5s'

        assert set(results) == {'26-04-11_ALTAR', '26-04-11_PRIZE'}
        for band in ('ALTAR', 'PRIZE'):
            for q in CAM_LABELS:
                assert (pipeline.vids_dest / perf_output_name(f'26-04-11_{band}', 'quad', q)).exists()


# ---------------------------------------------------------------------------
# TestFullSyntheticRun — pipeline.run() with ScriptRunner mocked
# ---------------------------------------------------------------------------

class TestFullSyntheticRun:
    """pipeline.run() drives the full queue path, no real ffmpeg."""

    def _install_mock_runner(self, pipeline):
        from unittest.mock import MagicMock

        def fake_run(job, progress_cb=None, proc_cb=None, clip_progress_cb=None):
            # Output names come from the production helpers — the mock cannot
            # drift from what the real scripts write (the 2026-05-31 hang class).
            if job.script == 'encode_quads':
                base     = job.args['base']
                dest_dir = pathlib.Path(job.args['dest_dir'])
                for q in CAM_LABELS:
                    (dest_dir / quad_temp_name(base, q)).write_bytes(b'\x00')
                return ScriptResult('encode_quads', 0, {}, '', 0.0)

            if job.script == 'export_clips':
                return ScriptResult('export_clips', 0,
                                    {'quads': []}, '', 0.0)

            if job.script == 'split_audio':
                src = pathlib.Path(job.args.get('source', ''))
                for i in (1, 2):
                    (src.parent / chan_wav_name(src.stem, i)).write_bytes(b'\x00')
                return ScriptResult('split_audio', 0, {}, '', 0.0)

            return ScriptResult(job.script, 0, {}, '', 0.0)

        mock = MagicMock()
        mock.run.side_effect = fake_run
        pipeline._script_runner = mock
        pipeline._job_queue._runner = mock
        return mock

    def test_batch_run_produces_quad_files(self, pipeline):
        """pipeline.run() with mocked runner creates quad files via temp-rename."""
        mov = pipeline.search_dir / '26-04-11_ALTAR.mov'
        mov.write_bytes(b'')
        self._install_mock_runner(pipeline)

        t = threading.Thread(target=pipeline.run, daemon=True)
        t.start()
        t.join(timeout=10.0)

        assert not t.is_alive(), 'pipeline.run() did not exit within 10s'

        base = '26-04-11_ALTAR'
        for q in CAM_LABELS:
            assert (pipeline.vids_dest / perf_output_name(base, 'quad', q)).exists(), (
                f'Missing quad: {base}_{q}.mp4'
            )

    def test_batch_run_no_failed_jobs(self, pipeline):
        """pipeline.run() completes with zero failed jobs in the queue."""
        mov = pipeline.search_dir / '26-04-11_ALTAR.mov'
        mov.write_bytes(b'')
        self._install_mock_runner(pipeline)

        # Guard the join like the sibling tests: a non-terminating run() must
        # fail this one test in 10s, not hang the whole suite indefinitely.
        t = threading.Thread(target=pipeline.run, daemon=True)
        t.start()
        t.join(timeout=10.0)
        assert not t.is_alive(), 'pipeline.run() did not exit within 10s'

        assert pipeline._job_queue.summary()['failed'] == 0, (
            f'Failed jobs: {pipeline._job_queue.summary()}'
        )

    def test_batch_run_exits_without_hanging(self, pipeline):
        """pipeline.run() must complete within 10s with a mocked runner."""
        mov = pipeline.search_dir / '26-04-11_ALTAR.mov'
        mov.write_bytes(b'')
        self._install_mock_runner(pipeline)

        exc = [None]

        def _run():
            try:
                pipeline.run()
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10.0)

        assert not t.is_alive(), 'pipeline.run() did not exit within 10s — possible drain hang'
        assert exc[0] is None, f'pipeline.run() raised: {exc[0]}'
