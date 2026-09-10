"""Tests for scripts/check-doc-count.py - the Agent.md Python-count sync tool.

Background (cycle cyc20260910-175308)
-------------------------------------
`Agent.md` documents the collected Python test count, and
`tests/test_doc_counts.py::test_python_count_matches_docs` fails when the doc and
the tree disagree. On 2026-09-10 the count line conflicted in **four** separate
merges (`#1119`, `#1120`, `#1121`, `#1122`), each carrying a different number;
master went 1277 -> 1283 -> 1284 depending on which branch landed. In a
conflicted merge both sides are stale by construction, so the only correct value
is the one you measure on the merged tree - which is what this tool does.

Both states are pinned here, never inferred from the failure case alone (#455):

* negative - a consistent doc reports OK and writes nothing;
* positive - a stale doc reports the measured value, and `--write` repairs it.

Nothing here runs the real pytest collection except the one integration test
that invokes the tool on the real tree; the unit tests inject the measurement.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-doc-count.py"
GUARD = REPO_ROOT / "tests" / "test_doc_counts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_doc_count", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _doc(tmp_path: Path, count: int | None) -> Path:
    """A minimal Agent.md copy.

    `count=None` writes a non-numeric placeholder in the same position, which is
    the "anchor present but unreadable" state the tool must reject.
    """
    number = str(count) if count is not None else "TBD"
    line = f"Python: `uv run pytest tests/ -v` ({number}) - import check: x\n"
    path = tmp_path / "Agent.md"
    path.write_text("# Agent.md\n\n" + line + "Renderer: other counts here\n")
    return path


def _guard_pattern() -> str:
    """The doc-count pattern out of the guard itself, so the two cannot drift apart.

    The guard may hold the regex either way, and may reach it through however
    many module-level helpers it likes:

    * inline - `re.search(r"...", text)` written directly in
      `test_python_count_matches_docs` (how the guard read until cycle
      `cyc20260910-213455`);
    * named constant, reached through a helper chain - e.g. the test calls
      `_single_documented_python_count`, which calls
      `_documented_python_counts`, which applies a module-level
      `PYTHON_COUNT_LINE = re.compile(r"...")` (how it reads now).

    So the search follows module-level *functions* as well as constants. That
    collects more than one candidate: the chain also reaches
    `_collected_pytest_count`, whose regex parses pytest's *output*
    (`"(\\d+) tests? collected"`) and never matches the doc. Candidates are
    therefore discriminated by the very property this test asserts - matching
    the real `Agent.md` - and the result is required to be unique. If two
    candidates ever both match, this fails instead of silently picking one.

    Hardcoding the constant's name would break the "cannot drift apart" promise
    the moment someone renames it: the extractor would deny its existence.
    """
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert "test_python_count_matches_docs" in functions, (
        "the guard function was renamed; point this extractor at the new one"
    )

    seen: set[str] = set()
    queue = ["test_python_count_matches_docs"]
    candidates: list[tuple[str, str]] = []
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Call):
                continue
            # inline literal: `re.search(r"...", ...)` (or any `.search(...)`)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "search"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                candidates.append((f"{name}: re.search", node.args[0].value))
                continue
            # a module-level compiled constant used as `NAME.search(...)` /
            # `NAME.findall(...)` / any other regex method
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
            ):
                compiled = _compiled_pattern_for(tree, node.func.value.id)
                if compiled is not None:
                    candidates.append((node.func.value.id, compiled))
                    continue
            # module-level helper referenced by name
            if isinstance(node.func, ast.Name) and node.func.id in functions:
                queue.append(node.func.id)

    text = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    matching = [(where, pat) for where, pat in candidates if re.search(pat, text)]
    assert len(matching) == 1, (
        "expected exactly one pattern reachable from the guard to match Agent.md's "
        f"count line, found {len(matching)}: {[w for w, _ in matching]}. All "
        f"candidates reached: {[w for w, _ in candidates]}. This test cannot tell "
        "which regex is the doc anchor, so it refuses to guess."
    )
    return matching[0][1]


def _compiled_pattern_for(tree: ast.Module, name: str) -> str | None:
    """Return the literal pattern of a module-level `name = re.compile(r"...")`."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == name]
        if not targets or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "compile"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            return call.args[0].value
    return None


