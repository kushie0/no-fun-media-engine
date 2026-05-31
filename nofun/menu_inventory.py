"""TUI command routing for the INVENTORY/STATUS menu.

This is a Pipeline mixin — it has no __init__ and assumes all instance state
is initialised on Pipeline.

Methods are extracted verbatim from media_engine.py — no behaviour changes.
"""

from __future__ import annotations

import datetime
import pathlib

from nofun.cleanup import canonical_sharepoint_name
from nofun.inventory import EXPIRE_AGE, RAW_EXPIRE_AGE, perf_key, short_date, _STATUS_ICON, _status_label
from nofun.job_manifest import JobManifest
from nofun.job_queue import JobCategory
from nofun.media_io import fmt_size
from nofun.state import MenuMode


_SHORT_LABEL: dict[str, str] = {
    'NEW':                      'NEW',
    'INCOMPLETE VIDEO + AUDIO': 'V+A',
    'INCOMPLETE VIDEO':         'V',
    'INCOMPLETE AUDIO':         'A',
    'UNSHARED':                 'OK',
    'SHARED':                   '↑',
}


class InventoryMenuMixin:

    _INVENTORY_HELP: list[tuple[str, str, list[str]]] = [
        ('1–N',      'Expand/collapse per-file encoding detail for that performance', [
            'Type a number to expand that row. The overlay shows per-file records',
            'from encoding_db.json: codec, resolution, fps, bitrate, duration,',
            'problematic flag (set when Main10 / 10-bit profile detected).',
            'Same number again collapses. A different number switches expand target.',
        ]),
        ('REUPLOAD', 'Re-copy selected performance to SharePoint; resets 40-day clock', [
            'Copies all _UL/_UR/_LL/_LR .mp4 quads and the matching .zip from',
            'vids_dest / audio_dest to the sharepoint_dest folder (OneDrive path).',
            'Uses shutil.copy (not copy2) so destination mtime = now, resetting the',
            'expiry clock. If the folder was moved to archived/ it is moved back first.',
            'Updates _nofun_info.txt with re-uploaded timestamps.',
        ]),
        ('REMASTER', 'Rebuild AUDIO + Instagram reel and replace both in SharePoint', [
            'Re-runs mastering (room denoise + per-channel OTT + DyERS resonance',
            'suppression) on the multitrack ZIP, then regenerates the Instagram reel',
            'from the new AUDIO. Overwrites the AUDIO mp3 and the reel already in the',
            'SharePoint folder so bands get the improved master. Press REMASTER again',
            'while it runs to cancel and restart from scratch (force).',
        ]),
        ('SCAN',     'Probe new + stale files and write to encoding_db.json', [
            'Collects .mp4 quads, .mov raws, .zip audio, clips, and SharePoint copies.',
            'Probes files not yet in the DB or whose mtime has changed (stale).',
            'Runs probe_file() (ffprobe) per file; stores codec, resolution, bitrate,',
            'fps, duration, and problematic flag. No time gate.',
        ]),
        ('BIGSCAN',  'Re-probe every file + full filesystem re-index (replaces REBUILD)', [
            'Probes all files unconditionally (8 parallel threads), then runs',
            '_run_inventory() to re-scan all source paths, update location/type',
            'classification, and rewrite file_summary.txt.',
            'Time-gated to before 4pm; type NOPROBLEM first to override.',
        ]),
        ('HOME',     'Exit the inventory menu', [
            'Sets _active_menu = MenuMode.NONE, hides the overlay, restores',
            'the home command bar. If HELP overlay is showing, HOME dismisses',
            'the help first and returns to the inventory list.',
        ]),
    ]

    def _enter_status_menu(self) -> None:
        """Open the interactive INVENTORY menu (TUI mode)."""
        if self._app:
            self._app.update_status('INVENTORY  ·  loading…')
        if not self._rebuild_status_entries():
            if self._app:
                self._app.update_status('')
            self.logger.info("INVENTORY  No data found — type BIGSCAN to index files")
            return
        self._collect_header_stats()   # refresh disk strings before opening the menu
        self._status_expanded_key = None
        self._remaster_state      = None
        self._show_status_list(_open=True)
        self._active_menu = MenuMode.STATUS

    def _show_status_list(self, _open: bool = False) -> None:
        """Build a MenuRow list and push it to the menu overlay.

        Pass _open=True only from _enter_status_menu() to open a fresh overlay.
        All other callers use the default (False) which only updates an already-open
        menu — this prevents worker threads from reopening it after HOME closes it.
        """
        from nofun.tui import MenuRow

        def _worst_state(perf_list: list) -> tuple[str, str]:
            priority = [
                'NEW', 'INCOMPLETE VIDEO + AUDIO', 'INCOMPLETE VIDEO',
                'INCOMPLETE AUDIO', 'UNSHARED', 'SHARED',
            ]
            best: tuple[str, str] | None = None
            for ps in perf_list:
                lbl, col = _status_label(ps)
                if best is None:
                    best = (lbl, col)
                elif lbl in priority and (
                    best[0] not in priority
                    or priority.index(lbl) < priority.index(best[0])
                ):
                    best = (lbl, col)
            return best or ('UNSHARED', 'cyan')

        rows: list[MenuRow] = []

        # Recording alert at the top if active
        recording = self._get_recording_files()
        if recording:
            stems = list(dict.fromkeys(f.stem for f in recording))
            rows.append(MenuRow(
                index=None,
                text=f"[bold red]● RECORDING[/bold red]  {'  '.join(stems)}",
            ))
            rows.append(MenuRow(index=None, text='', dim=True))

        # Column header
        rows.append(MenuRow(
            index=None,
            text=f"{'Show':<56}  St    Age",
            dim=True,
        ))

        n_shows = len(self._show_groups)
        scroll_to_row: int | None = None
        for i, (date, show_name, perf_list) in enumerate(self._show_groups, start=1):
            label, col = _worst_state(perf_list)
            icon       = _STATUS_ICON.get(label, '?')
            age        = perf_list[0].age_days if perf_list else None

            all_overdue: list[str] = []
            for ps in perf_list:
                all_overdue.extend(ps.lifecycle_overdue)
            overdue_badge = '  [yellow]⏰[/yellow]' if all_overdue else ''

            jstatus    = self._job_queue.manifest_status_by_date(short_date(date))
            job_badge  = f'  [cyan]⚙ {jstatus}[/cyan]' if jstatus else ''

            sn      = (show_name[:54] + '..') if len(show_name) > 56 else show_name
            short   = _SHORT_LABEL.get(label, label[:4])
            age_str = f"{age}d" if age is not None else '?d'

            if date == self._status_expanded_key:
                scroll_to_row = len(rows)
            rows.append(MenuRow(
                index=i,
                text=(
                    f"{sn:<56}  [{col}]{icon} {short}[/{col}]"
                    f"  [dim]{age_str}[/dim]{overdue_badge}{job_badge}"
                ),
            ))

            if date == self._status_expanded_key:
                rows.append(MenuRow(index=None, text='', dim=True))

                for ps in perf_list:
                    b_lbl, b_col = _status_label(ps)
                    b_icon = _STATUS_ICON.get(b_lbl, '?')
                    b = (ps.band[:38] + '..') if len(ps.band) > 40 else ps.band

                    ps_age = ps.age_days or 0
                    expected_missing = [
                        m for m in ps.missing_components
                        if not (m in ('cloud quadrants', 'cloud zip') and ps_age > EXPIRE_AGE)
                        and not (m in ('video raw', 'audio raw') and ps_age > RAW_EXPIRE_AGE)
                    ]
                    miss_text = (
                        f"  [dim]missing: {', '.join(expected_missing)}[/dim]"
                        if expected_missing else ''
                    )
                    rows.append(MenuRow(index=None,
                        text=f"    [{b_col}]{b_icon} {b}[/{b_col}]{miss_text}"))

                    # Quadrants — one summary line
                    if ps.quad_files:
                        total_size = sum(f.stat().st_size for f in ps.quad_files if f.exists())
                        prob_any = False
                        for f in ps.quad_files:
                            rec = self._encoding_db.lookup(f)
                            if rec and rec.get('problematic'):
                                prob_any = True
                                break
                        if prob_any:
                            qual = '  [bold red]PROBLEMATIC[/bold red]'
                        else:
                            first_rec = self._encoding_db.lookup(ps.quad_files[0])
                            qual_parts: list[str] = []
                            if first_rec and not self._encoding_db.is_stale(first_rec, ps.quad_files[0]):
                                if first_rec.get('resolution'):
                                    qual_parts.append(first_rec['resolution'])
                                dur = first_rec.get('duration')
                                if dur:
                                    mins, secs = divmod(int(dur), 60)
                                    qual_parts.append(f"{mins}:{secs:02d}")
                            qual = f"  [dim]{' · '.join(qual_parts)}[/dim]" if qual_parts else ''
                        rows.append(MenuRow(index=None,
                            text=f"      [dim]Quads  [/dim] {len(ps.quad_files)}× .mp4  {fmt_size(total_size)}{qual}"))

                    # Clips — one summary line
                    if ps.clip_files:
                        cs = self._encoding_db.get_clips_summary(date, ps.band)
                        if cs:
                            count = cs.get('count', len(ps.clip_files))
                            size  = fmt_size(cs.get('total_size', 0))
                            codec_hint = f"  [dim]{cs['codec']}[/dim]" if cs.get('codec') else ''
                            rows.append(MenuRow(index=None,
                                text=f"      [dim]Clips  [/dim] {count} clips  {size}{codec_hint}"))
                        else:
                            total_size = sum(f.stat().st_size for f in ps.clip_files if f.exists())
                            rows.append(MenuRow(index=None,
                                text=f"      [dim]Clips  [/dim] {len(ps.clip_files)} clips  {fmt_size(total_size)}"))

                    # Audio zip — one summary line
                    if ps.zip_files:
                        total_size = sum(f.stat().st_size for f in ps.zip_files if f.exists())
                        first_rec  = self._encoding_db.lookup(ps.zip_files[0])
                        ch_hint    = (
                            f"  [dim]{first_rec['channel_count']} ch[/dim]"
                            if first_rec and first_rec.get('channel_count') else ''
                        )
                        n_z = len(ps.zip_files)
                        zip_label = f"{n_z}× .zip" if n_z > 1 else ps.zip_files[0].name[:40]
                        rows.append(MenuRow(index=None,
                            text=f"      [dim]Audio  [/dim] {zip_label}  {fmt_size(total_size)}{ch_hint}"))

                    # Raw WAV — one summary line
                    raw_audio = ps.raw_wavs + ps.wav_files
                    if raw_audio:
                        total_size = sum(f.stat().st_size for f in raw_audio if f.exists())
                        n = len(raw_audio)
                        rows.append(MenuRow(index=None,
                            text=f"      [dim]Raw WAV[/dim] {n} wav{'s' if n != 1 else ''}  {fmt_size(total_size)}"))

                    # Raw .mov — one summary line
                    raw_video = ps.raw_movs + ps.mov_files
                    if raw_video:
                        total_size = sum(f.stat().st_size for f in raw_video if f.exists())
                        n = len(raw_video)
                        rows.append(MenuRow(index=None,
                            text=f"      [dim]Raw .mov[/dim]{n}× .mov  {fmt_size(total_size)}"))

                    # Cloud — one summary line
                    if ps.cloud_files:
                        cloud_size = sum(f.stat().st_size for f in ps.cloud_files if f.exists())
                        rows.append(MenuRow(index=None,
                            text=f"      [dim]Cloud  [/dim] {len(ps.cloud_files)} files  {fmt_size(cloud_size)}"))

                    rows.append(MenuRow(index=None, text='', dim=True))

                rows.append(MenuRow(index=None, text='', dim=True))

        subtitle = f"{n_shows} show{'s' if n_shows != 1 else ''}"
        footer   = subtitle
        if self._status_expanded_key:
            bar = (
                f"Available commands:  1–{n_shows} / [cyan]REUPLOAD[/cyan]"
                f" / [cyan]RENAME[/cyan] / [cyan]REMASTER[/cyan] / SCAN / BIGSCAN"
                f" / [green]HELP[/green] / [yellow]HOME[/yellow]"
            )
        else:
            bar = (
                f"Available commands:  1–{n_shows} / SCAN / BIGSCAN"
                f" / [green]HELP[/green] / [yellow]HOME[/yellow]"
            )

        disk_parts = [s for s in (self._disk_c, self._disk_d, self._disk_sp) if s]
        disk_stats = '   '.join(disk_parts)

        if self._app:
            if _open:
                self._app.show_menu('INVENTORY', subtitle, rows, footer, stats=disk_stats)
            elif self._active_menu == MenuMode.STATUS:
                self._app.update_menu(
                    'INVENTORY', subtitle, rows, footer,
                    stats=disk_stats, scroll_to=scroll_to_row,
                )
            else:
                return  # menu was closed (e.g. by immediate_home) — don't reopen
            self._app.update_command_bar(bar)
            self._app.update_status(f"INVENTORY  ·  {subtitle}")

    # ------------------------------------------------------------------
    # RENAME sub-flow (inside the INVENTORY menu)
    # ------------------------------------------------------------------

    def _cancel_rename(self) -> None:
        self._rename_state    = None
        self._rename_date     = None
        self._rename_band     = None
        self._rename_new_name = None
        self._show_status_list()

    def _show_rename_select(self) -> None:
        """Step 1: list bands for the expanded show so the user can pick one."""
        from nofun.tui import MenuRow
        date = self._status_expanded_key
        if not date:
            self._cancel_rename()
            return
        bands = [
            ps.band for (d, _), ps in self._status_entries
            if d == date and ps.band not in ('NOFUN', 'TBD', '')
        ]
        rows: list[MenuRow] = [
            MenuRow(index=None, text=f"  Show {date}  ·  select the band to rename", dim=True),
            MenuRow(index=None, text='', dim=True),
        ]
        for j, band in enumerate(bands, start=1):
            rows.append(MenuRow(index=None, text=f"  b{j}  {band}"))
        rows.append(MenuRow(index=None, text='', dim=True))
        bn = len(bands)
        bar = f"Available commands:  b1–b{bn} / [yellow]HOME[/yellow] to cancel"
        if self._app:
            self._app.update_menu('INVENTORY — RENAME', 'Select a band', rows, '', stats='')
            self._app.update_command_bar(bar)

    def _show_rename_enter_name(self) -> None:
        """Step 2: show the files that will be affected and ask for the new name."""
        from nofun.tui import MenuRow
        date, old = self._rename_date, self._rename_band
        if not date or not old:
            self._cancel_rename()
            return
        ps = next(
            (v for (d, b), v in self._status_entries if d == date and b == old),
            None,
        )
        rows: list[MenuRow] = [
            MenuRow(index=None, text=f"  Renaming  [bold]{old}[/bold]  ({date})", dim=True),
            MenuRow(index=None, text='', dim=True),
            MenuRow(index=None, text='  Files that will be renamed:', dim=True),
        ]
        if ps:
            for f in sorted(ps.quad_files):
                rows.append(MenuRow(index=None, text=f"    {f.name}"))
            if ps.clip_files:
                clip_dir = ps.clip_files[0].parent
                rows.append(MenuRow(index=None, text=f"    {clip_dir.name}/  ({len(ps.clip_files)} clips)"))
            for f in sorted(ps.zip_files):
                rows.append(MenuRow(index=None, text=f"    {f.name}"))
            for f in sorted(ps.cloud_files):
                if f.suffix.lower() in ('.mp4', '.zip'):
                    rows.append(MenuRow(index=None, text=f"    {f.name}  [dim](cloud)[/dim]"))
        rows += [
            MenuRow(index=None, text='', dim=True),
            MenuRow(index=None, text='  Type the new band name and press Enter.', dim=True),
        ]
        bar = "Available commands:  [dim]<new name>[/dim] / [yellow]HOME[/yellow] to cancel"
        if self._app:
            self._app.update_menu('INVENTORY — RENAME', f"Renaming {old}", rows, '', stats='')
            self._app.update_command_bar(bar)

    def _show_rename_confirm(self) -> None:
        """Step 3: show a diff preview and wait for CONFIRM."""
        from nofun.tui import MenuRow
        date, old, new = self._rename_date, self._rename_band, self._rename_new_name
        if not date or not old or not new:
            self._cancel_rename()
            return
        ps = next(
            (v for (d, b), v in self._status_entries if d == date and b == old),
            None,
        )
        rows: list[MenuRow] = [
            MenuRow(index=None, text=f"  [bold]{old}[/bold]  →  [bold green]{new}[/bold green]", dim=False),
            MenuRow(index=None, text='', dim=True),
        ]
        if ps:
            for f in sorted(ps.quad_files):
                rows.append(MenuRow(index=None, text=f"  {f.name}  →  {f.name.replace(old, new)}"))
            if ps.clip_files:
                clip_dir = ps.clip_files[0].parent
                new_dir  = clip_dir.name.replace(old, new)
                rows.append(MenuRow(
                    index=None,
                    text=f"  {clip_dir.name}/  →  {new_dir}/  [dim]({len(ps.clip_files)} clips)[/dim]",
                ))
            for f in sorted(ps.zip_files):
                rows.append(MenuRow(index=None, text=f"  {f.name}  →  {f.name.replace(old, new)}"))
            cloud_mp4s = [f for f in ps.cloud_files if f.suffix.lower() in ('.mp4', '.zip')]
            if cloud_mp4s:
                rows.append(MenuRow(index=None, text='', dim=True))
                rows.append(MenuRow(index=None, text='  Cloud:', dim=True))
                for f in sorted(cloud_mp4s):
                    rows.append(MenuRow(
                        index=None,
                        text=f"  {f.name}  →  {f.name.replace(old, new)}  [dim](cloud)[/dim]",
                    ))
        rows.append(MenuRow(index=None, text='', dim=True))
        bar = "Available commands:  [cyan]CONFIRM[/cyan] to proceed / [yellow]HOME[/yellow] to cancel"
        if self._app:
            self._app.update_menu('INVENTORY — RENAME', 'Confirm rename', rows, '', stats='')
            self._app.update_command_bar(bar)

    def _do_rename_performance(self, date_str: str, old_band: str, new_band: str) -> None:
        """Rename all files for a band on disk, in SharePoint, and in the encoding DB."""

        def subst(name: str) -> str:
            return name.replace(old_band, new_band)

        ps = next(
            (v for (d, b), v in self._status_entries if d == date_str and b == old_band),
            None,
        )
        if not ps:
            self.logger.warning(f"RENAME  no data found for {old_band} on {date_str}")
            return

        errors = 0

        def _mv(src: pathlib.Path, dst: pathlib.Path) -> bool:
            nonlocal errors
            if src == dst:
                return True
            if not src.exists():
                self.logger.warning(f"RENAME  missing: {src.name}")
                return False
            try:
                src.rename(dst)
                return True
            except OSError as e:
                self.logger.warning(f"RENAME  could not rename {src.name}: {e}")
                errors += 1
                return False

        # Quadrant files
        if ps.quad_files:
            self.logger.info(f"RENAME  quadrants ({len(ps.quad_files)} files)…")
        for f in ps.quad_files:
            _mv(f, f.parent / subst(f.name))

        # Clips: rename files inside dir first, then rename the dir itself
        if ps.clip_files:
            clip_dir     = ps.clip_files[0].parent
            new_clip_dir = clip_dir.parent / subst(clip_dir.name)
            self.logger.info(f"RENAME  clips ({len(ps.clip_files)} files)…")
            for f in ps.clip_files:
                _mv(f, clip_dir / subst(f.name))
            _mv(clip_dir, new_clip_dir)

        # Audio zip
        if ps.zip_files:
            self.logger.info("RENAME  audio zip…")
        for f in ps.zip_files:
            _mv(f, f.parent / subst(f.name))

        # Cloud files (quadrants + zip in SharePoint folder)
        cloud_files = [f for f in ps.cloud_files if f.suffix.lower() in ('.mp4', '.zip')]
        if cloud_files:
            self.logger.info(f"RENAME  cloud ({len(cloud_files)} files)…")
        for f in cloud_files:
            _mv(f, f.parent / subst(f.name))

        # Rename the SharePoint folder to reflect the updated band name
        if self.sharepoint_dest and self.sharepoint_dest.is_dir():
            try:
                y, mo, d = date_str.split('-')
                date_prefix = datetime.date(int(y), int(mo), int(d)).strftime('%y-%m-%d')
                folder = self._find_date_folder(self.sharepoint_dest, date_prefix)
                if folder:
                    all_bands = [
                        new_band if b == old_band else b
                        for (dt, b), _ in self._status_entries
                        if dt == date_str and b not in ('NOFUN', 'TBD', '')
                    ]
                    target = canonical_sharepoint_name(date_prefix, all_bands)
                    if target != folder.name:
                        folder.rename(folder.parent / target)
                        self.logger.info(f"RENAME  SharePoint folder → {target}")
            except (ValueError, OSError) as e:
                self.logger.warning(f"RENAME  could not rename SharePoint folder: {e}")
                errors += 1

        # Update encoding DB
        self.logger.info("RENAME  updating database…")
        self._encoding_db.rename_band(date_str, old_band, new_band)
        self._encoding_db.save()

        if errors:
            self.logger.info(f"RENAME  done with {errors} error(s) — check log above")
        else:
            self.logger.info(f"RENAME  {old_band} → {new_band} complete")

    def _rename_in_progress(self) -> bool:
        """True if a RENAME job is currently active in the queue."""
        return any(
            qj.manifest_key.endswith('_RENAME')
            for qj in self._job_queue.all_active()
        )

    def _run_rename_async(self) -> None:
        if self._rename_in_progress():
            self.logger.info("NOTICE  A rename is already running — please wait")
            return
        date, old, new = self._rename_date, self._rename_band, self._rename_new_name
        if not date or not old or not new:
            return

        from nofun.job_manifest import PipelineJob
        short = short_date(date)
        job = PipelineJob(
            kind='_rename',
            label=f'{short} {old} → {new} RENAME',
            priority=1,
        )

        def _fn(_date: str = date, _old: str = old, _new: str = new) -> None:
            self._do_rename_performance(_date, _old, _new)
            self._rename_state    = None
            self._rename_date     = None
            self._rename_band     = None
            self._rename_new_name = None
            if self._active_menu == MenuMode.STATUS:
                self._rebuild_status_entries()
                self._show_status_list()

        manifest = JobManifest(
            performance_key=f'{short}_RENAME',
            jobs=[job],
            python_fns={job.job_id: _fn},
        )
        self._job_queue.enqueue(manifest, JobCategory.MANUAL)

    def _handle_rename_command(self, cmd: str) -> None:
        """Route commands while _rename_state is active."""
        if cmd == 'HOME':
            self._cancel_rename()
            return

        if self._rename_state == 'select':
            # Expect b1, b2, …
            date = self._status_expanded_key
            if not date:
                self._cancel_rename()
                return
            bands = [
                ps.band for (d, _), ps in self._status_entries
                if d == date and ps.band not in ('NOFUN', 'TBD', '')
            ]
            if cmd.lower().startswith('b'):
                try:
                    idx = int(cmd[1:]) - 1
                    if 0 <= idx < len(bands):
                        self._rename_date  = date
                        self._rename_band  = bands[idx]
                        self._rename_state = 'enter_name'
                        self._show_rename_enter_name()
                        return
                except (ValueError, IndexError):
                    pass
            self.logger.info(f"NOTICE  type b1–b{len(bands)} to select a band, or HOME to cancel")
            return

        if self._rename_state == 'enter_name':
            # Any non-HOME text is the new name
            normalized = cmd.strip().upper().replace(' ', '_')
            if not normalized:
                self.logger.info("NOTICE  name cannot be empty")
                return
            if normalized == self._rename_band:
                self.logger.info("NOTICE  New name is identical — no change made.")
                self._cancel_rename()
                return
            # Check for collision with another band on the same date
            date = self._rename_date
            existing = {
                ps.band for (d, _), ps in self._status_entries
                if d == date and ps.band not in ('NOFUN', 'TBD', '')
                and ps.band != self._rename_band
            }
            if normalized in existing:
                self.logger.info(
                    f"NOTICE  A band named {normalized} already exists for {date}"
                )
                return
            self._rename_new_name = normalized
            self._rename_state    = 'confirm'
            self._show_rename_confirm()
            return

        if self._rename_state == 'confirm':
            if cmd == 'CONFIRM':
                if self._rename_in_progress():
                    self.logger.info("NOTICE  A rename is already running")
                    return
                self._run_rename_async()
            else:
                self.logger.info("NOTICE  Type CONFIRM to proceed, or HOME to cancel")
            return

    def _remaster_band(self, date: str, band: str) -> None:
        """Enqueue (or restart) a REMASTER for a single band of *date*."""
        manifest_key = perf_key(date, band) + '_REMASTER'
        active   = [qj for qj in self._job_queue.all_active()
                    if qj.manifest_key == manifest_key]
        if active:
            # Second press — cancel what's queued/running and restart from scratch
            running = [qj for qj in active if qj.status == 'running']
            if running:
                self._kill_all_ffmpeg_procs()
            self._job_queue.cancel_manifest(manifest_key)
            self._enqueue_remaster(date, force=True, band=band)
        else:
            self._enqueue_remaster(date, band=band)

    def _cancel_remaster(self) -> None:
        self._remaster_state = None
        self._show_status_list()

    def _show_remaster_select(self) -> None:
        """List bands for the expanded show so the user can pick one (or all)."""
        from nofun.tui import MenuRow
        date = self._status_expanded_key
        if not date:
            self._cancel_remaster()
            return
        bands = [
            ps.band for (d, _), ps in self._status_entries
            if d == date and ps.band not in ('NOFUN', 'TBD', '')
        ]
        rows: list[MenuRow] = [
            MenuRow(index=None, text=f"  Show {date}  ·  select the band to remaster", dim=True),
            MenuRow(index=None, text='', dim=True),
        ]
        for j, band in enumerate(bands, start=1):
            rows.append(MenuRow(index=None, text=f"  b{j}  {band}"))
        rows.append(MenuRow(index=None, text='   A  ALL bands'))
        rows.append(MenuRow(index=None, text='', dim=True))
        bn = len(bands)
        bar = f"Available commands:  b1–b{bn} / A (all) / [yellow]HOME[/yellow] to cancel"
        if self._app:
            self._app.update_menu('INVENTORY — REMASTER', 'Select a band', rows, '', stats='')
            self._app.update_command_bar(bar)

    def _handle_remaster_command(self, cmd: str) -> None:
        """Route commands while _remaster_state is active."""
        if cmd == 'HOME':
            self._cancel_remaster()
            return
        date = self._status_expanded_key
        if not date:
            self._cancel_remaster()
            return
        bands = [
            ps.band for (d, _), ps in self._status_entries
            if d == date and ps.band not in ('NOFUN', 'TBD', '')
        ]
        if cmd.upper() in ('A', 'ALL'):
            for band in bands:
                self._remaster_band(date, band)
            self._remaster_state = None
            self._show_status_list()
            return
        if cmd.lower().startswith('b'):
            try:
                idx = int(cmd[1:]) - 1
                if 0 <= idx < len(bands):
                    self._remaster_band(date, bands[idx])
                    self._remaster_state = None
                    self._show_status_list()
                    return
            except (ValueError, IndexError):
                pass
        self.logger.info(f"NOTICE  type b1–b{len(bands)} / A (all) to select, or HOME to cancel")

    def _handle_status_command(self, cmd: str) -> None:
        """Route commands while the INVENTORY menu is active."""
        # Delegate to rename sub-flow when active
        if self._rename_state is not None:
            self._handle_rename_command(cmd)
            return
        # Delegate to remaster band-picker when active
        if self._remaster_state is not None:
            self._handle_remaster_command(cmd)
            return

        if cmd == 'HOME':
            if self._help['inventory'].active:
                self._help['inventory'].reset()
                self._show_status_list()
                return
            self._active_menu         = MenuMode.NONE
            self._status_expanded_key = None
            self._remaster_state      = None

            self.logger.info("Inventory menu closed.")
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)
            return

        if cmd in ('SCAN', 'BIGSCAN'):
            if cmd == 'BIGSCAN':
                if not self._override_time and not self._job_queue.is_within_schedule(JobCategory.GPU_BOUND):
                    self.logger.info(
                        "BIGSCAN: outside processing hours. "
                        "Type NOPROBLEM first to override."
                    )
                    return
            self._run_scan_async(cmd)   # non-blocking — menu stays interactive
            return

        if cmd == 'REUPLOAD':
            if self._status_expanded_key is None:
                self.logger.info("NOTICE  Select a show first (type its number), then REUPLOAD")
                return
            date = self._status_expanded_key
            bands = [
                ps.band for (d, _), ps in self._status_entries
                if d == date and ps.band not in ('NOFUN', 'TBD', '')
            ]
            if not bands:
                self.logger.info("NOTICE  No uploadable bands found for this show")
                return
            for band in bands:
                self._enqueue_reupload(date, band)
            return

        if cmd == 'REMASTER':
            if self._status_expanded_key is None:
                self.logger.info("NOTICE  Expand a show first (type its number), then REMASTER")
                return
            date  = self._status_expanded_key
            bands = [
                ps.band for (d, _), ps in self._status_entries
                if d == date and ps.band not in ('NOFUN', 'TBD', '')
            ]
            if not bands:
                self.logger.info("NOTICE  No remasterable bands found for this show")
                return
            if len(bands) == 1:
                self._remaster_band(date, bands[0])
                return
            self._remaster_state = 'select'
            self._show_remaster_select()
            return

        if cmd == 'TESTREMASTER':
            if self._status_expanded_key is None:
                self.logger.info("NOTICE  Expand a show first (type its number), then TESTREMASTER")
                return
            self._enqueue_remaster(self._status_expanded_key, trial_seconds=30)
            return

        if cmd == 'RENAME':
            if self._status_expanded_key is None:
                self.logger.info("NOTICE  Expand a show first (type its number), then RENAME")
                return
            if self._rename_in_progress():
                self.logger.info("NOTICE  A rename is already running — please wait")
                return
            self._rename_state = 'select'
            self._show_rename_select()
            return

        if cmd == 'HELP':
            self._show_inventory_help()
            return

        try:
            num = int(cmd)
            idx = num - 1
            if 0 <= idx < len(self._show_groups):
                date, _, _ = self._show_groups[idx]
                self._status_expanded_key = (
                    None if date == self._status_expanded_key else date
                )
                self._show_status_list()
            else:
                self.logger.info(f"NOTICE  {num} is not a valid show number.")
        except ValueError:
            self.logger.info(
                f"NOTICE  Unknown inventory command: {cmd}  (HOME to exit)"
            )

    def _show_inventory_help(self) -> None:
        """Show INVENTORY HELP as an overlay. Toggles brief↔detailed on each call."""
        state = self._help['inventory']
        if self._app:
            verbose  = state.verbose
            rows     = self._build_help_rows(self._INVENTORY_HELP, verbose)
            subtitle = 'detailed — HELP to toggle' if verbose else 'brief — HELP for detail'
            self._app.update_menu('INVENTORY', subtitle, rows, subtitle)
            self._app.update_command_bar(
                '[green]HELP[/green] to toggle detail  /  [yellow]HOME[/yellow] to close'
            )
            state.active  = True
            state.verbose = not verbose
        else:
            tag = '(detailed)' if state.verbose else '(brief — run again for detail)'
            self.logger.info(f"INVENTORY commands {tag}:")
            for cmd_text, brief, details in self._INVENTORY_HELP:
                if state.verbose:
                    self.logger.info(f"  {cmd_text}")
                    for line in details:
                        self.logger.info(f"    {line}")
                else:
                    self.logger.info(f"  {cmd_text:<12} — {brief}")
            state.verbose = not state.verbose
