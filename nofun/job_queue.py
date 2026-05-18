"""nofun/job_queue.py — Priority queue with dependency tracking.

Jobs flow through: pending → running → done | failed | cancelled

The queue respects the Pipeline PAUSE state machine:
    RUNNING      → dispatch normally
    SOFT_PENDING → pause() called; dispatch stops, in-flight job finishes
    HARD_PENDING → kill_running() called; in-flight script process is killed
    PAUSED       → frozen; next_runnable() returns None until resume()

Four job categories:
    GPU_BOUND — video encode/clip/reel jobs (dispatched by GPU worker thread)
    CPU_BOUND — audio split/zip/silence/mastering jobs (dispatched by CPU worker thread)
    SCHEDULED — timed operations (inventory scan, cloud sync, expiry)
    MANUAL    — user-initiated (REMASTER, REEL)
"""

from __future__ import annotations

__all__ = ['JobCategory', 'QueuedJob', 'JobQueue', 'ScheduleRule', 'DEFAULT_SCHEDULE']

import datetime
import enum
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Callable

from nofun.job_manifest import JobManifest, PipelineJob
from nofun.script_runner import ScriptResult, ScriptRunner


class JobCategory(enum.Enum):
    GPU_BOUND = 'gpu_bound'   # encode, clips, reel
    CPU_BOUND = 'cpu_bound'   # audio split, silence, zip, mastering
    SCHEDULED = 'scheduled'
    MANUAL    = 'manual'


@dataclass
class ScheduleRule:
    """Time-window rule controlling when a job category may be dispatched.

    ``start_hour`` and ``end_hour`` are 24-hour integers.  Processing is
    allowed when ``start_hour <= current_hour < end_hour``.  Use
    ``end_hour=24`` to mean "until midnight".  Midnight wrap-around is not
    supported; split into two rules if needed.
    """
    name:       str
    start_hour: int          # inclusive (0–23)
    end_hour:   int          # exclusive (1–24, max 24 = midnight)
    category:   JobCategory
    enabled:    bool = True

    def is_active(self, hour: int | None = None) -> bool:
        """True if the given hour (or current local hour) falls within this window."""
        if not self.enabled:
            return False
        h = hour if hour is not None else datetime.datetime.now().hour
        return self.start_hour <= h < self.end_hour

    @property
    def window_str(self) -> str:
        """Human-readable window: '00:00–16:00'."""
        return f'{self.start_hour:02d}:00–{self.end_hour:02d}:00'


# Default schedule: heavy jobs midnight–4 pm; fast housekeeping always.
# SCHEDULED (sync, expire, scan) runs any time — low risk, seconds only.
# MANUAL (remaster, reel, reprocess) respects the same gate as GPU/CPU.
_ENCODE_END_HOUR = 16   # heavy-job window ends at 16:00 (exclusive)

DEFAULT_SCHEDULE: list[ScheduleRule] = [
    ScheduleRule('encode_window', start_hour=0, end_hour=_ENCODE_END_HOUR, category=JobCategory.GPU_BOUND),
    ScheduleRule('gpu_window',    start_hour=0, end_hour=_ENCODE_END_HOUR, category=JobCategory.GPU_BOUND),
    ScheduleRule('cpu_window',    start_hour=0, end_hour=_ENCODE_END_HOUR, category=JobCategory.CPU_BOUND),
    ScheduleRule('manual_window', start_hour=0, end_hour=_ENCODE_END_HOUR, category=JobCategory.MANUAL),
    ScheduleRule('sync_always',   start_hour=0, end_hour=24,               category=JobCategory.SCHEDULED),
]


