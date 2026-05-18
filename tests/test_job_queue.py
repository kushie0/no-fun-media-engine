"""Tests for nofun/job_queue.py — JobCategory, QueuedJob, JobQueue."""

from __future__ import annotations

import logging
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nofun.job_manifest import JobManifest, PipelineJob
from nofun.job_queue import DEFAULT_SCHEDULE, JobCategory, JobQueue, QueuedJob, ScheduleRule
from nofun.script_runner import ScriptResult, ScriptRunner


@pytest.fixture
def logger():
    return logging.getLogger('test_job_queue')


@pytest.fixture
def mock_runner(logger):
    """ScriptRunner that returns ok=True immediately for any job."""
    runner = ScriptRunner(logger)
    return runner


def _ok_result(script: str = 'test') -> ScriptResult:
    return ScriptResult(
        script=script, exit_code=0,
        stdout_json={'status': 'ok'},
        stderr_tail='', elapsed=0.01,
    )


def _make_queue(logger) -> JobQueue:
    runner = MagicMock(spec=ScriptRunner)
    runner.run.return_value = _ok_result()
    return JobQueue(runner, logger)


def _simple_manifest(perf_key: str = '26-04-12_TEST', n_jobs: int = 1) -> JobManifest:
    jobs = [PipelineJob(kind='encode_quads', label=f'job {i}')
            for i in range(n_jobs)]
    fns = {j.job_id: (lambda: None) for j in jobs}
    return JobManifest(performance_key=perf_key, jobs=jobs, python_fns=fns)


# ---------------------------------------------------------------------------
# TestQueueBasic
# ---------------------------------------------------------------------------

class TestQueueBasic:
    def test_empty_queue_pending_count(self, logger) -> None:
        q = _make_queue(logger)
        assert q.pending_count() == 0

    def test_empty_queue_next_runnable(self, logger) -> None:
        q = _make_queue(logger)
        assert q.next_runnable() is None

    def test_empty_queue_dispatch_one(self, logger) -> None:
        q = _make_queue(logger)
        assert q.dispatch_one() is None

    def test_enqueue_increases_pending_count(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(n_jobs=3), JobCategory.GPU_BOUND)
        assert q.pending_count() == 3

    def test_pending_count_filters_by_category(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(perf_key='gpu-a', n_jobs=2), JobCategory.GPU_BOUND)
        q.enqueue(_simple_manifest(perf_key='cpu-a', n_jobs=1), JobCategory.CPU_BOUND)
        q.enqueue(_simple_manifest(perf_key='man-a', n_jobs=3), JobCategory.MANUAL)

        assert q.pending_count()                       == 6
        assert q.pending_count(JobCategory.GPU_BOUND)  == 2
        assert q.pending_count(JobCategory.CPU_BOUND)  == 1
        assert q.pending_count(JobCategory.MANUAL)     == 3
        assert q.pending_count(JobCategory.SCHEDULED)  == 0

    def test_summary_reflects_counts(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(n_jobs=2), JobCategory.GPU_BOUND)
        s = q.summary()
        assert s['pending'] == 2
        assert s['running'] == 0
        assert s['done'] == 0


# ---------------------------------------------------------------------------
# TestQueueDispatch
# ---------------------------------------------------------------------------

