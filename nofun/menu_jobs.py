"""TUI command routing for the JOBS menu.

This is a Pipeline mixin — it has no __init__ and assumes all instance state
is initialised on Pipeline.

Methods are extracted verbatim from media_engine.py — no behaviour changes.
"""

from __future__ import annotations

import pathlib
import re

from nofun.job_queue import JobCategory
from nofun.state import MenuMode


_FINISH_RE = re.compile(
    r'^\[[\d-]+T[\d:]+\] JobQueue: finish (.+?)\s{2,}\((\w+)\s+(\d+)m\)'
)

_JOBS_HELP = [
    ('<number>', 'Select a job to see its details and available actions', [
        'Type the row number to expand it. Type the same number again to collapse.',
        'With a job selected: CANCEL stops it; HOME collapses the selection.',
    ]),
    ('CANCEL', 'Cancel or stop the selected job', [
        'Select a job first by typing its number, then type CANCEL.',
        'Pending jobs are removed from the queue immediately.',
        'Running jobs are killed — any partial output is discarded.',
    ]),
    ('HOME', 'Collapse selection, or exit the JOBS menu', [
        'If a job is selected, HOME collapses it back to the list.',
        'If nothing is selected, HOME exits to the home screen.',
        'The queue continues running in the background.',
    ]),
    ('HELP', 'Toggle brief / detailed help', [
        'First press: brief one-liner per command.',
        'Second press: full detail (this view).',
    ]),
]


