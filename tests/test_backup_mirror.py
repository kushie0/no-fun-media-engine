"""Tests for nofun/backup_mirror.py — N:->D: deliverable down-mirror."""

from pathlib import Path

from nofun.backup_mirror import DELIVERABLE_EXTS, mirror_deliverables


def _write(p: Path, data: bytes = b'x') -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _pairs(src: Path, dst: Path):
    return [(src, dst)]


class TestMirrorDeliverables:
    def test_copies_mp4_and_mp3_only(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp4')
        _write(src / 'b.mp3')
        _write(src / 'c.mov')          # raw — excluded
        _write(src / 'd.wav')          # raw — excluded
        _write(src / 'e_MULTITRACK.zip')  # intermediate — excluded
        copied, skipped = mirror_deliverables(_pairs(src, dst))
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
        copied, skipped = mirror_deliverables(_pairs(src, dst))
        assert copied == 0
        assert skipped == 1

    def test_copies_when_size_differs(self, tmp_path) -> None:
        """A re-master (different bytes) overwrites the stale backup."""
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp3', b'newmaster-longer')
        _write(dst / 'a.mp3', b'old')     # stale, different size
        copied, skipped = mirror_deliverables(_pairs(src, dst))
        assert copied == 1
        assert skipped == 0
        assert (dst / 'a.mp3').read_bytes() == b'newmaster-longer'

    def test_never_deletes_dst_only_files(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'a.mp4')
        extra = _write(dst / 'old_show.mp4')   # exists only on backup
        mirror_deliverables(_pairs(src, dst))
        assert extra.exists()

    def test_missing_src_dir_is_noop(self, tmp_path) -> None:
        src, dst = tmp_path / 'does_not_exist', tmp_path / 'd'
        copied, skipped = mirror_deliverables(_pairs(src, dst))
        assert (copied, skipped) == (0, 0)

    def test_nested_subdirs_preserved(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'sub' / 'deep' / 'x.mp4')
        copied, _ = mirror_deliverables(_pairs(src, dst))
        assert copied == 1
        assert (dst / 'sub' / 'deep' / 'x.mp4').exists()

    def test_multiple_pairs(self, tmp_path) -> None:
        nv, dv = tmp_path / 'n' / 'videos', tmp_path / 'd' / 'videos'
        na, da = tmp_path / 'n' / 'audio', tmp_path / 'd' / 'audio'
        _write(nv / 'q.mp4')
        _write(na / 'm.mp3')
        copied, _ = mirror_deliverables([(nv, dv), (na, da)])
        assert copied == 2
        assert (dv / 'q.mp4').exists()
        assert (da / 'm.mp3').exists()

    def test_extension_match_is_case_insensitive(self, tmp_path) -> None:
        src, dst = tmp_path / 'n', tmp_path / 'd'
        _write(src / 'A.MP4')
        copied, _ = mirror_deliverables(_pairs(src, dst))
        assert copied == 1
        assert (dst / 'A.MP4').exists()

    def test_default_exts_constant(self) -> None:
        assert DELIVERABLE_EXTS == ('.mp4', '.mp3')
