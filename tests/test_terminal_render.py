"""Unit tests for Terminal render — 宽度缩窄残留清除（rant 2026-08-14T11:47:11）。

窗口缩窄时终端屏幕每行右侧的旧字符不会被 diff 清除（diff_buffers 只比较
min(prev_w, curr_w) 列，且 Buffer.resize 缩窄已物理截断旧宽度信息）→
render 检测到缩窄时先 CLEAR_SCREEN 再全量重绘。本测试验证该行为。
"""

from __future__ import annotations

import io
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