class TestQueueDispatch:
    def test_dispatch_one_runs_python_fn(self, logger) -> None:
        """dispatch_one() should call python_fn for python-only jobs."""
        called = []
        job = PipelineJob(kind='_zip', label='zip')
        fn = lambda: called.append('zip')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: fn},
        )
        q = _make_queue(logger)
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        result = q.dispatch_one()

        assert result is not None
        assert result.ok
        assert called == ['zip']
        assert q.pending_count() == 0

    def test_dispatch_moves_job_to_history(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.dispatch_one()
        assert q.pending_count() == 0
        history = q.recent_history()
        assert len(history) == 1
        assert history[0].status == 'done'

    def test_dependency_blocks_dispatch(self, logger) -> None:
        """A job whose dep is pending should not be dispatched first."""
        executed = []
        job_a = PipelineJob(kind='encode_quads', label='encode', priority=10)
        job_b = PipelineJob(
            kind='export_clips', label='clips', priority=20,
            depends=[job_a.job_id],
        )
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job_a, job_b],
            python_fns={
                job_a.job_id: lambda: executed.append('encode'),
                job_b.job_id: lambda: executed.append('clips'),
            },
        )
        q = _make_queue(logger)
        q.enqueue(manifest, JobCategory.GPU_BOUND)

        # Only job_a should be runnable (job_b depends on it)
        result1 = q.dispatch_one()
        assert result1 is not None
        assert executed == ['encode']

        # Now job_b is runnable
        result2 = q.dispatch_one()
        assert result2 is not None
        assert executed == ['encode', 'clips']

    def test_failed_job_marks_status_failed(self, logger) -> None:
        def _fail():
            raise RuntimeError("oops")

        job = PipelineJob(kind='encode_quads')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: _fail},
        )
        q = _make_queue(logger)
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        result = q.dispatch_one()
        assert result is not None
        assert not result.ok
        history = q.recent_history()
        assert history[-1].status == 'failed'

    def test_dispatch_one_returns_none_when_all_done(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.dispatch_one()
        assert q.dispatch_one() is None  # nothing left


# ---------------------------------------------------------------------------
# TestQueuePause
# ---------------------------------------------------------------------------

class TestQueuePause:
    def test_pause_stops_dispatch(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.pause()
        result = q.dispatch_one()
        assert result is None  # paused — nothing dispatched
        assert q.pending_count() == 1  # still pending

    def test_resume_allows_dispatch(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.pause()
        q.resume()
        result = q.dispatch_one()
        assert result is not None
        assert result.ok

    def test_pause_does_not_affect_next_runnable(self, logger) -> None:
        """next_runnable() returns None when paused."""
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.pause()
        assert q.next_runnable() is None


# ---------------------------------------------------------------------------
# TestQueueCancel
# ---------------------------------------------------------------------------

class TestQueueCancel:
    def test_cancel_pending_job(self, logger) -> None:
        job = PipelineJob(kind='encode_quads')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: lambda: None},
        )
        q = _make_queue(logger)
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        result = q.cancel(job.job_id)
        assert result is True
        assert q.pending_count() == 0

    def test_cancel_nonexistent_returns_false(self, logger) -> None:
        q = _make_queue(logger)
        assert q.cancel('nonexistent_id_xyz') is False

    def test_cancel_manifest(self, logger) -> None:
        manifest = _simple_manifest('26-04-12_PRIZE', n_jobs=3)
        q = _make_queue(logger)
        q.enqueue(manifest, JobCategory.GPU_BOUND)
        n = q.cancel_manifest('26-04-12_PRIZE')
        assert n == 3
        assert q.pending_count() == 0

    def test_cancel_only_affects_target_manifest(self, logger) -> None:
        m1 = _simple_manifest('26-04-12_PRIZE', n_jobs=2)
        m2 = _simple_manifest('26-04-13_CLAY',  n_jobs=2)
        q = _make_queue(logger)
        q.enqueue(m1, JobCategory.GPU_BOUND)
        q.enqueue(m2, JobCategory.GPU_BOUND)
        q.cancel_manifest('26-04-12_PRIZE')
        assert q.pending_count() == 2  # CLAY jobs still pending

    def test_cancelled_dep_unblocks_independent_job(self, logger) -> None:
        """A job in a different manifest is not affected by a cancellation."""
        m1 = _simple_manifest('26-04-12_PRIZE', n_jobs=1)
        m2 = _simple_manifest('26-04-13_CLAY',  n_jobs=1)
        q = _make_queue(logger)
        q.enqueue(m1, JobCategory.GPU_BOUND)
        q.enqueue(m2, JobCategory.GPU_BOUND)
        q.cancel_manifest('26-04-12_PRIZE')
        # CLAY job has no dependency — should still be runnable
        runnable = q.next_runnable()
        assert runnable is not None
        assert runnable.manifest_key == '26-04-13_CLAY'


# ---------------------------------------------------------------------------
# TestQueueStatus
# ---------------------------------------------------------------------------

class TestQueueStatus:
    def test_manifest_status_empty(self, logger) -> None:
        q = _make_queue(logger)
        assert q.manifest_status('26-04-12_PRIZE') == ''

    def test_manifest_status_pending(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest('26-04-12_PRIZE', n_jobs=2), JobCategory.GPU_BOUND)
        badge = q.manifest_status('26-04-12_PRIZE')
        assert 'queued' in badge
        assert '2' in badge

    def test_manifest_status_by_date(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest('26-04-12_PRIZE', n_jobs=1), JobCategory.GPU_BOUND)
        badge = q.manifest_status_by_date('26-04-12')
        assert badge != ''

    def test_manifest_status_by_date_no_match(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest('26-04-12_PRIZE', n_jobs=1), JobCategory.GPU_BOUND)
        badge = q.manifest_status_by_date('26-04-13')
        assert badge == ''

    def test_clear_history(self, logger) -> None:
        q = _make_queue(logger)
        q.enqueue(_simple_manifest(), JobCategory.GPU_BOUND)
        q.dispatch_one()
        assert len(q.recent_history()) == 1
        q.clear_history()
        assert len(q.recent_history()) == 0


# ---------------------------------------------------------------------------
# TestScheduleRules
# ---------------------------------------------------------------------------

class TestScheduleRules:
    def test_rule_active_within_window(self) -> None:
        rule = ScheduleRule('enc', start_hour=0, end_hour=16,
                            category=JobCategory.GPU_BOUND)
        assert rule.is_active(hour=0)
        assert rule.is_active(hour=8)
        assert rule.is_active(hour=15)

    def test_rule_inactive_outside_window(self) -> None:
        rule = ScheduleRule('enc', start_hour=0, end_hour=16,
                            category=JobCategory.GPU_BOUND)
        assert not rule.is_active(hour=16)
        assert not rule.is_active(hour=22)
        assert not rule.is_active(hour=23)

    def test_rule_disabled_always_inactive(self) -> None:
        rule = ScheduleRule('enc', start_hour=0, end_hour=24,
                            category=JobCategory.GPU_BOUND, enabled=False)
        assert not rule.is_active(hour=12)

    def test_window_str_format(self) -> None:
        rule = ScheduleRule('enc', start_hour=0, end_hour=16,
                            category=JobCategory.GPU_BOUND)
        assert rule.window_str == '00:00–16:00'

    def test_is_within_schedule_respects_window(self, logger) -> None:
        schedule = [
            ScheduleRule('enc', start_hour=0, end_hour=16,
                         category=JobCategory.GPU_BOUND),
        ]
        q = JobQueue(MagicMock(spec=ScriptRunner), logger, schedule=schedule)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=10)
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=17)

    def test_is_within_schedule_no_rule_allows(self, logger) -> None:
        q = JobQueue(MagicMock(spec=ScriptRunner), logger, schedule=[])
        # No rules → always allowed
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=20)

    def test_set_rule_enabled_toggles(self, logger) -> None:
        schedule = [
            ScheduleRule('enc', start_hour=0, end_hour=16,
                         category=JobCategory.GPU_BOUND),
        ]
        q = JobQueue(MagicMock(spec=ScriptRunner), logger, schedule=schedule)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=10)
        q.set_rule_enabled('enc', False)
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=10)
        q.set_rule_enabled('enc', True)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=10)

    def test_set_rule_enabled_returns_false_for_unknown(self, logger) -> None:
        q = _make_queue(logger)
        assert q.set_rule_enabled('nonexistent', False) is False

    def test_manual_respects_time_gate_in_default_schedule(self, logger) -> None:
        q = _make_queue(logger)
        assert q.is_within_schedule(JobCategory.MANUAL, hour=3)    # allowed midnight–4pm
        assert not q.is_within_schedule(JobCategory.MANUAL, hour=20)  # blocked after 4pm

    def test_default_schedule_has_encode_window(self, logger) -> None:
        q = _make_queue(logger)
        names = [r.name for r in q.get_schedule_rules()]
        assert 'encode_window' in names

    def test_get_schedule_rules_returns_snapshot(self, logger) -> None:
        q = _make_queue(logger)
        rules = q.get_schedule_rules()
        assert isinstance(rules, list)
        assert all(isinstance(r, ScheduleRule) for r in rules)


