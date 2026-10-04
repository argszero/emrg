"""Tests for scripts/check-doc-count.py - the "no stored test count" rule.

Background (cycles cyc20260910-175308 -> cyc20260913-132356)
-----------------------------------------------------------
`Agent.md` used to state the collected Python test count and this tool kept it
true. Measured 2026-09-13 on the live queue: 11 of 14 open PRs were conflicting
and **all 11 conflicted on that one line** — every PR that adds a test had to
rewrite the same derived number. The dangerous direction was the other one: two
PRs writing the *same* value merge cleanly and leave the merged tree stale
(measured: #1179 + #1180 both said 1601, the merged tree collected 1603, and no
conflict marker appeared anywhere).

So the tool changed subject rather than gaining a stronger check: it now reports
any tracked file that *states* the count, and measures on demand (`--measure`).

Both states are pinned here, never inferred from the failure case alone (#455):

* negative — the real tree states no count, and the rule is silent on the wording
  that replaced the statement;
* positive — every claim shape is reported, with file, line and shape.

The unit tests inject the tree; the two integration tests run on the real one.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-doc-count.py"
GUARD = REPO_ROOT / "tests" / "test_doc_counts.py"
CANONICAL = "uv run --no-sync python3 scripts/check-doc-count.py"

# The guard's test that carries the host-visible failure message. Named here so
# the AST reader below and a rename cannot disagree silently.
GUARD_TEST = "test_no_tracked_file_states_the_python_test_count"


def _load_guard():
    """Load the guard module so its message can be read and driven.

    By path, the same way `test_doc_counts.py` loads the tool: pytest imports
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


