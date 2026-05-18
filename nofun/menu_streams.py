"""TUI command routing for the STREAMS menu.

This is a Pipeline mixin — it has no __init__ and assumes all instance state
is initialised on Pipeline.

Methods are extracted verbatim from media_engine.py — no behaviour changes.
"""

from __future__ import annotations

from nofun.state import MenuMode
from nofun.streams import BASE_PORT, STREAM_COUNT, StreamServer, get_local_ip


class StreamsMenuMixin:

    _STREAMS_HELP: list[tuple[str, str, list[str]]] = [
        ('START',   'Launch all ffmpeg MPEG-TS stream workers', [
            f'Creates a StreamServer with {STREAM_COUNT} StreamWorker threads on ports',
            f'{BASE_PORT}–{BASE_PORT + STREAM_COUNT - 1}. Each worker loops a shuffled batch of clips',
            'from clips_dest via an ffconcat playlist piped to ffmpeg → MPEG-TS stdout.',
            'Clients connect via HTTP GET /video; chunks are broadcast via per-client',
            'queues (BROADCAST_MAXQ). Codec auto-detected (h264/hevc_mp4toannexb).',
        ]),
        ('RESTART', 'Stop all workers and restart fresh', [
            'Calls StreamServer.stop() (terminates ffmpeg, sends None poison-pill to',
            'all client queues for clean disconnect), then creates a new StreamServer',
            'and calls start(). Codec and clip list are re-detected from scratch.',
        ]),
        ('STOP',    'Stop all streams and exit the menu', [
            'Stops all workers and HTTP servers, sets _stream_server = None.',
            'Connected TouchDesigner clients receive a clean TCP close (poison-pill',
            'flushes the queue, handler loop exits, socket closes normally).',
        ]),
        ('HOME',    'Exit the streams menu (streams keep running)', [
            'Sets _active_menu = MenuMode.NONE, restores home command bar.',
            'Workers and HTTP servers stay alive — streams continue serving clients.',
            'Use STOP to actually shut down the stream server.',
        ]),
        ('HELP',    'Show this help (again for technical detail)', [
            'First HELP: brief one-liner per command.',
            'HELP again while overlay is open: toggle to technical detail.',
            'HOME dismisses and returns to the stream status view.',
        ]),
    ]

    def _enter_streams_menu(self) -> None:
        """Open the STREAMS menu without auto-starting the server."""
        self._show_streams_menu()    # calls show_menu() — flag must be False here
        self._active_menu = MenuMode.STREAMS

    def _show_streams_menu(self) -> None:
        """Build a MenuRow list and push it to the menu overlay."""
        from nofun.tui import MenuRow

        if self._stream_server is None:
            # Idle state — server not started yet
            ip   = get_local_ip()
            rows = [MenuRow(index=None, text='  Streams are not running.', dim=True)]
            subtitle = f"{ip}  ·  stopped"
            footer   = 'stopped'
            bar      = f"[green]START[/green] to begin streaming  /  [green]HELP[/green]  /  [yellow]HOME[/yellow]"
            if self._app:
                if self._active_menu != MenuMode.STREAMS:
                    self._app.show_menu('STREAMS', subtitle, rows, footer)
                else:
                    self._app.update_menu('STREAMS', subtitle, rows, footer)
                self._app.update_command_bar(bar)
                self._app.update_status(f"STREAMS  ·  {subtitle}")
            return

        statuses = self._stream_server.status()
        n        = len(statuses)
        n_live   = sum(1 for s in statuses if s['live'])
        ip       = get_local_ip()

        BAR_W = 8

        rows: list[MenuRow] = []
        rows.append(MenuRow(
            index=None,
            text=f"{'Port':<6}  {'URL':<32}  {'Cl':>3}  {'Progress':<{BAR_W + 2}}  Current clip",
            dim=True,
        ))

        for i, s in enumerate(statuses, start=1):
            dot       = "[green]●[/green]" if s['live'] else "[red]○[/red]"
            cl_str    = f"[dim]{s['clients']:>2}cl[/dim]"
            remaining = s.get('time_remaining', 0.0)
            duration  = s.get('clip_duration', 0.0)
            if duration > 0:
                frac   = max(0.0, min(1.0, 1.0 - remaining / duration))
                filled = round(frac * BAR_W)
                pbar   = f"[green]{'█' * filled}[/green][dim]{'░' * (BAR_W - filled)}[/dim]"
            else:
                pbar   = f"[dim]{'░' * BAR_W}[/dim]"
            clip_nm   = s['clip'] if s['clip'] != '(none)' else '[dim](none)[/dim]'
            rows.append(MenuRow(
                index=i,
                text=f"{s['port']:<6}  {s['url']:<32}  {cl_str}  {dot} {pbar}  {clip_nm}",
            ))

        subtitle = f"{ip}  ·  {n_live} of {n} live"
        footer   = f"{n_live}/{n} live"
        bar      = f"Available commands:  RESTART / STOP / [green]HELP[/green] / [yellow]HOME[/yellow]"

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
        """Route commands while the STREAMS menu is active."""

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
            if self._stream_server and self._stream_server.running:
                self.logger.info("Streams menu closed. Streams continue running.")
            return

        if cmd == 'HELP':
            self._show_streams_help()
            return

        if cmd == 'START':
            if self._stream_server and self._stream_server.running:
                self.logger.info("NOTICE  Streams are already running. Use RESTART to restart.")
                return
            self._stream_server = StreamServer(self.clips_dest, BASE_PORT, STREAM_COUNT)
            self._stream_server.start()
            self._show_streams_menu()
            return

        if cmd == 'STOP':
            if self._stream_server:
                self._stream_server.stop()
                self._stream_server = None
            _exit_menu()
            self.logger.info("Streams stopped.")
            return

        if cmd == 'RESTART':
            if self._stream_server:
                self._stream_server.stop()
            self._stream_server = StreamServer(self.clips_dest, BASE_PORT, STREAM_COUNT)
            self._stream_server.start()
            self._show_streams_menu()
            return

        self.logger.info(f"NOTICE  Unknown stream command: {cmd}  (HOME to exit)")
