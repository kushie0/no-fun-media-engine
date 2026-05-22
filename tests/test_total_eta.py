"""tests/test_total_eta.py — queue-based total-ETA estimator on Pipeline."""
from __future__ import annotations

from unittest.mock import MagicMock

from nofun.job_manifest import PipelineJob
from nofun.job_queue import JobCategory, QueuedJob


def _queued(kind: str, category: JobCategory) -> QueuedJob:
    job = PipelineJob(kind=kind, label=kind)
    return QueuedJob(
        job=job, category=category, manifest_key='26-05-22_X',
        python_fn=lambda: None, status='pending',
    )


def _pipeline_with_active(active):
    """Build a Pipeline-like mock whose job queue returns `active`."""
    from media_engine import Pipeline
    pl = MagicMock(spec=Pipeline)
    pl._job_queue = MagicMock()
    pl._job_queue.all_active.return_value = active
    pl._KIND_WALL_S = Pipeline._KIND_WALL_S
    pl._UNKNOWN_KIND_WALL_S = Pipeline._UNKNOWN_KIND_WALL_S
    pl._estimate_total_eta = Pipeline._estimate_total_eta.__get__(pl)
    return pl


class TestEstimateTotalEta:
    def test_empty_queue_returns_none(self):
        pl = _pipeline_with_active([])
        assert pl._estimate_total_eta() is None

    def test_single_encode_returns_constant(self):
        pl = _pipeline_with_active([_queued('encode_quads', JobCategory.GPU_BOUND)])
        # 780s for one encode_quads
        assert pl._estimate_total_eta() == 780.0

    def test_unknown_kind_uses_fallback(self):
        pl = _pipeline_with_active([_queued('brand_new_kind', JobCategory.CPU_BOUND)])
        assert pl._estimate_total_eta() == 60.0

    def test_gpu_lane_dominates(self):
        active = [
            _queued('encode_quads',   JobCategory.GPU_BOUND),    # 780
            _queued('generate_reel',  JobCategory.GPU_BOUND),    # 540
            _queued('_remaster',      JobCategory.CPU_BOUND),    # 120
        ]
        pl = _pipeline_with_active(active)
        # GPU lane: 1320, CPU lane: 120 → max = 1320
        assert pl._estimate_total_eta() == 1320.0

    def test_cpu_lane_dominates(self):
        active = [
            _queued('_archive_audio', JobCategory.CPU_BOUND),    # 210
            _queued('_archive_audio', JobCategory.CPU_BOUND),    # 210
            _queued('_archive_audio', JobCategory.CPU_BOUND),    # 210
            _queued('encode_quads',   JobCategory.GPU_BOUND),    # 780
        ]
        pl = _pipeline_with_active(active)
        # GPU lane: 780, CPU lane: 630 → max = 780
        assert pl._estimate_total_eta() == 780.0

    def test_parallel_lanes_take_max_not_sum(self):
        active = [
            _queued('encode_quads',   JobCategory.GPU_BOUND),    # 780
            _queued('_archive_audio', JobCategory.CPU_BOUND),    # 210
        ]
        pl = _pipeline_with_active(active)
        # Not 780 + 210 = 990; lanes run concurrently → max
        assert pl._estimate_total_eta() == 780.0

    def test_scheduled_jobs_form_their_own_lane(self):
        active = [
            _queued('_scan',           JobCategory.SCHEDULED),   # 30
            _queued('_cleanup_execute', JobCategory.SCHEDULED),  # 30
            _queued('encode_quads',    JobCategory.GPU_BOUND),   # 780
        ]
        pl = _pipeline_with_active(active)
        # GPU = 780, SCHED = 60 → max = 780
        assert pl._estimate_total_eta() == 780.0
