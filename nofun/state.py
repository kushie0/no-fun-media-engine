"""nofun/state.py — Pipeline state enums shared across mixins."""

__all__ = [
    'MenuMode',
    'PauseState',
]

import enum


class MenuMode(enum.Enum):
    """Which interactive menu overlay is currently active, if any."""
    NONE      = 'none'
    STATUS    = 'status'
    STREAMS   = 'streams'
    JOBS      = 'jobs'
    REPROCESS = 'reprocess'


class PauseState(enum.Enum):
    """Current pause state of the pipeline worker."""
    RUNNING      = 'running'       # normal processing
    SOFT_PENDING = 'soft_pending'  # PAUSE#1 received; finish current job, then stop
    HARD_PENDING = 'hard_pending'  # PAUSE#2 received; kill the encoder immediately
    PAUSED       = 'paused'        # fully stopped; waiting for RESUME
