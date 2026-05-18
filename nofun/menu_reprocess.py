"""TUI command routing for the REPROCESS menu.

This is a Pipeline mixin — it has no __init__ and assumes all instance state
is initialised on Pipeline.

Methods are extracted verbatim from media_engine.py — no behaviour changes.
"""

from __future__ import annotations

import pathlib
import shutil

from nofun.job_queue import JobCategory
from nofun.state import MenuMode


class ReprocessMenuMixin:

    def _cmd_reprocess(self) -> None:
        """REPROCESS — list archived performances and let the user pick one to re-run."""
        from nofun.tui import MenuRow

        archived: dict[str, dict] = {}
        if self.video_archive.is_dir():
            for mov in sorted(self.video_archive.rglob('*.mov')):
                key = self._perf_key(mov)
                archived.setdefault(key, {'movs': [], 'wavs': []})['movs'].append(mov)
        if self.audio_archive.is_dir():
            for wav in sorted(self.audio_archive.rglob('*.wav')):
                key = self._perf_key(wav)
                archived.setdefault(key, {'movs': [], 'wavs': []})['wavs'].append(wav)

        if not archived:
            self.logger.info("REPROCESS: no archived performances found")
            return

        keys = sorted(archived.keys(), reverse=True)
        rows: list[MenuRow] = []
        for i, key in enumerate(keys, 1):
            a = archived[key]
            rows.append(MenuRow(
                index=i,
                text=f"  {key}  ·  {len(a['movs'])} .mov  {len(a['wavs'])} .wav",
            ))
        rows.append(MenuRow(index=None, text='', dim=True))
        rows.append(MenuRow(
            index=None,
            text='  Type a number to reprocess, or HOME to cancel',
            dim=True,
        ))

        self._reprocess_candidates = keys
        self._reprocess_archived   = archived
        self._active_menu = MenuMode.REPROCESS
        if self._app:
            self._app.show_menu(
                'REPROCESS', f'{len(keys)} archived performance(s)',
                rows, footer='select a number',
            )
            self._app.update_command_bar(
                "Type a number to reprocess  /  [yellow]HOME[/yellow] to cancel"
            )

    def _safe_link(self, src: pathlib.Path, dst: pathlib.Path) -> None:
        """Create dst as a symlink to src; fall back to copy on Windows without symlink privilege."""
        dst.unlink(missing_ok=True)
        try:
            dst.symlink_to(src)
        except OSError:
            self.logger.warning(
                f'REPROCESS  symlink unavailable for {src.name}, copying '
                '(enable Developer Mode for faster staging)'
            )
            shutil.copy2(src, dst)

    def _handle_reprocess_command(self, cmd: str) -> None:
        """Handle input while the REPROCESS menu is active."""
        if cmd.strip().upper() in ('HOME', 'Q', 'QUIT', 'EXIT', 'BACK'):
            self._active_menu = MenuMode.NONE
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)
            return

        try:
            sel = int(cmd.strip())
        except ValueError:
            return

        keys = self._reprocess_candidates
        if sel < 1 or sel > len(keys):
            if self._app:
                self._app.update_status(f'REPROCESS  ·  No entry #{sel}')
            return

        key  = keys[sel - 1]
        data = self._reprocess_archived[key]

        # Stage: create a temp directory with symlinks to archived files.
        staging = pathlib.Path(__file__).parent.parent / '_reprocess_staging' / key
        staging.mkdir(parents=True, exist_ok=True)
        for mov in data['movs']:
            link = staging / mov.name
            if not link.exists():
                self._safe_link(mov, link)
        for wav in data['wavs']:
            link = staging / wav.name
            if not link.exists():
                self._safe_link(wav, link)

        self.logger.info(f"REPROCESS: staged {key} → _reprocess_staging/{key}")

        # Build manifest and enqueue.
        mov_list = sorted(staging.glob('*.mov'))
        from collections import defaultdict as _defaultdict
        perf_ch: dict[str, list] = _defaultdict(list)
        for f in sorted(staging.glob('*.wav')):
            if self._CH_WAV.search(f.name):
                perf_ch[self._perf_key(f)].append(f)


        manifest, cat_map = self._build_full_manifest(
            key, mov_list,
            perf_ch.get(key, []), [], [],
        )
        if manifest.jobs:
            self._job_queue.enqueue(manifest, JobCategory.MANUAL, category_map=cat_map)
            with self._enqueued_keys_lock:
                self._enqueued_keys.add(key)
            self.logger.info(f"REPROCESS: enqueued {len(manifest.jobs)} jobs for {key}")
        else:
            self.logger.info(f"REPROCESS: nothing to do for {key} (already processed)")

        self._active_menu = MenuMode.NONE
        if self._app:
            self._app.hide_menu()
            self._app.update_command_bar(self._HOME_COMMANDS)