def _fake_tree(tmp_path: Path, name: str, text: str) -> Path:
    """A minimal tree to scan: the file's directory, nothing else."""
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _guard_assert_messages() -> list[str]:
    """The string constants in the guard test's assertion messages.

    Read by AST because the message is the product: a hint that lives in a
    comment, or in an assert the failure path never reaches, is not a hint. The
    driven half is `test_the_reported_claim_names_the_measurement_command`.
    """
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    funcs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == GUARD_TEST
    ]
    assert funcs, f"{GUARD_TEST} is gone from {GUARD.name}; point this reader at it"
    messages = []
    for node in ast.walk(funcs[0]):
        if isinstance(node, ast.Assert) and node.msg is not None:
            messages.append(
                "".join(
                    part.value
                    for part in ast.walk(node.msg)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return messages


# --- the rule: a stored count is reported, and the real tree has none ---------


def test_the_real_tree_states_no_count(mod, capsys) -> None:
    """The real tree must pass the rule this file exists to enforce."""
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "OK: no tracked file states the Python test count" in out, out


def test_measure_reports_the_real_count(mod, capsys) -> None:
    """`--measure` really measures — the doc names this command instead of a number.

    An integration test on purpose: the whole point of naming a command in the
    doc is that the command works, so it is run here against the real tree
    (`--collect-only`, ~10s) rather than modelled. Everything else in this file
    injects the tree so the unit tests stay instant.
    """
    assert mod.main(["--measure"]) == 0
    out = capsys.readouterr().out
    match = re.search(r"measured: (\d+) collected Python tests", out)
    assert match, out
    assert int(match.group(1)) > 100, out


def test_each_claim_shape_is_reported_with_its_location(
    mod, monkeypatch, tmp_path, capsys
) -> None:
    """Every way a count can be written down must be reported, not just the old one.

    The shapes are the ones this repo has actually carried: the `Agent.md` form
    (stored beside the command) and the `DEVELOPMENT.md` prose form ("currently
    681 items", off by 2.3x in a file no check read until issue #1158). A rule
    that only knew the first would have left the second in place while reporting
    `OK`.
    """
    claims = {
        "stored next to the test command": "Python: `uv run pytest tests/ -v` (1599) - import check: x",
        "parenthesised count": "# run tests (currently 681 items)",
        "'currently N' claim": "currently 681 items are collected",
        "bare 'N items' claim": "the suite holds 681 items",
        "count after a pytest command": "hint: pytest (681)",
    }
    body = "\n".join(f"line {i}: {claim}" for i, claim in enumerate(claims.values(), 1))
    _fake_tree(tmp_path, "docs.md", body + "\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "scanned_files", lambda: ["docs.md"])

    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL: 1 tracked file(s) state the Python test count" in out, out
    for shape in claims:
        assert f"[{shape}]" in out, f"{shape} was not reported:\n{out}"
    assert "docs.md:" in out, out
    # The remedy must be a command that runs, and the one the docs name.
    assert f"{CANONICAL} --measure" in out, out


def test_each_shape_is_recognised_by_the_rule_itself(mod) -> None:
    """The per-shape names come from the rule, so the message above cannot drift.

    A test that only read the CLI output could pass with the shapes in the wrong
    order and overlapping each other; this pins the rule's own verdict per line.
    """
    cases = {
        "Python: `uv run pytest tests/ -v` (1599)": "stored next to the test command",
        "(currently 681 items)": "parenthesised count",
        "currently 681 items remain": "'currently N' claim",
        "the suite holds 681 items": "bare 'N items' claim",
        "hint: pytest (681)": "count after a pytest command",
    }
    for line, shape in cases.items():
        found = mod.claims_in(line)
        assert [s for _, s, _ in found] == [shape], (line, found, shape)
    # One line, one claim: a line that matches several shapes must not be
    # reported several times, or the count in the message says nothing.
    assert len(mod.claims_in("python: pytest (681)")) == 1


def test_a_claim_is_reported_once_per_line(mod) -> None:
    """Two claims on two lines are two findings, numbered from 1."""
    found = mod.claims_in("clean\nPython: `uv run pytest tests/ -v` (1)\n")
    assert found == [(2, "stored next to the test command", "Python: `uv run pytest tests/ -v` (1)")]


def test_the_rule_skips_the_trees_that_must_spell_the_claim(mod, monkeypatch) -> None:
    """`tests/` and `scripts/` are excluded — measured, and bounded by one list.

    They are where the rule and its probes live, so they have to be able to write
    the claim out. Driven through `scanned_files` with a payload, so the exclusion
    is exercised rather than described; the "no exclusion may grow over a real
    doc" half is witnessed in `tests/test_doc_counts.py`.
    """

    class _Proc:
        returncode = 0
        stdout = "scripts/x.py\ntests/y.py\nemrg/z.py\nAgent.md\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    assert mod.scanned_files() == ["Agent.md", "emrg/z.py"]


def test_unlistable_files_fail_loud(mod, monkeypatch, tmp_path) -> None:
    """A checkout whose listing fails must say so, never walk or report `OK`.

    "I could not check" reported as healthy is how a broken tree reaches master:
    an empty scan and a clean tree are indistinguishable in the output. The
    fallback below (walk a tree with no `.git`) must not be reachable from a
    checkout, or the scan set would silently change meaning.
    """
    (tmp_path / ".git").mkdir()

    class _Proc:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(mod.DocCountError, match="could not list tracked files"):
        mod.scanned_files()


def test_a_tree_with_no_git_is_walked(mod, monkeypatch, tmp_path) -> None:
    """The `git archive` export `check-merge-sequence.py` judges has no `.git`.

    Measured while writing this rule: that caller extracts a merged tree into a
    temp directory and runs *the tree's own copy* of this guard there, so the
    tracked-files path cannot run at all. A guard that raised there would make
    every merge-sequence step a measurement error, and one that passed would be
    worse. An export holds only tracked content, so walking it sees the same
    files `git ls-files` would have listed.
    """
    _fake_tree(tmp_path, "docs.md", "no claim here\n")
    _fake_tree(tmp_path, "tests/test_x.py", "spelled in a fixture: (681 items)\n")
    _fake_tree(tmp_path, "scripts/y.py", "spelled in prose: (681 items)\n")
    _fake_tree(tmp_path, "emrg/z.py", "code\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.scanned_files() == ["docs.md", "emrg/z.py"]
    assert mod.offenders() == []


def test_the_walk_skips_build_output(mod, monkeypatch, tmp_path) -> None:
    """A non-checkout tree may still carry build output; it is not part of it.

    `dist/` and `node_modules/` hold vendored third-party sources, and a rule
    that read them would report claims nobody in this repo wrote.
    """
    _fake_tree(tmp_path, "docs.md", "clean\n")
    _fake_tree(tmp_path, "dist/runtime/lib/vendor.py", "pytest (681)\n")
    _fake_tree(tmp_path, "node_modules/pkg/readme.md", "681 items were run\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.scanned_files() == ["docs.md"]
    assert mod.offenders() == []


def test_a_checkout_is_scanned_through_git_not_by_walking(mod, monkeypatch, tmp_path) -> None:
    """The two paths must not be interchangeable in a checkout.

    Pinned on which path runs, not on the answer: an untracked file in a working
    checkout must be invisible *because it is untracked*, which is a different
    reason from it happening to be clean.
    """
    (tmp_path / ".git").mkdir()
    _fake_tree(tmp_path, "untracked.md", "Python: `uv run pytest tests/ -v` (1599)\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    class _Proc:
        returncode = 0
        stdout = "tracked.md\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    assert mod.scanned_files() == ["tracked.md"]


def test_an_unreadable_file_does_not_fail_the_scan(mod, monkeypatch, tmp_path) -> None:
    """A binary file is skipped, not fatal: a claim cannot live in bytes."""
    _fake_tree(tmp_path, "img.png", "")
    (tmp_path / "img.png").write_bytes(b"\xff\xfe\x00\x01")
    _fake_tree(tmp_path, "docs.md", "no claim here\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "scanned_files", lambda: ["img.png", "docs.md"])
    assert mod.offenders() == []


# --- resolving a conflicted count line ---------------------------------------


def _conflicted(ours: str, theirs: str) -> str:
    return (
        "before\n"
        "<<<<<<< HEAD\n"
        f"{ours}\n"
        "=======\n"
        f"{theirs}\n"
        ">>>>>>> feature\n"
        "after\n"
    )


def test_resolve_conflict_drops_the_claim_and_keeps_the_structure(mod, tmp_path) -> None:
    """The resolution is *deletion*, not a value: the count is a measurement.

    Two sides that are the same line but for the number carry no information
    about which number is right (both were measured on trees that no longer
    exist), so writing either one - or a fresh measurement - would be inventing a
    stored fact this rule exists to remove.
    """
    text = _conflicted(
        "Python: `uv run pytest tests/ -v` (1599) - import check: x",
        "Python: `uv run pytest tests/ -v` (1603) - import check: x",
    )
    resolved = mod.resolve_conflict(text)
    assert resolved == (
        "before\n"
        "Python: `uv run pytest tests/ -v` - import check: x\n"
        "after\n"
    )
    assert mod.claims_in(resolved) == []


def test_resolve_conflict_also_handles_one_side_already_count_free(mod) -> None:
    """The shape a rebase onto this change produces: numbered vs count-free.

    `git merge` against a master that no longer stores the count yields exactly
    one side with a number and one without. Removing the claim makes them equal,
    so the case is resolved by the same rule rather than by preferring a side.
    """
    text = _conflicted(
        "Python: `uv run pytest tests/ -v` (1599) - import check: x",
        "Python: `uv run pytest tests/ -v` - import check: x",
    )
    assert mod.resolve_conflict(text).count("import check: x") == 1
    assert "(1599)" not in mod.resolve_conflict(text)


def test_resolve_conflict_refuses_a_doc_with_no_conflict(mod) -> None:
    with pytest.raises(mod.DocCountError, match="no conflict block found"):
        mod.resolve_conflict("Python: `uv run pytest tests/ -v` (1599)\n")


def test_resolve_conflict_refuses_a_conflict_that_is_not_the_count_line(mod) -> None:
    """A different conflicted line is somebody else's decision, not this tool's."""
    text = _conflicted("def a(): pass", "def b(): pass")
    with pytest.raises(mod.DocCountError, match="is not the count line"):
        mod.resolve_conflict(text)


def test_resolve_conflict_refuses_when_the_sides_differ_by_more_than_the_count(mod) -> None:
    """Measured on the real 2026-09-13 queue: this is the shape it must refuse.

    Every in-flight branch's line carried the old *wording* plus a number, while
    the new form says something else entirely. Both sides differ in content, so
    taking the count-free one is a judgement (it drops whatever the other side
    was saying) — and a tool that makes that choice silently is the failure mode
    the whole conflict doctrine exists to prevent.
    """
    text = _conflicted(
        "Python: `uv run pytest tests/ -v` (1599) - import check: x",
        "Python: `uv run pytest tests/ -v` - count is measured, not stored",
    )
    with pytest.raises(mod.DocCountError, match="differ by more than the count"):
        mod.resolve_conflict(text)


def test_resolve_conflict_names_the_diff3_layout_it_cannot_parse(mod) -> None:
    text = (
        "before\n"
        "<<<<<<< HEAD\n"
        "Python: `uv run pytest tests/ -v` (1599)\n"
        "||||||| base\n"
        "Python: `uv run pytest tests/ -v` (1284)\n"
        "=======\n"
        "Python: `uv run pytest tests/ -v` (1307)\n"
        ">>>>>>> feature\n"
    )
    with pytest.raises(mod.DocCountError, match="diff3 layout"):
        mod.resolve_conflict(text)


def test_resolve_conflict_refuses_more_than_one_block(mod) -> None:
    text = _conflicted(
        "Python: `uv run pytest tests/ -v` (1599) - x",
        "Python: `uv run pytest tests/ -v` (1603) - x",
    ) + _conflicted("other", "other2")
    with pytest.raises(mod.DocCountError, match="2 conflict blocks"):
        mod.resolve_conflict(text)


def test_resolve_conflict_mode_writes_the_claim_free_doc(mod, monkeypatch, tmp_path, capsys) -> None:
    """The mode is driven end to end: markers gone, claim gone, structure kept."""
    _fake_tree(
        tmp_path,
        "Agent.md",
        _conflicted(
            "Python: `uv run pytest tests/ -v` (1599) - x",
            "Python: `uv run pytest tests/ -v` (1603) - x",
        ),
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.main(["--resolve-conflict"]) == 0
    written = (tmp_path / "Agent.md").read_text(encoding="utf-8")
    assert "<<<<<<<" not in written and ">>>>>>>" not in written, written
    assert "(1599)" not in written and "(1603)" not in written, written
    assert written == "before\nPython: `uv run pytest tests/ -v` - x\nafter\n"
    out = capsys.readouterr().out
    assert "count claim dropped" in out, out


def test_resolve_conflict_mode_leaves_a_conflict_free_doc_alone(mod, monkeypatch, tmp_path) -> None:
    """Refusing must not touch the file: a resolver that writes on the way to an
    error can turn a readable conflict into an unreadable one."""
    _fake_tree(tmp_path, "Agent.md", "Python: `uv run pytest tests/ -v` (1599) - x\n")
    before = (tmp_path / "Agent.md").read_text(encoding="utf-8")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.main(["--resolve-conflict"]) == 2
    assert (tmp_path / "Agent.md").read_text(encoding="utf-8") == before


def test_resolve_conflict_is_mutually_exclusive_with_measure() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _load_module().main(["--resolve-conflict", "--measure"])
    assert excinfo.value.code == 2


def test_the_real_tree_has_no_conflict_to_resolve(mod) -> None:
    with pytest.raises(mod.DocCountError, match="no conflict block found"):
        mod.resolve_conflict((REPO_ROOT / "Agent.md").read_text(encoding="utf-8"))


# --- the reported claim must name a command that runs ------------------------


def test_the_reported_claim_names_the_measurement_command(mod, monkeypatch, tmp_path, capsys) -> None:
    """The message a host sees must offer the next step, not just a verdict.

    The tool's own drift output is the state a host actually reaches, so it is
    driven (stubbed tree, no collection) rather than read out of the source.
    """
    _fake_tree(tmp_path, "docs.md", "Python: `uv run pytest tests/ -v` (1599)\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "scanned_files", lambda: ["docs.md"])
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert f"Measure it with: {CANONICAL} --measure" in out, out
    assert f"tree: {tmp_path}" in out, out


def test_the_guard_message_names_the_one_command(mod, monkeypatch) -> None:
    """The CI guard must fail with a runnable command too, and the same spelling.

    Driven, not read: the guard's message is an f-string interpolating the tool's
    own `INVOCATION`, and a source reader collects only its literal parts — so
    reading it would prove nothing about what a host sees.
    """
    guard = _load_guard()
    tool = SimpleNamespace(
        offenders=lambda: [("Agent.md", 122, "stored next to the test command", "x")],
        REPO_ROOT=REPO_ROOT,
        INVOCATION=CANONICAL,
    )
    monkeypatch.setattr(guard, "_load_doc_count_tool", lambda: tool)
    with pytest.raises(AssertionError) as excinfo:
        guard.test_no_tracked_file_states_the_python_test_count(monkeypatch)
    message = str(excinfo.value)
    assert CANONICAL in message, message
    assert "--measure" in message, message
    assert "Agent.md:122" in message, message


def test_guard_failure_names_the_tool_it_points_at(mod, capsys) -> None:
    """The guard's message and the tool it names must both exist.

    Necessary, not sufficient (the driven test above is the other half): a hint
    naming a path that no longer exists is a next step that fails.
    """
    assert SCRIPT.exists(), f"the guard points at {SCRIPT}, which does not exist"
    messages = _guard_assert_messages()
    assert messages, "the guard test carries no assertion message at all"
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--help"])
    assert excinfo.value.code == 0
    assert "--measure" in capsys.readouterr().out


def test_every_printed_invocation_uses_the_project_runner(mod) -> None:
    """A hint that cannot import pytest sends the reader into a second failure.

    Measured (cycle cyc20260910-191242, main clone): the canonical form exits 0
    while a bare `python3` form exits 2 having measured nothing - the host's
    `python3` has no pytest. Every site is checked: this tool's source, its error
    hint, and Agent.md's doc line.
    """
    assert mod.INVOCATION == CANONICAL

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
    assert CANONICAL in doc, "Agent.md no longer documents the canonical invocation"


def test_measure_failure_names_the_invocation_and_how_to_fix_it(mod, monkeypatch) -> None:
    """An error state must carry the same spelling, for the same measured reason."""

    class _Proc:
        returncode = 4
        stdout = ""
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(mod.DocCountError) as excinfo:
        mod.measured_count()
    assert f"`{mod.INVOCATION} --measure`" in str(excinfo.value)


def test_a_missing_pytest_inside_the_venv_is_diagnosed_as_an_unsynced_checkout(
    monkeypatch, tmp_path
) -> None:
    """The remedy must not be the command that just failed.

    Measured state this pins (2026-09-13, `cyc20260913-122923`): in a fresh
    review worktree the tool printed its own `INVOCATION` as the fix, and running
    that spelling produced byte-identical output, rc 2 - `uv run --no-sync` had
    left an empty `.venv` there. The message must name the environment instead.

    Everything the message is decided from is driven here rather than read off the
    machine this runs on: the same child output means the opposite thing when the
    interpreter is *not* the checkout's (the next test's arm), the search would
    otherwise consult the real `.venv` of whatever checkout is running the suite,
    and the sentence itself turns on whether a `.venv` is there - which only the
    tree this test builds can guarantee.
    """
    fresh = _load_module()
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(fresh, "REPO_ROOT", checkout)
    monkeypatch.setattr(fresh, "can_import_pytest", lambda python: False)
    monkeypatch.setattr(fresh, "interpreter_is_the_checkouts", lambda *a, **k: True)

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "/some/checkout/.venv/bin/python3: No module named pytest\n"

    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(fresh.DocCountError) as excinfo:
        fresh.measured_count()
    message = str(excinfo.value)
    assert "No module named pytest" in message
    assert "/some/checkout/.venv/bin/python3" in message, "the child's own words"
    assert "unsynced" in message, "the cause, named"
    assert "uv sync" in message, "a remedy that can actually work here"


def test_the_refusal_says_when_the_interpreter_is_not_the_checkouts(
    monkeypatch, tmp_path
) -> None:
    """The one thing the search cannot fix, said as the extra sentence it is.

    `cyc20261005-034541`: on this Windows host `.venv` holds pytest (9.1.1) while
    `uv run --no-sync python3 ...` resolves outside it - uv's venv provides
    `python.exe` and no `python3.exe`, so `python3` comes from PATH, where the
    EMRG install is. The search answers that case now; this arm is the one where
    it could not, because nothing reachable has pytest, and the reader is still
    owed the fact that the spelling they used reached a foreign interpreter.

    What the message must not do is the old thing: call a checkout with a
    populated `.venv` unsynced and offer `uv sync` for a cause it had just said
    `uv sync` cannot change. An environment that was never built is exactly what
    `uv sync` is for, whichever interpreter the caller's spelling reached.
    """
    fresh = _load_module()
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(fresh, "REPO_ROOT", checkout)
    monkeypatch.setattr(fresh, "can_import_pytest", lambda python: False)
    monkeypatch.setattr(fresh, "interpreter_is_the_checkouts", lambda *a, **k: False)

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "C:\\install\\bin\\python3.exe: No module named pytest\n"

    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(fresh.DocCountError) as excinfo:
        fresh.measured_count()
    message = str(excinfo.value)
    assert "No module named pytest" in message
    assert "is not this checkout's own environment" in message, (
        "the foreign interpreter, named as the sentence it is"
    )
    assert "cannot fix it" not in message, "the claim that was false here"
    assert "uv sync" in message, "the remedy that does fit it"


def test_the_refusal_distinguishes_a_missing_environment_from_an_empty_one(
    mod, tmp_path
) -> None:
    """Both states of the environment, on trees this test builds.

    `missing_pytest_remedy` takes the checkout as an argument, so it can be asked
    about a layout the test owns instead of about the machine the suite happens to
    run on: the message it replaced was written from one environment and read as
    all of them. An absent `.venv` and an empty one are different facts, and the
    sentence says which of the two was seen.
    """
    empty = tmp_path / "empty"
    (empty / ".venv" / "Scripts").mkdir(parents=True)
    text = mod.missing_pytest_remedy("/usr/bin/python3", empty, ["/usr/bin/python3"])
    assert "unsynced" in text, "a `.venv` that is there and holds no pytest"
    assert "uv sync" in text

    bare = tmp_path / "bare"
    bare.mkdir()
    text = mod.missing_pytest_remedy("/usr/bin/python3", bare, ["/usr/bin/python3"])
    assert "never created" in text, "no `.venv` at all is a different sentence"
    assert "uv sync" in text
    assert "is not this checkout's own environment" not in text, (
        "with no `.venv` there is nothing for an interpreter to be foreign to"
    )


def test_the_search_keeps_this_interpreters_answer_when_it_can_already_measure(
    mod, monkeypatch, tmp_path
) -> None:
    """The documented invocation must keep measuring exactly what it measured.

    `measure_interpreter` is a fallback, not a substitution: an interpreter that
    can already import pytest is the answer, and no candidate is probed - a search
    that ran anyway could pick a different `.venv` and change what a working
    invocation reports.
    """
    probes: list[str] = []
    monkeypatch.setattr(
        mod, "can_import_pytest", lambda python: probes.append(python) or True
    )
    checkout = tmp_path / "checkout"
    target = checkout / ".venv" / "Scripts" / "python.exe"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    assert mod.measure_interpreter(own="/some/python", checkout=checkout) == "/some/python"
    assert probes == ["/some/python"], "a working interpreter is not searched past"


def test_the_search_finds_the_checkouts_own_interpreter_when_this_one_cannot(
    mod, monkeypatch, tmp_path
) -> None:
    """The defect this exists for, on a tree the test owns (measured 2026-10-05).

    `cyc20261005-034541`, on this host: the interpreter that ran this tool was the
    EMRG install's `python3.exe`, which cannot import pytest, while the checkout's
    `.venv` held pytest - so the count *could* be measured and the tool refused
    instead. The `.venv` spelling is built here rather than found on this machine,
    so the assertion holds on a leg whose environment is the POSIX one.
    """
    monkeypatch.setattr(
        mod,
        "can_import_pytest",
        lambda python: python.replace("\\", "/").endswith(".venv/Scripts/python.exe"),
    )
    checkout = tmp_path / "checkout"
    target = checkout / ".venv" / "Scripts" / "python.exe"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    resolved = mod.measure_interpreter(own="/host/python3", checkout=checkout)
    assert resolved == str(target), "the environment one directory away, used"


def test_the_search_answers_with_this_interpreter_when_the_checkout_has_none(
    mod, monkeypatch, tmp_path
) -> None:
    """Nothing reachable can measure: the answer is the interpreter really tried.

    The refusal that follows names an interpreter this machine can show, which is
    why the search answers with the one it was given rather than inventing a path
    that does not exist.
    """
    monkeypatch.setattr(mod, "can_import_pytest", lambda python: False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    assert (
        mod.measure_interpreter(own="/host/python3", checkout=checkout) == "/host/python3"
    )


def test_the_interpreter_predicate_reads_the_checkout_it_was_given(mod, tmp_path) -> None:
    """The three answers, on a tree the test builds rather than the ambient one.

    Driven on its own tree because that is the defect: the branch this feeds read
    one environment and asserted it as the only case. A predicate tested only
    against the machine it happens to run on repeats it.
    """
    checkout = tmp_path / "checkout"
    scripts = checkout / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    inside = scripts / "python.exe"
    inside.write_text("", encoding="utf-8")

    outside_dir = tmp_path / "install" / "bin"
    outside_dir.mkdir(parents=True)
    outside = outside_dir / "python3.exe"
    outside.write_text("", encoding="utf-8")

    assert mod.interpreter_is_the_checkouts(str(inside), checkout) is True
    assert mod.interpreter_is_the_checkouts(str(outside), checkout) is False

    bare = tmp_path / "bare"
    bare.mkdir()
    assert mod.interpreter_is_the_checkouts(str(outside), bare) is None, (
        "no `.venv` to compare against is not the answer 'no'"
    )


def test_a_real_collection_failure_keeps_the_invocation_hint(monkeypatch) -> None:
    """The other cause of the same non-zero exit: pytest ran, and it failed.

    Without this arm, treating every non-zero collection as an unsynced checkout
    would be green - which would take the invocation hint away from the case it
    was written for.
    """
    fresh = _load_module()

    class _Proc:
        returncode = 2
        stdout = "ERROR: file or directory not found: tests/\n"
        stderr = ""

    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(fresh.DocCountError) as excinfo:
        fresh.measured_count()
    message = str(excinfo.value)
    assert f"`{fresh.INVOCATION} --measure`" in message
    assert "uv sync" not in message
    assert "unsynced" not in message


# --- which tree was scanned, and which revision of it ------------------------


def _fake_checkout(root: Path, count: int) -> Path:
    """A minimal checkout shape: the two things `_resolve_root` looks for."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "Agent.md").write_text(
        f"Python: `uv run pytest tests/ -v` ({count}) - import check: x\n",
        encoding="utf-8",
    )
    return root


def test_the_tree_is_the_checkout_you_are_standing_in(mod, monkeypatch, tmp_path):
    """The defect, measured 2026-09-11 while unblocking PRs.

    Unblocking means working in a git worktree; the natural invocation is
    `<worktree>/.venv/bin/python <main-checkout>/scripts/check-doc-count.py`, and
    the old root was `Path(__file__).parent.parent` - the *main* checkout. So the
    tool reported `OK` about a checkout the caller was not in, which is the one
    answer this tool must never give.

    Pinned on the predicate, not on the printed line: `_resolve_root` is the
    decision, and a fixture that made `main()` agree could pass while the wrong
    root was still chosen.
    """
    fake = _fake_checkout(tmp_path / "checkout", 1307)
    monkeypatch.chdir(fake)
    assert mod._resolve_root() == fake.resolve(), (
        "the tool must scan the checkout the caller is standing in; deriving the "
        "root from __file__ scans a different tree (the one the script happens to "
        "live in) and reports its verdict as if it were yours"
    )
    assert mod._resolve_root() != SCRIPT.parent.parent, (
        "the fixture must not be the script's own root, or this test proves nothing"
    )


def test_the_scanned_tree_is_named_in_the_output(mod, monkeypatch, tmp_path, capsys):
    """`which tree did you scan` must never be ambiguous."""
    fake = _fake_checkout(tmp_path / "checkout", 1307)
    monkeypatch.chdir(fake)
    monkeypatch.setattr(mod, "REPO_ROOT", mod._resolve_root())
    monkeypatch.setattr(mod, "scanned_files", lambda: [])
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert f"tree: {fake.resolve()}" in out, out


def _git_stdout(*argv: str) -> str:
    """One `git` read in this checkout, on the test's own account."""
    proc = subprocess.run(
        ["git", *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def test_the_measure_line_names_the_revision_it_counted(mod, monkeypatch, capsys) -> None:
    """A count is a number *about a tree*, so it is comparable only at a named revision.

    Measured 2026-09-28 (`cyc20260928-150510`): a cycle compared its own
    checkout's count with a worktree gate's count, attributed the one-test
    difference to the harness, and recorded that as a finding — the two runs were
    of **different trees**, and the reading that would have shown it (their
    revisions) was on neither line. `tree:` answers *which checkout*; this
    answers which revision of it, which is the half that moves while a cycle
    works.

    The count is injected (as everywhere else in this file) so the wiring is
    measured in milliseconds, but the revision is **not**: it is read from the
    real repository below and compared with what the line printed, because a
    revision the tool invented for itself would satisfy exactly this shape.
    """
    monkeypatch.setattr(mod, "measured_count", lambda: 1307)
    assert mod.main(["--measure"]) == 0
    out = capsys.readouterr().out
    match = re.search(
        r"^measured: 1307 collected Python tests at ([0-9a-f]{7,40}) \((.+)\)$",
        out,
        re.MULTILINE,
    )
    assert match, out
    head = _git_stdout("rev-parse", "HEAD")
    assert head, "this suite ran outside a git checkout; the reading is unmeasurable"
    assert head[: len(match.group(1))] == match.group(1), (
        f"the line names {match.group(1)}, this checkout is at {head[:8]} — a count "
        "reported at another tree's revision is the misreading this line prevents"
    )


def test_an_unreadable_revision_is_stated_never_omitted(mod, monkeypatch) -> None:
    """The absence has a wording, and every one of the three states is its own.

    A revision that silently reads as "unchanged" is exactly the defect the line
    exists for, so the failure modes are pinned here rather than left to whatever
    git prints on the host that happens to run this: no `git` on PATH (a
    `FileNotFoundError`, which is what `subprocess` raises for a missing binary),
    a directory that is not a repository (a non-zero `rev-parse`), and a detached
    HEAD — which is a *state*, not a failure, and must not be reported as one.
    """

    def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    class _Proc:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    answers: dict[str, tuple[str, int]] = {}

    def _run(cmd, **kwargs):
        for key, answer in answers.items():
            if key in cmd:
                return _Proc(*answer)
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(mod.subprocess, "run", _run)

    def _asked(case: str) -> str:
        answers.clear()
        # A git that answers nothing is the default: both reads non-zero with an
        # empty stdout, which is what a directory outside any repository gives.
        answers["rev-parse"] = ("", 128)
        answers["symbolic-ref"] = ("", 128)
        if case in ("branch", "detached"):
            answers["rev-parse"] = (("a" if case == "branch" else "b") * 40, 0)
        if case == "branch":
            answers["symbolic-ref"] = ("master", 0)
        return mod.measured_revision()

    assert _asked("branch") == "at aaaaaaaa (master)"
    assert _asked("detached") == "at bbbbbbbb (detached HEAD)"
    assert _asked("not-a-repository") == "revision unreadable (git rev-parse said nothing)"
    monkeypatch.setattr(mod.subprocess, "run", _raise_file_not_found)
    assert mod.measured_revision() == "revision unreadable (FileNotFoundError)"


def test_a_directory_that_is_not_a_checkout_falls_back_to_the_script_root(
    mod, monkeypatch, tmp_path
):
    """The documented invocation must keep working from anywhere.

    `python3 scripts/check-doc-count.py` is run from the repo root in every hint
    this tool prints, but an absolute-path call from elsewhere (a wrapper, an
    editor task, `git -C`) has no checkout in the cwd to stand in.
    """
    monkeypatch.chdir(tmp_path)  # a bare temp dir: no Agent.md, no scripts/
    assert mod._resolve_root() == SCRIPT.parent.parent.resolve()


def test_a_directory_with_only_half_the_shape_is_not_a_checkout(mod, monkeypatch, tmp_path):
    """Both markers are required, so a stray Agent.md does not claim the tree."""
    (tmp_path / "Agent.md").write_text(
        "Python: `uv run pytest tests/ -v`\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert mod._resolve_root() == SCRIPT.parent.parent.resolve()


# --- decoding, pinned before the locale can decide it ------------------------


def test_collect_output_is_decoded_independently_of_the_locale(mod) -> None:
    """The measured count must not depend on the host's locale codec.

    The sibling tool's identical defect was measured and filed as issue #1132:
    decoding with the locale codec left `proc.stdout` as `None` once subprocess's
    reader thread swallowed the `UnicodeDecodeError`, and the concatenation raised
    a bare `TypeError` past every handler in `main()` (which catches
    `DocCountError` and `OSError` only). pytest's own output is ASCII today, but a
    collected id or warning is not under this repo's control - one non-ASCII byte
    on a cp936 host would produce a traceback instead of the count.
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


def test_unparsable_collect_output_fails_loud(mod, monkeypatch) -> None:
    """A run that produces no summary is an error, not a count of zero."""

    class _Proc:
        returncode = 0
        stdout = "collected 0 items\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    with pytest.raises(mod.DocCountError, match="could not parse a collected count"):
        mod.measured_count()
