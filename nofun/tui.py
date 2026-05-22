"""nofun/tui.py — Textual TUI for the NOFUN Media Engine."""
from __future__ import annotations

import dataclasses
import datetime
import logging
import queue
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, Callable

from textual            import events
from textual.app        import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.message    import Message
from textual.reactive   import reactive
from textual.widgets    import Input, RichLog, Static
if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class MenuRow:
    """One displayable row in a menu panel."""
    index: int | None   # 1-based item number; None for header/separator rows
    text:  str          # Rich markup string (no timestamp prefix)
    dim:   bool = False # True for column headers, separators, sub-detail rows


# ---------------------------------------------------------------------------
# Static widgets
# ---------------------------------------------------------------------------

class Banner(Static):
    """Fixed header: the NOFUN logo box."""

    def render(self) -> str:
        return (
            "\n"
            "  ╔══════════════════════════════════════════╗\n"
            "  ║  [green]♪  NOFUN MEDIA ENGINE[/green]                   ║\n"
            "  ║  [dim]video · audio · archive pipeline[/dim]        ║\n"
            "  ╚══════════════════════════════════════════╝\n"
            "\n"
        )


def _time_ago(iso: str) -> str:
    """Return integer relative time: '5 minutes ago', '2 hours ago', etc."""
    if not iso:
        return ''
    try:
        delta = int((datetime.datetime.now() - datetime.datetime.fromisoformat(iso)).total_seconds())
    except (ValueError, TypeError):
        return iso
    if delta < 60:
        n = max(delta, 0)
        return f'{n} second{"s" if n != 1 else ""} ago'
    if delta < 3600:
        n = delta // 60
        return f'{n} minute{"s" if n != 1 else ""} ago'
    if delta < 86400:
        n = delta // 3600
        return f'{n} hour{"s" if n != 1 else ""} ago'
    n = delta // 86400
    return f'{n} day{"s" if n != 1 else ""} ago'


class InventoryPanel(Static):
    """Banner-right panel: curated lifetime stats + animation primitive."""

    perf_count:            reactive[int]   = reactive(0)
    total_runtime_seconds: reactive[float] = reactive(0.0)
    streams_text:          reactive[str]   = reactive('')
    queue_health:          reactive[str]   = reactive('')
    anim_phase:            reactive[int]   = reactive(0)

    # Legacy reactives — retained so existing call sites don't break during
    # rollout. Slated for removal once Phase 2 has soaked in prod.
    inv_counts: reactive[dict] = reactive({})
    inv_time:   reactive[str]  = reactive('')

    # Glyph cycle for the pulsing music note. Two glyphs keeps the eye-catch
    # gentle; extend to (♪, ♫, ♬, ♩) if a more frantic pulse is wanted.
    _NOTE_CYCLE = ('♪', '♫')

    def on_mount(self) -> None:
        # 0.5 s animation tick — drives anim_phase, used by render and any
        # future ephemeral overlays.
        self.set_interval(0.5, self._bump_anim)

    def _bump_anim(self) -> None:
        self.anim_phase += 1

    def _format_hours(self) -> str:
        hours = self.total_runtime_seconds / 3600.0
        if hours < 1.0:
            return ''
        return f'{hours:.1f} hours archived'

    def render(self) -> str:
        if not self.perf_count:
            return '\n\n  no inventory yet\n  run INVENTORY to scan\n\n'
        note = self._NOTE_CYCLE[self.anim_phase % len(self._NOTE_CYCLE)]
        pc   = self.perf_count
        hours = self._format_hours()
        plural = 's' if pc != 1 else ''
        line_stats = (
            f'  [bold green]{note}[/bold green]  '
            f'[bold cyan]{pc}[/bold cyan] performance{plural}'
        )
        if hours:
            line_stats += f'  [dim]·[/dim]  [bold cyan]{hours}[/bold cyan]'
        line_streams = (
            f'  [dim]streams:[/dim] {self.streams_text}'
            if self.streams_text else ''
        )
        return f'\n\n{line_stats}\n\n{line_streams}\n'


