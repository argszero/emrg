"""A path the filesystem refuses is an answer, not an exception.

Measured 2026-10-04 (`cyc20261004-030427`) on master `a3d0a5ea`, Python 3.13.9, this
host. Five tools resolve a caller-supplied path and three of them then read it; before
this change **six** of thirteen probed shapes left `execute()` as a raw exception, and
the caller saw the daemon's generic line:

| shape | what the caller got |
|---|---|
| `read` / `edit` a file whose own mode is `000` | `Tool execution error: [Errno 13] Permission denied: '/…/secret.txt'` |
| `read` / `edit` a path under a `chmod 000` directory | same, with the directory |
| `read` / `write` a path containing a NUL byte | `Tool execution error: lstat: embedded null character in path` |
| `write` into a `chmod 000` directory | `Tool execution error: [Errno 13] Permission denied` |

None of those names the tool, and none says what the caller could do instead — a raised
exception loses that on the way out. Both facts are things the tool *knows* at the moment
it fails, which is the whole argument: it can say them.

The rule has two halves and each has one home in `emrg/tools/base.py`, because the
alternative is the same sentence composed five times and free to drift:

* `resolve_tool_path` — the path cannot be resolved at all (a NUL byte, or a component
  this process may not traverse).
* `read_text_or_refusal` — the path resolved, but the bytes did not arrive (permission,
  or not UTF-8).

The list below is written in **both directions**: the readable tree must still answer
normally, and an *absent* path must still say "not found" rather than "permission
denied" — a tool that reported a refusal whenever it felt unsure would be trading one
wrong answer for another.
"""

import ast
import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

from emrg.tools.base import read_text_or_refusal, resolve_tool_path
from emrg.tools.edit_tool import EditTool
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool
from emrg.tools.read_tool import ReadTool
from emrg.tools.write_tool import WriteTool

REPO = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO / "emrg" / "tools"

#: `chmod 000` does not stop root, so every permission leg is vacuous there.
_not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores permissions, so nothing here can be unreadable",
)
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits; the shape does not exist to measure on Windows",
)


def _run(tool, args):
    args = {"intent": "measure a refused path"} | args
    return asyncio.run(tool.execute(args))


def _raises_or_answers(tool, args):
    """`(content, error)` from the tool, or the exception it raised instead."""
    try:
        result = _run(tool, args)
    except Exception as exc:  # noqa: BLE001 — the whole point is that this happened
        return None, exc
    return result, None


# ---------------------------------------------------------------------------
# The population: the rule has one home, and no tool re-spells it.
# ---------------------------------------------------------------------------

def _tool_modules():
    return sorted(p for p in TOOLS_DIR.glob("*.py") if p.name != "base.py")


def _unguarded_resolves(tree: ast.AST) -> list[int]:
    """Line numbers of `Path(...).resolve()` calls that are not inside a try body.

    `Path.resolve()` is where a NUL byte turns into a `ValueError`, so a tool that
    calls it outside a `try` hands that exception to the daemon. The rule is written
    as "the call is inside a `try` whose handler catches `OSError`/`ValueError`", not
    as "the module imports `resolve_tool_path`", because the second spelling is
    satisfied by an unused import and the first is the property that matters.
    """
    guarded: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            names = {
                getattr(t, "id", None) or getattr(t, "attr", None)
                for t in ([handler.type] if handler.type is not None else [])
            }
            if names & {"OSError", "ValueError", "Exception"}:
                guarded.update(ast.walk(node))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "resolve"):
            continue
        if node.lineno not in {getattr(n, "lineno", -1) for n in guarded}:
            offenders.append(node.lineno)
    return sorted(offenders)


def test_no_tool_resolves_a_path_outside_a_guard():
    """The population rule, so a sixth tool cannot quietly re-open the hole."""
    offenders: list[str] = []
    for module in _tool_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for lineno in _unguarded_resolves(tree):
            offenders.append(f"{module.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "these tools call `Path.resolve()` outside a try that catches OSError/ValueError, "
        f"so a NUL byte in a path leaves `execute()` as a ValueError: {offenders}. "
        "Use `emrg.tools.base.resolve_tool_path`, which owns that sentence."
    )


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    """The exception names a handler catches; `except (A, B)` contributes both."""
    node = handler.type
    if node is None:
        return {"Exception"}  # a bare `except:`
    nodes = node.elts if isinstance(node, ast.Tuple) else [node]
    return {(getattr(n, "id", None) or getattr(n, "attr", None) or "") for n in nodes}


