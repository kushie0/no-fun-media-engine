"""Unit tests for nofun/cleanup.py — FindingKind, AuditFinding, _audit_pipeline_state."""

import datetime
import logging
import os
import pathlib
import queue
import shutil
import zipfile
from unittest import mock
from unittest.mock import MagicMock, patch

from nofun.cleanup import AuditFinding, EXPIRE_AGE, FindingKind, archive_or_dedup, canonical_sharepoint_name, make_sharepoint_folder_name, write_sharepoint_info
from nofun.media_io import DeleteQueue


def _age_file(path: pathlib.Path, seconds: int = 7200) -> None:
    """Back-date a file's mtime so age checks treat it as old."""
    t = path.stat().st_mtime - seconds
    os.utime(path, (t, t))


from tests.fake_pipeline import FakePipeline as _FakePipeline


class TestFindingKind:
    def test_all_values_exist(self):
        kinds = {fk.value for fk in FindingKind}
        assert 'orphaned_temp'     in kinds
        assert 'redundant_source'  in kinds
        assert 'orphaned_channels' in kinds
        assert 'missing_clips'     in kinds
        assert 'orphaned_clips'    in kinds
        assert 'archive_dedup'     in kinds
        assert 'cloud_expired'     in kinds


class TestAuditFinding:
    def test_creation(self, tmp_path):
        f = tmp_path / 'foo.mp4'
        f.write_bytes(b'\x00' * 100)
        finding = AuditFinding(
            kind=FindingKind.ORPHANED_TEMP,
            label='test finding',
            files=[f],
            action='delete',
            size_bytes=100,
        )
        assert finding.kind == FindingKind.ORPHANED_TEMP
        assert finding.label == 'test finding'
        assert finding.action == 'delete'
        assert finding.destination is None
        assert finding.reason == ''