class StatusBar(Static):
    """Live status line — updated by the pipeline worker thread.

    Two display modes:
    - ``status_text`` set (non-empty): renders that string directly (used by menus
      and one-off overrides that want full control of the line).
    - ``status_text`` empty: renders the ``ops`` dict — each entry is one concurrent
      operation keyed by a short name (e.g. 'encode', 'scan', 'audio').  Entries
      are joined by a dim separator so multiple things show simultaneously.

    ``log_quiet_msg``: when non-empty, appended in yellow as a secondary advisory
    (e.g. "log quiet 3 min — ffmpeg running").
    """
    status_text:   reactive[str]  = reactive('')
    ops:           reactive[dict] = reactive({}, recompose=False)
    queue_info:    reactive[str]  = reactive('')
    log_quiet_msg: str            = ''   # plain attribute — set by _check_log_pulse

    def render(self) -> str:
        sep   = "  [dim]·[/dim]  "
        parts = []
        if self.status_text:
            parts.append(self.status_text)
        if self.ops:
            parts.extend(f"[dim]{k}:[/dim] {v}" for k, v in self.ops.items())
        if self.queue_info:
            parts.append(f"[dim]queue:[/dim] {self.queue_info}")
        base = f"  {sep.join(parts)}" if parts else "  [dim]—[/dim]"
        if self.log_quiet_msg:
            base = f"{base}  [yellow]{self.log_quiet_msg}[/yellow]"
        return base


class ProgressRow(Static):
    """Single-line GPU job progress (encode, reel, clips) — shown only while running."""
    pass


class AudioRow(Static):
    """Single-line CPU job progress (multitrack ZIP, mastering) — shown only while running."""
    pass


class CommandBar(Static):
    """Bottom hint bar listing available commands.

    ``commands_text`` is a reactive string so the pipeline worker can update
    it from its background thread via ``MediaEngineApp.update_command_bar()``.
    """

    DEFAULT_COMMANDS: str = (
        "Available commands:  NOPROBLEM / INVENTORY"
        " / STREAMS / PAUSE / [green]HELP[/green]"
    )

    commands_text: reactive[str] = reactive(DEFAULT_COMMANDS)

    def set_commands(self, text: str) -> None:
        """Set the command hint text (call from the main thread only)."""
        self.commands_text = text

    def render(self) -> str:
        return f"  {self.commands_text}"


# ---------------------------------------------------------------------------
# Menu overlay widgets
# ---------------------------------------------------------------------------

class MenuHeader(Static):
    """Full-width header shown in menu mode — replaces the normal Banner+Inventory row."""

    title:    reactive[str] = reactive('')
    subtitle: reactive[str] = reactive('')
    stats:    reactive[str] = reactive('')

    def render(self) -> str:
        inner_w = max(10, self.size.width - 4)
        line1   = f"  {self.title}  ·  {self.subtitle}"
        pad1    = max(0, inner_w - len(line1))
        top_bot = '═' * inner_w
        if self.stats:
            line2 = f"  {self.stats}"
            pad2  = max(0, inner_w - len(line2))
            return (
                f"  ╔{top_bot}╗\n"
                f"  ║{line1}{' ' * pad1}║\n"
                f"  ║{line2}{' ' * pad2}║\n"
                f"  ╚{top_bot}╝\n"
            )
        return (
            f"  ╔{top_bot}╗\n"
            f"  ║{line1}{' ' * pad1}║\n"
            f"  ╚{top_bot}╝\n"
        )


class MenuBorderTop(Static):
    """Top border of the list. Dims when the user has scrolled down."""
    at_top: reactive[bool] = reactive(True)

    def render(self) -> str:
        inner_w = max(4, self.size.width - 4)
        if self.at_top:
            return f"  ┌{'─' * inner_w}┐"
        else:
            return f"  [dim]┄{'┄' * inner_w}┄[/dim]"


class MenuBorderBottom(Static):
    """Bottom border of the list, showing a footer summary."""
    footer: reactive[str] = reactive('')

    def render(self) -> str:
        inner_w = max(4, self.size.width - 4)
        label   = f" {self.footer} " if self.footer else ''
        fill    = max(0, inner_w - len(label))
        return f"  └{label}{'─' * fill}┘"