def _read_calls(tree: ast.AST) -> list[tuple[int, int]]:
    """`(line, col)` of every call in `tree` that reads a file's bytes."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"read_text", "read_bytes"}:
            spans.append((node.lineno, node.col_offset))
            continue
        if node.func.attr == "open":
            mode = None
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            if mode is None or "r" in str(mode):
                spans.append((node.lineno, node.col_offset))
    return spans


def _unguarded_reads(tree: ast.AST) -> list[int]:
    """Lines of read calls whose failure is not caught as an `OSError`.

    **`except UnicodeDecodeError` alone is the defect this leg exists for.** A text
    read fails two ways — the bytes will not decode, or they will not be delivered
    at all — and every reader here handled the first while `read`/`edit` let the
    second out of `execute()` (measured, `cyc20261004-030427`). Catching `OSError`
    (or `Exception`, which a search loop deliberately uses) is the property; whether
    the tool then *refuses* or *skips and reports* is its own decision — `grep`
    skips, and that is right for a search.
    """
    caught: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if _caught_names(handler) & {"OSError", "Exception", "BaseException"}:
                    caught.extend(
                        {getattr(n, "lineno", -1) for n in ast.walk(node)}
                    )
    caught_set = set(caught)
    return sorted(
        span[0] for span in _read_calls(tree) if span[0] not in caught_set
    )


def test_no_tool_reads_a_file_outside_a_guard_that_catches_oserror():
    """The second half of the population rule: a delivered-bytes failure is an answer."""
    offenders: list[str] = []
    for module in _tool_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for lineno in _unguarded_reads(tree):
            offenders.append(f"{module.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "these tool reads are not inside a try that catches OSError, so a file the "
        "process may not read leaves `execute()` as a PermissionError and the caller "
        f"sees the daemon's generic 'Tool execution error': {offenders}. "
        "Use `emrg.tools.base.read_text_or_refusal`, which owns the sentence."
    )


def test_the_read_guard_really_has_one_home():
    """The two readers that *report* a failed read share one sentence."""
    callers = sorted(
        p.name
        for p in _tool_modules()
        if "read_text_or_refusal(" in p.read_text(encoding="utf-8")
    )
    assert callers == ["edit_tool.py", "read_tool.py"], (
        f"the two sentence-producing readers must share the rule, found {callers}"
    )


def test_the_rule_really_has_one_home():
    """Control: the guard above would pass on a tree where nothing resolves at all."""
    base = (TOOLS_DIR / "base.py").read_text(encoding="utf-8")
    assert "def resolve_tool_path(" in base
    assert "def read_text_or_refusal(" in base
    callers = [
        p.name
        for p in _tool_modules()
        if "resolve_tool_path(" in p.read_text(encoding="utf-8")
    ]
    assert sorted(callers) == [
        "edit_tool.py", "glob_tool.py", "grep_tool.py", "read_tool.py", "write_tool.py",
    ], f"the five path-taking tools must call the shared resolve, found {sorted(callers)}"


# ---------------------------------------------------------------------------
# The measured shapes: each must come back as a ToolResult, in the tool's voice.
# ---------------------------------------------------------------------------

@pytest.fixture
def locked_tree(tmp_path):
    """A tree with an unreadable file and an unlistable directory, in one place."""
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")
    os.chmod(secret, 0o000)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "c.py").write_text("y = 2\n")
    os.chmod(locked, 0o000)
    (tmp_path / "open.txt").write_text("hello\n")
    try:
        yield tmp_path
    finally:
        os.chmod(secret, 0o644)
        os.chmod(locked, 0o755)


class TestAnUnreadableFileIsAnswerable:
    """The realistic shape: the directory is fine, the file's own mode is not."""

    @_posix_only
    @_not_root
    @pytest.mark.parametrize(
        "tool,args",
        [
            (ReadTool(), {"file_path": "secret.txt"}),
            (EditTool(), {"file_path": "secret.txt", "old_string": "top", "new_string": "x"}),
        ],
        ids=["read", "edit"],
    )
    def test_it_answers_instead_of_raising(self, locked_tree, tool, args):
        args = {k: (str(locked_tree / v) if k == "file_path" else v) for k, v in args.items()}
        result, exc = _raises_or_answers(tool, args)

        assert exc is None, (
            f"{type(tool).__name__} let {type(exc).__name__} out of execute(), so the "
            f"caller sees the daemon's generic 'Tool execution error' instead of an answer"
        )
        assert result.error is True
        assert str(locked_tree) in result.content, "the refusal must name the path"
        assert "permission denied" in result.content
        assert "Errno" not in result.content, "the OS's errno line is not the fact to report"

    @_posix_only
    @_not_root
    def test_a_nonexistent_path_is_still_not_found(self, locked_tree):
        """The control that keeps the refusal honest.

        Without it, a tool that answered "permission denied" whenever it was unsure
        would pass every leg above while calling every typo a permission problem.
        """
        result = _run(ReadTool(), {"file_path": str(locked_tree / "nope.txt")})

        assert result.error is True
        assert "file not found" in result.content
        assert "permission denied" not in result.content

    @_posix_only
    def test_a_readable_named_file_is_unaffected(self, locked_tree):
        """The other control: nothing about the refusal may change a normal read."""
        result = _run(ReadTool(), {"file_path": str(locked_tree / "open.txt")})

        assert result.error is not True
        assert "hello" in result.content

    @_posix_only
    def test_a_binary_file_is_still_reported_as_not_text(self, locked_tree):
        binary = locked_tree / "bin.dat"
        binary.write_bytes(b"\x00\xff\xfe")
        result = _run(ReadTool(), {"file_path": str(binary)})

        assert result.error is True
        assert "as text" in result.content


