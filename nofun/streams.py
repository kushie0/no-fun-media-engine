"""nofun/streams.py — ffmpeg-based HTTP stream server (no VLC required).

Each stream is a worker thread that loops clips from a shuffled pool through
ffmpeg one at a time, broadcasting MPEG-TS chunks to any subscribed HTTP
clients (e.g. TouchDesigner).

Key properties
--------------
- No VLC dependency: uses the ffmpeg already required by the pipeline.
- Dynamic playlist: new clips are picked up between clip transitions via
  refresh_clips(); called automatically after each successful encode.
- Variable duration: each clip gets a random CLIP_DURATION_MIN–MAX slot.
- Timestamp continuity: -output_ts_offset keeps PTS monotonically increasing
  across clips so downstream decoders never see a backward jump.
- Lightweight: ffmpeg passthrough (-c:v copy) uses ~15–30 MB / <1% CPU per
  stream vs VLC's ~60 MB / ~3%.
"""

from __future__ import annotations

__all__ = [
    'StreamServer',
    'StreamWorker',
    'get_local_ip',
    'BASE_PORT',
    'STREAM_COUNT',
    'BROADCAST_MAXQ',
    'CLIP_DURATION_MIN',
    'CLIP_DURATION_MAX',
]

import http.server
import logging
import os
import pathlib
import queue
import random
import shutil
import socketserver
import subprocess
import threading
import time

from nofun.paths import is_windows

log = logging.getLogger('media_engine.streams')

BASE_PORT         = 8554
STREAM_COUNT      = 5
CLIP_DURATION_MIN = 30.0   # seconds per clip in the batch
CLIP_DURATION_MAX = 40.0   # seconds per clip in the batch
BATCH_SIZE        = 10      # clips per ffmpeg invocation (seamless within a batch)
CHUNK_SIZE        = 8_192   # bytes read from ffmpeg stdout per iteration
BROADCAST_MAXQ    = 512     # max queued chunks per client (~4 MB at 8 KB each)


# ---------------------------------------------------------------------------
# StreamWorker
# ---------------------------------------------------------------------------