# ---------------------------------------------------------------------------
# TestGpuCpuCategories
# ---------------------------------------------------------------------------

class TestGpuCpuCategories:
    def test_gpu_bound_enum_exists(self) -> None:
        assert hasattr(JobCategory, 'GPU_BOUND')
        assert hasattr(JobCategory, 'CPU_BOUND')
        assert JobCategory.GPU_BOUND.value == 'gpu_bound'
        assert JobCategory.CPU_BOUND.value == 'cpu_bound'

    def test_gpu_only_picks_gpu(self, logger) -> None:
        """next_runnable(GPU_BOUND) should ignore CPU_BOUND jobs."""
        q = _make_queue(logger)
        cpu_manifest = _simple_manifest('26-04-12_AUD')
        q.enqueue(cpu_manifest, JobCategory.CPU_BOUND)
        assert q.next_runnable(JobCategory.GPU_BOUND) is None

    def test_cpu_only_picks_cpu(self, logger) -> None:
        """next_runnable(CPU_BOUND) should ignore GPU_BOUND jobs."""
        q = _make_queue(logger)
        gpu_manifest = _simple_manifest('26-04-12_VID')
        q.enqueue(gpu_manifest, JobCategory.GPU_BOUND)
        assert q.next_runnable(JobCategory.CPU_BOUND) is None

    def test_schedule_rules_for_gpu_cpu(self, logger) -> None:
        """DEFAULT_SCHEDULE should include gpu_window and cpu_window."""
        q = _make_queue(logger)
        names = [r.name for r in q.get_schedule_rules()]
        assert 'gpu_window' in names
        assert 'cpu_window' in names

    def test_gpu_within_schedule(self, logger) -> None:
        q = _make_queue(logger)
        assert q.is_within_schedule(JobCategory.GPU_BOUND, hour=10)
        assert not q.is_within_schedule(JobCategory.GPU_BOUND, hour=17)

    def test_cpu_within_schedule(self, logger) -> None:
        q = _make_queue(logger)
        assert q.is_within_schedule(JobCategory.CPU_BOUND, hour=10)
        assert not q.is_within_schedule(JobCategory.CPU_BOUND, hour=17)


