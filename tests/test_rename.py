"""Unit tests for the NoFun rename feature.

Tests the WAV-matching logic and the interactive rename prompt without
requiring ffmpeg or real media files.

Run with:
    pytest tests/test_rename.py -v
"""

import pathlib
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Minimal Pipeline stub — avoids heavy __init__ side-effects
# ---------------------------------------------------------------------------


def _make_pipeline_stub(search_dir: pathlib.Path):
    """Return a Pipeline instance with just enough state for rename tests."""
    from media_engine import Pipeline

    # Bypass __init__ entirely — construct bare object
    obj = object.__new__(Pipeline)
    obj.search_dir = search_dir
    obj.logger = MagicMock()
    obj.trial_run = 0
    obj.force = False
    return obj


# ---------------------------------------------------------------------------
# Helper: patch Path.stat() to return controlled st_ctime values
# ---------------------------------------------------------------------------


def _patch_stat_ctime(fake_ctimes: dict[str, float]):
    """Return a context manager that patches Path.stat() to use fake ctimes.

    *fake_ctimes* maps ``str(path)`` → fake st_ctime value.
    The original stat result is preserved for all other fields.
    """
    orig_stat = pathlib.Path.stat

    def _fake_stat(self, *args, **kwargs):
        real = orig_stat(self, *args, **kwargs)
        key = str(self)
        if key in fake_ctimes:
            # Build a mock that delegates everything to real stat
            # but overrides st_ctime
            mock = MagicMock(wraps=real)
            mock.st_ctime = fake_ctimes[key]
            mock.st_size = real.st_size
            return mock
        return real

    return patch.object(pathlib.Path, 'stat', _fake_stat)


# ---------------------------------------------------------------------------
# _find_matching_wavs tests
# ---------------------------------------------------------------------------


class TestFindMatchingWavs:
    """Tests for Pipeline._find_matching_wavs."""

    def test_basic_chain(self, tmp_path: pathlib.Path):
        """First WAV within ±60 s, subsequent ones ~20 min apart."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'')

        wav0 = tmp_path / '25-3-4_NoFun.0.wav'
        wav1 = tmp_path / '25-3-4_NoFun.1.wav'
        wav2 = tmp_path / '25-3-4_NoFun.2.wav'
        for w in (wav0, wav1, wav2):
            w.write_bytes(b'')

        base_t = 1_000_000.0
        fake_ctimes = {
            str(mov):  base_t,
            str(wav0): base_t + 10,
            str(wav1): base_t + 1210,  # 20 min + 10 s
            str(wav2): base_t + 2410,  # 20 min after wav1
        }

        with _patch_stat_ctime(fake_ctimes):
            result = pipe._find_matching_wavs(mov)

        assert result == [wav0, wav1, wav2]

    def test_no_match(self, tmp_path: pathlib.Path):
        """WAVs too far from the MOV creation time → empty result."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'')

        wav = tmp_path / 'some_other.wav'
        wav.write_bytes(b'')

        base_t = 1_000_000.0
        fake_ctimes = {
            str(mov): base_t,
            str(wav): base_t + 5000,  # way too far
        }

        with _patch_stat_ctime(fake_ctimes):
            result = pipe._find_matching_wavs(mov)

        assert result == []

    def test_chain_breaks_on_gap(self, tmp_path: pathlib.Path):
        """Chain stops when gap exceeds the 22-minute threshold."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'')

        wav0 = tmp_path / 'a.wav'
        wav1 = tmp_path / 'b.wav'  # gap too large
        for w in (wav0, wav1):
            w.write_bytes(b'')

        base_t = 1_000_000.0
        fake_ctimes = {
            str(mov):  base_t,
            str(wav0): base_t + 30,
            str(wav1): base_t + 3000,  # 50 min — too far
        }

        with _patch_stat_ctime(fake_ctimes):
            result = pipe._find_matching_wavs(mov)

        assert result == [wav0]


# ---------------------------------------------------------------------------
# _prompt_rename_nofun tests
# ---------------------------------------------------------------------------


class TestPromptRenameNofun:
    """Tests for Pipeline._prompt_rename_nofun."""

    def test_skips_non_nofun(self, tmp_path: pathlib.Path):
        """A MOV with a real band name passes through unchanged."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_CoolBand.mov'
        mov.write_bytes(b'')

        result = pipe._prompt_rename_nofun(mov)
        assert result == mov

    def test_renames_mov_and_wavs(self, tmp_path: pathlib.Path):
        """User enters a band name → MOV and WAVs get renamed."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'fake mov')

        wav0 = tmp_path / '25-3-4_NoFun.0.wav'
        wav1 = tmp_path / '25-3-4_NoFun.1.wav'
        for w in (wav0, wav1):
            w.write_bytes(b'fake wav')

        base_t = 1_000_000.0
        fake_ctimes = {
            str(mov):  base_t,
            str(wav0): base_t + 10,
            str(wav1): base_t + 1210,
        }

        with (
            _patch_stat_ctime(fake_ctimes),
            patch('sys.stdin.isatty', return_value=True),
            patch('builtins.input', side_effect=['n', 'The Wombats']),
        ):
            result = pipe._prompt_rename_nofun(mov)

        assert result.name == '25-3-4_The Wombats.mov'
        assert result.exists()
        assert not mov.exists()
        assert (tmp_path / '25-3-4_The Wombats.0.wav').exists()
        assert (tmp_path / '25-3-4_The Wombats.1.wav').exists()

    def test_skip_command(self, tmp_path: pathlib.Path):
        """User types SKIP → nothing is renamed."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'fake mov')

        base_t = 1_000_000.0
        fake_ctimes = {str(mov): base_t}

        with (
            _patch_stat_ctime(fake_ctimes),
            patch('sys.stdin.isatty', return_value=True),
            patch('builtins.input', side_effect=['n', 'SKIP']),
        ):
            result = pipe._prompt_rename_nofun(mov)

        assert result == mov
        assert mov.exists()

    def test_empty_input_skips(self, tmp_path: pathlib.Path):
        """User presses Enter with no name → nothing is renamed."""
        pipe = _make_pipeline_stub(tmp_path)

        mov = tmp_path / '25-3-4_NoFun.mov'
        mov.write_bytes(b'fake mov')

        base_t = 1_000_000.0
        fake_ctimes = {str(mov): base_t}

        with (
            _patch_stat_ctime(fake_ctimes),
            patch('sys.stdin.isatty', return_value=True),
            patch('builtins.input', side_effect=['n', '']),
        ):
            result = pipe._prompt_rename_nofun(mov)

        assert result == mov
        assert mov.exists()
