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


def _load_guard():
    """Load the guard module so its function can be driven in both states.

    By path, the same way `test_doc_counts.py` loads itself: pytest imports
    these files as `tests.test_doc_counts`, and a plain top-level name does not
    resolve.
    """
    spec = importlib.util.spec_from_file_location("_doc_count_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _guard_assert_messages() -> list[str]:
    """Every assertion message in the count guard, as static text.

    Read from the AST, not by regex, so a reworded message is still read as the
    message it is. Adjacent string constants inside an f-string are joined; the
    `{doc}` / `{documented}` placeholders are `FormattedValue` nodes and drop
    out, which is fine - the hint this test cares about is static text.
    """
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "test_python_count_matches_docs"
    )
    messages = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assert) and node.msg is not None:
            messages.append(
                "".join(
                    part.value
                    for part in ast.walk(node.msg)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return messages


def test_guard_failure_names_a_runnable_repair_command(mod, capsys) -> None:
    """The guard must fail with a command the host can actually run.

    Reporting drift without naming the repair path leaves the host to find the
    tool - and the tool exists precisely for this failure. Both halves are
    needed, so both are asserted: the path in the message must exist, and
    `--write` must be a flag the tool really accepts. A hint spelled
    consistently is not a repair path; a repair path that only exists in a
    comment is not one either.

    Necessary, not sufficient: this reads the hint out of the guard's source, and
    a string that lives in an assert the drift path never reaches satisfies it.
    `test_guard_drift_message_names_the_repair_command` is the half that drives
    the guard and checks the message a host actually sees.
    """
    messages = _guard_assert_messages()
    hinted = [m for m in messages if "scripts/check-doc-count.py" in m]
    assert hinted, (
        "the guard's failure message no longer names the repair tool, so a host "
        f"who hits it in CI has no next step; messages found: {messages}"
    )
    assert any("--write" in m for m in hinted), (
        f"the hint does not offer the repair flag, only a path: {hinted[0]!r}"
    )
    assert SCRIPT.exists(), f"the guard points at {SCRIPT}, which does not exist"

    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--help"])
    assert excinfo.value.code == 0
    assert "--write" in capsys.readouterr().out


def test_guard_drift_message_names_the_repair_command(mod, monkeypatch) -> None:
    """The hint must be in the message a host actually gets, in the drift state.

    The static checks next door read the hint out of the guard's source, which
    makes them necessary but not sufficient. Measured (cyc20260910-192726): moving
    that same hint string onto the assert that fires when the *anchor* is missing
    left both of them green, while a drifting host saw a message with no hint at
    all - the exact regression this branch exists to prevent. So drive the guard
    the way drift drives it: stub the collected count (no subprocess, ~0s) and
    read the raised message.
    """
    guard = _load_guard()
    canonical = "uv run --no-sync python3 scripts/check-doc-count.py --write"
    documented = mod.documented_count((REPO_ROOT / "Agent.md").read_text(encoding="utf-8"))

    # Positive state: a consistent tree leaves the guard silent. Without this
    # half, a guard that compared nothing would still pass the negative one.
    monkeypatch.setattr(guard, "_collected_pytest_count", lambda: documented)
    assert guard.test_python_count_matches_docs() is None

    # Negative state: one test's worth of drift - the real incident shape.
    monkeypatch.setattr(guard, "_collected_pytest_count", lambda: documented + 1)
    with pytest.raises(AssertionError) as excinfo:
        guard.test_python_count_matches_docs()
    message = str(excinfo.value)
    assert f"Fix with: {canonical}" in message, (
        "the message a host sees on drift no longer names the repair command; "
        f"it says: {message!r}"
    )


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
        # The tool prints paths and counts; decode them as UTF-8, never with the
        # host locale (the class tests/test_script_decode_is_locale_independent.py
        # exists to keep out of the tree).
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: Agent.md documents" in proc.stdout


# --- the repair hint itself must be a command that runs -----------------------


def test_every_repair_hint_prints_one_runnable_command(mod, monkeypatch, tmp_path, capsys) -> None:
    """Every site that tells someone how to repair the count must agree.

    Measured (cycle cyc20260910-191242, main clone): the canonical form exits 0,
    while the bare `python3` form the drift hint used to print exits 2 having
    measured nothing - the host's `python3` cannot import pytest. The guard's
    message, this tool's error hint and Agent.md already used the canonical form,
    so the drift hint was the one site sending the reader into a second failure.
    A hint that fails is worse than no hint: it looks like a next step.

    All four sites are checked here (tool source, tool drift output, tool error
    output, guard message, plus Agent.md), because the defect was precisely a
    disagreement between them.
    """
    canonical = "uv run --no-sync python3 scripts/check-doc-count.py"
    assert mod.INVOCATION == canonical

    source = SCRIPT.read_text(encoding="utf-8")
    hits = list(re.finditer(r"python3 scripts/check-doc-count\.py", source))
    assert hits, "the tool no longer mentions its own invocation at all"
    for hit in hits:
        prefix = source[max(0, hit.start() - len("uv run --no-sync ")) : hit.start()]
        assert prefix == "uv run --no-sync ", (
            "a hint in scripts/check-doc-count.py spells the invocation without the "
            f"project runner: ...{source[max(0, hit.start() - 40) : hit.end() + 20]!r}"
        )

    doc = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    assert canonical in doc, "Agent.md no longer documents the canonical invocation"
    assert any(
        f"Fix with: {canonical} --write" in message
        for message in _guard_assert_messages()
    ), "the pytest guard's failure message no longer prints the canonical fix command"

    # Drift state, for real: the printed line must be the runnable one.
    mod.DOC = _doc(tmp_path, 1307)
    monkeypatch.setattr(mod, "measured_count", lambda: 1308)
    assert mod.main([]) == 1
    assert f"Fix with: {canonical} --write" in capsys.readouterr().out

    # Error state: the interpreter advice must be the same spelling. A fresh
    # module, because `measured_count` on `mod` is stubbed above to reach the
    # drift path, and this half needs the real function to run.
    fresh = _load_module()

    class _Proc:
        returncode = 4
        stdout = ""
        stderr = ""

    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(fresh.DocCountError) as excinfo:
        fresh.measured_count()
    assert f"`{canonical}`" in str(excinfo.value)


def test_collect_output_is_decoded_independently_of_the_locale(mod) -> None:
    """The collected count must not depend on the host's locale codec.

    The sibling tool's identical defect was measured and filed as issue #1132:
    decoding with the locale codec left `proc.stdout` as `None` once subprocess's
    reader thread swallowed the `UnicodeDecodeError`, and the concatenation
    raised a bare `TypeError` past every handler in `main()` (which catches
    `DocCountError` and `OSError` only). pytest's own output is ASCII today
    (measured: 0 non-ASCII lines in 1337), but a collected id or warning is not
    under this repo's control - one non-ASCII byte on a cp936 host would produce
    a traceback instead of the count.
    """
    child = "import sys; sys.stdout.buffer.write(b'\\xb9 7 tests collected\\n')"
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None, "the fix no longer protects the stream"
    match = mod.COLLECTED.search(proc.stdout)
    assert match, f"the summary no longer parses: {proc.stdout!r}"
    assert int(match.group(1)) == 7


def test_unreadable_collect_output_raises_the_tools_own_error(mod, monkeypatch) -> None:
    """A `None` stream is reported in this tool's vocabulary, never concatenated."""

    class _Proc:
        returncode = 0
        stdout = None
        stderr = None

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    with pytest.raises(mod.DocCountError, match="no readable output"):
        mod.measured_count()
