"""Regression tests for the clips skip-on-presence check in _build_full_manifest.

Bug (smoke #2, 2026-06-05): on a combined rebuild the quads are absent at
manifest-build time, so the old `_clips_done` generator — which filtered movs by
"quads already exist on disk" — produced an empty generator, `all([])` returned
True, and export_clips was silently skipped. Clips must be enqueued whenever they
are missing, regardless of whether quads exist yet (ordering is handled by
depends=encode_ids).
"""

import pathlib

from media_engine import Pipeline


def _bare_pipeline(tmp_path, force=False):
    p = Pipeline.__new__(Pipeline)
    p.force = force
    p.vids_dest = tmp_path / 'videos'
    p.clips_dest = tmp_path / 'clips'
    p.audio_dest = tmp_path / 'audio'
    for d in (p.vids_dest, p.clips_dest, p.audio_dest):
        d.mkdir(parents=True, exist_ok=True)
    return p


def _kinds(tmp_path, *, clips_present, force=False):
    p = _bare_pipeline(tmp_path, force=force)
    perf = '26-05-23_ONE_THRU_TEN'
    mov = pathlib.Path(f'{perf}.1.mov')          # stem → 26-05-23_ONE_THRU_TEN.1
    if clips_present:
        cdir = p.clips_dest / mov.stem
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / 'clip0001.mp4').write_bytes(b'x')
    manifest, _ = p._build_full_manifest(perf, [mov], [], [], [])
    return {j.kind for j in manifest.jobs}


def test_clips_enqueued_when_quads_and_clips_both_absent(tmp_path):
    # The bug: quads absent (so encode_quads is enqueued), clips absent → clips
    # MUST be enqueued too. Pre-fix this returned only encode_quads.
    kinds = _kinds(tmp_path, clips_present=False)
    assert 'encode_quads' in kinds
    assert 'export_clips' in kinds


def test_clips_skipped_when_clips_present(tmp_path):
    # Quads still absent (encode enqueued) but clips already on disk → skip clips.
    kinds = _kinds(tmp_path, clips_present=True)
    assert 'encode_quads' in kinds
    assert 'export_clips' not in kinds


def test_clips_reenqueued_when_present_but_force(tmp_path):
    # force overrides skip-on-presence for clips too.
    kinds = _kinds(tmp_path, clips_present=True, force=True)
    assert 'export_clips' in kinds
