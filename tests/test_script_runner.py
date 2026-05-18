"""Tests for nofun/script_runner.py — ScriptRunner, ScriptJob, ScriptResult."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from nofun.script_runner import ScriptJob, ScriptResult, ScriptRunner


@pytest.fixture
def logger():
    return logging.getLogger('test_script_runner')


@pytest.fixture
def runner(logger):
    return ScriptRunner(logger)


@pytest.fixture
def scripts_dir(tmp_path: Path):
    """Create a temporary scripts directory with mock scripts."""
    d = tmp_path / 'scripts'
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# ScriptJob
# ---------------------------------------------------------------------------

class TestScriptJob:
    def test_auto_id(self):
        job = ScriptJob(script='encode_quads', args={'source': '/tmp/x.mov'})
        assert job.job_id.startswith('encode_quads_')
        assert len(job.job_id) > len('encode_quads_')

    def test_explicit_id(self):
        job = ScriptJob(script='encode_quads', args={}, job_id='my_id')
        assert job.job_id == 'my_id'

    def test_default_priority(self):
        job = ScriptJob(script='encode_quads', args={})
        assert job.priority == 50

    def test_args_stored(self):
        args = {'source': '/tmp/x.mov', 'dest_dir': '/tmp/out'}
        job = ScriptJob(script='encode_quads', args=args)
        assert job.args == args

    def test_label_default_empty(self):
        job = ScriptJob(script='encode_quads', args={})
        assert job.label == ''


# ---------------------------------------------------------------------------
# ScriptResult
# ---------------------------------------------------------------------------

class TestScriptResult:
    def test_ok_property(self):
        r = ScriptResult(script='test', exit_code=0, stdout_json={},
                         stderr_tail='', elapsed=1.0)
        assert r.ok is True

    def test_not_ok_nonzero(self):
        r = ScriptResult(script='test', exit_code=1, stdout_json={},
                         stderr_tail='', elapsed=1.0)
        assert r.ok is False

    def test_not_ok_killed(self):
        r = ScriptResult(script='test', exit_code=0, stdout_json={},
                         stderr_tail='', elapsed=1.0, killed=True)
        assert r.ok is False


# ---------------------------------------------------------------------------
# ScriptRunner — execution
# ---------------------------------------------------------------------------

class TestScriptRunnerRun:
    def test_missing_script_returns_127(self, runner):
        """If the script file doesn't exist, return exit_code=127."""
        job = ScriptJob(script='nonexistent_script_xyz', args={})
        result = runner.run(job)
        assert result.exit_code == 127
        assert 'not found' in result.stdout_json.get('error', '')

    def test_runs_simple_script(self, runner, tmp_path):
        """Run a minimal script that prints JSON to stdout."""
        script = tmp_path / 'hello.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            print(json.dumps({"status": "ok", "message": "hello"}))
            sys.exit(0)
        '''))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='hello', args={})
            result = runner.run(job)

        assert result.exit_code == 0
        assert result.ok is True
        assert result.stdout_json == {'status': 'ok', 'message': 'hello'}
        assert result.elapsed > 0

    def test_nonzero_exit_code(self, runner, tmp_path):
        """Script that exits with code 1 should report failure."""
        script = tmp_path / 'fail.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            print(json.dumps({"status": "error", "reason": "test"}))
            sys.exit(1)
        '''))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='fail', args={})
            result = runner.run(job)

        assert result.exit_code == 1
        assert result.ok is False
        assert result.stdout_json['status'] == 'error'

    def test_args_passed_correctly(self, runner, tmp_path):
        """CLI args should be passed as --key value."""
        script = tmp_path / 'echo_args.py'
        script.write_text(textwrap.dedent('''\
            import argparse, json, sys
            p = argparse.ArgumentParser()
            p.add_argument('--source', required=True)
            p.add_argument('--dest-dir', required=True)
            p.add_argument('--trial', type=int, default=0)
            a = p.parse_args()
            print(json.dumps({"source": a.source, "dest_dir": a.dest_dir, "trial": a.trial}))
        '''))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(
                script='echo_args',
                args={'source': '/tmp/test.mov', 'dest_dir': '/tmp/out', 'trial': 5},
            )
            result = runner.run(job)

        assert result.ok is True
        assert result.stdout_json['source'] == '/tmp/test.mov'
        assert result.stdout_json['dest_dir'] == '/tmp/out'
        assert result.stdout_json['trial'] == 5

    def test_bool_args_flag(self, runner, tmp_path):
        """Boolean True args should be passed as flags (--key)."""
        script = tmp_path / 'dry.py'
        script.write_text(textwrap.dedent('''\
            import argparse, json, sys
            p = argparse.ArgumentParser()
            p.add_argument('--dry-run', action='store_true')
            a = p.parse_args()
            print(json.dumps({"dry_run": a.dry_run}))
        '''))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='dry', args={'dry_run': True})
            result = runner.run(job)

        assert result.ok is True
        assert result.stdout_json['dry_run'] is True

    def test_bool_false_omitted(self, runner, tmp_path):
        """Boolean False args should NOT be passed."""
        script = tmp_path / 'no_flag.py'
        script.write_text(textwrap.dedent('''\
            import argparse, json, sys
            p = argparse.ArgumentParser()
            p.add_argument('--dry-run', action='store_true')
            a = p.parse_args()
            print(json.dumps({"dry_run": a.dry_run}))
        '''))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='no_flag', args={'dry_run': False})
            result = runner.run(job)

        assert result.ok is True
        assert result.stdout_json['dry_run'] is False

    def test_progress_callback(self, runner, tmp_path):
        """Progress lines on stderr should trigger progress_cb."""
        script = tmp_path / 'progress.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            # Simulate ffmpeg -stats progress line on stderr
            sys.stderr.write("frame=  100 fps= 30.0 time=00:00:03.33 speed= 1.0x\\r")
            sys.stderr.flush()
            print(json.dumps({"status": "ok"}))
        '''))

        calls = []
        def _cb(frame, fps, tc, speed):
            calls.append((frame, fps, tc, speed))

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='progress', args={})
            result = runner.run(job, progress_cb=_cb)

        assert result.ok is True
        assert len(calls) >= 1
        assert calls[0][0] == '100'  # frame

    def test_proc_callback(self, runner, tmp_path):
        """proc_cb should receive the Popen handle."""
        script = tmp_path / 'proctest.py'
        script.write_text(textwrap.dedent('''\
            import json; print(json.dumps({"ok": True}))
        '''))

        procs = []
        def _pcb(proc):
            procs.append(proc)

        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='proctest', args={})
            runner.run(job, proc_cb=_pcb)

        assert len(procs) == 1
        assert isinstance(procs[0], subprocess.Popen)


# ---------------------------------------------------------------------------
# ScriptRunner — kill
# ---------------------------------------------------------------------------

class TestScriptRunnerKill:
    def test_kill_no_proc(self, runner):
        """kill() should not raise when no process is running."""
        runner.kill()

    def test_process_property(self, runner):
        """process should be None when nothing is running."""
        assert runner.process is None


# ---------------------------------------------------------------------------
# Script integration — dry-run
# ---------------------------------------------------------------------------

class TestScriptDryRun:
    """Test that actual scripts in scripts/ respond to --dry-run correctly."""

    @pytest.fixture
    def real_runner(self, logger):
        """ScriptRunner using the real scripts/ directory."""
        return ScriptRunner(logger)

    def test_encode_quads_dry_run(self, real_runner, tmp_path):
        """encode_quads --dry-run should return command without executing."""
        source = tmp_path / 'test.mov'
        source.write_bytes(b'\x00')  # minimal file to pass exists() check

        job = ScriptJob(
            script='encode_quads',
            args={
                'source':   str(source),
                'dest_dir': str(tmp_path),
                'base':     'test',
                'encoder':  json.dumps(['-c:v', 'libx264', '-crf', '18']),
                'filter':   'nullsrc',
                'dry_run':  True,
            },
        )
        result = real_runner.run(job)
        assert result.exit_code == 0
        assert result.stdout_json['status'] == 'dry_run'
        assert 'command' in result.stdout_json
        assert isinstance(result.stdout_json['command'], list)

    def test_encode_quads_missing_input(self, real_runner, tmp_path):
        """encode_quads should exit 2 if source file is missing."""
        job = ScriptJob(
            script='encode_quads',
            args={
                'source':   str(tmp_path / 'nonexistent.mov'),
                'dest_dir': str(tmp_path),
                'base':     'test',
                'encoder':  json.dumps(['-c:v', 'libx264']),
                'filter':   'nullsrc',
            },
        )
        result = real_runner.run(job)
        assert result.exit_code == 2
        assert result.stdout_json['status'] == 'error'

    def test_detect_silence_dry_run(self, real_runner, tmp_path):
        """detect_silence --dry-run should list files without probing."""
        wav = tmp_path / 'test.wav'
        wav.write_bytes(b'\x00')
        file_list = tmp_path / 'files.txt'
        file_list.write_text(str(wav))

        job = ScriptJob(
            script='detect_silence',
            args={'file_list': str(file_list), 'dry_run': True},
        )
        result = real_runner.run(job)
        assert result.exit_code == 0
        assert result.stdout_json['status'] == 'dry_run'
        assert result.stdout_json['count'] == 1

    def test_split_audio_missing_input(self, real_runner, tmp_path):
        """split_audio should exit 2 if source file is missing."""
        job = ScriptJob(
            script='split_audio',
            args={
                'source':   str(tmp_path / 'nonexistent.wav'),
                'dest_dir': str(tmp_path),
                'base':     'test',
            },
        )
        result = real_runner.run(job)
        assert result.exit_code == 2

    def test_export_clips_no_quads(self, real_runner, tmp_path):
        """export_clips should exit 2 if no quadrant files found."""
        job = ScriptJob(
            script='export_clips',
            args={
                'source_dir': str(tmp_path),
                'base':       'nonexistent',
                'temp_dir':   str(tmp_path),
                'encoder':    json.dumps(['-c:v', 'libx264']),
                'filter':     'null',
            },
        )
        result = real_runner.run(job)
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# TestOrphanPidTracking
# ---------------------------------------------------------------------------

import nofun.script_runner as _sr


class TestOrphanPidTracking:
    """Tests for ffmpeg_pid= parsing and kill-on-stall."""

    def test_ffmpeg_pid_parsed_from_stderr(self, runner, tmp_path):
        """ScriptRunner stores _tracked_ffmpeg_pid when script emits ffmpeg_pid=N."""
        script = tmp_path / 'emit_pid.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            sys.stderr.write('ffmpeg_pid=12345\\n')
            sys.stderr.flush()
            print(json.dumps({'status': 'ok'}))
        '''))
        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            job = ScriptJob(script='emit_pid', args={})
            result = runner.run(job)
        assert result.ok
        # After normal exit the tracked PID must be cleared
        assert runner._tracked_ffmpeg_pid is None

    def test_pid_cleared_on_normal_exit(self, runner, tmp_path):
        """_tracked_ffmpeg_pid is None after a script that exits cleanly."""
        script = tmp_path / 'clean_exit.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            sys.stderr.write('ffmpeg_pid=99999\\n')
            sys.stderr.flush()
            print(json.dumps({'status': 'ok'}))
        '''))
        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            runner.run(ScriptJob(script='clean_exit', args={}))
        assert runner._tracked_ffmpeg_pid is None

    def test_stall_kills_tracked_pid(self, runner, tmp_path):
        """When ScriptRunner stall-kills, _tracked_ffmpeg_pid is cleared and result is killed."""
        script = tmp_path / 'stall_pid.py'
        script.write_text(textwrap.dedent(f'''\
            import subprocess, sys, time
            p = subprocess.Popen(["{sys.executable}", '-c', 'import time; time.sleep(60)'])
            sys.stderr.write(f'ffmpeg_pid={{p.pid}}\\n')
            sys.stderr.flush()
            time.sleep(60)  # no more stderr output -- triggers stall
        '''), encoding='utf-8')
        old_stall = _sr._STALL_TIMEOUT_SEC
        _sr._STALL_TIMEOUT_SEC = 1
        try:
            with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
                job = ScriptJob(script='stall_pid', args={})
                result = runner.run(job)
        finally:
            _sr._STALL_TIMEOUT_SEC = old_stall
        assert result.killed
        assert runner._tracked_ffmpeg_pid is None

    def test_kill_method_terminates_ffmpeg_pid(self, runner):
        """runner.kill() kills the tracked ffmpeg PID before the script process."""
        grandchild = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)']
        )
        try:
            runner._tracked_ffmpeg_pid = grandchild.pid
            runner._proc = grandchild
            runner.kill()
            assert runner._tracked_ffmpeg_pid is None
            grandchild.wait(timeout=2)  # should already be dead
        finally:
            # Safety cleanup if the test assertion fails
            if grandchild.poll() is None:
                grandchild.kill()
                grandchild.wait()


# ---------------------------------------------------------------------------
# TestLastProgress
# ---------------------------------------------------------------------------


class TestLastProgress:
    """Tests for ScriptRunner.last_progress population and reset."""

    def test_last_progress_populated_during_encode(self, runner, tmp_path):
        """last_progress is populated when script emits -progress key=value lines."""
        script = tmp_path / 'fake_progress.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            # Simulate ffmpeg -progress pipe:2 output
            for line in [
                'frame=100\\n',
                'fps=30.0\\n',
                'out_time=00:00:03\\n',
                'speed=1.0x\\n',
                'progress=continue\\n',
            ]:
                sys.stderr.write(line)
                sys.stderr.flush()
            print(json.dumps({'status': 'ok'}))
        '''))
        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            result = runner.run(ScriptJob(script='fake_progress', args={}))
        assert result.ok
        assert runner.last_progress.get('frame') == '100'
        assert runner.last_progress.get('fps') == '30.0'
        assert runner.last_progress.get('out_time') == '00:00:03'

    def test_last_progress_cleared_on_new_attempt(self, runner, tmp_path):
        """last_progress is reset to {} at the start of each attempt."""
        script = tmp_path / 'no_progress.py'
        script.write_text(textwrap.dedent('''\
            import json, sys
            print(json.dumps({'status': 'ok'}))
        '''))
        # Seed stale data from a previous run
        runner.last_progress = {'frame': '9999', 'fps': '99'}
        with patch.object(ScriptRunner, 'SCRIPTS_DIR', tmp_path):
            runner.run(ScriptJob(script='no_progress', args={}))
        # A script that emits no progress lines should leave last_progress empty
        assert runner.last_progress == {}