def test_tool_pattern_agrees_with_the_guard(mod) -> None:
    """The guard and this tool must key on the same phrase in the real Agent.md.

    Without this, the guard's anchor could be edited while the tool kept matching
    the old one - the tool would then report OK for a line nobody checks.
    """
    guard = re.compile(_guard_pattern())
    text = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    guard_match = guard.search(text)
    assert guard_match, "the guard's own pattern no longer matches Agent.md"
    # Fail with the reason, not an IndexError: a pattern edit that drops the
    # capture group is exactly the failure this test exists to catch -- measured,
    # the first version of this test raised `IndexError: no such group` here and
    # said nothing about the guard having changed.
    assert guard.groups >= 1, (
        "the guard's count pattern no longer captures the number as group 1; "
        "this test compares through that group, so it must be updated alongside "
        f"the guard: {_guard_pattern()!r}"
    )
    assert int(guard_match.group(1)) == mod.documented_count(text)


def test_documented_count_reads_the_real_doc(mod, tmp_path) -> None:
    assert mod.documented_count(_doc(tmp_path, 1284).read_text()) == 1284


def test_missing_anchor_fails_loud(mod, tmp_path) -> None:
    with pytest.raises(mod.DocCountError, match="no documented Python count"):
        mod.documented_count(_doc(tmp_path, None).read_text())


def test_duplicate_anchor_fails_loud(mod, tmp_path) -> None:
    """Two counts in one doc = ambiguity; the tool must refuse, not pick one."""
    text = _doc(tmp_path, 1284).read_text()
    with pytest.raises(mod.DocCountError, match="2 documented Python counts"):
        mod.documented_count(text + "Python: `uv run pytest tests/ -v` (999)\n")


def test_patch_changes_only_the_number(mod, tmp_path) -> None:
    text = _doc(tmp_path, 1249).read_text()
    patched = mod.patch(text, 1284)
    assert patched != text
    assert "1249" not in patched
    # Everything else - including the surrounding prose and the other counts - is
    # untouched, which is the property that makes --write safe on a doc this size.
    assert patched == text.replace("(1249)", "(1284)")


def test_missing_anchor_is_a_tool_error_not_a_crash(mod, tmp_path) -> None:
    mod.DOC = _doc(tmp_path, None)
    assert mod.main([]) == 2


def test_unparsable_measurement_fails_loud(mod, monkeypatch) -> None:
    class _Proc:
        returncode = 0
        stdout = "no summary line here"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(mod.DocCountError, match="could not parse a collected count"):
        mod.measured_count()


def test_failed_collection_fails_loud(mod, monkeypatch) -> None:
    class _Proc:
        returncode = 4
        stdout = "ERROR: usage error"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(mod.DocCountError, match="collect-only failed") as excinfo:
        mod.measured_count()
    # The host-facing half: a bare `python3` without pytest must be told how to
    # run this, not left with "No module named pytest" and no next step.
    assert "uv run --no-sync python3 scripts/check-doc-count.py" in str(excinfo.value)


def test_consistent_doc_reports_ok_and_writes_nothing(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 1284)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_count", lambda: 1284)

    assert mod.main([]) == 0
    assert "OK: Agent.md documents 1284 collected Python tests" in capsys.readouterr().out
    assert doc.read_text() == before


def test_drift_is_reported_with_the_measured_value(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 1249)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_count", lambda: 1284)

    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "documents 1249 Python tests but 1284 are collected" in out
    assert "--write" in out
    assert doc.read_text() == before, "a reporting run must never write"


def test_write_repairs_the_doc(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 1249)
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_count", lambda: 1284)

    assert mod.main(["--write"]) == 0
    assert "updated Agent.md: 1249 -> 1284" in capsys.readouterr().out
    assert mod.documented_count(doc.read_text()) == 1284


def test_dry_run_reports_but_does_not_write(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 1249)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_count", lambda: 1284)

    assert mod.main(["--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert doc.read_text() == before


def test_write_and_dry_run_together_are_rejected() -> None:
    """The two modes contradict each other, so the pair must fail loud.

    Measured before this was enforced: `--write --dry-run` printed the dry-run
    line, wrote nothing and exited 0 -- the caller asked for a repair and the
    tool dropped the request without a word. That is the "silently reinterpret
    input" class this repo already rejected once (bump-version.py's `--check
    v0.2.94`, which discarded its argument and reported green about a version
    nobody asked about). argparse's own exit code for a usage error is 2, which
    matches this tool's convention for "cannot act on what you gave me".
    """
    with pytest.raises(SystemExit) as excinfo:
        mod_main = _load_module()
        mod_main.main(["--write", "--dry-run"])
    assert excinfo.value.code == 2


def test_real_tree_is_consistent() -> None:
    """Integration: the tool reports OK on the checked-in tree.

    Runs the real `pytest --collect-only` (1s), exactly as the guard it mirrors
    does. `--collect-only` collects and never executes test bodies.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: Agent.md documents" in proc.stdout
