"""nofun/audio.py — AudioMixin: channel split, silence drop, ZIP archive."""

from __future__ import annotations

import concurrent.futures
import datetime
import logging
import os
import pathlib
import re
import subprocess
import tempfile
import time
import zipfile as _zipfile
import zlib
from collections import defaultdict
from typing import TYPE_CHECKING

from nofun.inventory import extract_date_band, perf_key, perf_output_name
from nofun.media_io import DeleteQueue, fmt_size, format_eta, probe_stream
from nofun.paths import NULL_DEV
from nofun.script_runner import ScriptRunner, ScriptJob
from nofun.state import PauseState

if TYPE_CHECKING:
    from nofun.cleanup import ArchiveOutcome
    from nofun.tui import MediaEngineApp


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_ACTIVE_SECONDS = 5   # seconds — channels with less signal than this are dropped


def chan_wav_name(base: str, ch: int) -> str:
    """Channel-split WAV name the split_audio script writes (1-based channel).

    Must match scripts/split_audio.py (stateless, stdlib-only — keeps its own copy).
    """
    return f'{base}_ch{ch:02d}.wav'

# Sentinel returned by _encode_one_flac when a file disappears or fails to encode
# mid-ZIP. arcname='' signals the caller to skip this entry and log it as dropped.
_COMPRESS_MISSING: tuple[str, bytes, int, int] = ('', b'', -1, 0)


def _encode_one_flac(path: pathlib.Path) -> tuple[str, bytes, int, int]:
    """Encode a channel WAV to FLAC and return its bytes (for ZIP_STORED).

    FLAC models the waveform, so PCM shrinks to ~40% where raw deflate barely
    moved it. The already-compressed FLAC is then *stored* in the zip (no second
    deflate pass). ffmpeg runs as a subprocess, so threads encode in parallel.

    Bit depth: the source WAVs are 32-bit PCM but FLAC's ceiling is 24-bit, so
    the top 24 bits are kept bit-exact and the low 8 bits are dropped. Those low
    bits sit below any mic/preamp noise floor (24-bit ≈ 144 dB), so this is
    perceptually lossless and the mp3 deliverable is unaffected — but it is NOT
    a bit-exact round-trip back to the original 32-bit WAV. (Decision: accept
    24-bit FLAC as the archive depth, 2026-06-07.)

    Returns (arcname='{stem}.flac', flac_bytes, size, crc32), or
    _COMPRESS_MISSING if the source vanished or could not be encoded.
    """
    if not path.exists():
        return _COMPRESS_MISSING
    with tempfile.NamedTemporaryFile(suffix='.flac', delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        proc = subprocess.run(
            ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
             '-i', str(path), '-c:a', 'flac', '-compression_level', '8',
             str(tmp_path)],
            capture_output=True,
        )
        if proc.returncode != 0 or not tmp_path.exists():
            return _COMPRESS_MISSING
        data = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return path.stem + '.flac', data, len(data), crc


# ---------------------------------------------------------------------------
# AudioMixin
# ---------------------------------------------------------------------------

