"""Tests for nofun/job_manifest.py — JobManifest, PipelineJob."""

from __future__ import annotations

import time

import pytest

from nofun.job_manifest import JobManifest, PipelineJob


@pytest.fixture
def simple_job() -> PipelineJob:
    return PipelineJob(
        kind='encode_quads',
        label='test → quadrants',
    )


@pytest.fixture
def simple_manifest(simple_job: PipelineJob) -> JobManifest:
    return JobManifest(
        performance_key='26-04-12_TEST',
        jobs=[simple_job],
    )


# ---------------------------------------------------------------------------
# TestManifestBuild
# ---------------------------------------------------------------------------

class TestManifestBuild:
    def test_performance_key(self, simple_manifest: JobManifest) -> None:
        assert simple_manifest.performance_key == '26-04-12_TEST'

    def test_job_count(self, simple_manifest: JobManifest) -> None:
        assert len(simple_manifest.jobs) == 1

    def test_created_at_recent(self, simple_manifest: JobManifest) -> None:
        assert abs(simple_manifest.created_at - time.time()) < 5.0

    def test_dependency_edges(self) -> None:
        job_a = PipelineJob(kind='encode_quads', label='encode')
        job_b = PipelineJob(
            kind='export_clips', label='clips',
            depends=[job_a.job_id],
        )
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job_a, job_b],
        )
        assert manifest.jobs[1].depends == [job_a.job_id]

    def test_python_fn_stored(self) -> None:
        fn = lambda: None
        job = PipelineJob(kind='_zip', label='zip')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: fn},
        )
        assert manifest.python_fns[job.job_id] is fn

    def test_empty_manifest(self) -> None:
        manifest = JobManifest(performance_key='26-04-12_EMPTY', jobs=[])
        assert manifest.jobs == []
        assert manifest.python_fns == {}


# ---------------------------------------------------------------------------
# TestManifestMutation
# ---------------------------------------------------------------------------

class TestManifestMutation:
    def test_remove_job(self) -> None:
        job_a = PipelineJob(kind='encode_quads')
        job_b = PipelineJob(kind='export_clips')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job_a, job_b],
        )
        manifest.remove_job(job_a.job_id)
        assert len(manifest.jobs) == 1
        assert manifest.jobs[0].job_id == job_b.job_id

    def test_remove_job_clears_python_fn(self) -> None:
        fn = lambda: None
        job = PipelineJob(kind='_zip')
        manifest = JobManifest(
            performance_key='26-04-12_TEST',
            jobs=[job],
            python_fns={job.job_id: fn},
        )
        manifest.remove_job(job.job_id)
        assert job.job_id not in manifest.python_fns
        assert manifest.jobs == []
