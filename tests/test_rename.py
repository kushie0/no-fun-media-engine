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


# ---------------------------------------------------------------------------
# Band rename (_rename_targets / _do_rename_performance) — glob-based, the
# filesystem is the source of truth across every storage location.
# ---------------------------------------------------------------------------


def _make_rename_pipeline(tmp_path: pathlib.Path):
    """Pipeline stub backed by a real StorageConfig with distinct NAS vs D: roots."""
    from media_engine import Pipeline
    from nofun.storage_config import StorageConfig

    obj = object.__new__(Pipeline)
    obj.mount_c     = pathlib.Path('C:/')          # != '.' enables search_dir
    obj.mount_d     = tmp_path / 'D'               # D: backup tier lives here
    obj.logger      = MagicMock()
    obj.search_dir  = tmp_path / 'VenueLighting'
    obj.clips_dest  = tmp_path / 'clips'
    obj.media_root  = tmp_path / 'NAS'             # NAS, distinct from mount_d
    obj.storage = StorageConfig(
        mount_c=obj.mount_c, mount_d=obj.mount_d,
        search_dir=obj.search_dir, clips_dest=obj.clips_dest,
        sharepoint_dest=None,                      # cloud branch skipped in these tests
    )
    d = obj.storage.media_dests(obj.media_root)
    obj.vids_dest, obj.audio_dest = d['vids_dest'], d['audio_dest']
    obj.video_archive, obj.audio_archive = d['video_archive'], d['audio_archive']
    obj.sharepoint_dest = None
    obj.d_video_backup  = obj.storage.d_video_backup
    obj.d_audio_backup  = obj.storage.d_audio_backup
    for dd in (obj.search_dir, obj.clips_dest, obj.vids_dest, obj.audio_dest,
               obj.video_archive, obj.audio_archive, obj.d_video_backup, obj.d_audio_backup):
        dd.mkdir(parents=True, exist_ok=True)
    obj._encoding_db    = MagicMock()
    obj._status_entries = []
    return obj


def _seed_band_files(pipe, date='26-06-19', band='CRACKHEAD_BARBIE'):
    """Create a representative file in every location plus a clips subdir."""
    base = f'{date}_{band}'
    (pipe.video_archive / f'{base}.8.mov').write_bytes(b'mov')        # NAS video_archive
    (pipe.vids_dest / f'{base}.8_CAM1.mp4').write_bytes(b'q1')
    (pipe.vids_dest / f'{base}.8_CAM2.mp4').write_bytes(b'q2')
    (pipe.vids_dest / f'{base}.8_INSTAGRAM.mp4').write_bytes(b'reel')
    (pipe.audio_dest / f'{base}_MULTITRACK.zip').write_bytes(b'zip')
    # D: raw-backup tier — the location the old registry missed
    (pipe.d_video_backup / f'{base}.8.mov').write_bytes(b'dmov')
    (pipe.d_audio_backup / f'{base}_MULTITRACK.zip').write_bytes(b'dzip')
    clip_dir = pipe.clips_dest / f'{base}.8'
    clip_dir.mkdir()
    for i in range(3):
        (clip_dir / f'{base}.8_CAM1_{i}.mp4').write_bytes(b'c')
    return clip_dir


