"""Shared pytest configuration, fixtures, and hooks."""

import os
import pathlib
import subprocess
import sys

import pytest

REPO  = pathlib.Path(__file__).parent.parent
TF    = REPO / 'test_files'
TSECS = 10
TRIAL_FILES = {'26-01-01_TestBand.mov', '26-01-01_TestBand.wav'}

sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# --integration flag
# ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--integration', action='store_true', default=False,
        help='Run integration tests that invoke real ffmpeg (slow; ~60s).',
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skip_integration = pytest.mark.skip(
        reason='Integration tests skipped by default — run with --integration'
    )
    for item in items:
        if 'integration' in item.keywords and not config.getoption('--integration'):
            item.add_marker(skip_integration)


# ---------------------------------------------------------------------------
# Shared pipeline fixtures
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Queue-layer fixtures (used by test_queue.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Real Pipeline in batch mode with all output dirs under tmp_path.

    ScriptRunner is real; replace p._script_runner in individual tests.
    """
    from media_engine import Pipeline  # noqa: PLC0415
    monkeypatch.setenv('MOUNT_D', str(tmp_path))
    return Pipeline(
        directory=tmp_path,
        trial_run=0,
        exit_on_complete=True,
        skip_audio=False,
        force=False,
        gpu=False,
        cleanup_only=False,
    )


# ---------------------------------------------------------------------------
# Queue-layer helpers (imported by test_queue.py)
# ---------------------------------------------------------------------------

def wait_idle(job_queue, timeout: float = 5.0) -> bool:
    """Block until the queue has no pending or running jobs."""
    return job_queue.wait_drain(timeout)


def ok_result(script: str = '') -> 'ScriptResult':  # type: ignore[name-defined]
    from nofun.script_runner import ScriptResult
    return ScriptResult(script=script, exit_code=0, stdout_json={}, stderr_tail='', elapsed=0.0)


def fail_result(script: str = '') -> 'ScriptResult':  # type: ignore[name-defined]
    from nofun.script_runner import ScriptResult
    return ScriptResult(script=script, exit_code=1, stdout_json={}, stderr_tail='', elapsed=0.0)


# ---------------------------------------------------------------------------
# Shared pipeline fixtures (integration layer)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def video_trial(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Full pipeline run: video only (--skip-audio), 10 s trial.

    Files are copied to a temp source dir so committed test_files/ are never
    touched by pipeline output steps.
    """
    import shutil
    src = tmp_path_factory.mktemp('video_src')
    for name in TRIAL_FILES:
        shutil.copy(str(TF / name), str(src / name))
    out = tmp_path_factory.mktemp('v_trial')
    r = subprocess.run(
        [sys.executable, str(REPO / 'media_engine.py'),
         '-d', str(src), '-t', str(TSECS), '-s', '-f'],
        env={**os.environ, 'MOUNT_D': str(out), 'PYTHONIOENCODING': 'utf-8'},
        capture_output=True, text=True, encoding='utf-8', timeout=300,
    )
    assert r.returncode == 0, f"Pipeline failed:\n{r.stderr}\n{r.stdout}"
    return out


@pytest.fixture(scope='module')
def audio_trial(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Full pipeline run: audio only, 5 s trial.

    Files are copied to a temp source dir so the committed test_files/ are not
    modified by the split-and-delete pipeline behavior.
    """
    import shutil
    src = tmp_path_factory.mktemp('audio_src')
    for name in TRIAL_FILES:
        shutil.copy(str(TF / name), str(src / name))
    out = tmp_path_factory.mktemp('a_trial')
    r = subprocess.run(
        [sys.executable, str(REPO / 'media_engine.py'),
         '-d', str(src), '-t', '5', '-f'],
        env={**os.environ, 'MOUNT_D': str(out), 'PYTHONIOENCODING': 'utf-8'},
        capture_output=True, text=True, encoding='utf-8', timeout=600,
    )
    assert r.returncode == 0, f"Pipeline failed:\n{r.stderr}\n{r.stdout}"
    return out
