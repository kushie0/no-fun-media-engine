"""TUI routing for the STREAMS menu — a read-only dashboard for the gtv clip-wall.

Pipeline mixin (no __init__; state lives on Pipeline). As of Phase 3 this shows the *external* gtv
stack (scripts/streams/google_tv_run.ps1 → mediamtx :8656): per-feed liveness + reception + assigned
stick, observed via nofun.gtv_status. The engine does NOT own those processes — they run as the
GoogleTVStreams / GoogleTVHeal scheduled tasks — so this menu never starts/stops them; it just
reflects their state (an engine restart can't blip the wall). Start/stop/config controls arrive in
Phase 3 landing 2.
"""
from __future__ import annotations

from nofun.state import MenuMode
from nofun.gtv_status import GTV_RTSP_PORT, get_local_ip, gtv_feeds_status


class StreamsMenuMixin:

    _STREAMS_HELP: list[tuple[str, str, list[str]]] = [
        ('REFRESH', 'Re-read the gtv feed status', [
            'Re-samples live ffmpeg publishers (per-feed metadata tag), established RTSP readers on',
            f'{GTV_RTSP_PORT}, and the heal log assignments. Read-only — the gtv wall runs as the',
            'GoogleTVStreams scheduled task, not from inside the engine.',
        ]),
        ('HOME', 'Exit the dashboard (streams keep running)', [
            'Returns to HOME. The gtv wall is external to the engine, so it keeps running regardless',
            'of this menu or an engine restart.',
        ]),
        ('HELP', 'Show this help (again for detail)', [
            'First HELP: brief one-liner per command. HELP again toggles technical detail.',
        ]),
    ]

    def _enter_streams_menu(self) -> None:
        """Open the STREAMS dashboard."""
        self._show_streams_menu()
        self._active_menu = MenuMode.STREAMS

    def _show_streams_menu(self) -> None:
        """Render the gtv feed status as a MenuRow list."""
        from nofun.tui import MenuRow

        ip = get_local_ip()
        feeds = gtv_feeds_status()

        if not feeds:
            rows = [MenuRow(index=None,
                            text='  No gtv feeds live  (GoogleTVStreams task stopped?).', dim=True)]
            subtitle = f'{ip}:{GTV_RTSP_PORT}  ·  no feeds live'
            footer   = 'no feeds'
        else:
            n      = len(feeds)
            n_recv = sum(1 for f in feeds if f['receiving'])
            rows = [MenuRow(index=None,
                            text=f"{'Feed':<6}  {'Recv':<4}  {'URL':<34}  Assigned stick", dim=True)]
            for i, f in enumerate(feeds, start=1):
                if f['receiving'] is None:
                    dot = "[yellow]?[/yellow]"
                elif f['receiving']:
                    dot = "[green]●[/green]"
                else:
                    dot = "[red]○[/red]"
                stick = f['stick'] if f['stick'] != '(unassigned)' else '[dim](unassigned)[/dim]'
                rows.append(MenuRow(index=i,
                                    text=f"{f['feed']:<6}  {dot:<4}  {f['url']:<34}  {stick}"))
            subtitle = f'{ip}:{GTV_RTSP_PORT}  ·  {n_recv} of {n} receiving'
            footer   = f'{n_recv}/{n} recv'

        bar = 'Available commands:  REFRESH / [green]HELP[/green] / [yellow]HOME[/yellow]'
        if self._app:
            if self._active_menu != MenuMode.STREAMS:
                self._app.show_menu('STREAMS', subtitle, rows, footer)
            else:
                self._app.update_menu('STREAMS', subtitle, rows, footer)
            self._app.update_command_bar(bar)
            self._app.update_status(f"STREAMS  ·  {subtitle}")

    def _show_streams_help(self) -> None:
        """Show STREAMS HELP overlay. Toggles brief↔detailed on each call."""
        state = self._help['streams']
        if self._app:
            verbose  = state.verbose
            rows     = self._build_help_rows(self._STREAMS_HELP, verbose)
            subtitle = 'detailed — HELP to toggle' if verbose else 'brief — HELP for detail'
            self._app.update_menu('STREAMS', subtitle, rows, subtitle)
            self._app.update_command_bar(
                '[green]HELP[/green] to toggle detail  /  [yellow]HOME[/yellow] to close'
            )
            state.active  = True
            state.verbose = not verbose
        else:
            tag = '(detailed)' if state.verbose else '(brief — run again for detail)'
            self.logger.info(f"STREAMS commands {tag}:")
            for cmd_text, brief, details in self._STREAMS_HELP:
                if state.verbose:
                    self.logger.info(f"  {cmd_text}")
                    for line in details:
                        self.logger.info(f"    {line}")
                else:
                    self.logger.info(f"  {cmd_text:<10} — {brief}")
            state.verbose = not state.verbose

    def _handle_stream_command(self, cmd: str) -> None:
        """Route commands while the STREAMS dashboard is active (read-only)."""

        def _exit_menu() -> None:
            self._active_menu = MenuMode.NONE
            if self._app:
                self._app.hide_menu()
                self._app.update_command_bar(self._HOME_COMMANDS)

        if cmd == 'HOME':
            if self._help['streams'].active:
                self._help['streams'].reset()
                self._show_streams_menu()
                return
            _exit_menu()
            return

        if cmd == 'HELP':
            self._show_streams_help()
            return

        if cmd in ('REFRESH', ''):
            self._show_streams_menu()
            return

        self.logger.info(f"NOTICE  Unknown stream command: {cmd}  (HOME to exit)")