class TestRenameTargets:
    """Pipeline._rename_targets / _rename_top_level globbing."""

    def test_collects_every_location_children_first(self, tmp_path):
        pipe = _make_rename_pipeline(tmp_path)
        clip_dir = _seed_band_files(pipe)
        # decoy: a different band that shares the date must never be touched
        (pipe.vids_dest / '26-06-19_OTHERBAND.8_CAM1.mp4').write_bytes(b'x')
        # decoy: a band whose name has the target as a PREFIX must not match
        (pipe.vids_dest / '26-06-19_CRACKHEAD_BARBIEXTRA.8_CAM1.mp4').write_bytes(b'x')

        targets = pipe._rename_targets('26-06-19', 'CRACKHEAD_BARBIE')
        names = {p.name for p in targets}

        # NAS: mov + 3 videos + zip + clip dir + 3 clip files = 9; D: backup mov + zip = 11
        assert len(targets) == 11
        assert '26-06-19_CRACKHEAD_BARBIE.8.mov' in names
        assert '26-06-19_CRACKHEAD_BARBIE.8_INSTAGRAM.mp4' in names
        assert not any('OTHERBAND' in n for n in names)
        assert not any('BARBIEXTRA' in n for n in names)  # prefix-overmatch guard
        # the D: backup tier is covered (the gap the live test surfaced)
        assert pipe.d_video_backup / '26-06-19_CRACKHEAD_BARBIE.8.mov' in targets
        assert pipe.d_audio_backup / '26-06-19_CRACKHEAD_BARBIE_MULTITRACK.zip' in targets
        # each clip child precedes its parent dir (so the dir rename is safe)
        dir_idx = targets.index(clip_dir)
        child_idxs = [i for i, p in enumerate(targets) if p.parent == clip_dir]
        assert child_idxs and max(child_idxs) < dir_idx

    def test_do_rename_renames_everything_locally(self, tmp_path):
        pipe = _make_rename_pipeline(tmp_path)
        _seed_band_files(pipe)
        (pipe.vids_dest / '26-06-19_OTHERBAND.8_CAM1.mp4').write_bytes(b'x')

        pipe._do_rename_performance('26-06-19', 'CRACKHEAD_BARBIE', 'CRACKHEAD_BARNIE')

        # nothing with the old band name survives anywhere — including the D: tier
        leftover = [p for root in (pipe.vids_dest, pipe.audio_dest, pipe.clips_dest,
                                   pipe.video_archive, pipe.d_video_backup, pipe.d_audio_backup)
                    for p in root.rglob('*CRACKHEAD_BARBIE*')]
        assert leftover == []
        # the renamed dir holds all 3 clips under the new name
        new_dir = pipe.clips_dest / '26-06-19_CRACKHEAD_BARNIE.8'
        assert new_dir.is_dir()
        assert len(list(new_dir.glob('*CRACKHEAD_BARNIE*'))) == 3
        # decoy untouched; DB update fired
        assert (pipe.vids_dest / '26-06-19_OTHERBAND.8_CAM1.mp4').exists()
        pipe._encoding_db.rename_band.assert_called_once_with(
            '26-06-19', 'CRACKHEAD_BARBIE', 'CRACKHEAD_BARNIE')


class TestMatchesBand:
    """menu_inventory._matches_band — band-boundary guard against prefix overmatch."""

    def test_separator_and_exact_match(self):
        from nofun.menu_inventory import _matches_band
        p = '26-06-19_CRACKHEAD_BARBIE'
        assert _matches_band('26-06-19_CRACKHEAD_BARBIE.8.mov', p)        # '.' suffix
        assert _matches_band('26-06-19_CRACKHEAD_BARBIE_MULTITRACK.zip', p)  # '_' part
        assert _matches_band('26-06-19_CRACKHEAD_BARBIE', p)              # bare dir
        assert not _matches_band('26-06-19_CRACKHEAD_BARBIEXTRA.8.mp4', p)  # overmatch
        assert not _matches_band('26-06-19_OTHER.8.mp4', p)

    def test_cloud_prefix_no_date(self):
        from nofun.menu_inventory import _matches_band
        assert _matches_band('CRACKHEAD_BARBIE_AUDIO.mp3', 'CRACKHEAD_BARBIE')
        assert not _matches_band('CRACKHEAD_BARBIEXTRA_AUDIO.mp3', 'CRACKHEAD_BARBIE')


class TestRenameCloudFile:
    """media_io.rename_cloud_file — hydration-safe in-place rename."""

    def test_renamed_when_hydrated(self, tmp_path):
        from nofun.media_io import rename_cloud_file
        src = tmp_path / 'CRACKHEAD_BARBIE_CAM1.mp4'
        src.write_bytes(b'q')
        dst = tmp_path / 'CRACKHEAD_BARNIE_CAM1.mp4'
        assert rename_cloud_file(src, dst) == 'renamed'
        assert dst.exists() and not src.exists()

    def test_missing_source(self, tmp_path):
        from nofun.media_io import rename_cloud_file
        src = tmp_path / 'nope.mp4'
        assert rename_cloud_file(src, tmp_path / 'x.mp4') == 'missing'

    def test_skip_when_dest_exists(self, tmp_path):
        from nofun.media_io import rename_cloud_file
        src = tmp_path / 'a.mp4'; src.write_bytes(b'1')
        dst = tmp_path / 'b.mp4'; dst.write_bytes(b'2')
        assert rename_cloud_file(src, dst) == 'skip-exists'
        assert src.exists()  # untouched

    def test_skip_dehydrated_placeholder(self, tmp_path):
        # A cloud-only placeholder must not be renamed (would force a download).
        src = tmp_path / 'a.mp4'; src.write_bytes(b'1')
        dst = tmp_path / 'b.mp4'
        with patch('nofun.media_io.is_cloud_only', return_value=True):
            from nofun.media_io import rename_cloud_file
            assert rename_cloud_file(src, dst) == 'skip-dehydrated'
        assert src.exists() and not dst.exists()
