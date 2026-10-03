"""A frame the TUI cannot read must not end the reader (measured 2026-10-04).

`run_client` 的 `read_server()` 是服务端帧的唯一消费者：整个 while 循环里，一帧
的处理体放在**只接 `json.JSONDecodeError`** 的 try 里，而体里按字段读帧（`data.get`
调用点上百处）。任何别的形状（字段类型不对、辅助函数对它抛错）都会穿出循环：
websocket 还开着，客户端照常渲染得像连着，而**那之后的每一帧都不再被处理**——
而且是静默的，因为该 task 的异常只在关停时才被取回
（`read_task.cancel()` / `await read_task`）。daemon 侧读循环为同一件事专门加固过
（`daemon.py` 的 `_process_message`）。

无终端也能测：把真实文件里的 handler 子句用 `ast` 取出来，接到一个必抛的 body 上
执行。跑的是这个文件自己的 handler 列表，不是手抄的副本。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from emrg.client import app as client_app


def _frame_dispatch_try() -> ast.Try:
    """找出 `read_server` 里那个逐帧 try —— 它的体以 `"uptime_seconds" in data` 开头。"""
    src = pathlib.Path(client_app.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                if isinstance(stmt, ast.If) and "uptime_seconds" in ast.unparse(stmt.test):
                    return node
    raise AssertionError(
        "emrg/client/app.py 里找不到逐帧 try（判据：体首句含 \"uptime_seconds\" in data）"
        "—— 本测试的前提已失效，请先复核它守的是哪段代码"
    )


class _Recording:
    """记下被调用过的属性；`dirty = True` 这类赋值不影响判定。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def _call(*_a, **_k):
            self.calls.append(name)
            return None
        return _call


class _Falsy:
    """其它名字的替身：**假值**，所以 `if not _frame_warned:` 这种「只提示一次」的
    判断会走进提示分支；可调用、可属性访问，任何用法都不炸。"""

    def __bool__(self) -> bool:
        return False

    def __call__(self, *_a, **_k):
        return self

    def __getattr__(self, _name):
        return self


def _raise_attribute_error() -> ast.Raise:
    return ast.Raise(
        exc=ast.Call(
            func=ast.Name(id="AttributeError", ctx=ast.Load()),
            args=[ast.Constant(value="'list' object has no attribute 'get'")],
            keywords=[],
        ),
        cause=None,
    )


def _names_read(handlers: list[ast.ExceptHandler]) -> set[str]:
    out: set[str] = set()
    for handler in handlers:
        for node in ast.walk(handler):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                out.add(node.id)
    return out


def _run_handlers(handlers: list[ast.ExceptHandler], ns: dict) -> None:
    """把给定的 handler 子句接到一个必抛的 body 上执行。"""
    module = ast.Module(
        body=[ast.Try(body=[_raise_attribute_error()], handlers=list(handlers),
                      orelse=[], finalbody=[])],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, "<spliced-from-app.py>", "exec"), ns)


def _ns_for(handlers: list[ast.ExceptHandler]) -> tuple[dict, _Recording, _Recording]:
    logger, chat, term = _Recording(), _Recording(), _Recording()
    ns: dict = {"json": json, "logger": logger, "chat": chat, "term": term,
                "_frame_warned": False}
    # 真实文件里其余的名字（闭包局部量）在拼接出的模块里不存在 → 一律给假值替身；
    # 内建名（Exception 之类）与已给的照旧。
    import builtins

    for name in _names_read(handlers):
        if name in ns or hasattr(builtins, name):
            continue
        ns[name] = _Falsy()
    return ns, logger, chat


def test_the_frame_handler_survives_a_frame_it_cannot_read():
    """核心不变量：一帧读不了，循环不死。"""
    node = _frame_dispatch_try()
    ns, logger, _chat = _ns_for(node.handlers)
    _run_handlers(node.handlers, ns)  # 不抛 = 这一帧被吞掉，读取继续


def test_the_unreadable_frame_reaches_the_log():
    """而且要说出来：内部异常进日志（TUI 不显示 traceback）。"""
    node = _frame_dispatch_try()
    ns, logger, _chat = _ns_for(node.handlers)
    _run_handlers(node.handlers, ns)
    assert logger.calls, "读不了的帧必须留下一行日志（失败静默是本条要修的东西）"


def test_the_probe_discriminates_a_narrow_handler():
    """控制腿：把这个文件自己的窄 handler 拿去跑同一段必抛的体，异常**必须**穿出。

    即：上面那条测试之所以绿，是因为 handler 列表宽了，不是因为拼出来的探针
    无论如何都吞异常。
    """
    node = _frame_dispatch_try()
    narrow = [h for h in node.handlers if ast.unparse(h.type) == "json.JSONDecodeError"]
    assert narrow, "逐帧 try 里连 json.JSONDecodeError 都没有了 —— 请复核这条测试的前提"
    narrow[0].body = [ast.Pass()]  # 文件里的写法是 `except json.JSONDecodeError: pass`
    ns, _logger, _chat = _ns_for(narrow)
    with pytest.raises(AttributeError):
        _run_handlers(narrow, ns)


def test_the_dispatch_body_really_reads_the_frame_by_field():
    """前提腿：这个 try 的体确实按字段读帧 —— 「换个形状就炸」才成立。"""
    node = _frame_dispatch_try()
    field_reads = sum(
        1 for n in ast.walk(ast.Try(body=node.body, handlers=[], orelse=[], finalbody=[]))
        if isinstance(n, ast.Attribute) and n.attr == "get"
        and isinstance(n.value, ast.Name) and n.value.id == "data"
    )
    assert field_reads >= 20, f"逐帧体只剩 {field_reads} 处 data.get —— 前提变了，请复核"
