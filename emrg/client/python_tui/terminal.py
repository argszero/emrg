"""Terminal abstraction: raw mode, capability detection, lifecycle.

Equivalent to Codex's tui.rs terminal init and Claude Code's terminal.ts.
Manages TTY raw mode, queries terminal capabilities, provides draw/flush cycle.
"""

from __future__ import annotations

import os
import sys
import termios
import tty
from dataclasses import dataclass, field
from typing import Any, Callable

from emrg.client.python_tui.buffer import Buffer, CharPool, HyperlinkPool, StylePool, diff_buffers
from emrg.client.python_tui.output import (
    BSU,
    CURSOR_HIDE,
    CURSOR_SHOW,
    CURSOR_HOME,
    CLEAR_TO_EOL,
    ESU,
    RESET_SCROLL_REGION,
    RESTORE_CURSOR,
    SAVE_CURSOR,
    TerminalCapabilities,
    cursor_to,
    write_frame,
)
from emrg.client.python_tui.viewport import Viewport


def _probe_terminal() -> TerminalCapabilities:
    """Detect terminal dimensions and capabilities."""
    caps = TerminalCapabilities()

    # Terminal size
    try:
        size = os.get_terminal_size()
        caps.width = size.columns
        caps.height = size.lines
    except (OSError, ValueError):
        pass

    # Color depth
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    if colorterm in ("truecolor", "24bit"):
        caps.color_depth = 16777216
    elif term.endswith("256color") or "256" in colorterm:
        caps.color_depth = 256
    elif term.endswith("color"):
        caps.color_depth = 16
    else:
        caps.color_depth = 8

    # Kitty protocol
    caps.kitty_keyboard = bool(
        os.environ.get("KITTY_WINDOW_ID")
        or os.environ.get("TERMINFO", "").startswith("xterm-kitty")
    )

    # Synchronized output support (BSU/ESU, DECSET ?2026)
    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program in (
        "iTerm.app", "WezTerm", "WarpTerminal", "ghostty",
        "vscode", "alacritty",
    ):
        caps.sync_supported = True
    elif os.environ.get("WT_SESSION"):
        caps.sync_supported = True  # Windows Terminal

    # Hyperlinks (OSC 8) — supported by most modern terminals
    caps.hyperlinks = True

    return caps


