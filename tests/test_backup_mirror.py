"""Tests for nofun/backup_mirror.py — copy-only mirror + age-based expiry selection."""

from pathlib import Path

from nofun.backup_mirror import (
    DELIVERABLE_EXTS,
    RAW_BACKUP_EXTS,
    find_expired,
    mirror_files,
)


def _write(p: Path, data: bytes = b'x') -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _pairs(src: Path, dst: Path):
    return [(src, dst)]


class TestMirrorFiles:
    def test_copies_mp4_and_mp3_only(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp4')
        _write(src / 'b.mp3')
        _write(src / 'c.mov')          # raw — excluded
        _write(src / 'd.wav')          # raw — excluded
        _write(src / 'e_MULTITRACK.zip')  # intermediate — excluded
        copied, skipped = mirror_files(_pairs(src, dst))
        assert copied == 2
        assert skipped == 0
        assert (dst / 'a.mp4').exists()
        assert (dst / 'b.mp3').exists()
        assert not (dst / 'c.mov').exists()
        assert not (dst / 'd.wav').exists()
        assert not (dst / 'e_MULTITRACK.zip').exists()

    def test_skips_existing_same_size(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp4', b'hello')
        _write(dst / 'a.mp4', b'hello')   # already backed up, identical size
        copied, skipped = mirror_files(_pairs(src, dst))
        assert copied == 0
        assert skipped == 1

    def test_copies_when_size_differs(self, tmp_path) -> None:
        """A re-master (different bytes) overwrites the stale backup."""
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp3', b'newmaster-longer')
        _write(dst / 'a.mp3', b'old')     # stale, different size
        copied, skipped = mirror_files(_pairs(src, dst))
        assert copied == 1
        assert skipped == 0
        assert (dst / 'a.mp3').read_bytes() == b'newmaster-longer'

    def test_never_deletes_dst_only_files(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp4')
        extra = _write(dst / 'old_show.mp4')   # exists only on backup
        mirror_files(_pairs(src, dst))
        assert extra.exists()

    def test_missing_src_dir_is_noop(self, tmp_path) -> None:
        src, dst = tmp_path / 'does_not_exist', tmp_path / 'd'
        copied, skipped = mirror_files(_pairs(src, dst))
        assert (copied, skipped) == (0, 0)

    def test_nested_subdirs_preserved(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'sub' / 'deep' / 'x.mp4')
        copied, _ = mirror_files(_pairs(src, dst))
        assert copied == 1
        assert (dst / 'sub' / 'deep' / 'x.mp4').exists()

    def test_multiple_pairs(self, tmp_path) -> None:
        nv, dv = tmp_path / 'n' / 'videos', tmp_path / 'd' / 'videos'
        na, da = tmp_path / 'n' / 'audio', tmp_path / 'd' / 'audio'
        _write(nv / 'q.mp4')
        _write(na / 'm.mp3')
        copied, _ = mirror_files([(nv, dv), (na, da)])
        assert copied == 2
        assert (dv / 'q.mp4').exists()
        assert (da / 'm.mp3').exists()

    def test_extension_match_is_case_insensitive(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'A.MP4')
        copied, _ = mirror_files(_pairs(src, dst))
        assert copied == 1
        assert (dst / 'A.MP4').exists()

    def test_default_exts_constant(self) -> None:
        assert DELIVERABLE_EXTS == ('.mp4', '.mp3')


class TestMirrorRawBackup:
    """With RAW_BACKUP_EXTS the mirror copies raws (.mov/.zip), not deliverables."""

    def test_copies_mov_and_zip_only(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mov')
        _write(src / 'b_MULTITRACK.zip')
        _write(src / 'c.mp4')          # deliverable — excluded
        _write(src / 'd.mp3')          # deliverable — excluded
        copied, skipped = mirror_files(_pairs(src, dst), RAW_BACKUP_EXTS)
        assert copied == 2
        assert skipped == 0
        assert (dst / 'a.mov').exists()
        assert (dst / 'b_MULTITRACK.zip').exists()
        assert not (dst / 'c.mp4').exists()
        assert not (dst / 'd.mp3').exists()

    def test_raw_exts_constant(self) -> None:
        assert RAW_BACKUP_EXTS == ('.mov', '.zip')


class TestIncludePredicate:
    """The age-gate: a file is copied only when include(f) is True."""

    def test_excluded_file_not_copied(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'old.mov')        # missing at dst, but include says no
        copied, skipped = mirror_files(
            _pairs(src, dst), RAW_BACKUP_EXTS, include=lambda p: False
        )
        assert copied == 0
        assert skipped == 0
        assert not (dst / 'old.mov').exists()

    def test_included_file_is_copied(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'fresh.mov')
        copied, _ = mirror_files(
            _pairs(src, dst), RAW_BACKUP_EXTS, include=lambda p: True
        )
        assert copied == 1
        assert (dst / 'fresh.mov').exists()

    def test_predicate_selects_by_name(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'keep.mov')
        _write(src / 'drop.mov')
        copied, _ = mirror_files(
            _pairs(src, dst), RAW_BACKUP_EXTS,
            include=lambda p: p.name.startswith('keep'),
        )
        assert copied == 1
        assert (dst / 'keep.mov').exists()
        assert not (dst / 'drop.mov').exists()


class TestFindExpired:
    def test_returns_only_old_matching_ext(self, tmp_path) -> None:
        d = tmp_path / 'video_archive'
        old = _write(d / 'old.mov')
        _write(d / 'new.mov')          # not old
        _write(d / 'skip.mp4')         # wrong ext
        stale = find_expired([(d, ('.mov',))], is_old=lambda p: p.name == 'old.mov')
        assert stale == [old]

    def test_respects_per_dir_exts(self, tmp_path) -> None:
        va = tmp_path / 'video_archive'
        au = tmp_path / 'audio'
        mov = _write(va / 'x.mov')
        zip_ = _write(au / 'y.zip')
        _write(va / 'x.zip')           # .zip not wanted under video_archive
        _write(au / 'y.mov')           # .mov not wanted under audio
        stale = find_expired(
            [(va, ('.mov',)), (au, ('.zip',))],
            is_old=lambda p: True,
        )
        assert set(stale) == {mov, zip_}

    def test_missing_dir_skipped(self, tmp_path) -> None:
        stale = find_expired([(tmp_path / 'nope', ('.mov',))], is_old=lambda p: True)
        assert stale == []

    def test_never_returns_non_matching_ext(self, tmp_path) -> None:
        d = tmp_path / 'audio'
        _write(d / 'a.mp3')            # deliverable, not a raw .zip
        _write(d / 'b.wav')
        stale = find_expired([(d, ('.zip',))], is_old=lambda p: True)
        assert stale == []

    def test_pure_selection_no_deletion(self, tmp_path) -> None:
        d = tmp_path / 'video_archive'
        f = _write(d / 'old.mov')
        find_expired([(d, ('.mov',))], is_old=lambda p: True)
        assert f.exists()