@dataclass
class QueuedJob:
    """A PipelineJob placed in the JobQueue, with runtime state."""
    job:          PipelineJob
    category:     JobCategory
    manifest_key: str
    # Closure that does the actual work; supplied by the manifest's python_fns.
    # Captures all context — source paths, dest, sub-runners — in a closure.
    python_fn:    Callable[[], None]
    status:       str                       = 'pending'  # pending|running|done|failed|cancelled
    result:       ScriptResult | None       = None
    submitted_at: float                     = field(default_factory=time.time)
    started_at:   float | None             = None
    finished_at:  float | None             = None
    retry_count:  int                       = 0          # number of PermissionError retries so far
    max_retries:  int                       = 3          # give up after this many retries
    retry_after:  float | None             = None       # epoch time before which job is not runnable

    @property
    def job_id(self) -> str:
        return self.job.job_id

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at


class JobQueue:
    """Priority queue with dependency tracking.

    Thread-safe: all internal list mutations are guarded by ``_lock``.
    ``dispatch_one()`` is blocking — call it from a worker thread.
    """

    _MAX_HISTORY = 100

    def __init__(
        self,
        runner: ScriptRunner,
        logger: logging.Logger,
        schedule: 'list[ScheduleRule] | None' = None,
    ) -> None:
        self._runner   = runner
        self._logger   = logger
        self._queue:   list[QueuedJob] = []   # pending + running jobs
        self._history: list[QueuedJob] = []   # completed / failed / cancelled
        self._lock        = threading.Lock()
        self._paused      = False
        self._drain_event = threading.Event()
        self._drain_event.set()   # starts drained (no jobs yet)
        self._schedule: list[ScheduleRule] = (
            list(DEFAULT_SCHEDULE) if schedule is None else list(schedule)
        )

    # -----------------------------------------------------------------------
    # Enqueue
    # -----------------------------------------------------------------------

    def enqueue(
        self,
        manifest: JobManifest,
        category: JobCategory,
        category_map: 'dict[str, JobCategory] | None' = None,
    ) -> None:
        """Add all jobs from a manifest to the queue.

        Jobs are added in manifest order (which should already be
        dependency-ordered). Each job's depends list is respected by
        next_runnable().

        If *category_map* is provided, each job's category is looked up by
        job_id; jobs absent from the map fall back to *category*.
        """
        with self._lock:
            for job in manifest.jobs:
                cat = (category_map or {}).get(job.job_id, category)
                fn  = manifest.python_fns.get(job.job_id)
                if fn is None:
                    raise ValueError(
                        f'Manifest {manifest.performance_key!r} job {job.job_id!r} '
                        f'has no python_fn registered'
                    )
                qj = QueuedJob(
                    job=job,
                    category=cat,
                    manifest_key=manifest.performance_key,
                    python_fn=fn,
                )
                self._queue.append(qj)
            if manifest.jobs:
                self._drain_event.clear()
        self._logger.debug(
            f'JobQueue: enqueued {len(manifest.jobs)} job(s) for {manifest.performance_key}'
        )

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

    def next_runnable(self, category: 'JobCategory | None' = None) -> QueuedJob | None:
        """Return the highest-priority pending job whose dependencies are all done.

        If *category* is given, only jobs of that category are considered.
        Pass ``None`` (default) to pick from any category.

        A dependency is satisfied when the job_id appears in done history OR
        when the dep job is not in the queue (i.e. never queued or already
        evicted from history).
        """
        with self._lock:
            if self._paused:
                return None
            done_ids = {qj.job_id for qj in self._history if qj.status == 'done'}
            # Also treat cancelled deps as satisfied so independent jobs aren't blocked
            done_ids |= {qj.job_id for qj in self._history if qj.status == 'cancelled'}
            candidates = [
                qj for qj in self._queue
                if qj.status == 'pending'
                and (category is None or qj.category == category)
                and all(dep in done_ids for dep in qj.job.depends)
                and (qj.retry_after is None or time.time() >= qj.retry_after)
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda qj: qj.job.priority)

    def dispatch_one(
        self,
        category: 'JobCategory | None' = None,
        runner: 'ScriptRunner | None' = None,
    ) -> ScriptResult | None:
        """Run the next runnable job synchronously. Returns None if nothing runnable.

        If *category* is given, only jobs of that category are dispatched.
        Every queued job runs via its ``python_fn`` closure; the closure is
        responsible for invoking ScriptRunner if it needs to execute scripts.
        The *runner* parameter is accepted for backwards compatibility but
        unused at this layer.
        Blocking: returns only after the job completes (or fails/is killed).
        """
        del runner  # python_fn closures own their own runner
        qj = self.next_runnable(category)
        if qj is None:
            return None

        with self._lock:
            # Re-check status; another thread could have grabbed it
            if qj.status != 'pending':
                return None
            qj.status     = 'running'
            qj.started_at = time.time()

        label = qj.job.label or qj.job.kind
        self._logger.debug(f'JobQueue: start  {label}')

        try:
            qj.python_fn()
            elapsed = qj.elapsed or 0.0
            result = ScriptResult(
                script=qj.job.kind,
                exit_code=0,
                stdout_json={'status': 'ok'},
                stderr_tail='',
                elapsed=elapsed,
            )
        except PermissionError as exc:
            with self._lock:
                if qj.retry_count < qj.max_retries:
                    qj.retry_count += 1
                    qj.retry_after  = time.time() + 30.0
                    qj.status       = 'pending'
                    qj.started_at   = None
                    self._logger.warning(
                        f'JobQueue: {label} locked — '
                        f'retry {qj.retry_count}/{qj.max_retries} in 30s'
                    )
                    return None
                # Exhausted retries — mark failed
                result = ScriptResult(
                    script=qj.job.kind,
                    exit_code=-1,
                    stdout_json={'error': f'file locked after {qj.max_retries} retries'},
                    stderr_tail=str(exc),
                    elapsed=qj.elapsed or 0.0,
                )
        except Exception as exc:
            self._logger.error(f'JobQueue: job {qj.job_id} raised: {exc}')
            result = ScriptResult(
                script=qj.job.kind,
                exit_code=-1,
                stdout_json={'error': str(exc)},
                stderr_tail='',
                elapsed=qj.elapsed or 0.0,
            )

        with self._lock:
            qj.result      = result
            qj.finished_at = time.time()
            qj.status      = 'done' if result.ok else 'failed'
            self._queue.remove(qj)
            self._history.append(qj)
            if len(self._history) > self._MAX_HISTORY:
                self._history = self._history[-self._MAX_HISTORY:]
            if not self._queue:
                self._drain_event.set()

        elapsed = qj.elapsed or 0
        duration = (
            f'{int(elapsed // 60)}m{int(elapsed % 60):02d}s'
            if elapsed >= 60 else f'{elapsed:.1f}s'
        )
        self._logger.debug(f'JobQueue: finish {label}  ({qj.status}  {duration})')
        return result

    # -----------------------------------------------------------------------
    # Control
    # -----------------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending job by job_id. Returns True if found and cancelled."""
        with self._lock:
            for qj in self._queue:
                if qj.job_id == job_id and qj.status == 'pending':
                    qj.status     = 'cancelled'
                    qj.finished_at = time.time()
                    self._history.append(qj)
                    self._queue.remove(qj)
                    if not self._queue:
                        self._drain_event.set()
                    return True
        return False

    def cancel_manifest(self, perf_key: str) -> int:
        """Cancel all pending jobs for a performance. Returns count cancelled."""
        count = 0
        with self._lock:
            to_cancel = [
                qj for qj in self._queue
                if qj.manifest_key == perf_key and qj.status == 'pending'
            ]
            for qj in to_cancel:
                qj.status     = 'cancelled'
                qj.finished_at = time.time()
                self._history.append(qj)
                self._queue.remove(qj)
                count += 1
            if not self._queue:
                self._drain_event.set()
        return count

    def pause(self) -> None:
        """Stop dispatching new jobs. In-flight jobs run to completion."""
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        """Resume dispatching."""
        with self._lock:
            self._paused = False

    def kill_running(self, extra_runners: 'list[ScriptRunner] | None' = None) -> None:
        """Kill the currently running script process (for HARD_PENDING pause).

        *extra_runners* allows worker-thread runners to be killed alongside the
        queue's own default runner — necessary when GPU/CPU workers use their
        own independent ScriptRunner instances.
        """
        self._runner.kill()
        for r in (extra_runners or []):
            r.kill()

    def wait_drain(self, timeout: 'float | None' = None) -> bool:
        """Block until the queue has no pending or running jobs.

        Returns True if the queue drained before the timeout, False if timed out.
        Pass timeout=None (default) to block indefinitely.
        """
        return self._drain_event.wait(timeout)

    def clear_history(self) -> None:
        """Remove completed/failed/cancelled entries from history."""
        with self._lock:
            self._history.clear()

    # -----------------------------------------------------------------------
    # Scheduling
    # -----------------------------------------------------------------------

    def is_within_schedule(
        self,
        category: 'JobCategory',
        hour: int | None = None,
    ) -> bool:
        """True if any enabled rule allows *category* right now.

        Returns ``True`` (allow) when no rules exist for that category, so
        the pipeline is permissive by default.
        """
        has_any = False
        for rule in self._schedule:
            if rule.category == category:
                has_any = True
                if rule.is_active(hour):
                    return True
        return not has_any   # no rules → always allowed

    def get_schedule_rules(self) -> 'list[ScheduleRule]':
        """Return a snapshot of all schedule rules."""
        return list(self._schedule)

    def set_rule_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a named rule. Returns True if the rule was found."""
        for rule in self._schedule:
            if rule.name == name:
                rule.enabled = enabled
                return True
        return False

    # -----------------------------------------------------------------------
    # Status (for TUI display)
    # -----------------------------------------------------------------------

    def pending_count(self, category: 'JobCategory | None' = None) -> int:
        """Return pending-job count, optionally filtered by category."""
        with self._lock:
            return sum(1 for qj in self._queue
                       if qj.status == 'pending'
                       and (category is None or qj.category == category))

    def running_job(self) -> QueuedJob | None:
        with self._lock:
            for qj in self._queue:
                if qj.status == 'running':
                    return qj
        return None

    def all_active(self) -> list[QueuedJob]:
        """All pending and running jobs, in queue order."""
        with self._lock:
            return [qj for qj in self._queue if qj.status in ('pending', 'running')]

    def recent_history(self) -> list[QueuedJob]:
        """Completed / failed / cancelled jobs (most recent last)."""
        with self._lock:
            return list(self._history)

    def summary(self) -> dict:
        """Aggregate counts for the home status bar or JOBS menu header."""
        with self._lock:
            q_statuses = [qj.status for qj in self._queue]
            h_statuses = [qj.status for qj in self._history]
        return {
            'pending':   q_statuses.count('pending'),
            'running':   q_statuses.count('running'),
            'done':      h_statuses.count('done'),
            'failed':    h_statuses.count('failed'),
            'cancelled': h_statuses.count('cancelled'),
        }

    def manifest_status(self, perf_key: str) -> str:
        """Badge string for a specific performance key (exact match).

        Returns empty string if no active jobs for this performance.
        Used when the exact perf_key ('YY-MM-DD_Band') is known.
        """
        with self._lock:
            active = [qj for qj in self._queue if qj.manifest_key == perf_key]
        return JobQueue._format_badge(active)

    def manifest_status_by_date(self, short_date: str) -> str:
        """Badge string for all jobs whose manifest_key starts with short_date.

        ``short_date`` is a YY-MM-DD prefix (e.g. '26-04-12').
        Used by INVENTORY collapsed rows where we group all bands per date.
        Returns empty string if no active jobs for this date.
        """
        with self._lock:
            active = [qj for qj in self._queue
                      if qj.manifest_key.startswith(short_date)]
        return JobQueue._format_badge(active)

    @staticmethod
    def _format_badge(active: list[QueuedJob]) -> str:
        if not active:
            return ''
        running = [qj for qj in active if qj.status == 'running']
        pending = [qj for qj in active if qj.status == 'pending']
        if running:
            label = running[0].job.label or running[0].job.kind
            return f'encoding ({label[:20]})'
        if pending:
            return f'queued {len(pending)}'
        return ''