class TestAuditOrphanedTemp:
    def test_detects_temp_mp4(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / 'foo_temp_bar.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.ORPHANED_TEMP in [f.kind for f in findings]

    def test_detects_trial_temp(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / 'temp_trial_foo.wav').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.ORPHANED_TEMP in [f.kind for f in findings]

    def test_ignores_normal_mp4(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / 'foo_CAM1.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.ORPHANED_TEMP not in [f.kind for f in findings]


class TestAuditRedundantSource:
    def test_mov_with_all_quads_is_redundant(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.mov').write_bytes(b'\x00')
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'26-01-01_Band_{q}.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.REDUNDANT_SOURCE in [f.kind for f in findings]

    def test_mov_without_all_quads_not_redundant(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.mov').write_bytes(b'\x00')
        (fp.vids_dest / '26-01-01_Band_CAM1.mp4').write_bytes(b'\x00')  # only 1 of 4
        findings = fp._audit_pipeline_state()
        assert FindingKind.REDUNDANT_SOURCE not in [f.kind for f in findings]

    def test_wav_with_split_is_redundant(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.wav').write_bytes(b'\x00')
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.REDUNDANT_SOURCE in [f.kind for f in findings]


class TestAuditMissingClips:
    def test_quads_without_clips_flagged(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            f = fp.vids_dest / f'26-01-01_Band_{q}.mp4'
            f.write_bytes(b'\x00')
            _age_file(f)
        findings = fp._audit_pipeline_state()
        assert FindingKind.MISSING_CLIPS in [f.kind for f in findings]

    def test_quads_with_clips_not_flagged(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'26-01-01_Band_{q}.mp4').write_bytes(b'\x00')
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (clips_dir / f'26-01-01_Band_{q}_1.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.MISSING_CLIPS not in [f.kind for f in findings]


    def test_partial_clips_flagged(self, tmp_path):
        """Quads where clip counts differ (cancelled mid-quad) are flagged."""
        fp = _FakePipeline(tmp_path)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            f = fp.vids_dest / f'26-01-01_Band_{q}.mp4'
            f.write_bytes(b'\x00')
            _age_file(f)
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        for i in range(1, 6):
            (clips_dir / f'26-01-01_Band_CAM1_{i}.mp4').write_bytes(b'\x00')
        for i in range(1, 4):
            (clips_dir / f'26-01-01_Band_CAM2_{i}.mp4').write_bytes(b'\x00')
        result = fp._check_missing_clips()
        assert len(result) == 1
        assert result[0].kind == FindingKind.MISSING_CLIPS


class TestAuditOrphanedClips:
    def test_clips_without_quads_flagged(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        (clips_dir / '26-01-01_Band_CAM1_1.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.ORPHANED_CLIPS in [f.kind for f in findings]

    def test_clips_with_quads_not_orphaned(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'26-01-01_Band_{q}.mp4').write_bytes(b'\x00')
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        (clips_dir / '26-01-01_Band_CAM1_1.mp4').write_bytes(b'\x00')
        findings = fp._audit_pipeline_state()
        assert FindingKind.ORPHANED_CLIPS not in [f.kind for f in findings]


class TestAuditArchiveDedup:
    def test_archive_matching_live_is_flagged(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        content = b'\x00' * 256
        (fp.vids_dest / 'foo_CAM1.mp4').write_bytes(content)
        (fp.video_archive / 'foo_CAM1.mp4').write_bytes(content)
        findings = fp._audit_pipeline_state()
        assert FindingKind.ARCHIVE_DEDUP in [f.kind for f in findings]

    def test_archive_different_size_not_flagged(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (fp.vids_dest / 'foo_CAM1.mp4').write_bytes(b'\x00' * 256)
        (fp.video_archive / 'foo_CAM1.mp4').write_bytes(b'\x00' * 512)
        findings = fp._audit_pipeline_state()
        assert FindingKind.ARCHIVE_DEDUP not in [f.kind for f in findings]


class TestEmptyDirectoriesProduceNoFindings:
    def test_no_findings_on_empty_dirs(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        findings = fp._audit_pipeline_state()
        assert findings == []


# ---------------------------------------------------------------------------
# TestAuditChecks — isolated tests for each _check_*() method
# ---------------------------------------------------------------------------

class TestAuditChecks:
    def test_check_orphaned_temps(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / 'leftover_temp_encode.mp4').write_bytes(b'\x00')
        result = fp._check_orphaned_temps()
        assert len(result) == 1
        assert result[0].kind == FindingKind.ORPHANED_TEMP
        assert result[0].action == 'delete'

    def test_check_orphaned_temps_empty(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        assert fp._check_orphaned_temps() == []

    def test_check_redundant_mov_sources(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.mov').write_bytes(b'\x00')
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'26-01-01_Band_{q}.mp4').write_bytes(b'\x00')
        result = fp._check_redundant_mov_sources()
        assert len(result) == 1
        assert result[0].kind == FindingKind.REDUNDANT_SOURCE
        assert result[0].action == 'move'
        assert result[0].destination == fp.video_archive

    def test_check_redundant_mov_sources_partial_quads(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.mov').write_bytes(b'\x00')
        (fp.vids_dest / '26-01-01_Band_CAM1.mp4').write_bytes(b'\x00')  # only 1 of 4
        assert fp._check_redundant_mov_sources() == []

    def test_check_redundant_wav_sources(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.wav').write_bytes(b'\x00')
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        result = fp._check_redundant_wav_sources()
        assert len(result) == 1
        assert result[0].kind == FindingKind.REDUNDANT_SOURCE
        assert result[0].action == 'delete'

    def test_check_redundant_wav_sources_no_split(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band.wav').write_bytes(b'\x00')
        assert fp._check_redundant_wav_sources() == []

    def test_check_orphaned_channel_wavs(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        (tmp_path / '26-01-01_Band_ch02.wav').write_bytes(b'\x00')
        (fp.audio_dest / '26-01-01_Band_MULTITRACK.zip').write_bytes(b'\x00')
        result = fp._check_orphaned_channel_wavs()
        assert len(result) == 1
        assert result[0].kind == FindingKind.ORPHANED_CHANNELS
        assert len(result[0].files) == 2

    def test_check_orphaned_channel_wavs_no_zip(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band_ch01.wav').write_bytes(b'\x00')
        assert fp._check_orphaned_channel_wavs() == []

    def test_check_orphaned_hardware_wavs(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band_chan1.wav').write_bytes(b'\x00')
        (tmp_path / '26-01-01_Band_chan2.wav').write_bytes(b'\x00')
        (fp.audio_dest / '26-01-01_Band_MULTITRACK.zip').write_bytes(b'\x00')
        result = fp._check_orphaned_hardware_wavs()
        assert len(result) == 1
        assert result[0].kind == FindingKind.ORPHANED_CHANNELS
        assert result[0].action == 'move'
        assert len(result[0].files) == 2

    def test_check_orphaned_hardware_wavs_audio_subdir(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        audio_dir = tmp_path / 'Audio'
        audio_dir.mkdir(exist_ok=True)
        (audio_dir / '26-01-01_Band_chan1.wav').write_bytes(b'\x00')
        (fp.audio_dest / '26-01-01_Band_MULTITRACK.zip').write_bytes(b'\x00')
        result = fp._check_orphaned_hardware_wavs()
        assert len(result) == 1
        assert len(result[0].files) == 1

    def test_check_orphaned_hardware_wavs_no_zip_no_db(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band_chan1.wav').write_bytes(b'\x00')
        assert fp._check_orphaned_hardware_wavs() == []

    def test_check_orphaned_hardware_wavs_all_silent_no_zip(self, tmp_path):
        from nofun.encoding_db import EncodingDB
        fp = _FakePipeline(tmp_path)
        (tmp_path / '26-01-01_Band_chan1.wav').write_bytes(b'\x00' * 100)
        (tmp_path / '26-01-01_Band_chan2.wav').write_bytes(b'\x00' * 100)
        fp._encoding_db = EncodingDB(tmp_path / 'db.json')
        fp._encoding_db.upsert('26-01-01', 'Band', 'audio_all_silent',
                               {'path': '26-01-01_Band', 'updated': '2026-01-01T00:00:00'})
        result = fp._check_orphaned_hardware_wavs()
        assert len(result) == 1
        assert result[0].reason == 'all-silent (DB)'
        assert result[0].action == 'move'
        assert len(result[0].files) == 2

    def test_check_missing_clips(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        ul = fp.vids_dest / '26-01-01_Band_CAM1.mp4'
        ul.write_bytes(b'\x00')
        _age_file(ul)
        result = fp._check_missing_clips()
        assert len(result) == 1
        assert result[0].kind == FindingKind.MISSING_CLIPS
        assert result[0].action == 'reprocess'

    def test_check_missing_clips_present(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (fp.vids_dest / '26-01-01_Band_CAM1.mp4').write_bytes(b'\x00')
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        (clips_dir / '26-01-01_Band_CAM1_1.mp4').write_bytes(b'\x00')
        assert fp._check_missing_clips() == []

    def test_check_orphaned_clip_dirs(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        clips_dir = fp.clips_dest / '26-01-01_Ghost'
        clips_dir.mkdir()
        (clips_dir / '26-01-01_Ghost_CAM1_1.mp4').write_bytes(b'\x00')
        result = fp._check_orphaned_clip_dirs()
        assert len(result) == 1
        assert result[0].kind == FindingKind.ORPHANED_CLIPS

    def test_check_orphaned_clip_dirs_has_quads(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'26-01-01_Band_{q}.mp4').write_bytes(b'\x00')
        clips_dir = fp.clips_dest / '26-01-01_Band'
        clips_dir.mkdir()
        assert fp._check_orphaned_clip_dirs() == []

    def test_check_archive_duplicates(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        content = b'\x00' * 256
        (fp.vids_dest / 'foo_CAM1.mp4').write_bytes(content)
        (fp.video_archive / 'foo_CAM1.mp4').write_bytes(content)
        result = fp._check_archive_duplicates()
        assert len(result) == 1
        assert result[0].kind == FindingKind.ARCHIVE_DEDUP

    def test_check_archive_duplicates_different_size(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (fp.vids_dest / 'foo_CAM1.mp4').write_bytes(b'\x00' * 256)
        (fp.video_archive / 'foo_CAM1.mp4').write_bytes(b'\x00' * 512)
        assert fp._check_archive_duplicates() == []

    def test_check_expired_cloud_shares_no_sharepoint(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        fp.sharepoint_dest = None
        assert fp._check_expired_cloud_shares() == []

    def test_check_expired_cloud_shares_returns_finding(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        sharepoint = tmp_path / 'sharepoint'
        sharepoint.mkdir()
        date_dir = sharepoint / '26-01-01'
        date_dir.mkdir()
        (date_dir / '26-01-01_BAND.mp4').write_bytes(b'\x00')
        fp.sharepoint_dest = sharepoint

        result = fp._check_expired_cloud_shares()

        assert len(result) == 1
        assert result[0].kind == FindingKind.CLOUD_EXPIRED


# ---------------------------------------------------------------------------
# make_sharepoint_folder_name
# ---------------------------------------------------------------------------

class TestMakeSharepointFolderName:
    DATE = '26-04-07'

    def _named_folder(self, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
        """Create a folder with the given name under tmp_path."""
        folder = tmp_path / name
        folder.mkdir()
        return folder

    def test_first_band_no_folder_yet(self, tmp_path):
        folder = tmp_path / self.DATE  # does not exist
        result = make_sharepoint_folder_name(self.DATE, folder, 'PRIZE')
        assert result == '26-04-07_PRIZE'

    def test_first_band_bare_date_folder(self, tmp_path):
        folder = self._named_folder(tmp_path, self.DATE)
        result = make_sharepoint_folder_name(self.DATE, folder, 'PRIZE')
        assert result == '26-04-07_PRIZE'

    def test_second_band_added(self, tmp_path):
        folder = self._named_folder(tmp_path, '26-04-07_PRIZE')
        result = make_sharepoint_folder_name(self.DATE, folder, 'MALL_GOTH')
        assert result == '26-04-07_PRIZE_MALLGOTH'

    def test_third_band_added(self, tmp_path):
        folder = self._named_folder(tmp_path, '26-04-07_PRIZE_MALLGOTH')
        result = make_sharepoint_folder_name(self.DATE, folder, 'MX_LONELY')
        assert result == '26-04-07_PRIZE_MALLGOTH_MXLONELY'

    def test_manual_name_respected(self, tmp_path):
        # User manually named the folder; existing tokens preserved as-is
        folder = self._named_folder(tmp_path, '26-04-07_MXLONELY_HALOBITE_MALLGOTH_PRIZE')
        result = make_sharepoint_folder_name(self.DATE, folder, 'HALO_BITE')
        # HALOBITE already present → no rename
        assert result == '26-04-07_MXLONELY_HALOBITE_MALLGOTH_PRIZE'

    def test_manual_name_new_band_appended(self, tmp_path):
        # User manually named folder without one band; that band gets appended
        folder = self._named_folder(tmp_path, '26-04-07_MXLONELY_HALOBITE_MALLGOTH')
        result = make_sharepoint_folder_name(self.DATE, folder, 'PRIZE')
        assert result == '26-04-07_MXLONELY_HALOBITE_MALLGOTH_PRIZE'

    def test_long_band_name_becomes_acronym(self, tmp_path):
        folder = self._named_folder(tmp_path, self.DATE)
        result = make_sharepoint_folder_name(
            self.DATE, folder, 'THEY_ARE_GUTTING_A_BODY_OF_WATER'
        )
        assert result == '26-04-07_TAGABOW'

    def test_dedup_band_already_in_name(self, tmp_path):
        folder = self._named_folder(tmp_path, '26-04-07_PRIZE')
        result = make_sharepoint_folder_name(self.DATE, folder, 'PRIZE')
        assert result == '26-04-07_PRIZE'

    def test_dedup_case_insensitive(self, tmp_path):
        folder = self._named_folder(tmp_path, '26-04-07_PRIZE')
        result = make_sharepoint_folder_name(self.DATE, folder, 'prize')
        assert result == '26-04-07_PRIZE'

    def test_nofun_skipped(self, tmp_path):
        folder = self._named_folder(tmp_path, self.DATE)
        result = make_sharepoint_folder_name(self.DATE, folder, 'NoFun')
        assert result == self.DATE

    def test_nofun_with_underscore_skipped(self, tmp_path):
        folder = self._named_folder(tmp_path, self.DATE)
        result = make_sharepoint_folder_name(self.DATE, folder, 'No_Fun')
        assert result == self.DATE

    def test_tbd_skipped(self, tmp_path):
        folder = self._named_folder(tmp_path, self.DATE)
        result = make_sharepoint_folder_name(self.DATE, folder, 'TBD')
        assert result == self.DATE

    def test_total_over_50_chars_collapses_to_initials(self, tmp_path):
        # 26-04-07 (8) + _BANDONE(8) + _BANDTWO(8) + _BANDTHREE(10) + _BANDFOUR(9) + _BANDFIVE(9) = 52
        folder = self._named_folder(tmp_path, '26-04-07_BANDONE_BANDTWO_BANDTHREE_BANDFOUR')
        result = make_sharepoint_folder_name(self.DATE, folder, 'BAND_FIVE')
        assert result.startswith(self.DATE + '_')
        assert len(result) <= 50

    def test_space_separator_in_manual_name(self, tmp_path):
        # Folder named with a space separator — token is preserved, new name uses _
        folder = self._named_folder(tmp_path, '26-04-07 PRIZE')
        result = make_sharepoint_folder_name(self.DATE, folder, 'MALL_GOTH')
        assert result == '26-04-07_PRIZE_MALLGOTH'


# ---------------------------------------------------------------------------
# canonical_sharepoint_name
# ---------------------------------------------------------------------------

class TestCloudFilename:
    def test_strips_short_date(self):
        from nofun.cleanup import cloud_filename
        assert cloud_filename('26-05-17_DAISY_CHAIN_UL.mp4') == 'DAISY_CHAIN_UL.mp4'

    def test_strips_long_date(self):
        from nofun.cleanup import cloud_filename
        assert cloud_filename('20260517_DAISY_CHAIN.zip') == 'DAISY_CHAIN.zip'

    def test_strips_single_digit_month_day(self):
        from nofun.cleanup import cloud_filename
        assert cloud_filename('26-5-7_BAND_UL.mp4') == 'BAND_UL.mp4'

    def test_idempotent_no_prefix(self):
        from nofun.cleanup import cloud_filename
        assert cloud_filename('BAND_UL.mp4') == 'BAND_UL.mp4'

    def test_idempotent_double_call(self):
        from nofun.cleanup import cloud_filename
        once  = cloud_filename('26-05-17_BAND.zip')
        twice = cloud_filename(once)
        assert once == twice == 'BAND.zip'


class TestCanonicalSharepointName:
    def test_bands_with_spaces_produce_no_whitespace(self):
        # Reproduces the 26-04-18 incident: band names with spaces caused the
        # >50-char fallback to slice mid-word, leaving 'SARA ' with a trailing
        # space. SharePoint stripped it on disk, so the rename target never
        # matched the folder name and the rename fired every 15 minutes.
        result = canonical_sharepoint_name(
            '26-04-18',
            ['Bug Crush', 'LAURA', 'Russel the Leaf', 'Sara Devoe'],
        )
        assert ' ' not in result
        assert result == result.rstrip()


# ---------------------------------------------------------------------------
# Helpers for expiry quality-gate tests
# ---------------------------------------------------------------------------

def _old_mov(fp, base='26-04-06_ALTAR'):
    """Create a .mov in video_archive whose filename implies age > RAW_EXPIRE_AGE."""
    mov = fp.video_archive / f'{base}.mov'
    mov.write_bytes(b'\x00')
    return mov


def _create_quads(fp, base='26-04-06_ALTAR'):
    """Create all 4 dummy quadrant MP4s in vids_dest."""
    for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
        (fp.vids_dest / f'{base}_{q}.mp4').write_bytes(b'\x00')


# ---------------------------------------------------------------------------
# TestQuadsVerified
# ---------------------------------------------------------------------------

class TestQuadsVerified:
    def test_returns_true_when_all_quads_probe_valid(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        base = '26-04-11_ALTAR'
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'{base}_{q}.mp4').write_bytes(b'\x00')

        with patch('nofun.cleanup.probe_video', return_value=('h264', 'High', 'yuv420p')):
            assert fp._quads_verified(base) is True

    def test_returns_false_when_quad_missing(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        base = '26-04-11_ALTAR'
        for q in ('CAM1', 'CAM2', 'CAM3'):  # only 3 of 4
            (fp.vids_dest / f'{base}_{q}.mp4').write_bytes(b'\x00')

        with patch('nofun.cleanup.probe_video', return_value=('h264', 'High', 'yuv420p')):
            assert fp._quads_verified(base) is False

    def test_returns_false_when_probe_returns_unknown(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        base = '26-04-11_ALTAR'
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'{base}_{q}.mp4').write_bytes(b'\x00')

        with patch('nofun.cleanup.probe_video', return_value=('unknown', 'unknown', 'unknown')):
            assert fp._quads_verified(base) is False


# ---------------------------------------------------------------------------
# TestZipVerified
# ---------------------------------------------------------------------------

class TestZipVerified:
    def test_returns_true_for_valid_zip_with_entries(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        zip_path = fp.audio_dest / '26-04-11_ALTAR_MULTITRACK.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('channel_01.wav', b'WAV data')

        assert fp._zip_verified('26-04-11_ALTAR') is True

    def test_returns_false_when_zip_missing(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        assert fp._zip_verified('26-04-11_ALTAR') is False

    def test_returns_false_when_zip_is_zero_bytes(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (fp.audio_dest / '26-04-11_ALTAR_MULTITRACK.zip').write_bytes(b'')
        assert fp._zip_verified('26-04-11_ALTAR') is False

    def test_returns_false_when_zip_is_corrupt(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        (fp.audio_dest / '26-04-11_ALTAR_MULTITRACK.zip').write_bytes(b'not a zip file')
        assert fp._zip_verified('26-04-11_ALTAR') is False

    def test_returns_false_when_zip_has_no_entries(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        zip_path = fp.audio_dest / '26-04-11_ALTAR_MULTITRACK.zip'
        with zipfile.ZipFile(zip_path, 'w'):
            pass  # empty ZIP
        assert fp._zip_verified('26-04-11_ALTAR') is False


# ---------------------------------------------------------------------------
# TestAutoExpireRawWithQualityGate
# Note: _auto_expire_raw_files derives age from the filename date prefix, not
# mtime. Base '26-04-06_ALTAR' parses to 2026-04-06, always > RAW_EXPIRE_AGE
# (10d) on any realistic run date. No mtime manipulation needed.
# ---------------------------------------------------------------------------

class TestAutoExpireRawWithQualityGate:

    def test_mov_deleted_when_quads_verified(self, tmp_path):
        """Happy path: old .mov, all quads exist and probe valid → deleted."""
        fp = _FakePipeline(tmp_path)
        mov = _old_mov(fp)
        _create_quads(fp)

        with patch('nofun.cleanup.probe_video', return_value=('h264', 'High', 'yuv420p')):
            fp._auto_expire_raw_files()

        assert not mov.exists()

    def test_mov_kept_when_quad_probe_fails(self, tmp_path):
        """Quads exist but are corrupt (probe returns 'unknown') → raw .mov kept."""
        fp = _FakePipeline(tmp_path)
        mov = _old_mov(fp)
        _create_quads(fp)

        with patch('nofun.cleanup.probe_video', return_value=('unknown', 'unknown', 'unknown')):
            fp._auto_expire_raw_files()

        assert mov.exists()
        fp.logger.warning.assert_called()
        msg = fp.logger.warning.call_args[0][0]
        assert 'quadrant probe failed' in msg

    def test_wav_deleted_when_zip_verified(self, tmp_path):
        """Old .wav, valid ZIP exists → deleted.
        ZIP is named with the normalized date '26-04-06_ALTAR_MULTITRACK.zip' — the form
        that extract_date_band returns and that _zip_wav_group uses.
        """
        fp = _FakePipeline(tmp_path)
        wav = fp.audio_archive / '26-04-06_ALTAR.wav'
        wav.write_bytes(b'\x00')

        zip_path = fp.audio_dest / '26-04-06_ALTAR_MULTITRACK.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('channel_01.wav', b'audio data')

        fp._auto_expire_raw_files()

        assert not wav.exists()

    def test_wav_kept_when_zip_corrupt(self, tmp_path):
        """Old .wav, ZIP exists but is corrupt → raw .wav kept."""
        fp = _FakePipeline(tmp_path)
        wav = fp.audio_archive / '26-04-06_ALTAR.wav'
        wav.write_bytes(b'\x00')
        (fp.audio_dest / '26-04-06_ALTAR_MULTITRACK.zip').write_bytes(b'not a zip')

        fp._auto_expire_raw_files()

        assert wav.exists()
        fp.logger.warning.assert_called()
        msg = fp.logger.warning.call_args[0][0]
        assert 'ZIP probe failed' in msg

    def test_check_expired_raw_movs_skips_unverified(self, tmp_path):
        """AUTO CLEANUP must not emit findings for raw movs whose quads probe bad."""
        fp = _FakePipeline(tmp_path)
        _old_mov(fp)
        _create_quads(fp)
        with patch('nofun.cleanup.probe_video', return_value=('unknown', 'unknown', 'unknown')):
            findings = fp._check_expired_raw_movs()
        assert findings == []

    def test_check_expired_raw_wavs_skips_corrupt_zip(self, tmp_path):
        """AUTO CLEANUP must not emit findings for raw WAVs whose ZIP is corrupt."""
        fp = _FakePipeline(tmp_path)
        wav = fp.audio_archive / '26-04-06_ALTAR.wav'
        wav.write_bytes(b'\x00')
        (fp.audio_dest / '26-04-06_ALTAR_MULTITRACK.zip').write_bytes(b'not a zip')
        findings = fp._check_expired_raw_wavs()
        assert findings == []


class TestAutoExpireCloudShares:

    def _make_sp_folder(self, tmp_path: pathlib.Path, folder_name: str,
                        filenames: list[str]) -> pathlib.Path:
        sp = tmp_path / 'sharepoint'
        sp.mkdir(exist_ok=True)
        d = sp / folder_name
        d.mkdir()
        for name in filenames:
            (d / name).write_bytes(b'\x00')
        return sp

    def test_old_folder_cleaned_by_name(self, tmp_path):
        """Folder with YY-MM-DD name >40 days old is cleaned even if file mtime is recent."""
        fp = _FakePipeline(tmp_path)
        sp = self._make_sp_folder(tmp_path, '26-03-01', ['26-03-01_BAND_UL.mp4'])
        fp.sharepoint_dest = sp
        # File is on D: already
        (fp.vids_dest / '26-03-01_BAND_UL.mp4').write_bytes(b'\x00')

        fp._auto_expire_cloud_shares()

        assert not (sp / '26-03-01' / '26-03-01_BAND_UL.mp4').exists()

    def test_recent_folder_not_cleaned(self, tmp_path):
        """Folder with a recent YY-MM-DD name (≤40 days) is left alone."""
        fp = _FakePipeline(tmp_path)
        today = datetime.date.today()
        recent = today.strftime('%y-%m-%d')
        sp = self._make_sp_folder(tmp_path, recent, [f'{recent}_BAND_UL.mp4'])
        fp.sharepoint_dest = sp
        (fp.vids_dest / f'{recent}_BAND_UL.mp4').write_bytes(b'\x00')

        fp._auto_expire_cloud_shares()

        assert (sp / recent / f'{recent}_BAND_UL.mp4').exists()

    def test_cleaned_folder_moved_to_archived(self, tmp_path):
        """After all media files are deleted the folder moves to archived/."""
        fp = _FakePipeline(tmp_path)
        sp = self._make_sp_folder(tmp_path, '26-03-01', ['26-03-01_BAND_UL.mp4'])
        fp.sharepoint_dest = sp
        (fp.vids_dest / '26-03-01_BAND_UL.mp4').write_bytes(b'\x00')

        fp._auto_expire_cloud_shares()

        assert (sp / 'archived' / '26-03-01').is_dir()
        assert not (sp / '26-03-01').exists()

    def test_unparseable_folder_name_falls_back_to_mtime(self, tmp_path):
        """Folder with non-date name falls back to file mtime for age check."""
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir()
        d = sp / 'misc-folder'
        d.mkdir()
        f = d / 'somefile.mp4'
        f.write_bytes(b'\x00')
        # Back-date file mtime by 50 days so it looks old
        old_ts = (datetime.date.today() - datetime.timedelta(days=50)).timetuple()
        import time
        old_time = time.mktime(old_ts)
        os.utime(f, (old_time, old_time))
        fp.sharepoint_dest = sp
        (fp.vids_dest / 'somefile.mp4').write_bytes(b'\x00')

        fp._auto_expire_cloud_shares()

        assert not f.exists()

    def test_skips_recent_empty_folder(self, tmp_path):
        """Empty folder with a recent date name must NOT be archived.

        Regression for log_bugs.md #5 — _maybe_create_sharepoint_placeholder
        creates empty folders while recordings are in progress; Path A was
        archiving them immediately with no age check.
        """
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir(exist_ok=True)
        today = datetime.date.today().strftime('%y-%m-%d')
        new_empty = sp / f'{today}_OPENMIC'
        new_empty.mkdir()
        fp.sharepoint_dest = sp

        fp._auto_expire_cloud_shares()

        assert new_empty.exists(), "fresh empty folder must not be archived"
        assert not (sp / 'archived' / new_empty.name).exists()

    def test_archives_old_empty_folder(self, tmp_path):
        """Empty folder older than EXPIRE_AGE IS archived by Path A.

        Sister test to confirm the age guard doesn't disable Path A entirely
        for legitimately drained folders.
        """
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir(exist_ok=True)
        old_date = (datetime.date.today() - datetime.timedelta(days=EXPIRE_AGE + 5))
        old_name = old_date.strftime('%y-%m-%d') + '_OLDBAND'
        old_empty = sp / old_name
        old_empty.mkdir()
        fp.sharepoint_dest = sp

        fp._auto_expire_cloud_shares()

        assert not old_empty.exists(), "old empty folder should be archived"
        assert (sp / 'archived' / old_name).exists()

    def test_lease_overrides_folder_name(self, tmp_path):
        """Folder dated 100 days ago but with a fresh lease must NOT be cleaned."""
        fp = _FakePipeline(tmp_path)
        sp = self._make_sp_folder(tmp_path, '26-01-01', ['26-01-01_BAND_UL.mp4'])
        fp.sharepoint_dest = sp
        (fp.vids_dest / '26-01-01_BAND_UL.mp4').write_bytes(b'\x00')
        future = datetime.date.today() + datetime.timedelta(days=27)
        (sp / '26-01-01' / '_nofun_info.txt').write_text(
            f'NO FUN TROY\n\nexpiry: {future.isoformat()}\n\nget em…'
        )

        fp._auto_expire_cloud_shares()

        assert (sp / '26-01-01' / '26-01-01_BAND_UL.mp4').exists()

    def test_lease_in_past_overrides_folder_name(self, tmp_path):
        """Recent folder name but past lease — file must be deleted."""
        fp = _FakePipeline(tmp_path)
        today = datetime.date.today()
        recent = today.strftime('%y-%m-%d')
        sp = self._make_sp_folder(tmp_path, recent, [f'{recent}_BAND_UL.mp4'])
        fp.sharepoint_dest = sp
        (fp.vids_dest / f'{recent}_BAND_UL.mp4').write_bytes(b'\x00')
        past = today - datetime.timedelta(days=1)
        (sp / recent / '_nofun_info.txt').write_text(
            f'NO FUN TROY\n\nexpiry: {past.isoformat()}\n\nget em…'
        )

        fp._auto_expire_cloud_shares()

        assert not (sp / recent / f'{recent}_BAND_UL.mp4').exists()


class TestApplyFindingsCloudExpired:

    def test_cloud_file_backed_up_before_delete(self, tmp_path):
        """File not in D: archive is copied to vids_dest before delete-queue add."""
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir()
        d = sp / '26-01-01'
        d.mkdir()
        cloud_f = d / '26-01-01_BAND_UL.mp4'
        cloud_f.write_bytes(b'CLOUD CONTENT')
        finding = AuditFinding(
            kind=FindingKind.CLOUD_EXPIRED,
            label='', files=[cloud_f], action='delete',
            reason='expired', size_bytes=13,
        )
        fp.sharepoint_dest = sp
        fp._apply_findings([finding])
        assert (fp.vids_dest / '26-01-01_BAND_UL.mp4').exists()
        assert len(fp.delete_queue.items) == 1

    def test_skipped_when_already_in_archive(self, tmp_path):
        """No rehydration log when file is already in D: archive."""
        fp = _FakePipeline(tmp_path)
        cloud_f = tmp_path / '26-01-01_BAND_UL.mp4'
        cloud_f.write_bytes(b'\x00')
        (fp.vids_dest / '26-01-01_BAND_UL.mp4').write_bytes(b'\x00')
        finding = AuditFinding(
            kind=FindingKind.CLOUD_EXPIRED,
            label='', files=[cloud_f], action='delete',
            reason='expired', size_bytes=1,
        )
        fp._apply_findings([finding])
        logged = [c[0][0] for c in fp.logger.info.call_args_list]
        assert not any('rehydrating' in m for m in logged)


class TestReadExpiryDate:

    def test_parses_iso_tag(self, tmp_path):
        from nofun.cleanup import _read_expiry_date
        d = tmp_path / 'folder'
        d.mkdir()
        (d / '_nofun_info.txt').write_text(
            'NO FUN TROY\n\nexpiry: 2026-06-01\n\nget em…'
        )
        assert _read_expiry_date(d) == datetime.date(2026, 6, 1)

    def test_missing_file_returns_none(self, tmp_path):
        from nofun.cleanup import _read_expiry_date
        d = tmp_path / 'empty'
        d.mkdir()
        assert _read_expiry_date(d) is None

    def test_unparseable_tag_returns_none(self, tmp_path):
        from nofun.cleanup import _read_expiry_date
        d = tmp_path / 'bad'
        d.mkdir()
        (d / '_nofun_info.txt').write_text('expiry: tomorrow\n')
        assert _read_expiry_date(d) is None


class TestDehydrationSweep:

    def test_skips_when_no_sharepoint(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        fp.sharepoint_dest = None
        fp._dehydration_sweep()  # must not raise
        fp.logger.info.assert_not_called()

    def test_calls_dehydrate_for_hydrated_files(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir()
        d = sp / '26-04-07_BAND'
        d.mkdir()
        f1 = d / 'a.mp4'
        f1.write_bytes(b'\x00')
        f2 = d / 'b.zip'
        f2.write_bytes(b'\x00')
        fp.sharepoint_dest = sp
        with patch('nofun.media_io.is_cloud_only', return_value=False), \
             patch('nofun.media_io.dehydrate_cloud_files') as dh:
            fp._dehydration_sweep()
        dh.assert_called_once()
        assert sorted(p.name for p in dh.call_args[0][0]) == ['a.mp4', 'b.zip']

    def test_skips_already_cloud_only(self, tmp_path):
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir()
        d = sp / '26-04-07_BAND'
        d.mkdir()
        (d / 'a.mp4').write_bytes(b'\x00')
        fp.sharepoint_dest = sp
        with patch('nofun.media_io.is_cloud_only', return_value=True), \
             patch('nofun.media_io.dehydrate_cloud_files') as dh:
            fp._dehydration_sweep()
        dh.assert_not_called()

    def test_skips_archived_subfolder(self, tmp_path):
        """The 'archived/' folder is NOT walked — those files are gone."""
        fp = _FakePipeline(tmp_path)
        sp = tmp_path / 'sharepoint'
        sp.mkdir()
        (sp / 'archived').mkdir()
        (sp / 'archived' / 'a.mp4').write_bytes(b'\x00')
        fp.sharepoint_dest = sp
        with patch('nofun.media_io.is_cloud_only', return_value=False), \
             patch('nofun.media_io.dehydrate_cloud_files') as dh:
            fp._dehydration_sweep()
        dh.assert_not_called()


class TestArchiveOrDedup:
    """Regression tests for log_bugs.md #2, #3 — _pipeline_moved registration."""

    def _logger(self):
        return logging.getLogger('test_archive')

    def test_registers_before_move(self, tmp_path):
        """pipeline_moved must be populated BEFORE shutil.move runs.

        Regression for log_bugs.md #3 — REMOVED fired for in-flight moves
        because registration happened after the (slow) filesystem op.
        """
        moved: queue.Queue[str] = queue.Queue()
        src = tmp_path / 'src.wav'
        src.write_bytes(b'x' * 100)
        archive_dir = tmp_path / 'archive'
        archive_dir.mkdir()

        real_move = shutil.move
        def checked_move(s, d):
            assert not moved.empty(), "pipeline_moved must be set before shutil.move"
            return real_move(s, d)

        with mock.patch('nofun.cleanup.shutil.move', side_effect=checked_move):
            archive_or_dedup(src, archive_dir, self._logger(), DeleteQueue(),
                             pipeline_moved=moved)

        items: list[str] = []
        try:
            while True:
                items.append(moved.get_nowait())
        except queue.Empty:
            pass
        assert str(src) in items


class TestSyncEligiblePerformances:
    """Regression tests for the SharePoint upload-delete loop.

    A performance whose age has reached EXPIRE_AGE must not be uploaded — its
    lease (rec_date + EXPIRE_AGE) would already be at/past today, so the next
    EXPIRE CLOUD SHARES tick would delete the upload, restarting the cycle.

    See docs/active/archive/2026-05-09_sharepoint.md (SHAREPOINT_CLEANUP_RESEARCH section).
    """

    def _make_pipeline(self, tmp_path: pathlib.Path):
        from media_engine import Pipeline
        fp = _FakePipeline(tmp_path)
        # Borrow the methods under test from the real Pipeline class
        fp._sync_eligible_performances = Pipeline._sync_eligible_performances.__get__(fp)
        fp._find_date_folder = Pipeline._find_date_folder
        # encoding_db.upsert/save are called for every uploaded file; mock both
        fp._encoding_db = MagicMock()
        fp._app = None
        return fp

    def _seed_quads(self, fp, base: str) -> None:
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (fp.vids_dest / f'{base}_{q}.mp4').write_bytes(b'\x00')

    def test_sync_skips_files_at_or_past_expire_age(self, tmp_path):
        fp = self._make_pipeline(tmp_path)
        rec = datetime.date.today() - datetime.timedelta(days=EXPIRE_AGE)
        base = f'{rec.strftime("%y-%m-%d")}_BAND'
        self._seed_quads(fp, base)
        fp.sharepoint_dest = tmp_path / 'sharepoint'
        fp.sharepoint_dest.mkdir()

        fp._sync_eligible_performances()

        assert not list(fp.sharepoint_dest.rglob(f'{base}_CAM1.mp4')), \
            f"must not upload at age={EXPIRE_AGE} — would create dead-on-arrival lease"

    def test_sync_uploads_files_just_under_expire_age(self, tmp_path):
        fp = self._make_pipeline(tmp_path)
        rec = datetime.date.today() - datetime.timedelta(days=EXPIRE_AGE - 1)
        base = f'{rec.strftime("%y-%m-%d")}_BAND'
        self._seed_quads(fp, base)
        fp.sharepoint_dest = tmp_path / 'sharepoint'
        fp.sharepoint_dest.mkdir()

        fp._sync_eligible_performances()

        # Cloud filenames have the date prefix stripped (see cloud_filename())
        assert list(fp.sharepoint_dest.rglob('BAND_CAM1.mp4')), \
            f"should upload — age={EXPIRE_AGE - 1} still has lease left"
        assert not list(fp.sharepoint_dest.rglob(f'{base}_CAM1.mp4')), \
            "cloud filename must not retain date prefix"

    def test_sync_renames_existing_dated_copy_instead_of_reupload(self, tmp_path):
        """Pre-existing dated cloud copy must be renamed in place, not re-uploaded.

        Without this, a deploy of cloud_filename() against legacy folders doubles
        cloud storage and burns hours of bandwidth re-uploading content the cloud
        already has.
        """
        fp = self._make_pipeline(tmp_path)
        rec  = datetime.date.today() - datetime.timedelta(days=1)
        prefix = rec.strftime("%y-%m-%d")
        base = f'{prefix}_BAND'

        # Source quads on D:
        self._seed_quads(fp, base)

        # Pre-existing cloud folder with dated copies (from old engine)
        fp.sharepoint_dest = tmp_path / 'sharepoint'
        sp_folder = fp.sharepoint_dest / prefix
        sp_folder.mkdir(parents=True)
        sentinel_bytes = b'pre-existing cloud content'
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (sp_folder / f'{base}_{q}.mp4').write_bytes(sentinel_bytes)

        fp._sync_eligible_performances()

        # The sync also renames the folder to include band tokens — find it.
        final_folder = next(
            p for p in fp.sharepoint_dest.iterdir()
            if p.is_dir() and p.name.startswith(prefix)
        )

        # After sync: stripped names exist, dated names gone, content unchanged
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            stripped = final_folder / f'BAND_{q}.mp4'
            dated    = final_folder / f'{base}_{q}.mp4'
            assert stripped.exists(),     f'stripped {q} missing — was the dated copy uploaded instead of renamed?'
            assert not dated.exists(),    f'dated {q} still present — would burn cloud bandwidth on next deploy'
            assert stripped.read_bytes() == sentinel_bytes, \
                f'{q} content changed — was the source re-uploaded over the existing cloud copy?'

    def test_sync_skips_rename_of_dehydrated_legacy_copy(self, tmp_path):
        """Dehydrated (cloud-only) dated copies must NOT be renamed.

        Renaming a Files-On-Demand placeholder forces OneDrive to materialize
        (download) the full file before the rename can complete — burning WAN
        bandwidth on cold cloud copies the engine only wanted to rename for
        naming-convention consistency.  Old dated copies are left alone; they
        expire on the cloud's normal cadence.
        """
        fp = self._make_pipeline(tmp_path)
        rec  = datetime.date.today() - datetime.timedelta(days=1)
        prefix = rec.strftime("%y-%m-%d")
        base = f'{prefix}_BAND'

        self._seed_quads(fp, base)

        # Pre-existing cloud folder with dated copies (treated as placeholders).
        fp.sharepoint_dest = tmp_path / 'sharepoint'
        sp_folder = fp.sharepoint_dest / prefix
        sp_folder.mkdir(parents=True)
        sentinel_bytes = b'cold dehydrated cloud content'
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            (sp_folder / f'{base}_{q}.mp4').write_bytes(sentinel_bytes)

        # The hydration guard now lives in nofun.media_io.rename_cloud_file, so
        # patch is_cloud_only there (the call site the sync flow reaches).
        with patch('nofun.media_io.is_cloud_only', return_value=True):
            fp._sync_eligible_performances()

        final_folder = next(
            p for p in fp.sharepoint_dest.iterdir()
            if p.is_dir() and p.name.startswith(prefix)
        )

        # Stripped names must NOT exist (no rename happened, no materialization).
        # Dated copies must be untouched.
        for q in ('CAM1', 'CAM2', 'CAM3', 'CAM4'):
            dated    = final_folder / f'{base}_{q}.mp4'
            stripped = final_folder / f'BAND_{q}.mp4'
            assert dated.exists(), \
                f'dated {q} disappeared — engine renamed a dehydrated placeholder'
            assert dated.read_bytes() == sentinel_bytes, f'dated {q} content changed'
            assert not stripped.exists(), \
                f'stripped {q} created — skip should prevent forced materialization'


class TestArchiveOrDedupOutcome:
    """archive_or_dedup returns a structured outcome for batch aggregation."""

    def test_returns_moved_when_dest_missing(self, tmp_path):
        from nofun.cleanup import ArchiveOutcome
        src = tmp_path / 'src.wav'
        src.write_bytes(b'x' * 100)
        archive = tmp_path / 'archive'
        archive.mkdir()
        outcome = archive_or_dedup(src, archive, logging.getLogger('t'), DeleteQueue())
        assert outcome == ArchiveOutcome.MOVED
        assert (archive / 'src.wav').exists()

    def test_returns_deduped_when_dest_same_size(self, tmp_path):
        from nofun.cleanup import ArchiveOutcome
        src = tmp_path / 'src.wav'
        src.write_bytes(b'x' * 100)
        archive = tmp_path / 'archive'
        archive.mkdir()
        (archive / 'src.wav').write_bytes(b'x' * 100)
        dq = DeleteQueue()
        outcome = archive_or_dedup(src, archive, logging.getLogger('t'), dq)
        assert outcome == ArchiveOutcome.DEDUPED
        assert src.exists()

    def test_stub_source_queued_when_archive_larger(self, tmp_path, caplog):
        # Post-show recorder stubs: source is a tiny leftover, archive holds the
        # real (larger) recording. The source should be queued for deletion as
        # a stub (DEDUPED outcome) so the lifecycle stops re-enqueuing it.
        from nofun.cleanup import ArchiveOutcome
        src = tmp_path / 'src.wav'
        src.write_bytes(b'x' * 100)
        archive = tmp_path / 'archive'
        archive.mkdir()
        (archive / 'src.wav').write_bytes(b'x' * 2000)
        dq = DeleteQueue()
        with caplog.at_level(logging.INFO):
            outcome = archive_or_dedup(src, archive, logging.getLogger('t'), dq)
        assert outcome == ArchiveOutcome.DEDUPED
        assert src.exists()  # delete_queue defers the actual unlink
        assert any(p == src for p, _ in dq.items)
        info_msgs = [r.message for r in caplog.records if r.levelno >= logging.INFO]
        assert not any('LOCKED' in m for m in info_msgs)

    def test_partial_archive_replaced_when_source_larger(self, tmp_path, caplog):
        # Interrupted prior move: archive holds a partial, source is the
        # complete file. Unlink the partial and re-archive (MOVED outcome).
        from nofun.cleanup import ArchiveOutcome
        src = tmp_path / 'src.wav'
        src.write_bytes(b'x' * 2000)
        archive = tmp_path / 'archive'
        archive.mkdir()
        (archive / 'src.wav').write_bytes(b'x' * 100)
        with caplog.at_level(logging.INFO):
            outcome = archive_or_dedup(src, archive, logging.getLogger('t'), DeleteQueue())
        assert outcome == ArchiveOutcome.MOVED
        assert (archive / 'src.wav').stat().st_size == 2000
        assert not src.exists()
        info_msgs = [r.message for r in caplog.records if r.levelno >= logging.INFO]
        assert any('REPLACE' in m for m in info_msgs)


class TestArchiveAudioBatch:
    """_archive_audio_batch aggregates outcomes into one INFO line."""

    def _make_pipeline(self, tmp_path: pathlib.Path):
        from media_engine import Pipeline
        fp = _FakePipeline(tmp_path)
        fp._pipeline_moved = None
        fp._archive_audio_batch = Pipeline._archive_audio_batch.__get__(fp)
        return fp

    def test_summary_line_emitted_once_per_batch(self, tmp_path, caplog):
        fp = self._make_pipeline(tmp_path)
        fp.logger = logging.getLogger('test_archive_batch')
        movers, stubs = [], []
        for i in range(2):
            src = fp.search_dir / f'mover{i}.wav'
            src.write_bytes(b'x' * 100)
            movers.append(src)
        # Three stubs: source smaller than the archive copy (the real recording).
        # Under the recovery branch these are queued for deletion (DEDUPED) and
        # roll up into a single "dropped" count in the summary.
        for i in range(3):
            src = fp.search_dir / f'stub{i}.wav'
            src.write_bytes(b'x' * 100)
            (fp.audio_archive / src.name).write_bytes(b'x' * 2000)
            stubs.append(src)

        with caplog.at_level(logging.INFO):
            fp._archive_audio_batch(movers + stubs)

        info_lines = [r.message for r in caplog.records
                      if r.levelno == logging.INFO and 'ARCHIVE AUDIO' in r.message]
        assert len(info_lines) == 1
        assert '2 archived' in info_lines[0]
        assert '3 dropped' in info_lines[0]

    def test_no_summary_when_all_files_empty(self, tmp_path, caplog):
        fp = self._make_pipeline(tmp_path)
        fp.logger = logging.getLogger('test_archive_batch')
        with caplog.at_level(logging.INFO):
            fp._archive_audio_batch([])
        info_lines = [r.message for r in caplog.records
                      if r.levelno == logging.INFO and 'ARCHIVE AUDIO' in r.message]
        assert len(info_lines) == 0


class TestWriteSharepointInfo:
    """In-progress / convergence behaviour of write_sharepoint_info."""

    EXP = datetime.date(2026, 6, 21)

    def _read(self, folder: pathlib.Path) -> str:
        return (folder / '_nofun_info.txt').read_text(encoding='utf-8')

    def test_baseline_unchanged_without_expected(self, tmp_path):
        ul = tmp_path / 'BAND_UL.mp4'
        ul.write_bytes(b'x' * 10)
        write_sharepoint_info(tmp_path, [ul], expire_date=self.EXP)
        text = self._read(tmp_path)
        assert 'processing' not in text
        assert 'BAND_UL.mp4' in text
        assert 'uploaded' in text

    def test_absent_expected_marked_processing(self, tmp_path):
        write_sharepoint_info(
            tmp_path, [], expire_date=self.EXP, new_files=[],
            expected_names=['BAND_UL.mp4', 'BAND_UR.mp4', 'BAND.zip'],
        )
        text = self._read(tmp_path)
        assert 'still processing — files appear here as they finish.' in text
        assert text.count('processing…') == 3
        # markers must not be timestamp sub-lines, so no history was created
        assert 'uploaded' not in text

    def test_present_and_expected_mix(self, tmp_path):
        ul = tmp_path / 'BAND_UL.mp4'
        ul.write_bytes(b'x' * 10)
        write_sharepoint_info(
            tmp_path, [ul], expire_date=self.EXP, new_files=[ul],
            expected_names=['BAND_UL.mp4', 'BAND_UR.mp4', 'BAND.zip'],
        )
        text = self._read(tmp_path)
        assert 'still processing' in text
        ul_line = next(l for l in text.splitlines() if 'BAND_UL.mp4' in l)
        assert 'processing…' not in ul_line
        assert text.count('processing…') == 2  # UR + zip still pending

    def test_converges_to_baseline_no_pollution(self, tmp_path):
        expected = ['BAND_UL.mp4', 'BAND_UR.mp4', 'BAND.zip']
        # Same folder name in both arms — the info file embeds folder.name,
        # so identical names let us compare the rest byte-for-byte.
        prog = tmp_path / 'p1' / '26-06-21_BAND'
        prog.mkdir(parents=True)
        with patch('nofun.cleanup._fmt_ts', return_value='Jun 21, 2026  9:05pm'):
            # 1. in-progress write (nothing on disk yet)
            write_sharepoint_info(
                prog, [], expire_date=self.EXP, new_files=[],
                expected_names=expected,
            )
            # 2. files land; final write through the same expected_names
            landed = []
            for n in expected:
                p = prog / n
                p.write_bytes(b'x' * 10)
                landed.append(p)
            write_sharepoint_info(
                prog, landed, expire_date=self.EXP, new_files=landed,
                expected_names=expected,
            )
            after = self._read(prog)

            # 3. a folder that never saw an in-progress write
            clean = tmp_path / 'p2' / '26-06-21_BAND'
            clean.mkdir(parents=True)
            clean_files = []
            for n in expected:
                p = clean / n
                p.write_bytes(b'x' * 10)
                clean_files.append(p)
            write_sharepoint_info(
                clean, clean_files, expire_date=self.EXP, new_files=clean_files,
                expected_names=expected,
            )
            baseline = self._read(clean)

        assert 'processing' not in after
        assert after == baseline