class StreamWorker(threading.Thread):
    """Loops H264 clips through ffmpeg and broadcasts MPEG-TS to subscribers.

    Start with .start(); stop cleanly with .stop() (sets event + kills ffmpeg).
    Clients subscribe/unsubscribe via .subscribe()/.unsubscribe().
    """

    def __init__(self, clip_root: pathlib.Path, port: int, retry_delay: float = 10.0,
                 bsf: str | None = None) -> None:
        super().__init__(daemon=True, name=f'stream-{port}')
        self.port        = port
        self.clip_root   = clip_root
        self._retry_delay = retry_delay
        self._bsf        = bsf  # fallback bitstream filter if codec probe fails

        self._stop       = threading.Event()
        self._clips_lock = threading.Lock()
        self._clips: list[pathlib.Path] = []

        # codec cache: path str → 'h264' | 'hevc' | 'unknown'
        self._codec_cache: dict[str, str] = {}

        self._clients_lock = threading.Lock()
        self._clients: list[queue.Queue[bytes | None]] = []

        self._proc_lock    = threading.Lock()
        self._current_proc: subprocess.Popen | None = None

        # Batch state — updated each time a new ffmpeg concat process starts
        self._batch:         list[tuple[pathlib.Path, float]] = []
        self._batch_started: float = 0.0

    # ------------------------------------------------------------------
    # Public API — safe to call from any thread
    # ------------------------------------------------------------------

    def set_clips(self, clips: list[pathlib.Path]) -> None:
        """Update the clip pool from a pre-built list (avoids per-worker filesystem scan)."""
        with self._clips_lock:
            self._clips = clips

    @property
    def clip_count(self) -> int:
        with self._clips_lock:
            return len(self._clips)

    @property
    def current_clip(self) -> pathlib.Path | None:
        """Approximate clip currently playing, based on elapsed time in the batch."""
        if not self._batch:
            return None
        elapsed = time.time() - self._batch_started
        cumulative = 0.0
        for clip, duration in self._batch:
            cumulative += duration
            if elapsed < cumulative:
                return clip
        return self._batch[-1][0]

    @property
    def time_remaining(self) -> float:
        """Seconds remaining in the current clip (not the whole batch)."""
        if not self._batch:
            return 0.0
        elapsed = time.time() - self._batch_started
        cumulative = 0.0
        for _, duration in self._batch:
            cumulative += duration
            if elapsed < cumulative:
                return cumulative - elapsed
        return 0.0

    @property
    def current_clip_duration(self) -> float:
        """Total duration assigned to the clip currently playing."""
        if not self._batch:
            return 0.0
        elapsed = time.time() - self._batch_started
        cumulative = 0.0
        for _, duration in self._batch:
            cumulative += duration
            if elapsed < cumulative:
                return duration
        return self._batch[-1][1]

    def subscribe(self) -> queue.Queue[bytes | None]:
        """Return a new queue that will receive broadcast chunks."""
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=BROADCAST_MAXQ)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        with self._clients_lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def stop(self) -> None:
        """Signal the worker to exit and kill any running ffmpeg process."""
        self._stop.set()
        with self._proc_lock:
            if self._current_proc and self._current_proc.poll() is None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass
        # Unblock client threads waiting on q.get()
        with self._clients_lock:
            for q in self._clients:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clip_codec(self, clip: pathlib.Path) -> str:
        """Return 'h264', 'hevc', or 'unknown' for a clip. Results are cached."""
        key = str(clip)
        if key not in self._codec_cache:
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                     '-show_entries', 'stream=codec_name',
                     '-of', 'default=nw=1:nk=1', str(clip)],
                    capture_output=True, text=True, timeout=5,
                )
                self._codec_cache[key] = result.stdout.strip().lower()
            except Exception:
                self._codec_cache[key] = 'unknown'
        return self._codec_cache[key]

    def _bsf_for_codec(self, codec: str) -> str | None:
        if codec in ('hevc', 'h265'):
            return 'hevc_mp4toannexb'
        if codec in ('h264', 'avc'):
            return 'h264_mp4toannexb'
        return self._bsf  # fallback to startup default

    def _broadcast(self, chunk: bytes) -> None:
        """Push chunk to all client queues; skip the chunk for slow clients.

        Slow clients miss individual chunks (brief glitch) but stay connected.
        True disconnections are detected in the HTTP handler via socket errors,
        which then calls unsubscribe() to clean up.
        """
        with self._clients_lock:
            for q in self._clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass  # skip this chunk for the slow client; do NOT disconnect

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Loop forever: build a batch of clips, run one ffmpeg concat process per batch."""
        import tempfile

        while not self._stop.is_set():
            # Wait for clips
            with self._clips_lock:
                pool = list(self._clips)
            if not pool:
                if self._retry_delay >= 1.0:
                    log.warning(f"Stream {self.port}: no clips in pool — waiting {self._retry_delay:.0f}s")
                time.sleep(self._retry_delay)
                continue

            # Build a shuffled batch with random per-clip durations.
            # All clips in a batch must share the same codec so we can apply
            # a single bitstream filter.  Probe only the batch candidates
            # (BATCH_SIZE clips max) — not the whole pool — to avoid stalling
            # the first batch with hundreds of ffprobe calls.  The codec cache
            # fills naturally across batches.
            random.shuffle(pool)
            candidates  = pool[:BATCH_SIZE]
            first_codec = self._clip_codec(candidates[0])
            batch: list[tuple[pathlib.Path, float]] = [
                (clip, random.uniform(CLIP_DURATION_MIN, CLIP_DURATION_MAX))
                for clip in candidates
                if self._clip_codec(clip) == first_codec
            ]
            batch_bsf = self._bsf_for_codec(first_codec)

            # Write ffconcat playlist to a temp file
            try:
                pf = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False, encoding='utf-8',
                )
                pf.write('ffconcat version 1.0\n')
                for clip, duration in batch:
                    # Forward slashes work on Windows ffmpeg; escape any single quotes
                    path_str = str(clip).replace('\\', '/').replace("'", r"\'")
                    pf.write(f"file '{path_str}'\n")
                    pf.write(f"duration {duration:.3f}\n")
                pf.close()
                playlist_path = pf.name
            except Exception as exc:
                log.error(f"Stream {self.port}: failed to write playlist: {exc}")
                time.sleep(1.0)
                continue

            self._batch         = batch
            self._batch_started = time.time()
            total_dur           = sum(d for _, d in batch)
            log.debug(
                f"Stream {self.port}: batch {len(batch)} clips ~{total_dur:.0f}s"
                f" starting with {batch[0][0].name}"
            )

            cmd = [
                'ffmpeg',
                '-hide_banner', '-loglevel', 'warning',
                '-fflags', '+genpts+igndts',          # regenerate PTS/DTS — fixes non-monotonic timestamps at clip seams
                '-re',                               # real-time pacing
                '-f', 'concat', '-safe', '0',
                '-i', playlist_path,
                '-c:v', 'copy',                      # passthrough (no transcode)
                '-c:a', 'none',                      # strip audio
            ]
            if batch_bsf:
                # Convert MP4 bitstream packaging to Annex B for MPEG-TS muxer
                cmd += ['-bsf:v', batch_bsf]
            cmd += [
                '-flush_packets', '1',               # flush pipe after each packet
                '-f', 'mpegts', 'pipe:1',
            ]

            try:
                extra: dict = {'stdin': subprocess.DEVNULL}
                if is_windows():
                    extra['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **extra,
                )
            except FileNotFoundError:
                log.error('ffmpeg not found — stream worker cannot start')
                os.unlink(playlist_path)
                break

            with self._proc_lock:
                self._current_proc = proc

            stderr_lines: list[str] = []

            def _drain_stderr() -> None:
                assert proc.stderr is not None
                for raw in proc.stderr:
                    line = raw if isinstance(raw, str) else raw.decode(errors='replace')
                    stderr_lines.append(line.rstrip())

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            assert proc.stdout is not None
            produced_output = False
            while not self._stop.is_set():
                chunk = proc.stdout.read1(CHUNK_SIZE)  # type: ignore[attr-defined]
                if not chunk:
                    break
                produced_output = True
                self._broadcast(chunk)

            proc.stdout.close()
            proc.wait()
            stderr_thread.join(timeout=2.0)

            with self._proc_lock:
                self._current_proc = None

            try:
                os.unlink(playlist_path)
            except Exception:
                pass

            if not produced_output and not self._stop.is_set():
                stderr_summary = ' | '.join(stderr_lines[-5:]) if stderr_lines else '(no stderr)'
                log.warning(
                    f"Stream {self.port}: batch produced no output"
                    f" (exit {proc.returncode}) — {stderr_summary}"
                )
                time.sleep(1.0)
            elif stderr_lines:
                log.info(f"Stream {self.port}: ffmpeg stderr: " + ' | '.join(stderr_lines[-3:]))

        self._batch = []


# ---------------------------------------------------------------------------
# HTTP broadcast server
# ---------------------------------------------------------------------------

class _StreamHandler(http.server.BaseHTTPRequestHandler):
    """Relay MPEG-TS chunks from StreamWorker to one HTTP client."""

    # Injected by StreamServer via type() — each port gets its own subclass
    worker: StreamWorker

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence per-request log lines

    def do_GET(self) -> None:
        if self.path not in ('/video', '/video/'):
            log.warning(f"Stream {self.worker.port}: 404 {self.path}")
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'video/mp2t')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        q = self.worker.subscribe()
        chunks_sent = 0
        try:
            while True:
                chunk = q.get(timeout=15.0)
                if chunk is None:       # poison pill from worker.stop()
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                chunks_sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass    # client disconnected — normal
        except queue.Empty:
            log.warning(
                f"Stream {self.worker.port}: queue timeout after {chunks_sent} chunks — ffmpeg stalled?"
            )
        finally:
            self.worker.unsubscribe(q)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads      = True   # handler threads die with the main process
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# StreamServer — top-level manager
# ---------------------------------------------------------------------------

class StreamServer:
    """Manages STREAM_COUNT concurrent ffmpeg→HTTP streams.

    Usage::

        ss = StreamServer(clips_dest)
        ss.start()          # launches workers + HTTP servers
        ss.refresh_clips()  # call after new clips are encoded
        ss.stop()           # clean shutdown
    """

    def __init__(
        self,
        clip_root:   pathlib.Path,
        base_port:   int   = BASE_PORT,
        count:       int   = STREAM_COUNT,
        retry_delay: float = 10.0,
    ) -> None:
        self.clip_root   = clip_root
        self.base_port   = base_port
        self.count       = count
        self._retry_delay = retry_delay

        self._workers: list[StreamWorker]         = []
        self._servers: list[_ThreadingHTTPServer] = []
        self._running  = False

    @staticmethod
    def _detect_bsf(clips: list[pathlib.Path]) -> str | None:
        """Probe the first available clip to pick the right MP4→TS bitstream filter."""
        for clip in clips[:5]:
            try:
                result = subprocess.run(
                    [
                        'ffprobe', '-v', 'error',
                        '-select_streams', 'v:0',
                        '-show_entries', 'stream=codec_name',
                        '-of', 'default=nw=1:nk=1',
                        str(clip),
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                codec = result.stdout.strip().lower()
                if codec in ('hevc', 'h265'):
                    return 'hevc_mp4toannexb'
                if codec in ('h264', 'avc'):
                    return 'h264_mp4toannexb'
            except Exception:
                continue
        # Default: H264 — pipeline encodes to h264_videotoolbox / h264_amf / libx264
        return 'h264_mp4toannexb'

    def _scan_clips(self) -> list[pathlib.Path]:
        """Single filesystem scan shared by all workers."""
        if not self.clip_root.exists():
            log.warning(f'Streams: clip_root does not exist: {self.clip_root}')
            return []
        try:
            found = sorted(self.clip_root.rglob('*.mp4'))
        except Exception as exc:
            log.warning(f'Streams: rglob failed on {self.clip_root}: {exc}')
            return []
        if not found:
            subdirs = [p for p in self.clip_root.iterdir() if p.is_dir()]
            log.warning(
                f'Streams: 0 clips in {self.clip_root}'
                + (f' — {len(subdirs)} subdirs: {[d.name for d in subdirs[:5]]}' if subdirs else ' — directory is empty')
            )
        else:
            log.info(f'Streams: {len(found)} clips found in {self.clip_root}')
        return found

    @staticmethod
    def _evict_port_squatters(ports: list[int]) -> None:
        """Kill any foreign processes already listening on our ports (Windows only).

        Uses netstat to find PIDs, skips our own PID, kills the rest with
        taskkill.  Safe no-op on non-Windows and if no squatters are found.
        """
        if not is_windows():
            return
        own_pid = os.getpid()
        port_set = {str(p) for p in ports}
        try:
            out = subprocess.run(
                ['netstat', '-ano'],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return
        pids_to_kill: set[int] = set()
        for line in out.splitlines():
            parts = line.split()
            # netstat line: Proto  Local  Remote  State  PID
            if len(parts) < 5 or parts[3] != 'LISTENING':
                continue
            local = parts[1]          # e.g. 0.0.0.0:8554
            pid_str = parts[4]
            port_part = local.rsplit(':', 1)[-1]
            if port_part in port_set:
                try:
                    pid = int(pid_str)
                except ValueError:
                    continue
                if pid != own_pid:
                    pids_to_kill.add(pid)
        for pid in pids_to_kill:
            try:
                subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                               capture_output=True, timeout=5)
                log.info(f'Streams: evicted stale process PID {pid} from stream ports')
            except Exception as exc:
                log.warning(f'Streams: could not kill PID {pid}: {exc}')
        if pids_to_kill:
            time.sleep(0.5)   # give Windows a moment to release the sockets

    def start(self) -> None:
        if self._running:
            return
        ports = list(range(self.base_port, self.base_port + self.count))
        self._evict_port_squatters(ports)
        clips = self._scan_clips()
        bsf   = self._detect_bsf(clips)
        if bsf:
            log.info(f'Streams: using bitstream filter {bsf}')
        for i in range(self.count):
            port   = self.base_port + i
            worker = StreamWorker(self.clip_root, port, retry_delay=self._retry_delay, bsf=bsf)
            worker.set_clips(clips)

            # Each port gets its own handler subclass with the worker bound in
            handler = type(f'_H{port}', (_StreamHandler,), {'worker': worker})
            server  = _ThreadingHTTPServer(('', port), handler)

            worker.start()

            def _serve(srv=server, p=port):
                try:
                    srv.serve_forever()
                except Exception as exc:
                    log.error(f'Stream {p}: HTTP server crashed: {exc}')

            threading.Thread(target=_serve, name=f'http-{port}', daemon=True).start()

            self._workers.append(worker)
            self._servers.append(server)

        self._running = True
        ip    = get_local_ip()
        ports = f'{self.base_port}–{self.base_port + self.count - 1}'
        log.info(f'Streams started  ({self.count} streams · {ip} · ports {ports})')

    def stop(self) -> None:
        for w in self._workers:
            w.stop()
        for s in self._servers:
            s.shutdown()
        self._workers.clear()
        self._servers.clear()
        self._running = False
        log.info('Streams stopped')

    def refresh_clips(self) -> None:
        """Scan once and push the updated list to all workers."""
        clips = self._scan_clips()
        for w in self._workers:
            w.set_clips(clips)

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> list[dict]:
        """Per-stream status dicts for the STREAMS menu."""
        ip = get_local_ip()
        return [
            {
                'port':           self.base_port + i,
                'url':            f'http://{ip}:{self.base_port + i}/video',
                'clip':           w.current_clip.name if w.current_clip else '(none)',
                'clip_count':     w.clip_count,
                'time_remaining': w.time_remaining,
                'clip_duration':  w.current_clip_duration,
                'live':           w.is_alive() and not w._stop.is_set(),
                'clients':        w.client_count,
            }
            for i, w in enumerate(self._workers)
        ]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """Detect LAN IP: ipconfig.exe (Windows) → ip route (Linux) → ipconfig (macOS)."""
    if shutil.which('ipconfig.exe'):
        try:
            out = subprocess.run(
                ['ipconfig.exe'], capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if 'IPv4 Address' in line:
                    return line.split(':')[-1].strip().rstrip('\r')
        except Exception:
            pass

    if shutil.which('ip'):
        try:
            out = subprocess.run(
                ['ip', 'route', 'get', '1'], capture_output=True, text=True, timeout=5,
            ).stdout
            parts = out.split()
            if 'src' in parts:
                return parts[parts.index('src') + 1]
        except Exception:
            pass

    if shutil.which('ipconfig'):
        for iface in ('en0', 'en1'):
            try:
                out = subprocess.run(
                    ['ipconfig', 'getifaddr', iface],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if out:
                    return out
            except Exception:
                pass

    return '127.0.0.1'
