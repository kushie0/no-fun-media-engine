"""tests/test_sysmon.py — CPU/GPU stat polling for the TUI."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from nofun.sysmon import SysMon


class TestSysMonGet:
    def test_returns_tuple_of_two(self):
        cpu, gpu = SysMon.get()
        assert isinstance(cpu, float)
        assert gpu is None or isinstance(gpu, float)

    def test_cpu_non_negative(self):
        cpu, _ = SysMon.get()
        assert cpu >= 0.0


class TestProbeGpu:
    def test_returns_none_on_subprocess_raise(self):
        with patch('subprocess.run', side_effect=OSError('powershell not found')):
            assert SysMon._probe_gpu() is None

    def test_returns_none_on_timeout(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('ps', 5)):
            assert SysMon._probe_gpu() is None

    def test_returns_none_on_non_numeric_output(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout='not a number\n')
        with patch('subprocess.run', return_value=fake):
            assert SysMon._probe_gpu() is None

    def test_returns_float_on_valid_output(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout='42.7\n')
        with patch('subprocess.run', return_value=fake):
            assert SysMon._probe_gpu() == 42.7

    def test_caps_at_100(self):
        # Some systems report > 100 when multiple engines sum
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout='150\n')
        with patch('subprocess.run', return_value=fake):
            assert SysMon._probe_gpu() == 100.0
