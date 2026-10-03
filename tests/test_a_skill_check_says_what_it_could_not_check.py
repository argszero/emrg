"""A skill check that could not run must not be reported as a clean one.

Measured 2026-10-04, without a terminal and without the network: with
``api.github.com`` unreachable (the module's own docstring names this host
network — "raw.githubusercontent.com / github.com:443 may be blocked"), every
entry of a managed set lands in the update summary's `continue  # network/API
failure` branch. That branch appended the entry to **no list at all**, while
`checked` still counted it — so the summary the daemon sends back was
``{"checked": 1, "updated": [], "skipped": [], "errors": []}``, and the TUI row
for exactly that summary printed:

    **Skill update check:** 1 managed skill(s)
    All up to date.

"All up to date" is a verdict; the run had measured nothing. The fix gives the
summary two named lists (`unreadable`, `unknown`) and makes the row refuse the
verdict while either is non-empty.

Both halves run here, plus the composed one: the summary a real
`update_managed_skills` produces is fed into the **TUI's own render branch**,
lifted from `emrg/client/app.py` by `ast` — the branch is executed as written,
never re-typed.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

import emrg.skills.installer as installer
import emrg.skills.registry as registry
from emrg.client import app as client_app
from emrg.skills.registry import ensure_catalog_file, write_state


# ── fixtures (mirroring tests/test_skills_registry.py) ───────────────

@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    import emrg.skills.loader as loader

    emrg_dir = tmp_path / ".emrg"
    monkeypatch.setattr(registry, "config_dir", lambda: emrg_dir)
    monkeypatch.setattr(installer, "config_dir", lambda: emrg_dir)
    monkeypatch.setattr(loader.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


@pytest.fixture
def with_cli(monkeypatch):
    monkeypatch.setattr(installer, "cli_available", lambda: True)


VALID_SKILL_MD = """---
name: browser-harness
description: "Direct browser control via CDP: automation, scraping, testing, site work."
---

# browser-harness

Body text.
"""


class _FailingHttp:
    """The host network the module's docstring warns about: every read fails."""

    async def __call__(self, url):
        return None


class _UpToDateHttp:
    async def __call__(self, url):
        if url.endswith("/releases/latest"):
            return {"tag_name": "v0.1.8"}
        return None


def _run(coro):
    return asyncio.run(coro)


# ── the render branch, lifted from the real file ─────────────────────

def _skills_update_branch() -> ast.If:
    """The TUI's `skills_update_result` branch, as written in app.py."""
    src = pathlib.Path(client_app.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.If) and "skills_update_result" in ast.unparse(node.test):
            return node
    raise AssertionError(
        "emrg/client/app.py 里找不到 skills_update_result 分支 —— "
        "本测试的前提已失效，请先复核它守的是哪段代码"
    )


class _Chat:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def add(self, kind, text):
        self.rows.append((kind, text))


class _Noop:
    def __getattr__(self, _name):
        def _call(*_a, **_k):
            return None
        return _call


def _render(summary: dict) -> str:
    """Execute the TUI's own branch on `summary`; return the row it printed."""
    node = _skills_update_branch()
    body = [s for s in node.body if not isinstance(s, ast.Continue)]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    chat = _Chat()
    ns = {
        "data": {"type": "skills_update_result", **summary},
        "chat": chat,
        "status": _Noop(),
        "term": _Noop(),
        "server_id": "srv",
    }
    exec(compile(module, "<spliced-from-app.py>", "exec"), ns)
    assert chat.rows, "该分支没有打印任何一行 —— 探针没跑到渲染"
    return chat.rows[-1][1]


# ── producer: the summary can say a check did not happen ─────────────

def test_a_failed_release_read_is_named_and_not_counted_as_clean(tmp_home, with_cli):
    ensure_catalog_file()
    write_state({"browser-harness": {
        "version": "0.1.3", "installed_at": "2026-10-04T00:00:00+08:00", "managed": True}})

    result = _run(installer.update_managed_skills(
        runner=None, http_get=_FailingHttp()))

    assert result["unreadable"] == ["browser-harness"], (
        "读不出来的检查必须点名 —— 否则调用方只能看见一个空结果并把它读成「已完成」"
    )
    assert result["updated"] == [] and result["errors"] == []
    assert installer.read_state()["browser-harness"]["version"] == "0.1.3"


def test_a_managed_entry_the_catalog_dropped_is_named_too(tmp_home, with_cli):
    ensure_catalog_file()
    write_state({"ghost": {"version": "0.1.0", "managed": True}})

    result = _run(installer.update_managed_skills(http_get=_UpToDateHttp()))

    assert result["unknown"] == ["ghost"]
    assert result["checked"] == 1  # 仍然是「有一条被管理的」，但检查没有发生


def test_a_check_that_really_ran_names_nobody(tmp_home, with_cli):
    """控制腿：真跑过的检查两个名单都是空的 —— 上面的点名不是「总是点名」。"""
    ensure_catalog_file()
    write_state({"browser-harness": {
        "version": "0.1.8", "installed_at": "2026-10-04T00:00:00+08:00", "managed": True}})

    result = _run(installer.update_managed_skills(http_get=_UpToDateHttp()))

    assert result["unreadable"] == [] and result["unknown"] == []
    assert result["updated"] == [] and result["skipped"] == [] and result["errors"] == []


def test_the_failure_summary_has_the_same_shape(tmp_home, monkeypatch):
    """失败路径也是这份契约的一部分：形状不同，调用方就会对两种结果读法不同。"""
    async def _boom(*_a, **_k):
        raise RuntimeError("update check exploded")

    monkeypatch.setattr(installer, "update_managed_skills", _boom)
    result = _run(installer.run_update_check_once())

    assert result["error"] == "update check failed"
    assert result["unreadable"] == [] and result["unknown"] == []
    assert set(result) >= {"checked", "updated", "skipped", "errors", "unreadable", "unknown"}


# ── consumer: the row refuses a verdict it did not measure ───────────

def test_an_unreadable_check_does_not_print_all_up_to_date():
    row = _render({"checked": 1, "updated": [], "skipped": [],
                   "errors": [], "unreadable": ["browser-harness"]})

    assert "All up to date" not in row, f"读不出来的检查不是干净的检查：{row!r}"
    assert "browser-harness" in row, f"而且必须点名：{row!r}"


def test_an_unknown_entry_does_not_print_all_up_to_date():
    row = _render({"checked": 1, "updated": [], "skipped": [],
                   "errors": [], "unknown": ["ghost"]})

    assert "All up to date" not in row, f"没有目录项就没有比较过：{row!r}"
    assert "ghost" in row


def test_a_clean_check_still_says_all_up_to_date():
    """控制腿：真的检查完了、且没有更新项，这句话仍然要说 —— 修的是「没量就说」，不是这句话本身。"""
    row = _render({"checked": 1, "updated": [], "skipped": [],
                   "errors": [], "unreadable": [], "unknown": []})

    assert "All up to date." in row


# ── composed: the real summary into the real row ─────────────────────

def test_the_summary_of_a_dead_api_does_not_render_as_all_up_to_date(tmp_home, with_cli):
    """端到端：生产者给的那份摘要，喂进消费者自己的那一段代码。"""
    ensure_catalog_file()
    write_state({"browser-harness": {
        "version": "0.1.3", "installed_at": "2026-10-04T00:00:00+08:00", "managed": True}})

    summary = _run(installer.update_managed_skills(http_get=_FailingHttp()))
    row = _render(summary)

    assert "All up to date" not in row, f"生产者的摘要 + 消费者自己的渲染 = 谎：{row!r}"
    assert "browser-harness" in row