# ---------------------------------------------------------------------------
# TestDualLaneDispatch
# ---------------------------------------------------------------------------

class TestDualLaneDispatch:
    def test_gpu_and_cpu_dispatch_independently(self, logger) -> None:
        """GPU and CPU manifests both dispatch via their respective categories."""
        q = _make_queue(logger)
        q.enqueue(_simple_manifest('26-04-12_VID'), JobCategory.GPU_BOUND)
        q.enqueue(_simple_manifest('26-04-12_AUD'), JobCategory.CPU_BOUND)
        r_gpu = q.dispatch_one(JobCategory.GPU_BOUND)
        r_cpu = q.dispatch_one(JobCategory.CPU_BOUND)
        assert r_gpu is not None
        assert r_cpu is not None

    def test_gpu_worker_ignores_cpu_jobs(self, logger) -> None:
        """next_runnable(GPU_BOUND) returns None when only CPU jobs are pending."""
        q = _make_queue(logger)
        q.enqueue(_simple_manifest('26-04-12_AUD'), JobCategory.CPU_BOUND)
        assert q.next_runnable(JobCategory.GPU_BOUND) is None

    def test_concurrent_dispatch_no_deadlock(self, logger) -> None:
        """Two threads dispatching GPU and CPU simultaneously complete without deadlock."""
        q = _make_queue(logger)
        for i in range(5):
            q.enqueue(_simple_manifest(f'26-04-12_VID_{i}'), JobCategory.GPU_BOUND)
            q.enqueue(_simple_manifest(f'26-04-12_AUD_{i}'), JobCategory.CPU_BOUND)

        results: dict[str, int] = {'gpu': 0, 'cpu': 0}

        def _gpu() -> None:
            while True:
                r = q.dispatch_one(JobCategory.GPU_BOUND)
                if r is None:
                    break
                results['gpu'] += 1

        def _cpu() -> None:
            while True:
                r = q.dispatch_one(JobCategory.CPU_BOUND)
                if r is None:
                    break
                results['cpu'] += 1

        t1 = threading.Thread(target=_gpu)
        t2 = threading.Thread(target=_cpu)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)
        assert results['gpu'] == 5
        assert results['cpu'] == 5

    def test_kill_running_with_extra_runners(self, logger) -> None:
        """kill_running() kills all supplied extra runners."""
        from unittest.mock import MagicMock
        q = _make_queue(logger)
        r1 = MagicMock(spec=ScriptRunner)
        r2 = MagicMock(spec=ScriptRunner)
        q.kill_running(extra_runners=[r1, r2])
        r1.kill.assert_called_once()
        r2.kill.assert_called_once()


