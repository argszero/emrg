"""Unit tests for Terminal render — 宽度缩窄残留清除（rant 2026-08-14T11:47:11）。

窗口缩窄时终端屏幕每行右侧的旧字符不会被 diff 清除（diff_buffers 只比较
min(prev_w, curr_w) 列，且 Buffer.resize 缩窄已物理截断旧宽度信息）→
render 检测到缩窄时先 CLEAR_SCREEN 再全量重绘。本测试验证该行为。
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

import pytest

# 依赖 posix 路径（raw mode / termios 守卫），Windows 跳过（同 test_app_widgets）
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Terminal 渲染测试依赖 POSIX 路径")

from emrg.client.python_tui.output import CLEAR_SCREEN
from emrg.client.python_tui.terminal import Terminal
from emrg.client.python_tui.widgets.base import Line, RenderContext, Span


class FakeWidget:
    """Minimal widget: renders `width` chars per line."""

    def __init__(self) -> None:
        self.dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        return [Line(spans=[Span(text="x" * ctx.width)])]


def _set_width(term: Terminal, width: int) -> None:
    """Force terminal width (SIGWINCH path would probe real tty — not usable in tests)."""
    term.caps.width = width
    term.viewport.resize(term.caps.height, width)


def _render(term: Terminal) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        term.render(full=True)
    return buf.getvalue()


def test_render_shrink_emits_clear_screen():
    """缩窄 40 → 20：输出必须先 CLEAR_SCREEN（右侧残留清除）。"""
    term = Terminal()
    term.mount(chat=FakeWidget())
    _set_width(term, 40)
    _render(term)  # 建立 _last_render_width = 40

    _set_width(term, 20)
    out = _render(term)

    assert CLEAR_SCREEN in out


def test_render_same_width_no_clear_screen():
    """同宽两次渲染：无 CLEAR_SCREEN（避免无谓闪烁）。"""
    term = Terminal()
    term.mount(chat=FakeWidget())
    _set_width(term, 40)
    _render(term)

    out = _render(term)
    assert CLEAR_SCREEN not in out


def test_render_grow_no_clear_screen():
    """拉宽 40 → 60：无 CLEAR_SCREEN（新增列由 diff 覆盖，无需清屏）。"""
    term = Terminal()
    term.mount(chat=FakeWidget())
    _set_width(term, 40)
    _render(term)

    _set_width(term, 60)
    out = _render(term)
    assert CLEAR_SCREEN not in out


def test_shutdown_clears_screen():
    """退出清屏（rant 2026-08-18T11:13:17）：shutdown() 必须发 CLEAR_SCREEN。

    只归位 (0,0) 不清屏会把 TUI 残留留在终端里（退出后界面内容仍在屏幕上）。
    """
    term = Terminal()
    buf = io.StringIO()
    with redirect_stdout(buf):
        term.shutdown()
    out = buf.getvalue()
    assert CLEAR_SCREEN in out, "shutdown() must emit CLEAR_SCREEN (TUI residual on exit)"
    # 清屏后归位 (0,0)：CLEAR_SCREEN 之后必须紧跟 CURSOR_HOME
    from emrg.client.python_tui.output import CURSOR_HOME
    assert out.index(CLEAR_SCREEN) < out.index(CURSOR_HOME), "clear screen must precede cursor home"


class FakeComposer:
    """Composer fake that mimics InputWidget.render: one top/bottom separator line
    plus one Line per logical input line (multi-line text → more than 3 rows).

    rant 2026-08-28T22:53:24 — a fixed composer_height=3 mis-models multiline
    input; the viewport must track the actual rendered row count.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        lines = [Line(spans=[Span(text="─" * ctx.width)])]
        # Split on "\n": each logical line, including leading/trailing empty
        # strings (matches InputWidget.render's raw = text.split("\n")).
        for logical in self.text.split("\n"):
            lines.append(Line(spans=[Span(text="> " + logical)]))
        lines.append(Line(spans=[Span(text="─" * ctx.width)]))
        return lines


def test_composer_height_tracks_multiline_render():
    """viewport.composer_height must reflect the composer's actual line count.

    For a single-line input the composer renders 3 rows (sep + content + sep).
    For multiline input (text containing "\n") it renders more. A hard-coded
    height of 3 would make the viewport's region model disagree with the real
    layout → the reported "错行" (composer content misaligned with "> ").
    """
    term = Terminal()
    term.mount(composer=FakeComposer("hello"))
    with redirect_stdout(io.StringIO()):
        term.render(full=True)
    assert term.viewport.composer_height == 3, "single-line → 3 rows (sep+content+sep)"

    term2 = Terminal()
    term2.mount(composer=FakeComposer("line1\nline2"))
    with redirect_stdout(io.StringIO()):
        term2.render(full=True)
    assert term2.viewport.composer_height == 4, "2 logical lines → 4 rows"

    term3 = Terminal()
    term3.mount(composer=FakeComposer("\ntest"))
    with redirect_stdout(io.StringIO()):
        term3.render(full=True)
    assert term3.viewport.composer_height == 4, "leading newline → 4 rows"


def test_probe_terminal_zero_winsize_falls_back_to_80x24(monkeypatch):
    """rant 2026-08-31T11:36:22 — os.get_terminal_size() may return 0x0 when
    the PTY winsize hasn't been reported yet (macOS Terminal at startup).

    _probe_terminal() must not let width=0 overwrite the 80x24 default —
    width 0 would collapse RenderContext.width to 0 and split every CJK
    char (cell_len=2) onto its own line ("中文错行"). shutil's guard rejects
    0 values and falls back to the explicit (80, 24).
    """
    from emrg.client.python_tui import terminal as term_mod

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    monkeypatch.setattr(os, "get_terminal_size", lambda fd=None: os.terminal_size((0, 0)))

    caps = term_mod._probe_terminal()
    assert caps.width == 80, f"width must fall back to 80, got {caps.width}"
    assert caps.height == 24, f"height must fall back to 24, got {caps.height}"


def test_handle_resize_zero_winsize_keeps_last_known_good(monkeypatch):
    """rant 2026-08-31T11:36:22 — handle_resize() races the same 0x0 probe
    (a SIGWINCH can fire before Terminal.app reports the real winsize).

    It must fall back to the last known-good dimensions instead of collapsing
    to 0 (which would again split CJK chars onto separate lines).
    """
    term = Terminal()
    term.caps.width = 120
    term.caps.height = 40
    term.viewport.resize(term.caps.height, term.caps.width)

    monkeypatch.setattr(os, "get_terminal_size", lambda fd=None: os.terminal_size((0, 0)))

    with redirect_stdout(io.StringIO()):
        term.handle_resize()

    assert term.caps.width == 120, f"width must keep last-known-good 120, got {term.caps.width}"
    assert term.caps.height == 40, f"height must keep last-known-good 40, got {term.caps.height}"
    assert term.viewport.viewport_width == 120
