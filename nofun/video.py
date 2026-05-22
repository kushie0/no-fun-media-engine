"""nofun/video.py — VideoMixin: quadrant encode, clip export, NoFun rename flow."""

from __future__ import annotations

__all__ = [
    'build_encoder_config',
    'VideoMixin',
    'STEP_SECONDS',
    'QUAD_FILTER',
    'CLIP_FILTER',
    'MIN_QUAD',
    'SINGLE_FILTER',
]

import json
import logging
import pathlib
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from nofun.media_io import DeleteQueue, _DIM, _CYAN, _YELLOW, _R, fmt_size, probe_format, probe_stream, probe_total_frames
from nofun.paths import detect_platform
from nofun.script_runner import ScriptRunner, ScriptJob
from nofun.state import PauseState

if TYPE_CHECKING:
    from nofun.cleanup import ArchiveOutcome
    from nofun.tui import MediaEngineApp


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STEP_SECONDS = 40

QUAD_FILTER = (
    "[0:v]scale=out_range=limited:in_range=full,format=yuv420p,split=4[v1][v2][v3][v4];"
    "[v1]crop=iw/2:ih/2:0:0[ul];[v2]crop=iw/2:ih/2:iw/2:0[ur];"
    "[v3]crop=iw/2:ih/2:0:ih/2[ll];[v4]crop=iw/2:ih/2:iw/2:ih/2[lr]"
)

CLIP_FILTER = (
    'scale=320:180:flags=lanczos,fps=30,'
    'scale=out_range=limited:in_range=full,format=yuv420p'
)

SINGLE_FILTER = (
    'scale=trunc(iw/2)*2:trunc(ih/2)*2,'
    'scale=out_range=limited:in_range=full,format=yuv420p'
)

# Minimum quadrant dimensions (width, height) per encoder.
# A source of W×H produces quadrants of (W//2)×(H//2).
# h264_amf rejects anything below 128×64 (confirmed on AMD RX 6800).
# h264_videotoolbox is permissive; 32×32 is a safe conservative floor.
# libx264 (CPU) accepts dimensions ≥2×2 as long as they are even.
MIN_QUAD: dict[str, tuple[int, int]] = {
    'h264_amf':          (128, 64),
    'h264_videotoolbox': (32,  32),
    'libx264':           (2,   2),
}


# ---------------------------------------------------------------------------
# Encoder config
# ---------------------------------------------------------------------------

def build_encoder_config(gpu: bool = False, trial_run: int = 0) -> dict:
    """Return the encoder dict for the current platform.

    Keys: ``accel`` (list), ``enc_quad`` (list), ``enc_clip`` (list).

    Args:
        gpu:       Use GPU encoder on Windows (h264_amf + d3d11va).  Ignored
                   on macOS — videotoolbox is always used there.
        trial_run: Non-zero → use ultrafast preset for CPU encodes (speed over
                   quality during trial/test runs).
    """
    plat = detect_platform()
    if plat == 'darwin':
        return dict(
            accel    = [],
            enc_quad = ['-c:v', 'h264_videotoolbox', '-q:v', '82'],
            enc_clip = ['-c:v', 'h264_videotoolbox', '-q:v', '65'],
        )
    if gpu:
        return dict(
            accel    = ['-hwaccel', 'd3d11va'],
            enc_quad = ['-c:v', 'h264_amf', '-preset', 'quality',
                        '-rc', 'cqp', '-qp_i', '18', '-qp_p', '20'],
            enc_clip = ['-c:v', 'h264_amf', '-rc', 'cqp',
                        '-qp_i', '32', '-qp_p', '32'],
        )
    preset = 'ultrafast' if trial_run else 'veryslow'
    return dict(
        accel    = [],
        enc_quad = ['-c:v', 'libx264', '-preset', preset, '-crf', '18'],
        enc_clip = ['-c:v', 'libx264', '-preset', 'veryslow', '-crf', '23'],
    )