# ---------------------------------------------------------------------------
# TestFullLifecycleManifest
# ---------------------------------------------------------------------------

class TestFullLifecycleManifest:
    """Verify dependency ordering and category_map in lifecycle manifests."""

    def _make_lifecycle_manifest(self) -> tuple[JobManifest, dict]:
        """Build a minimal encode + audio + remaster manifest with category_map."""
        encode = PipelineJob(kind='encode_quads', label='encode', priority=10)
        audio  = PipelineJob(kind='split_audio',  label='audio',  priority=10)
        master = PipelineJob(kind='_remaster',    label='master', priority=50,
                             depends=[encode.job_id, audio.job_id])
        jobs = [encode, audio, master]
        fns  = {j.job_id: (lambda: None) for j in jobs}
        cat_map = {
            encode.job_id: JobCategory.GPU_BOUND,
            audio.job_id:  JobCategory.CPU_BOUND,
            master.job_id: JobCategory.MANUAL,
        }
        return JobManifest('26-04-17_TEST', jobs, fns), cat_map

    def test_category_map_assigns_correct_categories(self, logger) -> None:
        """enqueue() with category_map gives each job its declared category."""
        q = _make_queue(logger)
        m, cat_map = self._make_lifecycle_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        active = q.all_active()
        assert len(active) == 3
        cats = {qj.job.kind: qj.category for qj in active}
        assert cats['encode_quads'] == JobCategory.GPU_BOUND
        assert cats['split_audio']  == JobCategory.CPU_BOUND
        assert cats['_remaster']    == JobCategory.MANUAL

    def test_category_map_fallback_to_default(self, logger) -> None:
        """Jobs not in category_map get the default category."""
        q = _make_queue(logger)
        job = PipelineJob(kind='encode_quads')
        m = JobManifest('test', [job], {job.job_id: lambda: None})
        q.enqueue(m, JobCategory.GPU_BOUND, category_map={})  # empty map → fallback
        active = q.all_active()
        assert active[0].category == JobCategory.GPU_BOUND

    def test_remaster_blocked_until_encode_and_audio_done(self, logger) -> None:
        """REMASTER is not runnable until both ENCODE and AUDIO have completed."""
        q = _make_queue(logger)
        m, cat_map = self._make_lifecycle_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)

        # REMASTER should not be runnable yet
        master_jobs = [qj for qj in q.all_active() if qj.job.kind == '_remaster']
        assert len(master_jobs) == 1
        master_qj = master_jobs[0]
        assert q.next_runnable() is not master_qj

        # Dispatch encode and audio
        r1 = q.dispatch_one(JobCategory.GPU_BOUND)
        r2 = q.dispatch_one(JobCategory.CPU_BOUND)
        assert r1 is not None and r2 is not None

        # Now REMASTER is runnable
        assert q.next_runnable() is not None
        assert q.next_runnable().job.kind == '_remaster'

    def test_encode_and_audio_independent(self, logger) -> None:
        """ENCODE and AUDIO jobs can both be dispatched without waiting for each other."""
        q = _make_queue(logger)
        m, cat_map = self._make_lifecycle_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        enc = q.next_runnable(JobCategory.GPU_BOUND)
        aud = q.next_runnable(JobCategory.CPU_BOUND)
        assert enc is not None
        assert aud is not None
        assert enc.job.kind == 'encode_quads'
        assert aud.job.kind == 'split_audio'

    def test_dedup_with_enqueued_keys(self, logger) -> None:
        """_enqueued_keys pattern: same perf not re-enqueued while active."""
        q = _make_queue(logger)
        enqueued: set[str] = set()

        def _enqueue_once(perf: str) -> None:
            if perf in enqueued and any(qj.manifest_key == perf for qj in q.all_active()):
                return
            m = _simple_manifest(perf)
            q.enqueue(m, JobCategory.GPU_BOUND)
            enqueued.add(perf)

        _enqueue_once('26-04-17_TEST')
        _enqueue_once('26-04-17_TEST')  # should not add a second time
        assert q.pending_count() == 1


