"""Unit tests for nofun/audio.py (AudioMixin)."""

import io
import pathlib
import struct
import wave
import zipfile
from unittest.mock import MagicMock, patch

from nofun.audio import MIN_ACTIVE_SECONDS, AudioMixin
from nofun.inventory import perf_output_name
from nofun.script_runner import ScriptResult
from tests.fake_pipeline import FakePipeline


def _real_wav_bytes(frames: int = 2400, rate: int = 48000) -> bytes:
    """A valid little 16-bit mono WAV — ffmpeg/FLAC can actually encode it.

    The zip path now FLAC-encodes each channel via ffmpeg, so fixtures that get
    bundled must be real WAVs, not `b'\\x00'` placeholder bytes.
    """
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<h', (i % 100) - 50)
                                for i in range(frames)))
    return buf.getvalue()


_REAL_WAV = _real_wav_bytes()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _FakeAudio(tmp_path: pathlib.Path) -> FakePipeline:
    """Shared FakePipeline with a mocked delete_queue (tests assert .add calls)."""
    fa = FakePipeline(tmp_path)
    fa.delete_queue = MagicMock()
    return fa


# ---------------------------------------------------------------------------
# TestMinActiveSeconds
# ---------------------------------------------------------------------------

class TestMinActiveSeconds:
    def test_value(self):
        assert MIN_ACTIVE_SECONDS == 5


# ---------------------------------------------------------------------------
# TestGroupWavFiles
# ---------------------------------------------------------------------------