class MenuList(ScrollableContainer):
    """Scrollable body of a menu page. Children are one Static per MenuRow."""

    can_focus = True

    BINDINGS = [
        ('up',        'scroll_up',   'Scroll up'),
        ('down',      'scroll_down', 'Scroll down'),
        ('page_up',   'page_up',     'Page up'),
        ('page_down', 'page_down',   'Page down'),
    ]

    class Scrolled(Message):
        """Posted whenever the scroll position changes."""
        def __init__(self, at_top: bool) -> None:
            super().__init__()
            self.at_top = at_top

    def set_rows(self, rows: list[MenuRow]) -> None:
        """Replace all children with new rows, preserving scroll position."""
        saved_y = self.scroll_y
        with self.app.batch_update():
            self.remove_children()
            for row in rows:
                idx_str = f"[dim]{row.index:>3}[/dim]  " if row.index is not None else '       '
                text    = f"  {idx_str}{row.text}"
                child   = Static(text, markup=True)
                if row.dim:
                    child.add_class('menu-row-dim')
                self.mount(child)
        self.call_after_refresh(lambda: setattr(self, 'scroll_y', saved_y))

    def on_scroll(self, _event) -> None:
        self.post_message(MenuList.Scrolled(at_top=(self.scroll_y <= 0)))


# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------

class _WavRemovedRateLimit(logging.Filter):
    """Rate-limit 'disappeared externally' lines to 1 per 60 s in the TUI.

    File handlers do not get this filter — full detail stays in the log.
    Catches the visibility hole that opened during audio-archive cleanup:
    dozens of REMOVED events scroll real activity off-screen in seconds.
    """

    _WINDOW_S = 60.0

    def __init__(self) -> None:
        super().__init__()
        self._last: float = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        if 'disappeared externally' not in record.getMessage():
            return True
        now = time.monotonic()
        if now - self._last < self._WINDOW_S:
            return False
        self._last = now
        return True


class TextualLogHandler(logging.Handler):
    """Logging handler that writes records to a RichLog widget.

    Uses call_from_thread() so it is safe to call from the pipeline worker.
    File handlers (RollingRecentHandler, RemoteRotatingHandler) remain
    attached to the logger and continue writing plain text to disk.
    """

    _KEYWORD_MARKUP: dict[str, str] = {
        'CREATE':    'green',
        'MOVE':      'cyan',
        'DELETE':    'bold red',
        'DETECTED':  'green',
        'REMOVED':   'yellow',
        'RENAME':    'cyan',
        'SPLITTING': 'dim',
        'ENCODING':  'dim',
        'ZIPPING':   'dim',
        'PENDING':   'yellow',
        'NOTICE':    'yellow',
        'ERROR':     'bold red',
        'SKIP':      'dim',
        'LOCKED':    'yellow',
        'REEL':      'cyan',
        'MASTER':    'cyan',
        'SHARE':     'green',
        'ALIGN':     'dim',
        'LOAD':      'dim',
        'WRITE':     'dim',
        'PAUSE':     'bold yellow',
        'RESUME':    'green',
    }

    def __init__(self, app: MediaEngineApp, log_widget: RichLog) -> None:
        super().__init__()
        self.app           = app
        self.log_widget    = log_widget
        self.last_emit_ts: float = time.monotonic()

    def emit(self, record: logging.LogRecord) -> None:
        if not getattr(record, 'tui', True):
            return
        try:
            ts  = datetime.datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
            msg = record.getMessage()
            for keyword, colour in self._KEYWORD_MARKUP.items():
                if msg.startswith(keyword):
                    msg = msg.replace(keyword, f'[{colour}]{keyword}[/{colour}]', 1)
                    break
            for attr in ('size', 'elapsed'):
                val = getattr(record, attr, None)
                if val:
                    msg = f"{msg}  [dim]{val}[/dim]"
            line = f"[dim]{ts}[/dim]  {msg}"
            self.last_emit_ts = time.monotonic()
            self.app.call_from_thread(self.log_widget.write, line)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# The App
# ---------------------------------------------------------------------------