# ---------------------------------------------------------------------------
# TestFileLockRetry
# ---------------------------------------------------------------------------

class TestFileLockRetry:
    def test_permission_error_triggers_retry(self, logger) -> None:
        """PermissionError returns job to pending with retry_after set."""
        def _raise_perm():
            raise PermissionError("file locked")

        job = PipelineJob(kind='encode_quads')
        m = JobManifest('test_locked', [job], {job.job_id: _raise_perm})
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.GPU_BOUND)

        r = q.dispatch_one()
        assert r is None                 # None means "retrying, not failed"
        assert q.pending_count() == 1    # job is back in the queue

        qj = q.all_active()[0]
        assert qj.retry_count == 1
        assert qj.retry_after is not None
        assert qj.retry_after > time.time()

    def test_retry_after_prevents_early_dispatch(self, logger) -> None:
        """Job with retry_after in the future is not returned by next_runnable."""
        def _raise_perm():
            raise PermissionError("locked")

        job = PipelineJob(kind='encode_quads')
        m = JobManifest('test_locked', [job], {job.job_id: _raise_perm})
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.GPU_BOUND)

        q.dispatch_one()  # triggers retry, sets retry_after = now+30
        # Immediately — job is not runnable (retry_after in the future)
        assert q.next_runnable() is None

    def test_max_retries_exceeded_marks_failed(self, logger) -> None:
        """After max_retries PermissionErrors the job is marked failed."""
        def _raise_perm():
            raise PermissionError("locked")

        job = PipelineJob(kind='encode_quads')
        m = JobManifest('test_locked', [job], {job.job_id: _raise_perm})
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.GPU_BOUND)

        for _ in range(4):  # max_retries=3, so 4th triggers failure
            qj_list = q.all_active()
            if qj_list:
                qj_list[0].retry_after = 0.0  # bypass wait
            q.dispatch_one()

        assert q.pending_count() == 0
        h = q.recent_history()
        assert h[-1].status == 'failed'

    def test_retry_does_not_block_other_jobs(self, logger) -> None:
        """While one job is in retry backoff, other pending jobs can dispatch."""
        def _raise_perm():
            raise PermissionError("locked")

        j1 = PipelineJob(kind='encode_quads', label='locked', priority=10)
        j2 = PipelineJob(kind='encode_quads', label='ok',     priority=20)
        m1 = JobManifest('locked', [j1], {j1.job_id: _raise_perm})
        m2 = JobManifest('ok',     [j2], {j2.job_id: lambda: None})
        q = _make_queue(logger)
        q.enqueue(m1, JobCategory.GPU_BOUND)
        q.enqueue(m2, JobCategory.GPU_BOUND)

        q.dispatch_one()   # j1 hits PermissionError → retry, retry_after set
        r = q.dispatch_one()  # should pick j2 (j1 is not yet runnable)
        assert r is not None and r.ok

    def test_non_permission_errors_fail_immediately(self, logger) -> None:
        """RuntimeError and other non-PermissionError exceptions fail without retry."""
        def _raise_runtime():
            raise RuntimeError("unexpected failure")

        job = PipelineJob(kind='encode_quads')
        m = JobManifest('test_err', [job], {job.job_id: _raise_runtime})
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.GPU_BOUND)

        r = q.dispatch_one()
        assert r is not None      # returns a result (failed, not None)
        assert not r.ok
        assert q.pending_count() == 0
        assert q.recent_history()[-1].status == 'failed'


# ---------------------------------------------------------------------------
# TestScheduledJobs
# ---------------------------------------------------------------------------