@dataclass
class Terminal:
    """The rendering engine.

    Owns the viewport, cell buffer, pools, and output pipeline.
    Consumer application creates one Terminal and calls its methods.

    Usage:
        term = Terminal()
        term.mount(composer=Composer(on_submit=handle), status=StatusLine(...))
        # ... app loop ...
        term.shutdown()
    """

    caps: TerminalCapabilities = field(default_factory=_probe_terminal)
    viewport: Viewport = field(default_factory=Viewport)
    _raw_mode: bool = False
    _original_termios: list[Any] | None = None
    _front_buffer: Buffer | None = None
    _back_buffer: Buffer | None = None
    _char_pool: CharPool = field(default_factory=CharPool)
    _style_pool: StylePool = field(default_factory=StylePool)
    _hyperlink_pool: HyperlinkPool = field(default_factory=HyperlinkPool)
    _widgets: dict[str, Any] = field(default_factory=dict)
    _event_handlers: dict[str, list[Callable]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._enter_raw_mode()
        # SIGWINCH is handled in app.py's run_client() to avoid duplicate
        # handler registration. app.py calls term.handle_resize() directly.
        # Proportional viewport: fill most of terminal, leave 6 rows for native scrollback.
        # Replaces the old fixed 12-row default — on a 50-row terminal you get 44 rows.
        self.viewport.viewport_height = max(12, self.caps.height - 6)
        self.viewport.resize(self.caps.height, self.caps.width)
        w, h = self.viewport.viewport_width, self.viewport.viewport_height
        self._front_buffer = Buffer(
            w, h,
            char_pool=self._char_pool,
            style_pool=self._style_pool,
            hyperlink_pool=self._hyperlink_pool,
        )
        self._back_buffer = Buffer(
            w, h,
            char_pool=self._char_pool,
            style_pool=self._style_pool,
            hyperlink_pool=self._hyperlink_pool,
        )

    @property
    def viewport_height(self) -> int:
        """Configurable viewport height (rows at bottom of terminal)."""
        return self.viewport.viewport_height

    @viewport_height.setter
    def viewport_height(self, value: int) -> None:
        self.viewport.viewport_height = value
        self.viewport.resize(self.caps.height, self.caps.width)
        w, h = self.viewport.viewport_width, self.viewport.viewport_height
        if self._front_buffer:
            self._front_buffer.resize(w, h)
        if self._back_buffer:
            self._back_buffer.resize(w, h)

    def mount(self, **widgets: Any) -> None:
        """Mount widgets into viewport regions.

        Widgets are placed in named regions (status, composer, chat, prompts).
        The Terminal manages layout — consumers just provide widget instances.

        Args:
            status: StatusLine widget (1 row, bottom)
            composer: Composer widget (N rows, above status)
            chat: chat widget (remaining rows)
            prompts: InlinePrompt widget (1 row, above composer)
        """
        for region, widget in widgets.items():
            self._widgets[region] = widget

    def handle_resize(self) -> None:
        """Handle terminal resize (SIGWINCH).

        Re-probes terminal dimensions, resizes viewport and double-buffers,
        then triggers a full re-render to fix layout after resize.
        """
        try:
            size = os.get_terminal_size()
            self.caps.width = size.columns
            self.caps.height = size.lines
        except (OSError, ValueError):
            return
        self.viewport.resize(self.caps.height, self.caps.width)
        w, h = self.viewport.viewport_width, self.viewport.viewport_height
        if self._front_buffer:
            self._front_buffer.resize(w, h)
            self._front_buffer.clear()
        if self._back_buffer:
            self._back_buffer.resize(w, h)
            self._back_buffer.clear()
        # Clear render cache so all widgets re-render at new dimensions
        if hasattr(self, '_rendered_cache'):
            self._rendered_cache.clear()
        # Clear scrollback counter — viewport changed, recount needed
        if hasattr(self, '_scrollback_lines_pushed'):
            self._scrollback_lines_pushed = 0
        self.emit('resize', self.caps.width, self.caps.height)
        self.render(full=True)

    def render(self, full: bool = False) -> None:
        """Render dirty regions to the cell buffer and flush to stdout.

        If full=True, force redraw all regions regardless of dirty flags.
        Called automatically after state changes, or manually by consumer.
        """
        if not self._front_buffer or not self._back_buffer:
            return

        from emrg.client.python_tui.widgets.base import RenderContext
        from emrg.client.python_tui.buffer import write_lines_to_buffer

        width = self.viewport.viewport_width
        ctx = RenderContext(width=width)

        # Cache last rendered output per widget so dirty=False doesn't blank it
        if not hasattr(self, "_rendered_cache"):
            self._rendered_cache: dict[str, list[object]] = {}

        def _get_lines(name: str) -> list[object]:
            w = self._widgets.get(name)
            if w is None:
                return []
            if full or getattr(w, "dirty", True):
                try:
                    lines = w.render(ctx)
                    self._rendered_cache[name] = lines
                    return lines
                except Exception:
                    import traceback
                    traceback.print_exc()
                    return self._rendered_cache.get(name, [])
            return self._rendered_cache.get(name, [])

        status_lines = _get_lines("status")[:1]
        composer_lines = _get_lines("composer")[:10]
        prompt_lines = _get_lines("prompts")[:1]
        # Get ALL chat lines first (without truncation) so we know the real
        # needed height.  Truncation depends on viewport_height, but
        # viewport_height should depend on content — not the other way around.
        # If we truncate before computing needed, viewport shrink locks in and
        # prevents future growth (needed ≤ current height always → no resize).
        all_chat_lines = _get_lines("chat")

        # Calculate actual needed viewport height. Clamp to terminal height —
        # the viewport must not exceed the screen. When content overflows,
        # it's pushed into terminal scrollback (native, mouse-wheel browsable).
        old_vp_top = self.viewport.viewport_top
        old_vp_height = self.viewport.viewport_height
        fixed_rows = len(prompt_lines) + len(composer_lines) + len(status_lines)
        max_vp = max(self.caps.height - 6, 4)  # leave 6 rows for native scrollback
        needed = min(len(all_chat_lines) + fixed_rows, max_vp)
        if needed > 0 and needed != self.viewport.viewport_height:
            new_vp_h = max(needed, max(8, self.caps.height // 4))  # min: 1/4 term, ≥8
            old_vp_h_before = self.viewport.viewport_height
            self.viewport.viewport_height = new_vp_h
            self.viewport.resize(self.caps.height, self.caps.width)
            if self._front_buffer:
                self._front_buffer.resize(width, self.viewport.viewport_height)
            if self._back_buffer:
                self._back_buffer.resize(width, self.viewport.viewport_height)
            if new_vp_h != old_vp_h_before and self._front_buffer:
                self._front_buffer.clear()

        # Push overflow into terminal scrollback — incremental only.
        # Codex's insert-history pattern: only push NEW lines (not already
        # pushed). This avoids duplicate content in the scrollback buffer.
        # When total lines shrink (new task, context reset), reset the counter.
        max_chat = max(0, self.viewport.viewport_height - fixed_rows)
        total_chat = len(all_chat_lines)
        overflow_count = max(0, total_chat - max_chat)

        # Reset pushed-count when chat content shrinks (new conversation)
        pushed_before = getattr(self, "_scrollback_lines_pushed", 0)
        if total_chat < pushed_before:
            pushed_before = 0
            self._scrollback_lines_pushed = 0

        if overflow_count > pushed_before:
            new_lines = all_chat_lines[pushed_before:overflow_count]
            if new_lines:
                from emrg.client.python_tui.scrollback import push_lines_to_scrollback
                push_lines_to_scrollback(new_lines, self.viewport.viewport_top, self.caps.height)
            self._scrollback_lines_pushed = overflow_count

        # Show newest chat lines at bottom (terminal-scrollback has the rest)
        chat_lines = all_chat_lines[-max_chat:] if max_chat > 0 else []

        # When viewport_top moves or viewport height changes, the entire
        # old viewport area (old_vp_top .. old_vp_top + old_vp_height) may
        # contain stale content invisible to the diff engine.
        # diff_buffers only compares min(old_h, new_h) rows, and when
        # viewport_top shifts, the terminal-row ↔ buffer-row mapping
        # changes — so even equal-height regions have stale ghost content.
        # Explicitly erase every row the old viewport occupied.
        new_top = self.viewport.viewport_top
        new_height = self.viewport.viewport_height
        if old_vp_top != new_top or old_vp_height != new_height:
            old_end = old_vp_top + old_vp_height
            orphan_cleanup = "".join(
                f"{cursor_to(0, row)}{CLEAR_TO_EOL}"
                for row in range(old_vp_top, old_end)
            )
            sys.stdout.write(orphan_cleanup)
            sys.stdout.flush()

        # Re-clear with potentially resized buffers
        self._back_buffer.clear()

        # Bottom-up write
        cur = self.viewport.viewport_height
        for lines, start_offset in [
            (status_lines, -len(status_lines)),
            (composer_lines, -len(composer_lines) - len(status_lines)),
            (prompt_lines, -len(prompt_lines) - len(composer_lines) - len(status_lines)),
            (chat_lines, -len(chat_lines) - len(prompt_lines) - len(composer_lines) - len(status_lines)),
        ]:
            if lines:
                row = cur + start_offset
                write_lines_to_buffer(self._back_buffer, lines, start_row=row, style_pool=self._style_pool)

        # Diff and flush
        diffs = diff_buffers(self._front_buffer, self._back_buffer)

        # Offset y by viewport_top for absolute terminal positions
        vp_top = self.viewport.viewport_top
        if vp_top > 0 and diffs:
            diffs = [(x, y + vp_top, prev, curr) for x, y, prev, curr in diffs]

        if diffs:
            output = (
                SAVE_CURSOR
                + write_frame(
                    diffs,
                    style_pool=self._style_pool,
                    hyperlink_pool=self._hyperlink_pool,
                    sync=self.caps.sync_supported,
                )
                + RESTORE_CURSOR
            )
            sys.stdout.write(output)
            sys.stdout.flush()

        # Swap buffers
        self._front_buffer, self._back_buffer = self._back_buffer, self._front_buffer

    def append_to_scrollback(self, widget: Any) -> None:
        """Render widget and push content above viewport into terminal scrollback.

        Content is written using insert-lines (CSI Ps L) so it enters the
        terminal's native scrollback — fully selectable/copyable.
        """
        from emrg.client.python_tui.scrollback import push_lines_to_scrollback
        from emrg.client.python_tui.widgets.base import RenderContext

        ctx = RenderContext(width=self.viewport.viewport_width)
        try:
            lines = widget.render(ctx)
        except Exception:
            return

        if lines:
            push_lines_to_scrollback(
                lines,
                self.viewport.viewport_top,
                self.caps.height,
            )

    def on(self, event: str, handler: Callable) -> None:
        """Register an event handler.

        Supported events: 'key', 'submit', 'resize', 'mouse', 'paste'.
        """
        handlers = self._event_handlers.setdefault(event, [])
        handlers.append(handler)

    def emit(self, event: str, *args: Any) -> None:
        """Emit an event to registered handlers."""
        for handler in self._event_handlers.get(event, []):
            handler(*args)

    async def run(self) -> None:
        """Run the async event loop (convenience for simple apps).

        Reads stdin for key events, dispatches to handlers, and renders
        on each event. For complex apps, the consumer should build their
        own asyncio loop and call .render() / .append_to_scrollback() manually.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        self.render()  # Initial paint

        while True:
            try:
                # Read stdin with a short timeout (allows checking for shutdown)
                if sys.stdin.isatty():
                    data = await loop.run_in_executor(
                        None, sys.stdin.buffer.read, 16
                    )
                    if data:
                        from emrg.client.python_tui.events import parse_keypress, parse_mouse_sgr

                        # Try mouse first (longer sequences)
                        mouse = parse_mouse_sgr(data)
                        if mouse:
                            self.emit("mouse", mouse)
                        else:
                            key = parse_keypress(data)
                            if key:
                                self.emit("key", key)
                    else:
                        # EOF on stdin — exit
                        break
                else:
                    # Not a TTY — just yield and keep running
                    await asyncio.sleep(0.1)

                self.render()
            except KeyboardInterrupt:
                break
            except Exception:
                import traceback
                traceback.print_exc()
                break

    def shutdown(self) -> None:
        """Restore terminal and exit raw mode."""
        if self._raw_mode:
            self._exit_raw_mode()
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.write(CURSOR_HOME)
        sys.stdout.write(RESET_SCROLL_REGION)
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()

    # ── Raw mode ─────────────────────────────────────────────

    def _enter_raw_mode(self) -> None:
        """Enable raw mode on stdin for direct key reading."""
        if not sys.stdin.isatty():
            return
        try:
            self._original_termios = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            self._raw_mode = True
            sys.stdout.write(CURSOR_HIDE)
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
        except (termios.error, OSError):
            pass

    def _exit_raw_mode(self) -> None:
        """Restore original terminal settings."""
        if self._original_termios is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self._original_termios
                )
                self._raw_mode = False
            except (termios.error, OSError):
                pass

    def __enter__(self) -> Terminal:
        return self

    def __exit__(self, *args: Any) -> None:
        self.shutdown()