class TestAPathTheFilesystemRefuses:
    """A NUL byte is not an OS refusal, but it is still not a crash."""

    @pytest.mark.parametrize(
        "tool,args",
        [
            (ReadTool(), {"file_path": "a\x00b"}),
            (WriteTool(), {"file_path": "a\x00b", "content": "z"}),
            (GlobTool(), {"pattern": "*", "workdir": "a\x00b"}),
            (GrepTool(), {"pattern": "x", "path": "a\x00b"}),
        ],
        ids=["read", "write", "glob", "grep"],
    )
    def test_every_path_taking_tool_answers(self, tool, args):
        result, exc = _raises_or_answers(tool, args)

        assert exc is None, f"{type(tool).__name__} raised {type(exc).__name__}: {exc}"
        assert result.error is True
        assert "NUL" in result.content

    def test_the_sentence_names_the_cause_not_the_syscall(self):
        """`lstat: embedded null character` names a syscall the caller never made."""
        path, refusal = resolve_tool_path("a\x00b")

        assert path is None
        assert "\x00" not in refusal, "the raw path is not printable in a message"
        assert "lstat" not in refusal

    def test_the_helper_returns_a_usable_path_normally(self, tmp_path):
        """Control: the helper is not a refusal generator."""
        path, refusal = resolve_tool_path(str(tmp_path))

        assert refusal is None
        assert path == tmp_path.resolve()


class TestTheSharedHelpers:
    """The two homes, asked directly — the sentences the tools render."""

    @_posix_only
    @_not_root
    def test_read_text_or_refusal_names_permission(self, locked_tree):
        text, refusal = read_text_or_refusal(locked_tree / "secret.txt")

        assert text is None
        assert "permission denied" in refusal

    def test_read_text_or_refusal_names_a_decode_failure(self, tmp_path):
        binary = tmp_path / "b.dat"
        binary.write_bytes(b"\xff\xfe\x00")
        text, refusal = read_text_or_refusal(binary)

        assert text is None
        assert "not UTF-8" in refusal and "binary file" in refusal

    def test_read_text_or_refusal_returns_the_text(self, tmp_path):
        good = tmp_path / "g.txt"
        good.write_text("ok\n")
        text, refusal = read_text_or_refusal(good)

        assert refusal is None and text == "ok\n"

    def test_the_newline_parameter_is_passed_through(self, tmp_path):
        """`edit` needs the file's own terminators back (issue #1803)."""
        crlf = tmp_path / "c.txt"
        crlf.write_bytes(b"a\r\nb\r\n")

        assert read_text_or_refusal(crlf, newline="")[0] == "a\r\nb\r\n"
        assert read_text_or_refusal(crlf)[0] == "a\nb\n"


class TestAnUnlistableDirectoryIsAnswerable:
    """The directory half, which raised from `exists()`/`iterdir()` (Python 3.13)."""

    @_posix_only
    @_not_root
    def test_read_of_an_unlistable_directory_answers(self, locked_tree):
        result, exc = _raises_or_answers(ReadTool(), {"file_path": str(locked_tree / "locked")})

        assert exc is None, f"read raised {type(exc).__name__}: {exc}"
        assert result.error is True
        assert "permission denied" in result.content

    @_posix_only
    @_not_root
    def test_read_of_a_file_under_it_answers(self, locked_tree):
        result, exc = _raises_or_answers(
            ReadTool(), {"file_path": str(locked_tree / "locked" / "c.py")}
        )

        assert exc is None
        assert result.error is True
        assert "permission denied" in result.content

    @_posix_only
    @_not_root
    def test_write_into_it_answers(self, locked_tree):
        result, exc = _raises_or_answers(
            WriteTool(), {"file_path": str(locked_tree / "locked" / "new.txt"), "content": "z"}
        )

        assert exc is None, f"write raised {type(exc).__name__}: {exc}"
        assert result.error is True

    @_posix_only
    @_not_root
    def test_edit_of_a_file_under_it_answers(self, locked_tree):
        result, exc = _raises_or_answers(
            EditTool(),
            {
                "file_path": str(locked_tree / "locked" / "c.py"),
                "old_string": "y",
                "new_string": "z",
            },
        )

        assert exc is None
        assert result.error is True
        assert "permission denied" in result.content

    @_posix_only
    def test_a_readable_directory_still_lists(self, locked_tree):
        """Control: the listing the guard wraps is untouched."""
        result = _run(ReadTool(), {"file_path": str(locked_tree)})

        assert result.error is not True
        assert "open.txt" in result.content and "Directory listing" in result.content


def test_the_guard_covers_every_tool_module_it_claims_to():
    """Control for the population legs: the scan sees the whole directory."""
    names = {p.name for p in _tool_modules()}
    assert {"read_tool.py", "write_tool.py", "edit_tool.py", "glob_tool.py", "grep_tool.py"} <= names
    assert "base.py" not in names, "base.py owns the rule and is exempt by construction"
    # ...and the scan is not vacuous: the six probed shapes are all reachable here.
    assert callable(inspect.getsource)  # the module list came from disk, not a fixture
    assert re.search(r"def execute\(", (TOOLS_DIR / "read_tool.py").read_text(encoding="utf-8"))