class TestScheduledJobs:
    def test_scheduled_job_dispatches(self, logger) -> None:
        """SCHEDULED category jobs are dispatched by dispatch_one(SCHEDULED)."""
        called = []
        q = _make_queue(logger)
        job = PipelineJob(kind='_sync', label='SYNC PERFORMANCES')
        m = JobManifest('_scheduled_sync', [job], {job.job_id: lambda: called.append(1)})
        q.enqueue(m, JobCategory.SCHEDULED)
        r = q.dispatch_one(JobCategory.SCHEDULED)
        assert r is not None and r.ok
        assert called == [1]

    def test_gpu_worker_ignores_scheduled_jobs(self, logger) -> None:
        """GPU worker does not dispatch SCHEDULED jobs."""
        q = _make_queue(logger)
        job = PipelineJob(kind='_sync')
        m = JobManifest('_sched', [job], {job.job_id: lambda: None})
        q.enqueue(m, JobCategory.SCHEDULED)
        assert q.next_runnable(JobCategory.GPU_BOUND) is None

    def test_scheduled_dedup_by_label(self, logger) -> None:
        """Same label not enqueued twice while the first is still active."""
        q = _make_queue(logger)
        active_labels: set[str] = set()

        def _enqueue_if_missing(label: str, kind: str) -> None:
            if label not in {qj.job.label for qj in q.all_active()}:
                job = PipelineJob(kind=kind, label=label)
                m = JobManifest(f'_{kind}', [job], {job.job_id: lambda: None})
                q.enqueue(m, JobCategory.SCHEDULED)
                active_labels.add(label)

        _enqueue_if_missing('SYNC PERFORMANCES', '_sync')
        _enqueue_if_missing('SYNC PERFORMANCES', '_sync')  # should be skipped
        assert q.pending_count() == 1

    def test_scheduled_always_within_schedule(self, logger) -> None:
        """SCHEDULED category is allowed at any hour by default."""
        q = _make_queue(logger)
        for hour in (0, 8, 16, 23):
            assert q.is_within_schedule(JobCategory.SCHEDULED, hour=hour)


# ---------------------------------------------------------------------------
# TestFullDagDependencies
# ---------------------------------------------------------------------------


def _make_8job_manifest() -> tuple[JobManifest, dict]:
    """Build a minimal 8-job lifecycle manifest mirroring _build_full_manifest output."""
    encode = PipelineJob(kind='encode_quads', priority=10)
    audio  = PipelineJob(kind='split_audio',  priority=10)
    clips  = PipelineJob(kind='export_clips', priority=30,
                         depends=[encode.job_id])
    sq     = PipelineJob(kind='_sync_quads',  priority=40,
                         depends=[encode.job_id])
    sa     = PipelineJob(kind='_sync_audio',  priority=40,
                         depends=[audio.job_id])
    master = PipelineJob(kind='_remaster',    priority=50,
                         depends=[encode.job_id, audio.job_id])
    reel   = PipelineJob(kind='generate_reel', priority=60,
                         depends=[master.job_id])
    sreel  = PipelineJob(kind='_sync_reel',   priority=70,
                         depends=[reel.job_id])
    jobs = [encode, audio, clips, sq, sa, master, reel, sreel]
    fns  = {j.job_id: (lambda: None) for j in jobs}
    cat_map = {
        encode.job_id: JobCategory.GPU_BOUND,
        audio.job_id:  JobCategory.CPU_BOUND,
        clips.job_id:  JobCategory.GPU_BOUND,
        sq.job_id:     JobCategory.SCHEDULED,
        sa.job_id:     JobCategory.SCHEDULED,
        master.job_id: JobCategory.MANUAL,
        reel.job_id:   JobCategory.GPU_BOUND,
        sreel.job_id:  JobCategory.SCHEDULED,
    }
    return JobManifest('26-04-17_TEST', jobs, fns), cat_map