class AudioMixin:
    """Methods for splitting multichannel WAVs and creating ZIP archives."""

    # Attributes provided by Pipeline.__init__
    search_dir:              pathlib.Path
    audio_dest:              pathlib.Path
    audio_archive:           pathlib.Path
    logger:               logging.Logger
    delete_queue:         DeleteQueue
    trial_run:            int
    force:                bool
    mount_d:              pathlib.Path
    _file_sizes:          dict
    _app:          MediaEngineApp | None
    _pause_state:  PauseState
    _script_runner:        ScriptRunner | None

    def _move_to_hard_paused(self, files: list[pathlib.Path]) -> None: ...
    def _is_file_stable(self, path: pathlib.Path) -> bool: ...
    def _flush_commands(self) -> None: ...
    # _archive_or_dedup is NOT stubbed here — the real implementation lives in
    # CleanupMixin and must be found first in Pipeline's MRO. A stub here would
    # shadow it and silently do nothing (Python MRO resolves AudioMixin before
    # CleanupMixin in `class Pipeline(VideoMixin, AudioMixin, CleanupMixin)`).
    if TYPE_CHECKING:
        def _archive_or_dedup(self, src: pathlib.Path, archive_dir: pathlib.Path,
                              *extra_check_dirs: pathlib.Path) -> ArchiveOutcome: ...
    def _set_ffmpeg_proc(self, key: str, proc: 'subprocess.Popen | None') -> None: ...
    def _set_op(self, key: str, text: str) -> None: ...
    def _clear_op(self, key: str) -> None: ...

    def _db_record_zip(self, zip_path: pathlib.Path, channel_count: int) -> None:
        """Record a finished ZIP in the encoding DB."""
        db = getattr(self, '_encoding_db', None)
        if db is None:
            return
        from nofun.encoding_db import _now_iso
        date_str, band = extract_date_band(zip_path.stem)
        if date_str == 'TBD':
            return
        try:
            db.upsert(date_str, band, 'zipped_audio', {
                'path':          str(zip_path),
                'size':          zip_path.stat().st_size,
                'mtime':         zip_path.stat().st_mtime,
                'channel_count': channel_count,
                'scanned':       _now_iso(),
            })
            db.save()
        except Exception:
            pass

    def _db_mark_audio_all_silent(self, group_key: str, files: list[pathlib.Path]) -> None:
        """Record in the encoding DB that every channel for this group was silent.

        Prevents the AUDIO job from being re-enqueued if an external process
        restores the source files to VenueLighting/ after we archived them.
        """
        db = getattr(self, '_encoding_db', None)
        if db is None:
            return
        from nofun.encoding_db import _now_iso
        if files:
            date_str, band = extract_date_band(files[0].name)
            if date_str == 'TBD':
                return
        else:
            parts = group_key.split('_', 1)
            if len(parts) < 2:
                return
            date_str = parts[0]
            band = parts[1]
        try:
            db.upsert(date_str, band, 'audio_all_silent', {
                'path': group_key, 'updated': _now_iso(), 'n_channels': len(files),
            })
            db.save()
        except Exception:
            pass

    # Regex matching channel-split WAV filenames (e.g. 26-3-8_NoFun.29_ch01.wav)
    _CH_WAV = re.compile(r'_ch\d+\.wav$', re.IGNORECASE)

    # -----------------------------------------------------------------------
    # Audio — channel splitting
    # -----------------------------------------------------------------------

    def _active_seconds(self, wav: pathlib.Path) -> float | None:
        """Return seconds of audio above -50 dB (in 5 s+ runs).

        silencedetect answers the question we actually want: did this channel
        carry sustained signal anywhere in the file? Mean volume gets dragged
        up by faint EMI (cable-pickup channels read ~-68 dB over a full hour)
        and gets dragged down by long quiet stretches; max volume gets fooled
        by a single recorder-arm click. silencedetect ignores both.
        """
        result = subprocess.run(
            ['ffmpeg', '-i', str(wav), '-af',
             'silencedetect=noise=-50dB:d=5', '-f', 'null', NULL_DEV],
            capture_output=True, text=True,
        )
        dur_m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', result.stderr)
        if not dur_m:
            return None
        h, m, s = dur_m.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
        silence_total = sum(
            float(d) for d in re.findall(r'silence_duration:\s*([\d.]+)', result.stderr)
        )
        return max(0.0, duration - silence_total)

    def _detect_silence_batch(self, wav_files: list[pathlib.Path]) -> dict[str, float | None]:
        """Batch silence detection via scripts/detect_silence.py.

        Returns {filepath_str: active_seconds_or_none} for all files.
        Falls back to per-file _active_seconds() if ScriptRunner is unavailable.
        """
        runner = getattr(self, '_script_runner', None)
        if runner is None:
            return {str(f): self._active_seconds(f) for f in wav_files}

        # Write file list to a temp file for the script
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False,
        ) as tmp:
            for f in wav_files:
                tmp.write(f'{f}\n')
            file_list_path = tmp.name

        try:
            job = ScriptJob(
                script='detect_silence',
                args={'file_list': file_list_path},
                label=f'silence detection ({len(wav_files)} files)',
            )
            result = runner.run(job)
            if result.ok and 'results' in result.stdout_json:
                active: dict[str, float | None] = {}
                for entry in result.stdout_json['results']:
                    active[entry['file']] = entry.get('active_seconds')
                return active
        finally:
            pathlib.Path(file_list_path).unlink(missing_ok=True)

        # Fallback on script failure
        self.logger.warning('ScriptRunner: detect_silence failed, falling back to inline')
        return {str(f): self._active_seconds(f) for f in wav_files}

    def _archive_empty_wavs(self, wavs: list[pathlib.Path]) -> list[pathlib.Path]:
        """Move 0-byte WAVs to audio_archive; return non-empty files."""
        non_empty: list[pathlib.Path] = []
        for f in wavs:
            try:
                size = f.stat().st_size
            except OSError:
                non_empty.append(f)
                continue
            if size > 0:
                non_empty.append(f)
                continue
            self.logger.info(f"Audio: {f.name} is empty (0 bytes) — archiving")
            if not self.trial_run and self.mount_d != pathlib.Path('.'):
                self._archive_or_dedup(f, self.audio_archive)
            else:
                self.delete_queue.add(f, "empty WAV (0 bytes)", self.logger, tui=False)
        return non_empty

    def _split_multichannel_wavs(self, wav_files: list[pathlib.Path]) -> None:
        for f in wav_files:
            num_ch_str = probe_stream(f, 'channels', stream='a:0')
            if not num_ch_str:
                continue
            num_ch = int(num_ch_str)
            if num_ch <= 1:
                continue

            base = f.stem
            if (f.parent / chan_wav_name(base, 1)).exists() and not self.force:
                # Split already done; ensure original is gone from search_dir.
                if not self.trial_run and self.mount_d != pathlib.Path('.'):
                    self._archive_or_dedup(f, self.audio_archive)
                self.logger.debug(f"SKIP    {base} audio split (ch01 exists)")
                continue

            self.logger.info(
                f"SPLITTING {base}  ({num_ch} ch → mono)",
                extra={
                    'src':      str(f),
                    'channels': num_ch,
                    'codec':    probe_stream(f, 'codec_name', stream='a:0') or 'unknown',
                },
            )

            runner = self._script_runner
            job = ScriptJob(
                script='split_audio',
                args={
                    'source':   str(f),
                    'dest_dir': str(f.parent),
                    'base':     base,
                    'trial':    self.trial_run,
                },
                label=f'{base} → channels',
            )
            result = runner.run(
                job,
                proc_cb=lambda p: self._set_ffmpeg_proc('audio', p),
            )
            rc = result.exit_code

            self._set_ffmpeg_proc('audio', None)
            if rc == 0:
                if not self.trial_run and self.mount_d != pathlib.Path('.'):
                    self._archive_or_dedup(f, self.audio_archive)
                else:
                    self.delete_queue.add(f, "original multi-ch WAV (trial)", self.logger, tui=False)

                # ---- Batch silence detection ----
                ch_files = [
                    f.parent / chan_wav_name(base, ch + 1)
                    for ch in range(num_ch)
                ]
                ch_files = [cf for cf in ch_files if cf.exists()]
                self.logger.info(f"Audio: cleaning up silent channels from {base}…")

                # Use batch detection (ScriptRunner) when available
                active_map = self._detect_silence_batch(ch_files)

                silent_secs: list[float] = []
                active_secs: list[float] = []
                kept = dropped = 0
                for ch_file in ch_files:
                    active = active_map.get(str(ch_file))
                    if active is None:
                        active = self._active_seconds(ch_file)
                    if active is None:
                        self.logger.warning(f"Audio: silence probe failed for {ch_file.name} — skipping")
                        continue
                    if active < MIN_ACTIVE_SECONDS:
                        self.logger.debug(f"SILENT  {ch_file.name} ({active:.1f}s active) — archiving")
                        self._archive_or_dedup(ch_file, self.audio_archive)
                        silent_secs.append(active)
                        dropped += 1
                    else:
                        try:
                            sz = ch_file.stat().st_size
                        except OSError:
                            sz = 0
                        self.logger.debug(
                            f"CREATE  {ch_file.name}  (active {active:.0f}s)",
                            extra={'path': str(ch_file), 'size': fmt_size(sz)},
                        )
                        active_secs.append(active)
                        kept += 1
                if dropped:
                    rng = f" ({min(silent_secs):.1f}–{max(silent_secs):.1f}s active)"
                    self.logger.info(f"Audio: {base} — dropped {dropped} silent channel{'s' if dropped != 1 else ''}{rng}")
                if kept:
                    rng = f" ({min(active_secs):.0f}–{max(active_secs):.0f}s active)" if active_secs else ''
                    self.logger.info(f"Audio: {base} — kept {kept} with signal{rng}")
            else:
                self.logger.error(f"Audio split failed for {base}")
                partial = [
                    f.parent / chan_wav_name(base, ch + 1)
                    for ch in range(num_ch)
                ]
                partial = [p for p in partial if p.exists()]
                if self._pause_state == PauseState.HARD_PENDING and partial:
                    self._move_to_hard_paused(partial)
                else:
                    for ch_file in partial:
                        ch_file.unlink(missing_ok=True)

    # -----------------------------------------------------------------------
    # Audio — ZIP archiving
    # -----------------------------------------------------------------------

    def _group_wav_files(self, wav_files: list[pathlib.Path]) -> dict[str, list[pathlib.Path]]:
        """Group WAV files by (date, band) using filename-based date extraction.

        Falls back to mtime with 4-hour midnight rollback when the filename
        doesn't contain a recognisable date (e.g. recorder files).
        """
        groups: dict[str, list[pathlib.Path]] = defaultdict(list)
        for f in wav_files:
            date, band = extract_date_band(f.name)

            if date == 'TBD' or band == 'TBD':
                # Fallback: derive date from mtime with 4-hour rollback
                mtime = f.stat().st_mtime - 4 * 3600
                date  = datetime.datetime.fromtimestamp(mtime).strftime('%y-%m-%d')
                base  = f.stem
                m = re.match(r'^\d{2}-\d{1,2}-\d{1,2}_(.+)$', base)
                band  = m.group(1) if m else base
            # Strip per-channel and numbered suffixes that inventory_generator may leave
            band = re.sub(r'_chan[\d.]*$', '', band)
            band = re.sub(r'_ch\d+$', '', band)
            band = re.sub(r'\.[0-9]+$', '', band)

            groups[perf_key(date, band)].append(f)
        return groups

    @staticmethod
    def _perf_key(path: pathlib.Path) -> str:
        """Return a sortable 'YY-MM-DD_Band' key for any media file."""
        date, band = extract_date_band(path.name)
        if date == 'TBD' or band == 'TBD':
            try:
                mtime = path.stat().st_mtime - 4 * 3600
            except OSError:
                mtime = 0.0
            date = datetime.datetime.fromtimestamp(mtime).strftime('%y-%m-%d')
            band = re.sub(r'\.(wav|mp4|mov)$', '', path.name, flags=re.IGNORECASE)
        band = re.sub(r'_chan[\d.]*$', '', band)
        band = re.sub(r'_ch\d+$', '', band)
        band = re.sub(r'\.[0-9]+$', '', band)
        band = re.sub(r'_(CAM[1-4])$', '', band, flags=re.IGNORECASE)
        return perf_key(date, band)

    def _create_and_verify_zip(self, zip_path: pathlib.Path,
                                source_files: list[pathlib.Path],
                                progress_cb=None,
                                ) -> tuple[bool, list[pathlib.Path]]:
        """Encode source_files to FLAC and bundle them into zip_path.

        Each channel WAV is FLAC-encoded in a worker thread (ffmpeg runs as a
        subprocess, releasing the GIL, so threads encode truly in parallel).
        The already-compressed FLAC bytes are then written into the zip serially
        with ZIP_STORED — no second deflate pass.

        Returns (success, dropped). dropped is the subset of source_files that
        vanished mid-ZIP (FileNotFoundError on read_bytes); they're skipped
        rather than failing the whole job. success is True iff the resulting
        ZIP exists and contains every surviving file's arcname.
        """
        # Parallel FLAC encoders. Each worker is one ffmpeg `-compression_level 8`
        # subprocess (≈1 core), so CPU load scales ~linearly with this count: 4
        # workers saturated the box (~92%). Capped to 2 by default (~50% CPU) to
        # leave headroom on show night; raise FLAC_ZIP_WORKERS to trade CPU for
        # faster zips. Takes effect on the next engine restart.
        try:
            n_workers = max(1, int(os.environ.get('FLAC_ZIP_WORKERS') or 2))
        except ValueError:
            n_workers = 2
        _preexisted = zip_path.exists()
        dropped: list[pathlib.Path] = []

        try:
            # --- Step 1: parallel compression ---
            completed = 0
            ordered: dict[pathlib.Path, tuple[str, bytes, int, int]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                future_map = {pool.submit(_encode_one_flac, f): f
                              for f in source_files}
                for fut in concurrent.futures.as_completed(future_map):
                    if self._pause_state == PauseState.HARD_PENDING:
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    result = fut.result()
                    orig_f = future_map[fut]
                    if result is _COMPRESS_MISSING:
                        dropped.append(orig_f)
                        self.logger.warning(
                            f"ZIP     {zip_path.stem}  {orig_f.name} vanished — dropping"
                        )
                    else:
                        ordered[orig_f] = result
                    completed += 1
                    if progress_cb:
                        progress_cb(completed, len(source_files))

            if self._pause_state == PauseState.HARD_PENDING:
                zip_path.unlink(missing_ok=True)
                return False, dropped

            survivors = [f for f in source_files if f in ordered]
            if not survivors:
                self.logger.error(
                    f"ZIP     {zip_path.stem}  every channel vanished — nothing to zip"
                )
                return False, dropped

            # --- Step 2: write zip, injecting pre-encoded FLAC bytes ---
            # The channels are already FLAC-compressed, so we STORE them (no
            # second deflate pass) by writing the local file header + raw bytes
            # directly.  Uses semi-internal ZipFile.fp / filelist / NameToInfo —
            # stable across CPython 3.8+.
            with _zipfile.ZipFile(zip_path, 'w', allowZip64=True) as zf:
                for f in survivors:
                    name, compressed, orig_size, crc = ordered[f]
                    zinfo = _zipfile.ZipInfo(filename=name)
                    zinfo.compress_type  = _zipfile.ZIP_STORED
                    zinfo.file_size      = orig_size
                    zinfo.compress_size  = len(compressed)
                    zinfo.CRC            = crc
                    zinfo.flag_bits      = 0  # sizes known upfront; no data descriptor
                    zip64 = orig_size > 0xFFFFFFFF or len(compressed) > 0xFFFFFFFF
                    with zf._lock:  # type: ignore[attr-defined]
                        zinfo.header_offset = zf.fp.tell()  # type: ignore[union-attr]
                        zf._didModify = True                # type: ignore[attr-defined]
                        zf.fp.write(zinfo.FileHeader(zip64=zip64))  # type: ignore[union-attr]
                        zf.fp.write(compressed)             # type: ignore[union-attr]
                        if zf._seekable:                    # type: ignore[attr-defined]
                            zf.start_dir = zf.fp.tell()     # type: ignore[union-attr]
                        zf.filelist.append(zinfo)
                        zf.NameToInfo[zinfo.filename] = zinfo

            # --- Step 3: verify ---
            # Entries are written under their FLAC arcname (ordered[f][0]), not
            # the source WAV name, so compare against those.
            with _zipfile.ZipFile(zip_path, 'r') as zf:
                names_in_zip = {e.filename for e in zf.infolist()}
            ok = {ordered[f][0] for f in survivors}.issubset(names_in_zip)
            return ok, dropped

        except Exception as e:
            self.logger.error(f"ZIP error: {e}")
            if not _preexisted:
                zip_path.unlink(missing_ok=True)
            return False, dropped

    def _zip_wav_group(
        self,
        group_key: str,
        files: list[pathlib.Path],
        zip_dest: pathlib.Path,
        trim_dir: pathlib.Path,
        *,
        on_success_real_drive: str,
    ) -> None:
        """Zip one group of WAV files, handling trial mode, logging, and cleanup.

        Parameters
        ----------
        group_key
            Date-band key used for the zip filename and log messages.
        files
            Source WAV files to include in the zip.
        zip_dest
            Directory where the .zip file should be written.
        trim_dir
            Directory for temporary trial-trim copies.
        on_success_real_drive
            Disposition of source files after a successful zip when a real
            drive is mounted: 'delete' queues them; 'archive' moves them to
            audio_archive.
        """
        zip_path = zip_dest / perf_output_name(group_key, 'multitrack')
        if zip_path.exists() and not self.force:
            self.logger.debug(f"SKIP    {zip_path.name} (exists)")
            for f in files:
                self.delete_queue.add(f, "channel WAV already zipped", self.logger, tui=False)
            return

        if self.trial_run:
            source_files: list[pathlib.Path] = []
            for f in files:
                tmp = trim_dir / f'temp_trial_{f.name}'
                subprocess.run(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                     '-t', str(self.trial_run), '-i', str(f),
                     '-c:a', 'copy', str(tmp), '-y'],
                    check=True,
                )
                source_files.append(tmp)
        else:
            source_files = files

        self.logger.info(
            f"ZIPPING   {group_key}  ({len(files)} ch"
            + ("  trial" if self.trial_run else "") + ")"
        )
        zip_start = time.monotonic()
        total_files = len(source_files)

        def _progress(done: int, total: int) -> None:
            elapsed = time.monotonic() - zip_start
            # ETA needs ≥ 2 datapoints — first per-file callback is too early
            # to extrapolate; with 4 parallel workers the second arrives
            # within ~1 s of the first under normal load.
            eta = ''
            if done >= 2 and elapsed > 0:
                eta = format_eta((elapsed / done) * (total - done))
            if self._app:
                self._app.update_audio_progress(
                    'multitrack', group_key, done, total, elapsed, eta,
                )

        success, dropped = self._create_and_verify_zip(
            zip_path, source_files,
            progress_cb=_progress if total_files > 1 else None,
        )

        if not success:
            self.logger.error(f"Zip verification failed for {zip_path.name}")
            if self._app:
                self._app.clear_row('audio_progress')
            return

        try:
            sz = zip_path.stat().st_size
        except OSError:
            sz = 0
        self.logger.info(
            f"CREATE  {zip_path.name}",
            extra={'path': str(zip_path), 'size': fmt_size(sz)},
        )
        self._db_record_zip(zip_path, len(source_files) - len(dropped))

        dropped_set = set(dropped)
        if self.trial_run:
            for f in source_files:
                self.delete_queue.add(f, "trial temp after zip", self.logger, tui=False)
        elif on_success_real_drive == 'archive' and self.mount_d != pathlib.Path('.'):
            for f in files:
                if f not in dropped_set:
                    self._archive_or_dedup(f, self.audio_archive)
        else:
            for f in files:
                if f not in dropped_set:
                    self.delete_queue.add(f, "source WAV zipped", self.logger, tui=False)

        if self._app:
            self._app.clear_row('audio_progress')

    def _collect_chan_candidates(
        self, scan_dir: pathlib.Path, *, exclude_split: bool = False
    ) -> dict[str, list[pathlib.Path]]:
        """Collect and group stable single-channel WAVs from scan_dir.

        Archives 0-byte files, probes channel counts, and returns a dict
        mapping performance keys → file lists, ready for _process_audio_group().

        Parameters
        ----------
        exclude_split
            When True, exclude _ch??.wav files (used when scan_dir is
            search_dir — those are handled separately by the split pipeline).
        """
        if not scan_dir.is_dir():
            return {}

        queued = {item[0] for item in self.delete_queue.items}
        wavs = [
            f for f in sorted(scan_dir.glob('*.wav'))
            if self._is_file_stable(f)
            and not (exclude_split and self._CH_WAV.search(f.name))
            and f not in queued
        ]
        if not wavs:
            return {}

        wavs = self._archive_empty_wavs(wavs)
        if not wavs:
            return {}

        one_ch = [f for f in wavs if probe_stream(f, 'channels', stream='a:0') == '1']
        if not one_ch:
            return {}

        return self._group_wav_files(one_ch)

    def _process_audio_group(
        self, group_key: str, files: list[pathlib.Path], scan_dir: pathlib.Path
    ) -> None:
        """Silence-check one performance group, queue silent files, and ZIP active ones."""
        self.logger.info(f"Audio: cleaning up silent channels from {group_key}…")

        silent_secs: list[float] = []
        active: list[pathlib.Path] = []
        active_secs: list[float] = []

        for f in files:
            secs = self._active_seconds(f)
            if secs is None:
                self.logger.warning(f"Audio: silence probe failed for {f.name} — skipping")
                continue
            if secs < MIN_ACTIVE_SECONDS:
                self.logger.debug(f"SILENT  {f.name} ({secs:.1f}s active) — archiving")
                self._archive_or_dedup(f, self.audio_archive)
                silent_secs.append(secs)
            else:
                active.append(f)
                active_secs.append(secs)
                self.logger.debug(f"CREATE  {f.name}  (active {secs:.0f}s)")

        if silent_secs:
            rng = f" ({min(silent_secs):.1f}–{max(silent_secs):.1f}s active)"
            self.logger.info(f"Audio: {group_key} — dropped {len(silent_secs)} silent channel{'s' if len(silent_secs) != 1 else ''}{rng}")
        if not active:
            self._db_mark_audio_all_silent(group_key, files)
            return
        rng = f" ({min(active_secs):.0f}–{max(active_secs):.0f}s active)" if active_secs else ''
        self.logger.info(f"Audio: {group_key} — kept {len(active)} with signal{rng}")
        self._zip_wav_group(
            group_key, active,
            zip_dest=self.audio_dest,
            trim_dir=scan_dir,
            on_success_real_drive='archive',
        )

    def _export_audio_zips(self) -> None:
        """Zip _ch??.wav channel files from search_dir."""
        wav_files = sorted(
            f for f in self.search_dir.glob('*.wav')
            if self._CH_WAV.search(f.name)
        )
        if not wav_files:
            return
        self.logger.info(f"Audio: archiving {len(wav_files)} channel WAV files")
        for group_key, files in self._group_wav_files(wav_files).items():
            self._zip_wav_group(
                group_key, files,
                zip_dest=self.audio_dest,
                trim_dir=self.search_dir,
                on_success_real_drive='delete',
            )

    def _sweep_leftover_wavs(self) -> None:
        leftovers = sorted(self.search_dir.glob('*.wav'))
        if not leftovers:
            return
        self.logger.info(f"Audio: cleaning up {len(leftovers)} leftover WAV(s)")
        for f in leftovers:
            if self._CH_WAV.search(f.name):
                # Channel files are never archived — always deleted after zipping
                self.delete_queue.add(f, "leftover channel WAV", self.logger, tui=False)
            elif not self.trial_run and self.mount_d != pathlib.Path('.'):
                # Original multi-channel WAVs go to audio_archive
                self._archive_or_dedup(f, self.audio_archive, self.audio_dest)
            else:
                self.delete_queue.add(f, "leftover WAV", self.logger, tui=False)

    def _process_audio_dir_wavs(self, scan_dir: pathlib.Path) -> None:
        """Process single-channel WAVs from scan_dir (convenience wrapper).

        Used by tests and backward-compatible callers. The main pipeline calls
        _collect_chan_candidates and _process_audio_group directly to enable
        per-performance ordering interleaved with video.
        """
        exclude_split = (scan_dir == self.search_dir)
        groups = self._collect_chan_candidates(scan_dir, exclude_split=exclude_split)
        for group_key, files in sorted(groups.items()):
            self._process_audio_group(group_key, files, scan_dir)