# ---------------------------------------------------------------------------
# VideoMixin
# ---------------------------------------------------------------------------

class VideoMixin:
    """Methods for encoding quadrant files and proxy clips from .mov sources."""

    # Attributes provided by Pipeline.__init__
    search_dir:             pathlib.Path
    vids_dest:              pathlib.Path
    clips_dest:             pathlib.Path
    video_archive:          pathlib.Path
    logger:               logging.Logger
    delete_queue:         DeleteQueue
    trial_run:            int
    force:                bool
    enc:                  dict
    mount_d:              pathlib.Path
    _app:                  MediaEngineApp | None
    _pause_state:          PauseState
    _script_runner:        ScriptRunner | None

    def _move_to_hard_paused(self, files: list[pathlib.Path]) -> None: ...
    # _archive_or_dedup must NOT be stubbed at class level — see AudioMixin comment.
    if TYPE_CHECKING:
        def _archive_or_dedup(self, src: pathlib.Path, archive_dir: pathlib.Path,
                              *extra_check_dirs: pathlib.Path) -> ArchiveOutcome: ...
    def _set_ffmpeg_proc(self, key: str, proc: 'subprocess.Popen | None') -> None: ...

    def _db_record_quads(self, base: str, dests: dict[str, pathlib.Path]) -> None:
        """Probe each finished quad file and upsert into the encoding DB."""
        db = getattr(self, '_encoding_db', None)
        if db is None:
            return
        from nofun.encoding_db import probe_file, _now_iso
        from nofun.inventory import extract_date_band
        date_str, band = extract_date_band(base)
        if date_str == 'TBD':
            return
        for q, p in dests.items():
            if not p.exists():
                continue
            try:
                info = probe_file(p)
                db.upsert(date_str, band, 'quadrant_video', {
                    'path':     str(p),
                    'quadrant': q,
                    'size':     p.stat().st_size,
                    'mtime':    p.stat().st_mtime,
                    'scanned':  _now_iso(),
                    **info,
                })
            except Exception:
                pass
        perf = db.get_performance(date_str, band) or {}
        rs   = db.derive_runtime_seconds(perf)
        if rs > 0:
            db.set_runtime_seconds(date_str, band, rs)
        try:
            db.save()
        except Exception:
            pass

        # Bump the banner perf counter the first time this perf encodes.
        bumped: set = getattr(self, '_banner_bumped_perfs', set())
        key = (date_str, band)
        if key not in bumped and rs > 0 and self._app:
            bumped.add(key)
            self._app.bump_perf_count()

    # -----------------------------------------------------------------------
    # Quadrant encoding
    # -----------------------------------------------------------------------

    def _encode_quadrants(self, source: pathlib.Path) -> bool:
        base  = source.stem
        quads = ('UL', 'UR', 'LL', 'LR')
        temps = {q: self.vids_dest / f'{base}_{q}_temp.mp4' for q in quads}
        dests = {q: self.vids_dest / f'{base}_{q}.mp4'      for q in quads}

        # MJPEG safety: d3d11va hardware decode corrupts MJPEG frames
        src_codec = probe_stream(source, 'codec_name')
        accel = []
        if src_codec == 'mjpeg' and self.enc['accel']:
            self.logger.info(f"NOTICE  {base} is MJPEG — skipping hardware decode")
        elif self.enc['accel']:
            accel = self.enc['accel']

        # Pre-flight: verify quadrant dimensions are above the encoder minimum.
        # probe_stream returns '' on failure — treat as 0 and skip the gate.
        try:
            src_w = int(probe_stream(source, 'width')  or '0')
            src_h = int(probe_stream(source, 'height') or '0')
        except ValueError:
            src_w = src_h = 0

        if src_w > 0 and src_h > 0:
            qw, qh = src_w // 2, src_h // 2
            encoder_name = self.enc['enc_quad'][1]
            floor_w, floor_h = MIN_QUAD.get(encoder_name, (2, 2))
            if qw < floor_w or qh < floor_h:
                self.logger.error(
                    f"ALERT   {base}: quadrant size {qw}×{qh} is below "
                    f"{encoder_name} minimum {floor_w}×{floor_h} — skipping encode"
                )
                return False

        accel_str = accel[1] if accel else 'none'
        try:
            src_dur = float(probe_format(source, 'duration') or 0.0)
        except (ValueError, TypeError):
            src_dur = 0.0
        total_frames = probe_total_frames(source, src_dur)
        from nofun.inventory import extract_date_band
        _, band = extract_date_band(base)
        self.logger.info(
            f"ENCODING  {base} → quadrants  ({self.enc['enc_quad'][1]})",
            extra={
                'src':   str(source),
                'codec': src_codec or 'unknown',
                'accel': accel_str,
                'trial': f"{self.trial_run}s" if self.trial_run else 'full',
            },
        )

        def _cb(frame: str, fps: str, tc: str, speed: str) -> None:
            if self._app:
                self._app.update_progress(
                    frame, fps, tc, speed,
                    duration=src_dur, job_label='quadrants',
                    band=band, total_frames=total_frames,
                )

        runner = self._script_runner
        job = ScriptJob(
            script='encode_quads',
            args={
                'source':   str(source),
                'dest_dir': str(self.vids_dest),
                'base':     base,
                'accel':    accel_str,
                'encoder':  json.dumps(self.enc['enc_quad']),
                'filter':   QUAD_FILTER,
                'trial':    self.trial_run,
            },
            label=f'{base} → quadrants',
        )
        result = runner.run(
            job,
            progress_cb=_cb,
            proc_cb=lambda p: self._set_ffmpeg_proc('encode', p),
        )
        rc = result.exit_code

        self._set_ffmpeg_proc('encode', None)
        if self._app:
            self._app.clear_row('progress')
        if rc == 0:
            for q in quads:
                temps[q].rename(dests[q])
                try:
                    sz = dests[q].stat().st_size
                except OSError:
                    sz = 0
                self.logger.info(
                    f"CREATE  {dests[q].name}",
                    extra={'path': str(dests[q]), 'size': fmt_size(sz)},
                )
            self._db_record_quads(base, dests)
            return True

        # Cleanup / preserve partial temp files on failure
        partial = [tf for tf in temps.values() if tf.exists()]
        if self._pause_state == PauseState.HARD_PENDING and partial:
            self._move_to_hard_paused(partial)
        else:
            for tf in partial:
                tf.unlink(missing_ok=True)
        self.logger.error(f"Quadrant generation failed for {base}")
        return False

    # -----------------------------------------------------------------------
    # Single-camera transcode (Singles/ subdirectory)
    # -----------------------------------------------------------------------

    def _transcode_single(self, source: pathlib.Path) -> bool:
        """Transcode a single-camera .mov to vids_dest/{base}.mp4 (no quad split).

        Output is written atomically via {base}_single_temp.mp4 → {base}.mp4.
        Returns True on success.
        """
        base = source.stem
        dest = self.vids_dest / f'{base}.mp4'
        temp = self.vids_dest / f'{base}_single_temp.mp4'

        src_codec = probe_stream(source, 'codec_name')
        accel: list = []
        if src_codec == 'mjpeg' and self.enc['accel']:
            self.logger.info(f"NOTICE  {base} is MJPEG — skipping hardware decode")
        elif self.enc['accel']:
            accel = self.enc['accel']

        try:
            src_dur = float(probe_format(source, 'duration') or 0.0)
        except (ValueError, TypeError):
            src_dur = 0.0
        total_frames = probe_total_frames(source, src_dur)
        from nofun.inventory import extract_date_band
        _, band = extract_date_band(base)
        self.logger.info(
            f"ENCODING  {base} → single  ({self.enc['enc_quad'][1]})",
            extra={
                'src':   str(source),
                'codec': src_codec or 'unknown',
                'trial': f"{self.trial_run}s" if self.trial_run else 'full',
            },
        )

        def _cb(frame: str, fps: str, tc: str, speed: str) -> None:
            if self._app:
                self._app.update_progress(
                    frame, fps, tc, speed,
                    duration=src_dur, job_label='transcode',
                    band=band, total_frames=total_frames,
                )

        job = ScriptJob(
            script='transcode_single',
            args={
                'source':   str(source),
                'dest_dir': str(self.vids_dest),
                'base':     base,
                'accel':    accel[1] if accel else 'none',
                'encoder':  json.dumps(self.enc['enc_quad']),
                'filter':   SINGLE_FILTER,
                'trial':    self.trial_run,
            },
            label=f'{base} → single',
        )
        result = self._script_runner.run(  # type: ignore[union-attr]
            job,
            progress_cb=_cb,
            proc_cb=lambda p: self._set_ffmpeg_proc('encode', p),
        )
        self._set_ffmpeg_proc('encode', None)
        if self._app:
            self._app.clear_row('progress')

        if result.exit_code == 0:
            temp.rename(dest)
            try:
                sz = dest.stat().st_size
            except OSError:
                sz = 0
            self.logger.info(
                f"CREATE  {dest.name}",
                extra={'path': str(dest), 'size': fmt_size(sz)},
            )
            return True

        partial = [temp] if temp.exists() else []
        if self._pause_state == PauseState.HARD_PENDING and partial:
            self._move_to_hard_paused(partial)
        else:
            for tf in partial:
                tf.unlink(missing_ok=True)
        self.logger.error(f"Single transcode failed for {base}")
        return False

    # -----------------------------------------------------------------------
    # Clip export
    # -----------------------------------------------------------------------

    def _export_clips(self, base: str) -> None:
        clips_dir = self.clips_dest / base
        per_quad_start: dict[str, int] = {}  # empty = all quads from clip 1

        if not self.force:
            quads_present = [q for q in ('UL', 'UR', 'LL', 'LR')
                             if (self.vids_dest / f'{base}_{q}.mp4').exists()]
            if not quads_present:
                return

            counts = {
                q: sum(1 for p in clips_dir.glob(f'{base}_{q}_*.mp4')
                       if p.stem.rsplit('_', 1)[-1].isdigit())
                if clips_dir.exists() else 0
                for q in quads_present
            }
            top = max(counts.values())
            if top > 0 and len(set(counts.values())) == 1:
                self.logger.debug(f"SKIP    {base} clips (complete)")
                return

            per_quad_start = {q: c + 1 for q, c in counts.items() if c < top}
            resuming = [f'{q} from {c + 1}' for q, c in counts.items() if 0 < c < top]
            if resuming:
                self.logger.info(f"RESUME  {base} clips ({', '.join(resuming)})")

        clips_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"CLIPS   {base}")

        any_quad = next(
            (self.vids_dest / f'{base}_{q}.mp4'
             for q in ('UL', 'UR', 'LL', 'LR')
             if (self.vids_dest / f'{base}_{q}.mp4').exists()),
            None,
        )
        try:
            src_dur = float(probe_format(any_quad, 'duration') or 0.0) if any_quad else 0.0
        except (ValueError, TypeError):
            src_dur = 0.0
        total_frames = probe_total_frames(any_quad, src_dur) if any_quad else None
        from nofun.inventory import extract_date_band
        _, band = extract_date_band(base)

        runner = self._script_runner
        job = ScriptJob(
            script='export_clips',
            args={
                'source_dir':     str(self.vids_dest),
                'base':           base,
                'temp_dir':       str(self.search_dir),
                'clips_dir':      str(clips_dir),
                'encoder':        json.dumps(self.enc['enc_clip']),
                'filter':         CLIP_FILTER,
                'step':           STEP_SECONDS,
                'per_quad_start': json.dumps(per_quad_start),
            },
            label=f'{base} → clips',
        )

        def _cb(frame: str, fps: str, tc: str, speed: str) -> None:
            if self._app:
                self._app.update_progress(
                    frame, fps, tc, speed,
                    duration=src_dur, job_label='clips',
                    band=band, total_frames=total_frames,
                )

        _clip_t0: list[float] = []

        def _clip_cb(n: int, total: int) -> None:
            if not _clip_t0:
                _clip_t0.append(time.monotonic())
            elapsed = time.monotonic() - _clip_t0[0] if _clip_t0 else 0.0
            if self._app:
                self._app.update_clip_progress(n, total, band=band, elapsed_s=elapsed)

        result = runner.run(
            job,
            progress_cb=_cb,
            proc_cb=lambda p: self._set_ffmpeg_proc('encode', p),
            clip_progress_cb=_clip_cb,
        )
        self._set_ffmpeg_proc('encode', None)
        if self._app:
            self._app.clear_row('progress')

        quads_data = result.stdout_json.get('quads') if not result.killed else None
        if quads_data:
            failed_quads: list[str] = []
            for qdata in quads_data:
                quad = qdata['quad']
                if qdata.get('status') == 'ok':
                    moved = qdata.get('moved_count', 0)
                    if moved:
                        self.logger.info(f"CREATE  {moved} clips  ({quad})")
                else:
                    failed_quads.append(quad)
                    for tf in self.search_dir.glob(f'{base}_{quad}_temp_*.mp4'):
                        tf.unlink(missing_ok=True)
            if failed_quads:
                tail = f'\n{result.stderr_tail}' if result.stderr_tail else ''
                self.logger.error(
                    f"Clip export failed quads: {', '.join(failed_quads)}  ({base}){tail}"
                )
        elif not result.ok:
            for quad in ('UL', 'UR', 'LL', 'LR'):
                partial = list(self.search_dir.glob(f'{base}_{quad}_temp_*.mp4'))
                if self._pause_state == PauseState.HARD_PENDING and partial:
                    self._move_to_hard_paused(partial)
                else:
                    for tf in partial:
                        tf.unlink(missing_ok=True)
            tail = f'\n{result.stderr_tail}' if result.stderr_tail else ''
            self.logger.error(f"Clip export failed: {base}{tail}")

    # -----------------------------------------------------------------------
    # Process a single .mov file (quadrants + clips)
    # -----------------------------------------------------------------------

    def _process_mov(self, source: pathlib.Path, skip_clips: bool = False) -> bool:
        base = source.stem
        quads = [self.vids_dest / f'{base}_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        all_exist = all(q.exists() for q in quads)

        if all_exist and not self.force:
            self.logger.debug(f"SKIP    {base} quadrants (exist)")
            if not self.trial_run and self.mount_d != pathlib.Path('.'):
                self._archive_or_dedup(source, self.video_archive)
        else:
            if not self._encode_quadrants(source):
                return False
            if not self.trial_run and self.mount_d != pathlib.Path('.'):
                dest = self.video_archive / source.name
                shutil.move(str(source), str(dest))
                self.logger.info(f"MOVE    {source.name} → {self.video_archive.name}/")

        if not skip_clips:
            self._export_clips(base)
        return True

    # -----------------------------------------------------------------------
    # NoFun rename — identify band before processing
    # -----------------------------------------------------------------------

    def _find_matching_wavs(self, mov: pathlib.Path) -> list[pathlib.Path]:
        """Find WAV files that belong to the same recording session as *mov*.

        The first WAV is the one whose creation time is within ±60 s of the
        MOV.  Subsequent WAVs are chained: each must be created ~20 min
        (±2 min) after the previous one.
        """
        try:
            mov_ctime = mov.stat().st_ctime
        except OSError:
            return []

        wavs = sorted(self.search_dir.glob('*.wav'))
        if not wavs:
            return []

        # Build list of (path, ctime) and sort by ctime
        wav_times = []
        for w in wavs:
            try:
                wav_times.append((w, w.stat().st_ctime))
            except OSError:
                continue
        wav_times.sort(key=lambda x: x[1])

        # Find first WAV within ±60 s of the MOV
        first = None
        for w, ct in wav_times:
            if abs(ct - mov_ctime) <= 60:
                first = (w, ct)
                break

        if first is None:
            return []

        chain = [first[0]]
        prev_ct = first[1]

        # Chain forward: each next WAV is ~20 min (1200 s ± 120 s) after prev
        for w, ct in wav_times:
            if ct <= prev_ct:
                continue
            gap = ct - prev_ct
            if 1080 <= gap <= 1320:  # 18–22 minutes
                chain.append(w)
                prev_ct = ct

        return chain

    def _prompt_rename_nofun(self, mov: pathlib.Path) -> pathlib.Path:
        """If *mov* has the default 'NoFun' name, pause and let the user
        preview it in VLC, enter a band name, and rename the MOV + WAVs.

        Returns the (possibly renamed) MOV path.
        """
        stem = mov.stem
        # Match stems ending with _NoFun (case-insensitive)
        if not re.search(r'_NoFun$', stem, re.IGNORECASE):
            return mov

        matching_wavs = self._find_matching_wavs(mov)

        self.logger.info(f"NOTICE  '{mov.name}' has the default NoFun name")
        if matching_wavs:
            self.logger.info(
                f"        Found {len(matching_wavs)} associated WAV file(s): "
                + ', '.join(w.name for w in matching_wavs)
            )

        # Support headless testing / non-interactive shells
        import sys
        if not sys.stdin.isatty():
            return mov

        # Offer VLC preview
        print(f"\n  {_YELLOW}> Open '{mov.name}' in VLC to identify the band?{_R}  [Y/n] ", end='', flush=True)
        vlc_answer = input().strip().lower()
        if vlc_answer in ('', 'y', 'yes'):
            vlc_cmd = 'vlc'
            if shutil.which('vlc') is None and shutil.which('vlc.exe') is None:
                # Common Windows install path
                vlc_default = pathlib.Path(r'C:\Program Files\VideoLAN\VLC\vlc.exe')
                if vlc_default.exists():
                    vlc_cmd = str(vlc_default)
                else:
                    self.logger.info("NOTICE  VLC not found on PATH — skipping preview")
                    vlc_cmd = None
            if vlc_cmd:
                try:
                    subprocess.Popen([vlc_cmd, str(mov)],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    self.logger.info(f"        Opened {mov.name} in VLC")
                except OSError as e:
                    self.logger.info(f"NOTICE  Could not launch VLC: {e}")

        # Prompt for band name
        print(f"\n  {_CYAN}Enter band name{_R} (or {_DIM}SKIP{_R} to process as-is): ", end='', flush=True)
        band_name = input().strip()

        if not band_name or band_name.upper() == 'SKIP':
            self.logger.info("        Skipping rename — processing as-is")
            return mov

        # Build new stem: replace 'NoFun' with the band name
        new_stem = re.sub(r'NoFun$', band_name, stem, flags=re.IGNORECASE)
        new_mov = mov.parent / f'{new_stem}{mov.suffix}'

        # Rename the .mov
        mov.rename(new_mov)
        self.logger.info(
            f"RENAME  {mov.name} → {new_mov.name}",
            extra={'src': str(mov), 'dst': str(new_mov)},
        )

        # Rename associated WAVs
        for wav in matching_wavs:
            wav_stem = wav.stem
            new_wav_stem = re.sub(r'NoFun', band_name, wav_stem, flags=re.IGNORECASE)
            if new_wav_stem == wav_stem:
                # WAV doesn't contain NoFun — try matching the date prefix
                # and just keep the original name
                continue
            new_wav = wav.parent / f'{new_wav_stem}{wav.suffix}'
            wav.rename(new_wav)
            self.logger.info(
                f"RENAME  {wav.name} → {new_wav.name}",
                extra={'src': str(wav), 'dst': str(new_wav)},
            )

        return new_mov