class TestGroupWavFiles:
    def _wav(self, parent: pathlib.Path, name: str) -> pathlib.Path:
        f = parent / name
        f.write_bytes(b'\x00')
        return f

    def test_groups_by_date_and_band(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f1 = self._wav(tmp_path, '26-01-01_CoolBand_ch01.wav')
        f2 = self._wav(tmp_path, '26-01-01_CoolBand_ch02.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert 'CoolBand' in key
        assert len(groups[key]) == 2

    def test_strips_ch_suffix_before_grouping(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f1 = self._wav(tmp_path, '26-02-07_NoFun_ch01.wav')
        f2 = self._wav(tmp_path, '26-02-07_NoFun_ch02.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert '_ch' not in key

    def test_strips_part_number_suffix(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f1 = self._wav(tmp_path, '26-03-01_TheBand.1.wav')
        f2 = self._wav(tmp_path, '26-03-01_TheBand.2.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 1

    def test_different_bands_different_groups(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f1 = self._wav(tmp_path, '26-01-01_BandA_ch01.wav')
        f2 = self._wav(tmp_path, '26-01-01_BandB_ch01.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 2


# ---------------------------------------------------------------------------
# TestCreateAndVerifyZip
# ---------------------------------------------------------------------------

class TestCreateAndVerifyZip:
    def test_creates_valid_zip(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        src = tmp_path / 'test.wav'
        src.write_bytes(_REAL_WAV)
        zip_path = tmp_path / 'test.zip'

        ok, dropped = fa._create_and_verify_zip(zip_path, [src])

        assert ok is True
        assert dropped == []
        assert zip_path.exists()
        # WAV is FLAC-encoded into the bundle; the arcname switches to .flac.
        with zipfile.ZipFile(zip_path) as zf:
            assert 'test.flac' in zf.namelist()
            assert 'test.wav' not in zf.namelist()

    def test_returns_false_and_drops_missing_file(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        missing = tmp_path / 'ghost.wav'
        zip_path = tmp_path / 'bad.zip'

        ok, dropped = fa._create_and_verify_zip(zip_path, [missing])

        assert ok is False
        assert dropped == [missing]
        assert not zip_path.exists()

    def test_uses_stored_compression(self, tmp_path):
        """FLAC is already compressed, so the entry is STORED (no second deflate)."""
        fa = _FakeAudio(tmp_path)
        src = tmp_path / 'audio.wav'
        src.write_bytes(_REAL_WAV)
        zip_path = tmp_path / 'audio.zip'

        fa._create_and_verify_zip(zip_path, [src])

        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo('audio.flac')
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.compress_size == info.file_size

    def test_partial_missing_produces_zip_with_survivors(self, tmp_path):
        """Some files missing: ZIP still created with survivors; missing returned in dropped."""
        fa = _FakeAudio(tmp_path)
        present = tmp_path / 'chan1.wav'
        present.write_bytes(_REAL_WAV)
        missing = tmp_path / 'chan2.wav'
        zip_path = tmp_path / 'partial.zip'

        ok, dropped = fa._create_and_verify_zip(zip_path, [present, missing])

        assert ok is True
        assert dropped == [missing]
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert 'chan1.flac' in zf.namelist()
            assert 'chan2.flac' not in zf.namelist()


class TestZipWavGroupDroppedFiles:
    """_zip_wav_group emits a WARNING when files drop during zipping."""

    def test_dropped_files_logged_as_warning(self, tmp_path, caplog):
        import logging
        fa = _FakeAudio(tmp_path)
        fa.logger = logging.getLogger('test_zip_drop')
        present = tmp_path / '26-05-13_BAND_chan1.wav'
        present.write_bytes(_REAL_WAV)
        missing = tmp_path / '26-05-13_BAND_chan2.wav'
        # Don't create missing — pre-flight will drop it
        with caplog.at_level(logging.WARNING, logger='test_zip_drop'):
            fa._zip_wav_group(
                '26-05-13_BAND', [present, missing],
                zip_dest=fa.audio_dest, trim_dir=tmp_path,
                on_success_real_drive='delete',
            )
        warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any('chan2.wav' in m and 'vanished' in m for m in warns)


# ---------------------------------------------------------------------------
# TestGroupWavFilesAudioDir — chan-style filenames from Audio/ subfolder
# ---------------------------------------------------------------------------

class TestGroupWavFilesAudioDir:
    def _wav(self, parent: pathlib.Path, name: str) -> pathlib.Path:
        f = parent / name
        f.write_bytes(b'\x00')
        return f

    def test_groups_chan_files_by_performance(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        f1 = self._wav(audio_dir, '26-3-11_DAISY_CHAIN_chan7.3.wav')
        f2 = self._wav(audio_dir, '26-3-11_DAISY_CHAIN_chan8.1.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert 'DAISY_CHAIN' in key
        assert 'chan' not in key

    def test_different_performances_separate_groups(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        f1 = self._wav(audio_dir, '26-3-11_DAISY_CHAIN_chan1.wav')
        f2 = self._wav(audio_dir, '26-3-12_DAISY_CHAIN_chan1.wav')
        groups = fa._group_wav_files([f1, f2])
        assert len(groups) == 2


# ---------------------------------------------------------------------------
# TestProcessAudioDirWavs
# ---------------------------------------------------------------------------

class TestProcessAudioDirWavs:
    def _make_wav(self, path: pathlib.Path, name: str) -> pathlib.Path:
        f = path / name
        f.write_bytes(_REAL_WAV)
        return f

    def test_skips_missing_audio_dir(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        # No Audio/ subdir — should return without error
        fa._process_audio_dir_wavs(fa.search_dir / 'Audio')
        fa.delete_queue.add.assert_not_called()

    def test_skips_multi_channel_files(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan1.wav')

        with patch('nofun.audio.probe_stream', return_value='32'):
            fa._process_audio_dir_wavs(fa.search_dir / 'Audio')

        fa.delete_queue.add.assert_not_called()

    def test_drops_silent_channels(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan1.wav')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=0.0), \
             patch.object(fa, '_archive_or_dedup') as mock_archive:
            fa._process_audio_dir_wavs(fa.search_dir / 'Audio')

        mock_archive.assert_called_once()
        dest = mock_archive.call_args[0][1]
        assert dest == fa.audio_archive

    def test_zips_active_channels(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan7.3.wav')
        self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan8.wav')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._process_audio_dir_wavs(fa.search_dir / 'Audio')

        expected_zip = fa.audio_dest / perf_output_name('26-03-11_DAISY_CHAIN', 'multitrack')
        assert expected_zip.exists()

    def test_moves_wavs_to_archive_after_zip(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa.mount_d = tmp_path  # simulate real D: drive so archive branch fires
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        wav = self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan7.wav')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._process_audio_dir_wavs(fa.search_dir / 'Audio')

        expected_zip = fa.audio_dest / perf_output_name('26-03-11_DAISY_CHAIN', 'multitrack')
        assert expected_zip.exists()
        assert not wav.exists(), "Original WAV should have been moved to audio_archive"
        assert (fa.audio_archive / wav.name).exists(), "WAV should be in audio_archive"

    def test_skips_existing_zip(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        wav = self._make_wav(audio_dir, '26-3-11_DAISY_CHAIN_chan7.3.wav')

        # Pre-create the zip so it appears already done
        zip_path = fa.audio_dest / perf_output_name('26-03-11_DAISY_CHAIN', 'multitrack')
        zip_path.write_bytes(b'existing')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._process_audio_dir_wavs(fa.search_dir / 'Audio')

        # Existing zip preserved AND the skip branch queued the WAV for delete
        # (proves _zip_wav_group's skip branch actually ran, not just that the
        # write was no-op'd somewhere upstream).
        assert zip_path.read_bytes() == b'existing'
        queued = [c.args[0] for c in fa.delete_queue.add.call_args_list]
        assert wav in queued, 'skip branch should queue the channel WAV for delete'

    def test_zips_chan_wavs_directly_in_search_dir(self, tmp_path):
        """Chan-style single-channel WAVs in search_dir (not Audio/) are zipped."""
        fa = _FakeAudio(tmp_path)
        self._make_wav(tmp_path, '26-3-11_DAISY_CHAIN_chan7.3.wav')
        self._make_wav(tmp_path, '26-3-11_DAISY_CHAIN_chan8.wav')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._process_audio_dir_wavs(fa.search_dir)

        assert (fa.audio_dest / perf_output_name('26-03-11_DAISY_CHAIN', 'multitrack')).exists()

    def test_excludes_split_ch_wavs_when_scanning_search_dir(self, tmp_path):
        """_ch??.wav files in search_dir are excluded (handled by _export_audio_zips)."""
        fa = _FakeAudio(tmp_path)
        self._make_wav(tmp_path, '26-3-11_DAISY_CHAIN_ch01.wav')
        self._make_wav(tmp_path, '26-3-11_DAISY_CHAIN_ch02.wav')

        with patch('nofun.audio.probe_stream', return_value='1'), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._process_audio_dir_wavs(fa.search_dir)

        # No zip should be created — _ch files are excluded from this path
        assert not any(fa.audio_dest.glob('*.zip'))


# ---------------------------------------------------------------------------
# TestArchiveEmptyWavs
# ---------------------------------------------------------------------------

class TestArchiveEmptyWavs:
    def test_non_empty_returned_unchanged(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f = tmp_path / 'test.wav'
        f.write_bytes(b'\x00' * 256)
        result = fa._archive_empty_wavs([f])
        assert result == [f]
        fa.delete_queue.add.assert_not_called()

    def test_empty_wav_queued_without_real_drive(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa.mount_d = pathlib.Path('.')  # not a real drive
        f = tmp_path / 'empty.wav'
        f.write_bytes(b'')
        result = fa._archive_empty_wavs([f])
        assert result == []
        fa.delete_queue.add.assert_called_once()
        reason = fa.delete_queue.add.call_args[0][1]
        assert 'empty' in reason

    def test_empty_wav_moved_with_real_drive(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa.mount_d = tmp_path  # simulate real drive
        f = tmp_path / '26-3-11_DAISY_CHAIN_chan1.wav'
        f.write_bytes(b'')
        result = fa._archive_empty_wavs([f])
        assert result == []
        assert not f.exists()
        assert (fa.audio_archive / f.name).exists()
        fa.delete_queue.add.assert_not_called()

    def test_mixed_empty_and_nonempty(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa.mount_d = pathlib.Path('.')  # not a real drive
        good = tmp_path / 'good.wav'
        good.write_bytes(b'\x00' * 256)
        bad = tmp_path / 'bad.wav'
        bad.write_bytes(b'')
        result = fa._archive_empty_wavs([good, bad])
        assert result == [good]
        fa.delete_queue.add.assert_called_once()


# ---------------------------------------------------------------------------
# TestZipWavGroup — direct tests for the shared _zip_wav_group helper
# ---------------------------------------------------------------------------

class TestZipWavGroup:
    def _wav(self, parent: pathlib.Path, name: str) -> pathlib.Path:
        f = parent / name
        f.write_bytes(_REAL_WAV)
        return f

    def test_creates_zip(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f = self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        fa._zip_wav_group(
            '26-01-01_Band', [f],
            zip_dest=fa.audio_dest,
            trim_dir=tmp_path,
            on_success_real_drive='delete',
        )
        assert (fa.audio_dest / perf_output_name('26-01-01_Band', 'multitrack')).exists()

    def test_skips_existing_zip(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f = self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        existing = fa.audio_dest / perf_output_name('26-01-01_Band', 'multitrack')
        existing.write_bytes(b'sentinel')
        fa._zip_wav_group(
            '26-01-01_Band', [f],
            zip_dest=fa.audio_dest,
            trim_dir=tmp_path,
            on_success_real_drive='delete',
        )
        assert existing.read_bytes() == b'sentinel'
        fa.delete_queue.add.assert_called_once()

    def test_on_success_delete_queues_source(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        f = self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        fa._zip_wav_group(
            '26-01-01_Band', [f],
            zip_dest=fa.audio_dest,
            trim_dir=tmp_path,
            on_success_real_drive='delete',
        )
        fa.delete_queue.add.assert_called_once()
        reason = fa.delete_queue.add.call_args[0][1]
        assert 'zipped' in reason

    def test_on_success_archive_moves_source(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa.mount_d = tmp_path  # real drive → archive branch
        f = self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        fa._zip_wav_group(
            '26-01-01_Band', [f],
            zip_dest=fa.audio_dest,
            trim_dir=tmp_path,
            on_success_real_drive='archive',
        )
        assert not f.exists()
        assert (fa.audio_archive / f.name).exists()
        fa.delete_queue.add.assert_not_called()

    def test_updates_audio_progress_row(self, tmp_path):
        """_zip_wav_group should call update_audio_progress + clear_row on the app."""
        fa = _FakeAudio(tmp_path)
        fa._app = MagicMock()
        files = [self._wav(tmp_path, f'26-01-01_Band_ch{n:02d}.wav') for n in range(1, 4)]
        fa._zip_wav_group(
            '26-01-01_Band', files,
            zip_dest=fa.audio_dest,
            trim_dir=tmp_path,
            on_success_real_drive='delete',
        )
        # Progress callback fires per file when total > 1
        assert fa._app.update_audio_progress.called, \
            "Expected update_audio_progress to be called"
        # Always cleared on the way out
        fa._app.clear_row.assert_called_with('audio_progress')

    def test_export_audio_zips_drives_audio_row(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa._app = MagicMock()
        # Two channel files for one perf → total > 1 → progress fires
        for n in (1, 2):
            (tmp_path / f'26-01-01_Band_ch{n:02d}.wav').write_bytes(_REAL_WAV)
        fa._export_audio_zips()
        assert fa._app.update_audio_progress.called

    def test_progress_includes_eta_for_multi_file_zip(self, tmp_path):
        """update_audio_progress receives a non-empty eta string once ≥ 2 files are done.

        Real test ZIPs complete in microseconds, so we fake time.monotonic to
        return monotonically increasing values that simulate ~1.5 s per file.
        """
        fa = _FakeAudio(tmp_path)
        fa._app = MagicMock()
        files = [
            self._wav(tmp_path, f'26-01-01_Band_ch{n:02d}.wav')
            for n in range(1, 5)
        ]

        counter = [0.0]
        def _fake_mono() -> float:
            counter[0] += 1.5
            return counter[0]

        with patch('nofun.audio.time.monotonic', side_effect=_fake_mono):
            fa._zip_wav_group(
                '26-01-01_Band', files,
                zip_dest=fa.audio_dest,
                trim_dir=tmp_path,
                on_success_real_drive='delete',
            )
        # The 6th positional arg of update_audio_progress is eta_str
        eta_strings = [
            call.args[5] if len(call.args) > 5 else call.kwargs.get('eta_str', '')
            for call in fa._app.update_audio_progress.call_args_list
        ]
        assert any(s and 'eta ' in s for s in eta_strings), \
            f"Expected an 'eta ' in progress calls; got eta args: {eta_strings}"


# ---------------------------------------------------------------------------
# TestSplitMultichannelWavs — multichannel WAV → per-channel files
# ---------------------------------------------------------------------------

class TestSplitMultichannelWavs:
    def _wav(self, path: pathlib.Path, name: str) -> pathlib.Path:
        f = path / name
        f.write_bytes(b'\x00' * 256)
        return f

    def _ok_result(self) -> ScriptResult:
        return ScriptResult(
            script='split_audio', exit_code=0,
            stdout_json={}, stderr_tail='', elapsed=0.0,
        )

    def test_skips_single_channel_file(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        wav = self._wav(tmp_path, '26-01-01_Band.wav')
        with patch('nofun.audio.probe_stream', return_value='1'):
            fa._split_multichannel_wavs([wav])
        fa._script_runner.run.assert_not_called()

    def test_skips_when_ch01_already_exists(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        wav = self._wav(tmp_path, '26-01-01_Band.wav')
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        with patch('nofun.audio.probe_stream', return_value='2'):
            fa._split_multichannel_wavs([wav])
        fa._script_runner.run.assert_not_called()

    def test_retries_archive_when_ch01_exists_and_original_remains(self, tmp_path):
        # If shutil.move failed on a previous run, the original multi-ch WAV may
        # still be in search_dir beside the already-split _ch01.wav.  The skip path
        # should re-try moving it to audio_archive.
        fa         = _FakeAudio(tmp_path)
        fa.mount_d  = tmp_path
        wav        = self._wav(tmp_path, '26-01-01_Band.wav')
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        with patch('nofun.audio.probe_stream', return_value='2'):
            fa._split_multichannel_wavs([wav])
        fa._script_runner.run.assert_not_called()
        assert not wav.exists(), "original WAV should have been archived"
        assert (fa.audio_archive / wav.name).exists()

    def test_runner_called_with_split_audio_script(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        wav = self._wav(tmp_path, '26-01-01_Band.wav')
        with patch('nofun.audio.probe_stream', side_effect=['2', 'pcm_s24le']):
            fa._split_multichannel_wavs([wav])
        split_calls = [c for c in fa._script_runner.run.call_args_list
                       if c[0][0].script == 'split_audio']
        assert len(split_calls) == 1
        assert split_calls[0][0][0].args['base'] == '26-01-01_Band'

    def test_archives_original_on_real_drive(self, tmp_path):
        fa        = _FakeAudio(tmp_path)
        fa.mount_d = tmp_path
        wav       = self._wav(tmp_path, '26-01-01_Band.wav')

        def _run_side_effect(job, **kw):
            (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
            return self._ok_result()

        fa._script_runner.run.side_effect = _run_side_effect
        with patch('nofun.audio.probe_stream', side_effect=['2', 'pcm_s24le']), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._split_multichannel_wavs([wav])

        assert not wav.exists()
        assert (fa.audio_archive / wav.name).exists()

    def test_queues_original_for_deletion_in_trial_mode(self, tmp_path):
        fa           = _FakeAudio(tmp_path)
        fa.trial_run = 5
        wav          = self._wav(tmp_path, '26-01-01_Band.wav')

        def _run_side_effect(job, **kw):
            (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
            return self._ok_result()

        fa._script_runner.run.side_effect = _run_side_effect
        with patch('nofun.audio.probe_stream', side_effect=['2', 'pcm_s24le']), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._split_multichannel_wavs([wav])

        fa.delete_queue.add.assert_called()
        reasons = [call.args[1] for call in fa.delete_queue.add.call_args_list]
        assert any('trial' in r for r in reasons)

    def test_silent_channels_archived(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        wav = self._wav(tmp_path, '26-01-01_Band.wav')
        ch1 = tmp_path / '26-01-01_Band_ch01.wav'
        ch2 = tmp_path / '26-01-01_Band_ch02.wav'

        def _run_side_effect(job, **kw):
            ch1.write_bytes(b'\x00')
            ch2.write_bytes(b'\x00')
            return self._ok_result()

        fa._script_runner.run.side_effect = _run_side_effect
        with patch('nofun.audio.probe_stream', side_effect=['2', 'pcm_s24le']), \
             patch.object(fa, '_detect_silence_batch',
                          return_value={str(ch1): 0.0, str(ch2): 0.0}), \
             patch.object(fa, '_archive_or_dedup') as mock_archive:
            fa._split_multichannel_wavs([wav])

        assert mock_archive.call_count >= 2
        dests = [call.args[1] for call in mock_archive.call_args_list]
        assert all(d == fa.audio_archive for d in dests)

    def test_probe_stream_called_for_channels_and_codec(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        wav = self._wav(tmp_path, '26-01-01_Band.wav')
        entries: list = []

        def _fake_probe(path, entry, stream='a:0'):
            entries.append(entry)
            if entry == 'channels':
                return '2'
            return 'pcm_s24le'

        with patch('nofun.audio.probe_stream', side_effect=_fake_probe), \
             patch.object(fa, '_active_seconds', return_value=300.0):
            fa._split_multichannel_wavs([wav])

        assert 'channels' in entries
        assert 'codec_name' in entries

# ---------------------------------------------------------------------------
# TestSweepLeftoverWavs — routes leftover WAVs by filename pattern + mount
# ---------------------------------------------------------------------------

class TestSweepLeftoverWavs:
    def _wav(self, path: pathlib.Path, name: str) -> pathlib.Path:
        f = path / name
        f.write_bytes(b'\x00' * 256)
        return f

    def test_empty_search_dir_is_noop(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        fa._sweep_leftover_wavs()
        fa.delete_queue.add.assert_not_called()

    def test_channel_wavs_queued_for_deletion(self, tmp_path):
        fa = _FakeAudio(tmp_path)
        self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        self._wav(tmp_path, '26-01-01_Band_ch02.wav')
        fa._sweep_leftover_wavs()
        assert fa.delete_queue.add.call_count == 2
        reasons = [call.args[1] for call in fa.delete_queue.add.call_args_list]
        assert all('leftover channel WAV' in r for r in reasons)

    def test_original_wav_archived_on_real_drive(self, tmp_path):
        fa         = _FakeAudio(tmp_path)
        fa.mount_d  = tmp_path
        wav        = self._wav(tmp_path, '26-01-01_Band.wav')
        with patch.object(fa, '_archive_or_dedup') as mock_archive:
            fa._sweep_leftover_wavs()
        mock_archive.assert_called_once_with(wav, fa.audio_archive, fa.audio_dest)

    def test_original_wav_queued_without_real_drive(self, tmp_path):
        fa  = _FakeAudio(tmp_path)
        fa.mount_d = pathlib.Path('.')  # not a real drive
        self._wav(tmp_path, '26-01-01_Band.wav')
        fa._sweep_leftover_wavs()
        fa.delete_queue.add.assert_called_once()
        reason = fa.delete_queue.add.call_args.args[1]
        assert 'leftover WAV' in reason

    def test_trial_mode_queues_instead_of_archiving(self, tmp_path):
        fa          = _FakeAudio(tmp_path)
        fa.mount_d   = tmp_path
        fa.trial_run = 5
        self._wav(tmp_path, '26-01-01_Band.wav')
        with patch.object(fa, '_archive_or_dedup') as mock_archive:
            fa._sweep_leftover_wavs()
        mock_archive.assert_not_called()
        fa.delete_queue.add.assert_called_once()

    def test_mixed_channel_and_original_in_same_dir(self, tmp_path):
        fa         = _FakeAudio(tmp_path)
        fa.mount_d  = tmp_path
        self._wav(tmp_path, '26-01-01_Band_ch01.wav')
        original = self._wav(tmp_path, '26-01-01_Band.wav')
        with patch.object(fa, '_archive_or_dedup') as mock_archive:
            fa._sweep_leftover_wavs()
        # Channel file → delete_queue; original → archive
        fa.delete_queue.add.assert_called_once()
        mock_archive.assert_called_once_with(original, fa.audio_archive, fa.audio_dest)


# ---------------------------------------------------------------------------
# TestMROArchiveOrDedup
# ---------------------------------------------------------------------------

class TestMROArchiveOrDedup:
    """Guard against AudioMixin stub shadowing CleanupMixin._archive_or_dedup.

    Pipeline(VideoMixin, AudioMixin, CleanupMixin) — if AudioMixin defines
    _archive_or_dedup as a runtime stub, Python's MRO finds it before
    CleanupMixin's real implementation, and all archiving silently does nothing.
    """

    def test_audio_mixin_has_no_runtime_archive_or_dedup(self):
        """AudioMixin must NOT define _archive_or_dedup as a runtime method."""
        assert '_archive_or_dedup' not in AudioMixin.__dict__, (
            "AudioMixin.__dict__ contains _archive_or_dedup — this stubs out "
            "CleanupMixin's real implementation via MRO. Use TYPE_CHECKING to "
            "declare it for type checkers only."
        )
