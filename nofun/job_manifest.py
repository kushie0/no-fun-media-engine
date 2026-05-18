"""nofun/job_manifest.py — Declarative job descriptions for one performance.

A manifest is a complete, ordered list of PipelineJobs for one performance,
with dependency edges. Built by the pipeline when it detects work to do.
"""

from __future__ import annotations

__all__ = ['JobManifest', 'PipelineJob']

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PipelineJob:
    """One node in a JobManifest's dependency graph.

    PipelineJobs always run via a ``python_fn`` closure registered on the
    manifest — they never invoke ScriptRunner directly. The ``kind`` field is
    a stable identifier (e.g. ``'encode_quads'``, ``'_remaster'``) used for
    SCRIPT_REGISTRY lookups in the JOBS menu and for log/result tagging; it is
    *not* an executable script name. Use ``ScriptJob`` (in script_runner.py)
    when you actually want to execute a script as a subprocess.
    """
    kind:     str
    label:    str         = ''
    priority: int         = 50
    depends:  list[str]   = field(default_factory=list)
    job_id:   str         = ''

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = f'{self.kind}_{uuid.uuid4().hex[:8]}'


@dataclass
class JobManifest:
    """All work needed for one performance.

    ``jobs`` is an ordered list of PipelineJobs (ordered by dependency + priority).

    ``python_fns`` maps job_id → zero-argument callable. Every job in the
    manifest must have an entry here; the queue calls these closures to run
    each job. Closures capture all context (paths, args, sub-runners, etc.).
    """
    performance_key: str
    jobs:            list[PipelineJob]
    python_fns:      dict[str, Callable[[], None]] = field(default_factory=dict)
    created_at:      float                         = field(default_factory=time.time)

    # -----------------------------------------------------------------------
    # Mutation (call before enqueuing)
    # -----------------------------------------------------------------------

    def remove_job(self, job_id: str) -> None:
        """Drop a job from the manifest. Must be called before enqueuing."""
        self.jobs = [j for j in self.jobs if j.job_id != job_id]
        self.python_fns.pop(job_id, None)
