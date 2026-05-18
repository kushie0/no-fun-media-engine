"""tests/test_streams.py — unit tests for nofun/streams.py."""

from __future__ import annotations

import pathlib
import queue
import threading
import time
import unittest.mock as mock

import pytest

from nofun.streams import (
    BROADCAST_MAXQ,
    CLIP_DURATION_MAX,
    CLIP_DURATION_MIN,
    StreamServer,
    StreamWorker,
    get_local_ip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_mp4(directory: pathlib.Path, name: str = "clip.mp4") -> pathlib.Path:
    p = directory / name
    p.write_bytes(b"\x00" * 16)
    return p


# ---------------------------------------------------------------------------
# StreamWorker — clip pool management
# ---------------------------------------------------------------------------

class TestStreamWorkerClipPool:
    def test_set_clips_stores_list(self, tmp_path):
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w.set_clips([a, b])
        with w._clips_lock:
            assert len(w._clips) == 2

    def test_set_clips_empty(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w.set_clips([])
        with w._clips_lock:
            assert w._clips == []

    def test_clip_count_property(self, tmp_path):
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        assert w.clip_count == 0
        w.set_clips([a, b])
        assert w.clip_count == 2


# ---------------------------------------------------------------------------
# StreamWorker — subscribe / broadcast / unsubscribe
# ---------------------------------------------------------------------------

class TestStreamWorkerBroadcast:
    def test_subscribe_returns_queue(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q = w.subscribe()
        assert isinstance(q, queue.Queue)

    def test_broadcast_delivers_to_subscriber(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q = w.subscribe()
        w._broadcast(b"hello")
        assert q.get_nowait() == b"hello"

    def test_broadcast_delivers_to_multiple_subscribers(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q1, q2, q3 = w.subscribe(), w.subscribe(), w.subscribe()
        w._broadcast(b"data")
        assert q1.get_nowait() == b"data"
        assert q2.get_nowait() == b"data"
        assert q3.get_nowait() == b"data"

    def test_broadcast_skips_slow_client_but_keeps_connected(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q_fast = w.subscribe()
        q_slow = w.subscribe()
        # Fill the slow client's queue to capacity
        for _ in range(BROADCAST_MAXQ):
            q_slow.put_nowait(b"x")
        # Broadcast one more — slow client's chunk is skipped, but it stays subscribed
        w._broadcast(b"new")
        assert q_fast.get_nowait() == b"new"
        with w._clients_lock:
            assert q_slow in w._clients   # still connected

    def test_unsubscribe_removes_queue(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q = w.subscribe()
        assert w.client_count == 1
        w.unsubscribe(q)
        assert w.client_count == 0

    def test_unsubscribe_unknown_queue_is_safe(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        orphan: queue.Queue[bytes | None] = queue.Queue()
        w.unsubscribe(orphan)  # must not raise

    def test_client_count_property(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        assert w.client_count == 0
        q1 = w.subscribe()
        assert w.client_count == 1
        q2 = w.subscribe()
        assert w.client_count == 2
        w.unsubscribe(q1)
        assert w.client_count == 1


# ---------------------------------------------------------------------------
# StreamWorker — stop sends poison pill
# ---------------------------------------------------------------------------

class TestStreamWorkerStop:
    def test_stop_sends_none_to_clients(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        q = w.subscribe()
        w.stop()
        assert q.get_nowait() is None

    def test_stop_sets_event(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        assert not w._stop.is_set()
        w.stop()
        assert w._stop.is_set()


# ---------------------------------------------------------------------------
# StreamWorker — batch / current_clip property
# ---------------------------------------------------------------------------

class TestBatch:
    def test_current_clip_from_batch(self, tmp_path):
        """current_clip reflects which clip should be playing based on elapsed time."""
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w._batch = [(a, 10.0), (b, 10.0)]
        w._batch_started = time.time()
        assert w.current_clip == a   # at t=0, first clip

    def test_current_clip_advances(self, tmp_path):
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w._batch = [(a, 0.001), (b, 10.0)]   # a expires almost instantly
        w._batch_started = time.time() - 0.01  # 10ms elapsed → past a's 1ms slot
        assert w.current_clip == b

    def test_current_clip_none_when_idle(self, tmp_path):
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        assert w.current_clip is None

    def test_time_remaining_counts_down(self, tmp_path):
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w._batch = [(a, 30.0), (b, 30.0)]
        w._batch_started = time.time()
        # should be time left in clip a (~30s), not the whole batch (~60s)
        assert 25.0 < w.time_remaining <= 30.0

    def test_current_clip_duration(self, tmp_path):
        a = _make_fake_mp4(tmp_path, "a.mp4")
        b = _make_fake_mp4(tmp_path, "b.mp4")
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w._batch = [(a, 30.0), (b, 20.0)]
        w._batch_started = time.time()
        assert w.current_clip_duration == 30.0  # at t=0, playing clip a

    def test_duration_per_clip_in_range(self, tmp_path):
        """Durations assigned during run() must fall within configured bounds."""
        clips = [_make_fake_mp4(tmp_path, f"c{i}.mp4") for i in range(5)]
        w = StreamWorker(tmp_path, port=9999, retry_delay=0.05)
        w.set_clips(clips)

        batches_seen: list[list[tuple]] = []

        def _fake_popen(cmd, **kwargs):
            # Read the playlist file path from the cmd
            i_idx = cmd.index('-i') + 1
            playlist = cmd[i_idx]
            with open(playlist, encoding='utf-8') as f:
                lines = f.readlines()
            batch = []
            for j, line in enumerate(lines):
                if line.startswith('duration '):
                    d = float(line.split()[1])
                    batch.append(d)
            batches_seen.append(batch)
            w._stop.set()   # stop after first batch
            proc = mock.MagicMock()
            proc.stdout.read1.return_value = b""
            proc.poll.return_value = 0
            return proc

        with mock.patch("nofun.streams.subprocess.Popen", side_effect=_fake_popen):
            w.run()

        assert batches_seen, "no batch was produced"
        for d in batches_seen[0]:
            assert CLIP_DURATION_MIN <= d <= CLIP_DURATION_MAX


# ---------------------------------------------------------------------------
# StreamServer — mocked HTTP server (no real ports bound)
# ---------------------------------------------------------------------------

class TestStreamServer:
    """StreamServer orchestration tests.

    _ThreadingHTTPServer is replaced with a MagicMock so no real TCP ports
    are bound.  StreamWorker threads still start (retry_delay=0.05, empty
    tmp_path) so the worker lifecycle and status() logic are exercised.
    """

    @pytest.fixture(autouse=True)
    def _no_real_ports(self, monkeypatch):
        monkeypatch.setattr(
            'nofun.streams._ThreadingHTTPServer',
            lambda addr, handler: mock.MagicMock(),
        )

    def test_start_sets_running(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=1)
        try:
            srv.start()
            assert srv.running
        finally:
            srv.stop()

    def test_start_is_idempotent(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=1)
        try:
            srv.start()
            srv.start()  # second call is a no-op
            assert len(srv._workers) == 1
            assert srv.running
        finally:
            srv.stop()

    def test_stop_clears_running(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=1)
        srv.start()
        srv.stop()
        assert not srv.running

    def test_status_returns_correct_count(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=2)
        try:
            srv.start()
            assert len(srv.status()) == 2
        finally:
            srv.stop()

    def test_status_contains_expected_keys(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=1)
        try:
            srv.start()
            s = srv.status()[0]
            assert {'port', 'url', 'clip', 'clip_count', 'time_remaining', 'clip_duration', 'live', 'clients'} <= s.keys()
        finally:
            srv.stop()

    def test_status_port_matches_base_port(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=2)
        try:
            srv.start()
            statuses = srv.status()
            assert statuses[0]['port'] == 19000
            assert statuses[1]['port'] == 19001
        finally:
            srv.stop()

    def test_refresh_clips_does_not_crash(self, tmp_path):
        srv = StreamServer(tmp_path, retry_delay=0.05, base_port=19000, count=1)
        try:
            srv.start()
            srv.refresh_clips()  # must not raise even with empty dir
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# get_local_ip
# ---------------------------------------------------------------------------

class TestGetLocalIp:
    def test_returns_string(self):
        ip = get_local_ip()
        assert isinstance(ip, str)
        assert len(ip) > 0

    def test_falls_back_to_loopback(self):
        with mock.patch("nofun.streams.shutil.which", return_value=None):
            ip = get_local_ip()
        assert ip == "127.0.0.1"