class JobsMenuMixin:

    def _read_job_history_from_logs(self, max_entries: int = 20) -> list[dict]:
        """Parse JobQueue: finish lines from the latest 2 log files."""
        candidates: list[pathlib.Path] = []
        local_log = self.script_dir / 'convert_recent.log'
        if local_log.exists():
            candidates.append(local_log)
        remote_log_dir = self.mount_d / 'logs'
        if remote_log_dir.is_dir():
            remote_logs = sorted(
                remote_log_dir.glob('log_*.txt'),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(remote_logs[:2])

        seen_paths: set[pathlib.Path] = set()
        log_files: list[pathlib.Path] = []
        for p in candidates:
            rp = p.resolve()
            if rp not in seen_paths:
                seen_paths.add(rp)
                log_files.append(p)
        log_files = log_files[:2]

        entries: list[dict] = []
        seen_keys: set[tuple] = set()
        for log_path in log_files:
            try:
                text = log_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                m = _FINISH_RE.match(line)
                if not m:
                    continue
                label, status, mins_str = m.group(1), m.group(2), m.group(3)
                ts = line[1:15]  # YY-MM-DDTHH:MM
                key = (ts, label)
                if key not in seen_keys:
                    seen_keys.add(key)
                    entries.append({'ts': ts, 'label': label,
                                    'status': status, 'mins': int(mins_str)})

        entries.sort(key=lambda e: e['ts'])
        return entries[-max_entries:]

    def _enter_jobs_menu(self) -> None:
        self._jobs_selected_idx = None
        self._show_jobs_list()          # calls show_menu() — flag must be False here
        self._active_menu = MenuMode.JOBS

    def _show_jobs_list(self) -> None:
        """Build a MenuRow list from the job queue and push it to the overlay.

        When _jobs_selected_idx is set, the selected active job row is expanded
        with an inline detail section (label, status, elapsed, manifest key, and
        available action hint).
        """
        from nofun.tui import MenuRow

        active   = self._job_queue.all_active()
        log_hist = self._read_job_history_from_logs()
        summary  = self._job_queue.summary()

        # Clamp or clear selection if the active list shrank
        if self._jobs_selected_idx is not None:
            if self._jobs_selected_idx >= len(active):
                self._jobs_selected_idx = None

        parts: list[str] = []
        if summary['pending'] or summary['running']:
            parts.append(f"{summary['pending'] + summary['running']} active")
        if log_hist:
            parts.append(f"{len(log_hist)} in history")
        subtitle = '  ·  '.join(parts) or 'queue empty'

        rows: list[MenuRow] = []
        if not active and not log_hist:
            rows.append(MenuRow(index=None, text='  No jobs in queue.', dim=True))
        else:
            for i, qj in enumerate(active, start=1):
                from scripts import SCRIPT_REGISTRY as _REG
                selected  = (self._jobs_selected_idx == i - 1)
                icon      = '[green]▶[/green]' if qj.status == 'running' else ' '
                sel_marker = '[cyan]›[/cyan]' if selected else ' '
                elapsed_s = int(qj.elapsed or 0)
                elapsed_str = (
                    f'  [dim]{elapsed_s // 3600}h {elapsed_s % 3600 // 60}m[/dim]'
                    if elapsed_s >= 3600 else
                    f'  [dim]{elapsed_s // 60}m {elapsed_s % 60}s[/dim]'
                    if elapsed_s else ''
                )
                if qj.status == 'running':
                    st = '[green]running[/green]'
                elif qj.job.depends:
                    st = '[dim]waiting[/dim]'
                else:
                    st = '[dim]pending[/dim]'
                reg   = _REG.get(qj.job.kind, {})
                lane  = reg.get('lane', '')
                lane_tag = f'  [dim][{lane}][/dim]' if lane else ''
                label = (qj.job.label or reg.get('label', qj.job.kind))[:38]
                rows.append(MenuRow(
                    index=i,
                    text=f"  {sel_marker}{icon} {label:<38}{lane_tag}  {st}{elapsed_str}",
                ))

                if qj.status == 'running' and self._script_runner.last_progress:
                    p = self._script_runner.last_progress
                    frame = p.get('frame', '')
                    fps   = p.get('fps', '')
                    tc    = p.get('out_time', '')
                    speed = p.get('speed', '')
                    if frame:
                        rows.append(MenuRow(
                            index=None,
                            text=f"       [dim]◎ frame {frame}  fps {fps}  {tc}  {speed}[/dim]",
                        ))

                if selected:
                    # Inline detail section
                    rows.append(MenuRow(index=None, text='', dim=True))
                    rows.append(MenuRow(index=None,
                        text=f"     [dim]manifest:[/dim]  {qj.manifest_key}"))
                    if qj.job.depends:
                        rows.append(MenuRow(index=None,
                            text=f"     [dim]depends: [/dim]  {', '.join(qj.job.depends)}"))
                    if elapsed_s:
                        est_m = reg.get('est_minutes', 0)
                        eta   = f'  ETA ~{max(0, int(est_m * 60) - elapsed_s) // 60}m' if est_m and qj.status == 'running' else ''
                        rows.append(MenuRow(index=None,
                            text=f"     [dim]elapsed: [/dim]  {elapsed_s // 60}m {elapsed_s % 60}s{eta}"))
                    rows.append(MenuRow(index=None, text='', dim=True))
                    if qj.status == 'running':
                        rows.append(MenuRow(index=None,
                            text='     [yellow]CANCEL[/yellow] — stop this job now'))
                    else:
                        rows.append(MenuRow(index=None,
                            text='     [yellow]CANCEL[/yellow] — remove from queue'))
                    rows.append(MenuRow(index=None, text='', dim=True))

            if log_hist:
                rows.append(MenuRow(index=None, text='─ ─ completed ─ ─', dim=True))
                for entry in reversed(log_hist):
                    col = 'green' if entry['status'] == 'done' else (
                          'red'   if entry['status'] == 'failed' else 'yellow')
                    mins_str = f"  {entry['mins']}m" if entry['mins'] else ''
                    label    = entry['label'][:42]
                    ts_str   = entry['ts'][9:14]  # HH:MM
                    rows.append(MenuRow(
                        index=None,
                        text=(
                            f"  [dim]    {label:<42}  [{col}]{entry['status']}[/{col}]"
                            f"  {ts_str}{mins_str}[/dim]"
                        ),
                        dim=True,
                    ))

        if self._jobs_selected_idx is not None:
            bar = "[yellow]CANCEL[/yellow] / [green]HOME[/green] to deselect"
        else:
            bar = "Type a number to select  /  [green]HELP[/green] / [yellow]HOME[/yellow]"
        if self._app:
            if self._active_menu != MenuMode.JOBS:
                self._app.show_menu('JOBS', subtitle, rows, subtitle)
            else:
                self._app.update_menu('JOBS', subtitle, rows, subtitle)
            self._app.update_command_bar(bar)
            self._app.update_status(f"JOBS  ·  {subtitle}")

    def _show_jobs_help(self) -> None:
        state = self._help['jobs']
        if self._app:
            verbose  = state.verbose
            rows     = self._build_help_rows(_JOBS_HELP, verbose)
            subtitle = 'detailed — HELP to toggle' if verbose else 'brief — HELP for detail'
            self._app.update_menu('JOBS', subtitle, rows, subtitle)
            self._app.update_command_bar(
                '[green]HELP[/green] to toggle  /  [yellow]HOME[/yellow] to close'
            )
            state.active  = True
            state.verbose = not verbose

    def _handle_jobs_command(self, cmd: str) -> None:
        """Route commands while the JOBS menu is active."""

        def _exit_menu() -> None:
            self._active_menu       = MenuMode.NONE
            self._jobs_selected_idx = None
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)

        if cmd == 'HOME':
            if self._help['jobs'].active:
                self._help['jobs'].reset()
                self._show_jobs_list()
                return
            if self._jobs_selected_idx is not None:
                # Collapse selection, stay in menu
                self._jobs_selected_idx = None
                self._show_jobs_list()
                return
            _exit_menu()
            return

        if cmd == 'HELP':
            self._show_jobs_help()
            return

        if cmd.startswith('SCHEDULE'):
            parts = cmd.split(maxsplit=1)
            self._handle_schedule_command(parts[1] if len(parts) == 2 else '')
            return

        if cmd == 'CANCEL':
            idx = self._jobs_selected_idx
            if idx is None:
                if self._app:
                    self._app.update_status('JOBS  ·  Select a job first (type its number)')
                return
            active = self._job_queue.all_active()
            if idx < len(active):
                qj    = active[idx]
                label = qj.job.label or qj.job.kind
                if qj.status == 'running':
                    self._kill_all_ffmpeg_procs()
                    self._kill_worker_runners()
                    self._job_queue.kill_running()
                    self.logger.info(f"JOBS  Stopping: {label}")
                else:
                    if self._job_queue.cancel(qj.job_id):
                        self.logger.info(f"JOBS  Cancelled: {label}")
            self._jobs_selected_idx = None
            self._show_jobs_list()
            return

        # Number input: toggle selection
        try:
            n = int(cmd)
            active = self._job_queue.all_active()
            if 1 <= n <= len(active):
                new_idx = n - 1
                self._jobs_selected_idx = (
                    None if self._jobs_selected_idx == new_idx else new_idx
                )
                self._show_jobs_list()
            else:
                if self._app:
                    self._app.update_status(f'JOBS  ·  No job #{n}')
            return
        except ValueError:
            pass

        if self._app:
            self._app.update_status(f"JOBS  ·  Unknown: {cmd!r} — type HOME")

    def _show_jobs_dryrun(self) -> None:
        """Show what the next pending job would execute, without running it."""
        from nofun.tui import MenuRow
        active  = self._job_queue.all_active()
        pending = [qj for qj in active if qj.status == 'pending']
        if not pending:
            if self._app:
                self._app.update_status('JOBS  ·  No pending jobs to preview')
            return

        qj   = pending[0]
        job  = qj.job
        rows: list[MenuRow] = []
        rows.append(MenuRow(index=None, text=f"  [cyan]DRY RUN[/cyan]  ·  {qj.manifest_key}", dim=False))
        rows.append(MenuRow(index=None, text='', dim=True))

        label = job.label or job.kind
        rows.append(MenuRow(index=None, text=f"  [dim]type:[/dim]  Python function", dim=False))
        rows.append(MenuRow(index=None, text=f"  [dim]job: [/dim]  {label}", dim=False))
        rows.append(MenuRow(index=None, text='', dim=True))
        rows.append(MenuRow(index=None,
            text='  [dim]This job runs in-process — no external script.[/dim]', dim=True))

        if self._app:
            self._app.update_menu('JOBS', 'dry run preview', rows, 'dry run preview')
            self._app.update_command_bar('[yellow]HOME[/yellow] to return  /  [green]HELP[/green]')

    def _handle_schedule_command(self, sub: str) -> None:
        """Handle SCHEDULE / SCHEDULE ON / SCHEDULE OFF."""
        from nofun.tui import MenuRow
        from nofun.job_queue import JobCategory

        if sub == 'OFF':
            self._job_queue.set_rule_enabled('encode_window', False)
            self._job_queue.set_rule_enabled('gpu_window', False)
            self.logger.info("SCHEDULE  encode time-gate disabled until midnight or SCHEDULE ON")
        elif sub == 'ON':
            self._job_queue.set_rule_enabled('encode_window', True)
            self._job_queue.set_rule_enabled('gpu_window', True)
            self.logger.info("SCHEDULE  encode time-gate re-enabled")

        rules = self._job_queue.get_schedule_rules()
        rows: list[MenuRow] = []
        for rule in rules:
            active   = rule.is_active()
            state    = '[green]active[/green]' if active else '[yellow]outside window[/yellow]'
            enabled  = '' if rule.enabled else '  [dim](disabled)[/dim]'
            cat_icon = {
                JobCategory.GPU_BOUND: '⚙',
                JobCategory.CPU_BOUND: '⚙',
                JobCategory.SCHEDULED: '⏰',
                JobCategory.MANUAL:    '▶',
            }.get(rule.category, '?')
            rows.append(MenuRow(
                index=None,
                text=(
                    f"  {cat_icon}  [dim]{rule.name:<20}[/dim]"
                    f"  {rule.window_str}  {state}{enabled}"
                ),
            ))

        rows.append(MenuRow(index=None, text='', dim=True))
        rows.append(MenuRow(index=None,
            text='  Type [cyan]SCHEDULE OFF[/cyan] / [cyan]SCHEDULE ON[/cyan] to toggle encode window.',
            dim=True))

        subtitle = 'schedule rules'
        if self._app:
            self._app.update_menu('JOBS', subtitle, rows, subtitle)
            self._app.update_command_bar(
                '[cyan]SCHEDULE ON[/cyan] / [cyan]SCHEDULE OFF[/cyan]'
                '  /  [yellow]HOME[/yellow] to return'
            )
