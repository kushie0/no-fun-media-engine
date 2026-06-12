"""Unit tests for the runtime NAS→D: media-root fallback.

Covers the helper that re-points the five dest attrs atomically, the debounced
loop reconciler that flips media_root on NAS outage/return, and the
timeout-guarded reachability probe. None of these need ffmpeg or a real NAS, so
the Pipeline is built via __new__ with only the attributes the methods touch.
"""

import logging
import threading
import time

import media_engine
from media_engine import Pipeline
from nofun.paths import nas_reachable


def _make_pipeline(mount_d, media_root):
    """A bare Pipeline with just the attrs the fallback methods read/write."""
    p = Pipeline.__new__(Pipeline)
    p.trial_run = 0
    p.mount_d = mount_d
    p.clips_dest = mount_d / 'clips'
    p.logger = logging.getLogger('test_media_root_fallback')
    p._nas_miss = 0
    p._nas_hit = 0
    # Start on the NAS (media_root != mount_d).
    p.media_root = media_root
    p.vids_dest = media_root / 'videos'
    p.audio_dest = media_root / 'audio'
    p.video_archive = media_root / 'video_archive'
    p.audio_archive = media_root / 'audio_archive'
    return p


# ---------------------------------------------------------------------------
# 1. _set_media_root re-points all five attrs; clips_dest untouched
# ---------------------------------------------------------------------------

def test_set_media_root_repoints_all_and_leaves_clips(tmp_path):
    p = Pipeline.__new__(Pipeline)
    p.clips_dest = tmp_path / 'clips'      # C:\clips — must NOT move
    new_root = tmp_path / 'N'

    p._set_media_root(new_root)

    assert p.media_root == new_root
    assert p.vids_dest == new_root / 'videos'
    assert p.audio_dest == new_root / 'audio'
    assert p.video_archive == new_root / 'video_archive'
    assert p.audio_archive == new_root / 'audio_archive'
    assert p.clips_dest == tmp_path / 'clips'        # unchanged
    # the four media dests are created on re-point
    for d in (p.vids_dest, p.audio_dest, p.video_archive, p.audio_archive):
        assert d.is_dir()


# ---------------------------------------------------------------------------
# 2. Debounce: 2 misses → flip to D:, then 2 hits → flip back + reconcile
# ---------------------------------------------------------------------------

def test_debounce_failover_then_failback(tmp_path, monkeypatch):
    nas = tmp_path / 'N'
    nas.mkdir()
    mount_d = tmp_path / 'D'
    mount_d.mkdir()
    monkeypatch.setenv('NAS_ROOT', str(nas))
    p = _make_pipeline(mount_d, nas)

    # --- NAS goes away: probe returns False ---
    monkeypatch.setattr(media_engine, 'nas_reachable', lambda root, **k: False)

    p._reconcile_media_root()                      # tick 1 — no flip yet
    assert p.media_root == nas
    assert p._nas_miss == 1

    p._reconcile_media_root()                      # tick 2 — flip to D:
    assert p.media_root == mount_d
    assert p._nas_miss == 0

    # --- NAS returns: probe returns True; capture the failback reconcile ---
    failback_calls = []
    monkeypatch.setattr(p, '_enqueue_failback_reconcile',
                        lambda root: failback_calls.append(root))
    monkeypatch.setattr(media_engine, 'nas_reachable', lambda root, **k: True)

    p._reconcile_media_root()                      # tick 1 — no flip yet
    assert p.media_root == mount_d
    assert p._nas_hit == 1
    assert failback_calls == []

    p._reconcile_media_root()                      # tick 2 — flip back + reconcile
    assert p.media_root == nas
    assert p._nas_hit == 0
    assert failback_calls == [nas]


def test_steady_state_resets_counters(tmp_path, monkeypatch):
    nas = tmp_path / 'N'
    nas.mkdir()
    mount_d = tmp_path / 'D'
    mount_d.mkdir()
    monkeypatch.setenv('NAS_ROOT', str(nas))
    p = _make_pipeline(mount_d, nas)
    p._nas_miss = 1                                # a stray earlier miss
    monkeypatch.setattr(media_engine, 'nas_reachable', lambda root, **k: True)

    p._reconcile_media_root()                      # on NAS + up → steady
    assert p.media_root == nas
    assert p._nas_miss == 0
    assert p._nas_hit == 0


# ---------------------------------------------------------------------------
# 2b. _enqueue_failback_reconcile mirrors reversed (D:→N:) pairs
# ---------------------------------------------------------------------------

def test_failback_reconcile_uses_reversed_pairs(tmp_path, monkeypatch):
    nas = tmp_path / 'N'
    mount_d = tmp_path / 'D'
    p = _make_pipeline(mount_d, nas)

    recorded = {}
    done = threading.Event()

    def _fake_mirror(pairs, *a, **kw):
        recorded['pairs'] = pairs
        done.set()
        return (len(pairs), 0)

    monkeypatch.setattr(media_engine, 'mirror_files', _fake_mirror)
    p._enqueue_failback_reconcile(nas)

    assert done.wait(timeout=2.0), 'failback reconcile thread never ran'
    # every pair must copy FROM local D: TO the NAS (the inverse of the down-mirror)
    for src, dst in recorded['pairs']:
        assert src.is_relative_to(mount_d)
        assert dst.is_relative_to(nas)
    assert (mount_d / 'videos', nas / 'videos') in recorded['pairs']
    assert (mount_d / 'audio', nas / 'audio') in recorded['pairs']


# ---------------------------------------------------------------------------
# 3. No-op for trial runs / unset NAS_ROOT
# ---------------------------------------------------------------------------

def test_noop_when_trial_run(tmp_path, monkeypatch):
    nas = tmp_path / 'N'
    mount_d = tmp_path / 'D'
    monkeypatch.setenv('NAS_ROOT', str(nas))
    p = _make_pipeline(mount_d, nas)
    p.trial_run = 1

    called = []
    monkeypatch.setattr(media_engine, 'nas_reachable',
                        lambda root, **k: called.append(root) or True)
    p._reconcile_media_root()

    assert called == []                            # probe never run
    assert p.media_root == nas                     # nothing re-pointed


def test_noop_when_nas_root_unset(tmp_path, monkeypatch):
    mount_d = tmp_path / 'D'
    monkeypatch.delenv('NAS_ROOT', raising=False)
    p = _make_pipeline(mount_d, mount_d)           # no NAS → already on D:

    called = []
    monkeypatch.setattr(media_engine, 'nas_reachable',
                        lambda root, **k: called.append(root) or True)
    p._reconcile_media_root()

    assert called == []
    assert p.media_root == mount_d


# ---------------------------------------------------------------------------
# 4. nas_reachable: timeout → False; happy/raising paths
# ---------------------------------------------------------------------------

def test_nas_reachable_true(tmp_path):
    assert nas_reachable(tmp_path) is True


def test_nas_reachable_false_on_missing(tmp_path):
    assert nas_reachable(tmp_path / 'nope') is False


def test_nas_reachable_times_out():
    class SlowDir:
        def is_dir(self):
            time.sleep(1.0)
            return True

    start = time.monotonic()
    assert nas_reachable(SlowDir(), timeout=0.1) is False  # type: ignore[arg-type]
    # returns promptly at the timeout, not after the full 1.0s block
    assert time.monotonic() - start < 0.9


def test_nas_reachable_false_on_oserror():
    class Bad:
        def is_dir(self):
            raise OSError('unreachable share')

    assert nas_reachable(Bad()) is False  # type: ignore[arg-type]
