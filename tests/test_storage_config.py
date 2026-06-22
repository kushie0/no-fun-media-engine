"""Unit tests for nofun.storage_config.StorageConfig.

Run with: pytest tests/test_storage_config.py -v
"""

import logging

from nofun.storage_config import StorageConfig


def _cfg(tmp_path, **over):
    base = dict(
        mount_c=tmp_path / 'C',
        mount_d=tmp_path / 'D',
        search_dir=tmp_path / 'src',
        clips_dest=tmp_path / 'clips',
        sharepoint_dest=None,
    )
    base.update(over)
    return StorageConfig(**base)


class TestDefaultsAndComposition:
    def test_defaults_reproduce_current_layout(self, tmp_path):
        c = _cfg(tmp_path)
        assert c.videos_subdir == 'videos'
        assert c.audio_subdir == 'audio'
        assert c.video_archive_subdir == 'video_archive'
        assert c.audio_archive_subdir == 'audio_archive'

    def test_media_dests_compose_against_live_root(self, tmp_path):
        c = _cfg(tmp_path)
        nas = tmp_path / 'NAS'
        d = c.media_dests(nas)
        assert d['vids_dest'] == nas / 'videos'
        assert d['video_archive'] == nas / 'video_archive'

    def test_d_backup_tier_under_mount_d(self, tmp_path):
        c = _cfg(tmp_path)
        assert c.d_video_backup == (tmp_path / 'D') / 'video_archive'
        assert c.d_audio_backup == (tmp_path / 'D') / 'audio'

    def test_subdir_override_flows_through(self, tmp_path):
        c = _cfg(tmp_path, videos_subdir='vid', video_archive_subdir='varch')
        nas = tmp_path / 'NAS'
        assert c.media_dests(nas)['vids_dest'] == nas / 'vid'
        assert c.d_video_backup == (tmp_path / 'D') / 'varch'


class TestAllStorageRoots:
    def test_includes_d_backup_tier(self, tmp_path):
        c = _cfg(tmp_path)
        roots = c.all_storage_roots(tmp_path / 'NAS')
        assert c.d_video_backup in roots
        assert c.d_audio_backup in roots

    def test_sharepoint_included_when_set(self, tmp_path):
        sp = tmp_path / 'sp'
        sp.mkdir()
        c = _cfg(tmp_path, sharepoint_dest=sp)
        assert sp in c.all_storage_roots(tmp_path / 'NAS')

    def test_sharepoint_absent_when_none(self, tmp_path):
        c = _cfg(tmp_path, sharepoint_dest=None)
        assert all('sp' not in str(p) for p in c.all_storage_roots(tmp_path / 'NAS'))


class TestFromEnv:
    def test_subdir_and_sharepoint_env_overrides(self, tmp_path, monkeypatch):
        sp = tmp_path / 'tenant'
        sp.mkdir()
        monkeypatch.setenv('VIDEOS_SUBDIR', 'vids2')
        monkeypatch.setenv('SHAREPOINT_DEST', str(sp))
        c = StorageConfig.from_env(tmp_path / 'C', tmp_path / 'D',
                                   tmp_path / 'src', tmp_path / 'clips')
        assert c.videos_subdir == 'vids2'
        assert c.sharepoint_dest == sp

    def test_missing_sharepoint_dir_becomes_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv('SHAREPOINT_DEST', str(tmp_path / 'does_not_exist'))
        c = StorageConfig.from_env(tmp_path / 'C', tmp_path / 'D',
                                   tmp_path / 'src', tmp_path / 'clips')
        assert c.sharepoint_dest is None


class TestValidate:
    def test_warns_on_missing_source_does_not_raise(self, tmp_path):
        c = _cfg(tmp_path)  # search_dir doesn't exist
        logger = logging.getLogger('test_validate')
        c.validate(logger)  # must not raise (warn-and-degrade)

    def test_runs_clean_when_source_exists(self, tmp_path):
        (tmp_path / 'src').mkdir()
        c = _cfg(tmp_path)
        c.validate(logging.getLogger('test_validate'))
