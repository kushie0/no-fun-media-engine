"""Unit tests for nofun/check_encoding.py"""

import pathlib
from unittest.mock import patch

from nofun.check_encoding import is_problematic, scan_encodings


class TestIsProblematic:
    def test_main10_profile_is_bad(self):
        assert is_problematic('Main 10', 'yuv420p') is True

    def test_10le_pix_fmt_is_bad(self):
        assert is_problematic('Main', 'yuv420p10le') is True

    def test_10be_pix_fmt_is_bad(self):
        assert is_problematic('Main', 'yuv420p10be') is True

    def test_normal_8bit_is_ok(self):
        assert is_problematic('Main', 'yuv420p') is False


class TestScanEncodings:
    def test_returns_three_tuple(self, tmp_path):
        (tmp_path / 'fake.mp4').write_bytes(b'\x00')
        with patch('nofun.check_encoding.probe_video', return_value=('hevc', 'Main', 'yuv420p')):
            result = scan_encodings([tmp_path])
        assert len(result) == 3

    def test_third_element_is_dict(self, tmp_path):
        (tmp_path / 'fake.mp4').write_bytes(b'\x00')
        with patch('nofun.check_encoding.probe_video', return_value=('hevc', 'Main', 'yuv420p')):
            _, _, per_file = scan_encodings([tmp_path])
        assert isinstance(per_file, dict)

    def test_per_file_values_are_3_tuples(self, tmp_path):
        f = tmp_path / 'fake.mp4'
        f.write_bytes(b'\x00')
        with patch('nofun.check_encoding.probe_video', return_value=('hevc', 'Main', 'yuv420p')):
            _, _, per_file = scan_encodings([tmp_path])
        assert f in per_file
        codec, profile, pix_fmt = per_file[f]
        assert codec == 'hevc'
        assert profile == 'Main'
        assert pix_fmt == 'yuv420p'

    def test_bad_files_detected(self, tmp_path):
        (tmp_path / 'bad.mp4').write_bytes(b'\x00')
        with patch('nofun.check_encoding.probe_video', return_value=('hevc', 'Main 10', 'yuv420p10le')):
            _, bad_files, _ = scan_encodings([tmp_path])
        assert len(bad_files) == 1

    def test_empty_dir_returns_empty_results(self, tmp_path):
        summary, bad, per_file = scan_encodings([tmp_path])
        assert len(summary) == 0
        assert bad == []
        assert per_file == {}

    def test_progress_cb_called_with_path(self, tmp_path):
        (tmp_path / 'a.mp4').write_bytes(b'\x00')
        calls = []
        with patch('nofun.check_encoding.probe_video', return_value=('hevc', 'Main', 'yuv420p')):
            scan_encodings([tmp_path], progress_cb=lambda n, t, p: calls.append((n, t, p)))
        assert len(calls) == 1
        n, total, path = calls[0]
        assert n == 1
        assert total == 1
        assert isinstance(path, pathlib.Path)