class TestFullDagDependencies:
    """Verify 8-job lifecycle DAG dependency ordering."""

    def test_clips_depends_on_encode(self, logger) -> None:
        """export_clips is not runnable until encode_quads completes."""
        q = _make_queue(logger)
        m, cat_map = _make_8job_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        # Only encode_quads and split_audio have no deps — GPU lane should return encode
        first_gpu = q.next_runnable(JobCategory.GPU_BOUND)
        assert first_gpu is not None and first_gpu.job.kind == 'encode_quads'

    def test_reel_depends_on_remaster(self, logger) -> None:
        """generate_reel is not runnable until _remaster completes."""
        q = _make_queue(logger)
        m, cat_map = _make_8job_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        reel = next(qj for qj in q.all_active() if qj.job.kind == 'generate_reel')
        assert q.next_runnable(JobCategory.GPU_BOUND) is not reel

    def test_sync_quads_depends_on_encode(self, logger) -> None:
        """sync_quads is not runnable before encode_quads completes."""
        q = _make_queue(logger)
        m, cat_map = _make_8job_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        sq = next(qj for qj in q.all_active() if qj.job.kind == '_sync_quads')
        assert q.next_runnable(JobCategory.SCHEDULED) is not sq

    def test_sync_audio_depends_on_split(self, logger) -> None:
        """sync_audio is not runnable before split_audio completes."""
        q = _make_queue(logger)
        m, cat_map = _make_8job_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        sa = next(qj for qj in q.all_active() if qj.job.kind == '_sync_audio')
        assert q.next_runnable(JobCategory.SCHEDULED) is not sa

    def test_video_only_perf_no_audio_jobs(self, logger) -> None:
        """Manifest with no audio jobs omits split_audio and _sync_audio."""
        encode = PipelineJob(kind='encode_quads', priority=10)
        clips  = PipelineJob(kind='export_clips', priority=30,
                             depends=[encode.job_id])
        jobs = [encode, clips]
        fns  = {j.job_id: (lambda: None) for j in jobs}
        cat_map = {encode.job_id: JobCategory.GPU_BOUND,
                   clips.job_id:  JobCategory.GPU_BOUND}
        m = JobManifest('26-04-17_VIDEO_ONLY', jobs, fns)
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        scripts = {qj.job.kind for qj in q.all_active()}
        assert 'split_audio'  not in scripts
        assert '_sync_audio'  not in scripts

    def test_audio_only_perf_no_video_jobs(self, logger) -> None:
        """Manifest with no video jobs omits encode_quads and export_clips."""
        audio = PipelineJob(kind='split_audio', priority=10)
        jobs  = [audio]
        fns   = {audio.job_id: lambda: None}
        cat_map = {audio.job_id: JobCategory.CPU_BOUND}
        m = JobManifest('26-04-17_AUDIO_ONLY', jobs, fns)
        q = _make_queue(logger)
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        scripts = {qj.job.kind for qj in q.all_active()}
        assert 'encode_quads' not in scripts
        assert 'export_clips' not in scripts

    def test_full_8_job_dependency_chain(self, logger) -> None:
        """All 8 jobs dispatch in correct dependency order."""
        q = _make_queue(logger)
        m, cat_map = _make_8job_manifest()
        q.enqueue(m, JobCategory.MANUAL, category_map=cat_map)
        assert q.pending_count() == 8

        # Dispatch encode and audio (no dependencies)
        enc_r = q.dispatch_one(JobCategory.GPU_BOUND)
        aud_r = q.dispatch_one(JobCategory.CPU_BOUND)
        assert enc_r is not None and enc_r.script == 'encode_quads'
        assert aud_r is not None and aud_r.script == 'split_audio'

        # Now clips, sync_quads, sync_audio, remaster are all unblocked
        clips_r  = q.dispatch_one(JobCategory.GPU_BOUND)
        sq_r     = q.dispatch_one(JobCategory.SCHEDULED)
        sa_r     = q.dispatch_one(JobCategory.SCHEDULED)
        master_r = q.dispatch_one(JobCategory.MANUAL)
        assert clips_r  is not None and clips_r.script  == 'export_clips'
        assert sq_r     is not None and sq_r.script     == '_sync_quads'
        assert sa_r     is not None and sa_r.script     == '_sync_audio'
        assert master_r is not None and master_r.script == '_remaster'

        # Reel unlocked after remaster
        reel_r = q.dispatch_one(JobCategory.GPU_BOUND)
        assert reel_r is not None and reel_r.script == 'generate_reel'

        # Sync reel last
        sreel_r = q.dispatch_one(JobCategory.SCHEDULED)
        assert sreel_r is not None and sreel_r.script == '_sync_reel'

        assert q.pending_count() == 0