class MediaEngineApp(App):
    """NOFUN Media Engine — Textual TUI wrapper around Pipeline."""

    CSS_PATH = 'tui.tcss'
    TITLE    = 'NOFUNMEDIAENGINE'

    def __init__(self, pipeline) -> None:
        super().__init__()
        self.pipeline         = pipeline
        self._cmd_queue:      queue.Queue[str]           = queue.Queue()
        self._tui_log_handler: TextualLogHandler | None  = None

    # -----------------------------------------------------------------------
    # Layout
    # -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Normal mode header
        with Horizontal(id='header'):
            yield Banner(id='banner')
            yield InventoryPanel(id='inv_panel')

        # Menu mode overlay (hidden until show_menu() is called)
        yield MenuHeader(id='menu_header')
        yield MenuBorderTop(id='menu_border_top')
        yield MenuList(id='menu_list')
        yield MenuBorderBottom(id='menu_border_bot')

        # Always visible
        yield RichLog(id='log', highlight=True, markup=True, wrap=True)
        yield ProgressRow(id='progress')
        yield AudioRow(id='audio_progress')
        yield Input(placeholder='type a command…', id='cmd_input')
        yield CommandBar(id='cmd_bar')
        yield StatusBar(id='status')

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------

    def on_mount(self) -> None:
        """Wire up logging and start the pipeline worker."""
        log_widget = self.query_one('#log', RichLog)

        # Replace the console StreamHandler with one writing to our RichLog widget.
        # Use exact type check — FileHandler subclasses (RollingRecentHandler etc.)
        # also inherit from StreamHandler and must NOT be removed.
        logger = logging.getLogger('media_engine')
        for h in list(logger.handlers):
            if type(h) is logging.StreamHandler:
                logger.removeHandler(h)
        handler = TextualLogHandler(self, log_widget)
        handler.setLevel(logging.INFO)
        handler.addFilter(_WavRemovedRateLimit())
        logger.addHandler(handler)
        self._tui_log_handler = handler

        # Start the pipeline in a background thread
        self.run_worker(self._pipeline_worker, thread=True, name='pipeline')
        self.query_one('#cmd_input', Input).focus()
        # Refresh live-updating menus (streams countdown, etc.) every second
        self.set_interval(1.0, self._tick_live_menu)
        # Check log quiet advisory every 60 seconds
        self.set_interval(60.0, self._check_log_pulse)

    def on_unmount(self) -> None:
        """Stop streams cleanly when the TUI exits (window close, Ctrl+C, etc.)."""
        if hasattr(self.pipeline, '_cleanup'):
            self.pipeline._cleanup()

    # -----------------------------------------------------------------------
    # Live menu ticker (runs on Textual event-loop thread)
    # -----------------------------------------------------------------------

    def _tick_live_menu(self) -> None:
        """Called every second; refreshes menus that need live countdown updates."""
        from nofun.state import MenuMode
        if not hasattr(self.pipeline, '_active_menu'):
            return
        match self.pipeline._active_menu:
            case MenuMode.STREAMS:
                self.pipeline._show_streams_menu()
            case MenuMode.JOBS:
                # Skip the 1-second rebuild while a job is expanded — the
                # completion callback (_on_job_complete) handles the update
                # when it matters.  Prevents a race between the timer and the
                # worker thread calling _show_jobs_list simultaneously.
                if not self.pipeline._jobs_selected_idx:
                    self.pipeline._show_jobs_list()

    _LOG_QUIET_THRESHOLD = 120.0   # seconds of TUI silence before advisory shows

    def _check_log_pulse(self) -> None:
        """Called every 60 s; shows a status-bar advisory when the TUI log has been
        silent for >2 minutes while an ffmpeg job is actively running."""
        status = self.query_one('#status', StatusBar)
        if self._tui_log_handler is None:
            return
        silent_secs = time.monotonic() - self._tui_log_handler.last_emit_ts
        lock = getattr(self.pipeline, '_ffmpeg_procs_lock', None)
        with lock if lock is not None else nullcontext():
            procs = getattr(self.pipeline, '_current_ffmpeg_procs', {})
            has_active_job = bool(procs)
        if silent_secs >= self._LOG_QUIET_THRESHOLD and has_active_job:
            mins = int(silent_secs // 60)
            status.log_quiet_msg = f'log quiet {mins} min — ffmpeg running'
        else:
            status.log_quiet_msg = ''
        status.refresh()

    # -----------------------------------------------------------------------
    # Pipeline worker
    # -----------------------------------------------------------------------

    def _pipeline_worker(self) -> None:
        """Runs Pipeline.run_with_queue() in a background thread."""
        self.pipeline.run_with_queue(self._cmd_queue, self)

    # -----------------------------------------------------------------------
    # Input handling
    # -----------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip().upper()
        if cmd:
            self._cmd_queue.put(cmd)
            # HOME: also close the overlay immediately so it doesn't stay open
            # while the pipeline worker is blocked inside ffmpeg.  The queue
            # entry is still processed later for any remaining state cleanup.
            if cmd == 'HOME':
                self.pipeline.immediate_home()
        event.input.clear()

    # -----------------------------------------------------------------------
    # Menu overlay methods
    # -----------------------------------------------------------------------

    def show_menu(
        self,
        title:    str,
        subtitle: str,
        rows:     list[MenuRow],
        footer:   str = '',
        stats:    str = '',
    ) -> None:
        """Activate the menu overlay. Safe to call from the pipeline worker thread."""
        def _do() -> None:
            # Hide normal-mode widgets
            self.query_one('#header').add_class('menu-active')
            self.query_one('#log', RichLog).add_class('menu-active')

            # Configure menu header
            mh          = self.query_one('#menu_header', MenuHeader)
            mh.title    = title
            mh.subtitle = subtitle
            mh.stats    = stats
            mh.add_class('active')

            # Top border — reset to "at top"
            top        = self.query_one('#menu_border_top', MenuBorderTop)
            top.at_top = True
            top.add_class('active')

            # Bottom border footer
            bot        = self.query_one('#menu_border_bot', MenuBorderBottom)
            bot.footer = footer
            bot.add_class('active')

            # Populate list and scroll to top
            ml = self.query_one('#menu_list', MenuList)
            ml.scroll_home(animate=False)
            ml.set_rows(rows)
            ml.add_class('active')

        self._dispatch(_do)

    def update_menu(
        self,
        title:     str,
        subtitle:  str,
        rows:      list[MenuRow],
        footer:    str = '',
        stats:     str = '',
        scroll_to: int | None = None,
    ) -> None:
        """Refresh the menu overlay content without toggling display.

        If *scroll_to* is given it is a row-list index; after the refresh the
        MenuList will scroll that child into view (overriding position restore).
        """
        def _do() -> None:
            mh          = self.query_one('#menu_header', MenuHeader)
            mh.title    = title
            mh.subtitle = subtitle
            mh.stats    = stats
            bot         = self.query_one('#menu_border_bot', MenuBorderBottom)
            bot.footer  = footer
            ml          = self.query_one('#menu_list', MenuList)
            ml.set_rows(rows)
            # Deliberately do NOT scroll_home — preserve position after item deletion
            if scroll_to is not None:
                idx = scroll_to
                def _scroll() -> None:
                    children = list(ml.children)
                    if idx < len(children):
                        children[idx].scroll_visible(animate=False)
                ml.call_after_refresh(_scroll)

        self._dispatch(_do)

    def hide_menu(self) -> None:
        """Deactivate the menu overlay and restore normal mode."""
        def _do() -> None:
            self.query_one('#header').remove_class('menu-active')
            self.query_one('#log', RichLog).remove_class('menu-active')
            for wid_id in ('#menu_header', '#menu_border_top',
                           '#menu_list', '#menu_border_bot'):
                self.query_one(wid_id).remove_class('active')
            self.query_one('#cmd_input', Input).focus()

        self._dispatch(_do)

    def on_menu_list_scrolled(self, event: MenuList.Scrolled) -> None:
        """Dim the top border when the list is not scrolled to the top."""
        top        = self.query_one('#menu_border_top', MenuBorderTop)
        top.at_top = event.at_top

    def on_key(self, event: events.Key) -> None:
        """Route arrow/page keys to MenuList (menu open) or RichLog (home).

        cmd_input (single-line Input) doesn't consume Up/Down so they bubble
        here. This lets the user scroll the list with the keyboard without
        needing to click it first — important when running inside tmux where
        mouse events don't reach the app.
        """
        ml = self.query_one('#menu_list', MenuList)
        if 'active' in ml.classes:
            target: ScrollableContainer = ml
        else:
            target = self.query_one('#log', RichLog)
        scroll_map = {
            'up':       target.scroll_up,
            'down':     target.scroll_down,
            'pageup':   target.scroll_page_up,
            'pagedown': target.scroll_page_down,
            'home':     target.scroll_home,
            'end':      target.scroll_end,
        }
        action = scroll_map.get(event.key)
        if action:
            action()
            event.prevent_default()

    # -----------------------------------------------------------------------
    # Thread-safe widget update helpers
    # -----------------------------------------------------------------------

    def _dispatch(self, fn: 'Callable[[], None]') -> None:
        """Call fn on the event-loop thread.

        If already on the event-loop thread (e.g. set_interval callback), call
        directly.  If on a background thread (pipeline worker), schedule via
        call_from_thread.  Using call_from_thread from the event-loop thread
        itself causes a crash in Textual.
        """
        import asyncio
        try:
            asyncio.get_running_loop()
            fn()          # already on the event-loop — call directly
        except RuntimeError:
            self.call_from_thread(fn)   # background thread — schedule safely

    def update_status(self, text: str) -> None:
        """Override the whole status bar with a single string (menus / one-offs).
        Pass '' to revert to the concurrent-ops display."""
        def _do() -> None:
            self.query_one('#status', StatusBar).status_text = text
        self._dispatch(_do)

    def set_op(self, key: str, text: str) -> None:
        """Add or update a named concurrent-operation slot in the status bar."""
        def _do() -> None:
            bar = self.query_one('#status', StatusBar)
            bar.ops = {**bar.ops, key: text}
        self._dispatch(_do)

    def clear_op(self, key: str) -> None:
        """Remove a named operation slot from the status bar."""
        def _do() -> None:
            bar = self.query_one('#status', StatusBar)
            bar.ops = {k: v for k, v in bar.ops.items() if k != key}
        self._dispatch(_do)

    def update_progress(
        self,
        frame:        str,
        fps:          str,
        tc:           str,
        speed:        str,
        duration:     float | None = None,
        job_label:    str          = 'encode',
        band:         str          = '',
        total_frames: int | None   = None,
    ) -> None:
        """Show GPU job progress. Safe to call from the pipeline worker thread.

        Renders ``\u25ce  {job_label} {band}  [frame N \u00b7 pct \u00b7 eta]  GPU N%``.
        Each bracket segment is added only when its source data is available;
        the GPU segment is omitted when SysMon can't read the counter.
        """
        from nofun.media_io import compute_ffmpeg_eta
        from nofun.sysmon   import SysMon
        eta_str  = compute_ffmpeg_eta(tc, speed, duration)
        _, gpu_pct = SysMon.get()

        try:
            frame_int = int(frame)
        except (ValueError, TypeError):
            frame_int = 0
        bracket = f'frame [cyan]{frame_int}[/cyan]'
        if total_frames and frame_int > 0:
            pct = min(frame_int * 100 // total_frames, 99)
            bracket += f'/~{total_frames} \u00b7 {pct}%'
        if eta_str:
            bracket += f' \u00b7 {eta_str}'

        label = f'{job_label} [yellow]{band}[/yellow]' if band else job_label
        # \[ escapes the literal opening bracket so Textual's markup parser
        # doesn't try to parse `[frame [cyan]N[/cyan]\u2026]` as a malformed tag.
        text  = f'  \u25ce  {label}  \\[{bracket}]'
        if gpu_pct is not None:
            text += f'  [dim]GPU {gpu_pct:.0f}%[/dim]'
        row = self.query_one('#progress', ProgressRow)
        self.call_from_thread(row.update, text)
        self.call_from_thread(row.add_class, 'active')

    def update_audio_progress(
        self,
        job_label: str,
        band:      str,
        done:      int,
        total:     int,
        elapsed_s: float,
        eta_str:   str = '',
    ) -> None:
        """Show CPU job progress (multitrack ZIP). Safe to call from any thread.

        Renders ``\u229e  {job_label} {band}  [done/total \u00b7 pct \u00b7 elapsed \u00b7 eta]  CPU N%``.
        """
        from nofun.sysmon import SysMon
        cpu_pct, _ = SysMon.get()
        pct = done * 100 // total if total else 0
        mins, secs = divmod(int(elapsed_s), 60)
        t = f'{mins}:{secs:02d}' if mins else f'{int(elapsed_s)}s'
        bracket = f'{done}/{total} \u00b7 {pct}% \u00b7 {t}'
        if eta_str:
            bracket += f' \u00b7 {eta_str}'
        label = f'{job_label} [yellow]{band}[/yellow]' if band else job_label
        text  = f'  \u229e  {label}  \\[{bracket}]  [dim]CPU {cpu_pct:.0f}%[/dim]'
        row = self.query_one('#audio_progress', AudioRow)
        self.call_from_thread(row.update, text)
        self.call_from_thread(row.add_class, 'active')

    def clear_row(self, row_id: str) -> None:
        """Hide a progress row by id ('progress' or 'audio_progress'). Thread-safe."""
        row = self.query_one(f'#{row_id}', Static)
        self.call_from_thread(row.remove_class, 'active')

    def update_command_bar(self, text: str) -> None:
        """Update the CommandBar text. Safe to call from any thread."""
        bar = self.query_one('#cmd_bar', CommandBar)
        self._dispatch(lambda: bar.set_commands(text))

    def update_queue_info(self, text: str) -> None:
        """Update the queue summary section of the status bar. Thread-safe."""
        def _do() -> None:
            self.query_one('#status', StatusBar).queue_info = text
        self._dispatch(_do)

    def update_queue_health(self, text: str) -> None:
        """Update the queue health indicator in the inventory panel. Thread-safe."""
        def _do() -> None:
            self.query_one('#inv_panel', InventoryPanel).queue_health = text
        self._dispatch(_do)

    def update_clip_progress(self, n: int, total: int, band: str = '', elapsed_s: float = 0.0) -> None:
        """Show clip export progress X/T in the progress row. Thread-safe."""
        label   = f'clips [yellow]{band}[/yellow]' if band else 'clips'
        row     = self.query_one('#progress', ProgressRow)
        pct_str = f'  {int(n * 100 / total)}%' if total else ''
        eta_str = ''
        if elapsed_s > 0 and 0 < n < total:
            secs = int((total - n) * elapsed_s / n)
            eta_str = f'  eta {secs // 60}m {secs % 60}s' if secs >= 60 else f'  eta {secs}s'
        text = f"  \u25ce  {label}  \\[{n}/{total}{pct_str}{eta_str}]"
        self.call_from_thread(row.update, text)
        self.call_from_thread(row.add_class, 'active')

    def update_inventory_stats(
        self,
        counts:                dict,
        time_str:              str,
        perf_count:            int   = -1,
        total_runtime_seconds: float = -1.0,
    ) -> None:
        """Update the inventory panel. Safe to call from any thread.

        Pass perf_count >= 0 to also update the performance counter.
        Pass total_runtime_seconds >= 0 to also update the archive runtime stat.
        """
        def _do_update() -> None:
            panel = self.query_one('#inv_panel', InventoryPanel)
            panel.inv_counts = counts
            panel.inv_time   = time_str
            if perf_count >= 0:
                panel.perf_count = perf_count
            if total_runtime_seconds >= 0:
                panel.total_runtime_seconds = total_runtime_seconds
        self.call_from_thread(_do_update)

    def bump_perf_count(self) -> None:
        """Increment the banner perf counter by 1. Thread-safe."""
        def _do() -> None:
            panel = self.query_one('#inv_panel', InventoryPanel)
            panel.perf_count += 1
        self.call_from_thread(_do)

    def update_streams_text(self, text: str) -> None:
        """Update the live-streams line on the inventory panel. Thread-safe."""
        def _do() -> None:
            self.query_one('#inv_panel', InventoryPanel).streams_text = text
        self._dispatch(_do)
