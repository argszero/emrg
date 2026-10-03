"""Tests for scripts/check-node-test-count.py - the Agent.md Node-count sync tool.

Background (cycle cyc20260910-211447)
-------------------------------------
`Agent.md` documents two Node-suite totals next to the Python one, and
`tests/test_doc_counts.py` guards both **statically**: it counts `it(`/`test(`
definitions per file, which is all the pytest job can do without node_modules.

A static count is a *model* of the runner, and this repo has been burned three
times by the model drifting from the runner (R2254's 445 -> 448; #1120's label
collision dropping a whole file; #1125's regex missing `it.each`/`test.skip`).
The guard cannot tell "my model matches reality" from "my model matches itself",
and the pytest job has no node_modules to find out. This tool closes the loop
from the other side - it asks the real runners.

Both states are pinned here, never inferred from the failure case alone (#455):

* positive - a consistent doc reports OK and writes nothing;
* negative - a stale doc reports the executed value, and `--write` repairs it,
  changing only the two numbers and nothing else in the doc.

The one test that touches the real runners is the integration test against the
real tree; every other test injects the measurement.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-node-test-count.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_node_test_count", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _renderer_runner(mod):
    """The renderer's runner as `npm test` will look for it: the vitest package.

    A **directory named** `node_modules` is not an installation. Measured
    2026-10-03 (`cyc20261003-115810`) on the evolution host:
    `emrg/gui/renderer/node_modules` exists and is **empty** - 0 entries, no
    `.bin`, no `vitest` - so the existence check passed, `npm test` ran, and it
    answered `'vitest' is not recognized as an internal or external command`. The
    renderer's script is `vitest run`, so the runner's presence is the fact that
    decides whether the count can be measured at all.
    """
    return mod.RENDERER_ROOT / "node_modules" / "vitest" / "package.json"


def _cannot_ask_the_runners(mod) -> str | None:
    """The precondition this host fails, or ``None`` when every half is met.

    There are **three**, and the pair framing was short by one: the tool resolves
    `npm` through PATH, runs it inside a directory it expects to hold
    `node_modules`, and that directory has to hold the runner the script starts.
    Having one is not having the next.

    Measured 2026-10-03 (`cyc20261003-065523`) on the evolution host: the
    renderer's `node_modules` is present while `npm`, `node` and `npx` are all
    absent from PATH. The integration test let the run through on the
    `node_modules` check alone, so `_run` raised `NodeCountError: cannot run 'npm'`
    and a host that simply *cannot ask* reported a failure - while the docstring
    beside it promised a skip. One screen down, `test_a_bare_name_starts_the_real_runner`
    already skips for exactly this reason and states it ("a missing toolchain is
    not a defect in `_run`"); this helper is that rule, applied to the one test that
    needs both halves.

    Measured again the same day (`cyc20261003-115810`), on the same host and after
    that two-part check had landed: `npm` and the empty `node_modules` were both
    there, the run went through, and it failed on `vitest` not being found. The
    third half is the one that decides the count: the **runner** has to be
    installed, not merely a directory named after where it would be.
    """
    if not _renderer_runner(mod).exists():
        return f"no vitest installed under {mod.RENDERER_ROOT}/node_modules"
    if mod.shutil.which("npm") is None:
        return "no `npm` on PATH"
    return None


def test_the_runner_precondition_names_which_half_is_missing(
    mod, monkeypatch, tmp_path
) -> None:
    """Every half, each isolated, plus the case where none is missing.

    The defect this replaces was a check that could not tell its subjects apart -
    it asked about `node_modules` and returned a verdict about the whole
    toolchain - so the directions are the whole content: the directory absent, the
    directory present but empty, the runner installed with no `npm`, and nothing
    missing. The last keeps the helper from turning an askable host into a
    permanent skip, and the empty third is the measured one: it is what a green
    suite on this host was hiding.
    """
    installed = tmp_path / "renderer"
    installed.mkdir()
    monkeypatch.setattr(mod, "RENDERER_ROOT", installed)
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    # No node_modules at all: the runner is what is missing, named by its place.
    assert "node_modules" in (_cannot_ask_the_runners(mod) or ""), (
        "a host without the renderer's node_modules must be told so by name"
    )

    # node_modules present and empty - the state measured on the evolution host.
    # A directory of that name satisfied the old check and the run failed on
    # `vitest`; the answer here must be the missing runner, not silence.
    (installed / "node_modules").mkdir()
    empty = _cannot_ask_the_runners(mod) or ""
    assert "vitest" in empty, (
        "an empty node_modules is not an installation, and the host has to be told "
        f"which fact is missing; got {empty!r}"
    )

    # The runner installed, no `npm`: the executable is what is missing. This is
    # the half the integration test used to walk past - measured on the evolution
    # host, where node_modules is installed and no node is on PATH at all.
    runner = _renderer_runner(mod)
    runner.parent.mkdir(parents=True)
    runner.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert "npm" in (_cannot_ask_the_runners(mod) or ""), (
        "a host with node_modules but no `npm` must be told so by name - checking "
        "the modules alone let this host reach `_run` and report a failure"
    )

    # Every half present: nothing is missing, so the integration test has to run.
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert _cannot_ask_the_runners(mod) is None, (
        "an askable host must not be skipped - a helper that always answers "
        "would make the integration test green forever by never running"
    )


def test_the_integration_test_gates_on_every_half() -> None:
    """The call site, which the helper's own pins cannot reach.

    The helper can be correct while the test beside it keeps the old one-line
    check - that is exactly the state this cycle found. Pinned on the body, in the
    shape `tests/test_check_merge_order.py` pins its forced refspec: the call is
    what has to be there, and it is not observable through the helper.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Anchored at a line start with the parameter list, because this test's own
    # source contains the name in a string literal - an unanchored search found
    # *this* function first and pinned the wrong body.
    marker = "\ndef test_real_tree_is_consistent("
    assert marker in source, "the integration test this pin is about is gone"
    body = source.split(marker, 1)[1].split("\ndef ", 1)[0]
    assert "Integration: the tool reports OK" in body, (
        "the extraction did not land on the integration test, so this pin would "
        f"assert against some other function: {body[:200]!r}"
    )
    assert "_cannot_ask_the_runners(mod)" in body, (
        "the integration test no longer asks the precondition, so a host that "
        "cannot run the runners reports a failure instead of a skip"
    )
    assert 'node_modules").exists()' not in body, (
        "the integration test still carries its own node_modules existence check: "
        "one half standing in for the rest is the defect, and a second copy of it "
        "is a second place to keep in step"
    )


def _doc(tmp_path: Path, renderer: int | str, gui: int | str) -> Path:
    """A minimal Agent.md copy carrying both Node count lines."""
    head = (
        "# Agent.md\n\n"
        "Python: `uv run pytest tests/ -v` (1307) - import check\n"
        "GUI: `cd emrg/gui && npm test` "
        f"({gui}: 44 daemon_client + 20 conn-manager)\n"
    )
    renderer_line = (
        "Renderer: `cd emrg/gui/renderer && npm run typecheck && npm test` "
        f"({renderer}: 5 snapshot-store + 9 utils)\n"
    )
    path = tmp_path / "Agent.md"
    path.write_text(head + renderer_line)
    return path


# --- the doc side: two lines, one number each, no ambiguity ------------------


def test_documented_counts_read_the_real_doc(mod, tmp_path) -> None:
    assert mod.documented_counts(_doc(tmp_path, 514, 100).read_text()) == (514, 100)


def test_missing_renderer_line_fails_loud(mod, tmp_path) -> None:
    text = _doc(tmp_path, 514, 100).read_text()
    stripped = "\n".join(
        ln for ln in text.splitlines() if not ln.startswith("Renderer:")
    )
    with pytest.raises(mod.NodeCountError, match="exactly one Agent.md Renderer"):
        mod.documented_counts(stripped)


def test_missing_gui_line_fails_loud(mod, tmp_path) -> None:
    text = _doc(tmp_path, 514, 100).read_text()
    stripped = "\n".join(ln for ln in text.splitlines() if not ln.startswith("GUI:"))
    with pytest.raises(mod.NodeCountError, match="exactly one Agent.md GUI"):
        mod.documented_counts(stripped)


def test_duplicate_line_fails_loud(mod, tmp_path) -> None:
    """Two counts for one suite = ambiguity; the tool must refuse, not pick one."""
    text = _doc(tmp_path, 514, 100).read_text()
    with pytest.raises(mod.NodeCountError, match="found 2"):
        mod.documented_counts(text + "Renderer: `npm test` (999: x)\n")


def test_gui_line_and_renderer_line_are_not_confused(mod, tmp_path) -> None:
    """The GUI anchor must not match the Renderer line (both contain `npm test`)."""
    text = _doc(tmp_path, 514, 100).read_text()
    renderer, gui = mod.documented_counts(text)
    assert (renderer, gui) == (514, 100)


# --- the patch side: only the numbers move ----------------------------------


def test_patch_changes_only_the_number(mod, tmp_path) -> None:
    text = _doc(tmp_path, 514, 100).read_text()
    patched = mod.patch_count(mod.RENDERER_LINE, text, 520)
    assert patched != text
    assert mod.documented_counts(patched) == (520, 100)
    # Everything except the digits must be identical.
    mask = lambda s: re.sub(r"\d+", "#", s)  # noqa: E731
    assert mask(patched) == mask(text)


def test_patch_missing_line_fails_loud(mod, tmp_path) -> None:
    with pytest.raises(mod.NodeCountError, match="disappeared"):
        mod.patch_count(mod.RENDERER_LINE, "nothing here\n", 1)


# --- runner-output parsing: the formats are coloured, prefixed, and specific --


def test_ansi_escapes_are_stripped_before_parsing(mod) -> None:
    """Both runners colour the summary, so escapes land *inside* the match.

    Measured 2026-09-10: vitest prints
    `\\x1b[2m Tests \\x1b[22m \\x1b[32m514 passed\\x1b[39m`, and without this
    strip the summary is unmatchable even though it is plainly on stdout.
    """
    coloured = "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m514 passed\x1b[39m\x1b[90m (514)\x1b[39m\n"
    assert mod.VITEST_SUMMARY.search(coloured) is None
    assert mod._plain(coloured) == "      Tests  514 passed (514)\n"
    match = mod.VITEST_SUMMARY.search(mod._plain(coloured))
    assert match and match.group(2) == "514"


def test_node_summary_prefix_is_not_word_class(mod) -> None:
    """node prefixes its summary with `ℹ` (U+2139), which `\\w` *matches*.

    Measured 2026-09-10: `^(\\W*)tests (\\d+)$` never fired - the character is
    Unicode-letter class, so `\\W` was empty where the pattern needed it. Pinned
    here because the failure mode is a silent "could not parse", not a crash.
    """
    assert re.match(r"\w", "ℹ"), "premise changed: U+2139 is no longer a \\w char"
    assert mod.NODE_TESTS.search("ℹ tests 101\n"), "the node summary regex must fire on ℹ"
    assert mod.NODE_FAIL.search("ℹ fail 0\n")


def test_module_skip_entries_counts_the_call_not_the_comment(mod) -> None:
    """integration.test.js both *calls* `skip(` and *mentions* it in a comment.

    The entry node registers is the call; a naive `skip(` scan also counts the
    prose mention (measured: 2 hits, 1 entry). The regex requires it to be the
    first token on the line, which excludes the `//`-prefixed comment.
    """
    assert mod.module_skip_entries() == 1


# --- end-to-end: both states, with the runners injected ---------------------


def test_consistent_doc_reports_ok_and_writes_nothing(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 514, 100)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)

    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "OK: Agent.md documents 514 renderer + 100 GUI tests" in out
    assert doc.read_text() == before


def test_drift_is_reported_with_the_measured_values(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 510, 96)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)

    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "Renderer: documents 510, runner executed 514" in out
    assert "GUI: documents 96, runner executed 100" in out
    assert "--write" in out
    assert doc.read_text() == before, "a reporting run must never write"


def test_the_verdict_names_the_revision_it_measured(mod, tmp_path, monkeypatch, capsys) -> None:
    """A total is a number *about a tree*; `--write` makes it a stored number too.

    Measured 2026-09-28 (`cyc20260928-150510`): a cycle compared its own
    checkout's count with a worktree gate's count, attributed the one-test
    difference to the harness, and recorded that as a finding — the two runs were
    of different trees, and the reading that would have shown it was on neither
    line. `tree:` says *which checkout*; the revision says which state of it, and
    this is the tool whose numbers get written down, so a reader deciding whether
    a stored number is stale has to be able to ask which revision produced it.

    Both verdicts carry it, and the reading is the tool's real one — stubbing
    `measured_revision` here would pin the wiring against a placeholder that the
    line could then print forever without anyone noticing.
    """
    expected = mod.measured_revision()
    assert re.match(r"^at [0-9a-f]{7,40} \((.+)\)$", expected), (
        f"this checkout's revision did not read cleanly ({expected!r}); the assertion "
        "below compares the printed line against this reading, so it is unmeasurable here"
    )

    mod.DOC = _doc(tmp_path, 514, 100)
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert out.strip().endswith(expected), out

    mod.DOC = _doc(tmp_path, 510, 96)
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)
    assert mod.main([]) == 1
    lines = capsys.readouterr().out.splitlines()
    drift_lines = [line for line in lines if line.startswith("FAIL: ")]
    assert drift_lines, lines
    for line in drift_lines:
        assert line.endswith(expected), lines


def test_write_repairs_only_the_two_numbers(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 510, 100)
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)

    assert mod.main(["--write"]) == 0
    text = doc.read_text()
    assert "updated Agent.md: renderer 510 -> 514, GUI 100 -> 100" in capsys.readouterr().out
    assert mod.documented_counts(text) == (514, 100)
    # The Python count and the per-file breakdowns must be untouched.
    assert "`uv run pytest tests/ -v` (1307)" in text
    assert "5 snapshot-store + 9 utils" in text
    assert "44 daemon_client + 20 conn-manager" in text


def test_dry_run_reports_but_does_not_write(mod, tmp_path, monkeypatch, capsys) -> None:
    doc = _doc(tmp_path, 510, 100)
    before = doc.read_text()
    mod.DOC = doc
    monkeypatch.setattr(mod, "measured_renderer", lambda: 514)
    monkeypatch.setattr(mod, "measured_gui", lambda: 100)

    assert mod.main(["--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert doc.read_text() == before


def test_write_and_dry_run_together_are_rejected(mod) -> None:
    """The two modes contradict each other, so the pair must fail loud.

    Same rule, and same measured reason, as scripts/check-doc-count.py:
    `--write --dry-run` once printed the dry-run line, wrote nothing and exited
    0 - the caller asked for a repair and the tool dropped the request without a
    word. argparse's exit code for a usage error is 2, this repo's convention
    for "cannot act on what you gave me".
    """
    with pytest.raises(SystemExit) as excinfo:
        _load_module().main(["--write", "--dry-run"])
    assert excinfo.value.code == 2


def test_missing_node_modules_fails_with_that_reason(mod, monkeypatch) -> None:
    """A missing toolchain is reported as such, not as a bogus count."""
    monkeypatch.setattr(mod, "RENDERER_ROOT", Path("/nonexistent/renderer"))
    with pytest.raises(mod.NodeCountError, match="no node_modules"):
        mod.measured_renderer()


def test_runner_failure_is_reported_with_its_output(mod, monkeypatch) -> None:
    class _Proc:
        returncode = 1
        stdout = "boom"
        stderr = ""

    monkeypatch.setattr(
        mod, "_run", lambda *a, **k: (_ for _ in ()).throw(mod.NodeCountError("boom: rc=1"))
    )
    with pytest.raises(mod.NodeCountError, match="boom"):
        mod.measured_renderer()


# --- the hint itself must be a command that runs -----------------------------


def test_every_hint_in_this_tool_uses_the_runnable_invocation(mod) -> None:
    """Every mention of this tool's own invocation must carry the project runner.

    The sibling tool's spelling was already fixed once because a hint that fails
    is worse than no hint - it looks like a next step (a bare `python3` cannot
    import pytest on this host, measured rc=2 vs rc=0). Every site in this
    module is checked the same way, including the usage block in the docstring
    and the `Fix with:` line the tool actually prints on drift.
    """
    canonical = "uv run --no-sync python3 scripts/check-node-test-count.py"
    assert mod.INVOCATION == canonical

    source = SCRIPT.read_text(encoding="utf-8")
    hits = list(re.finditer(r"python3 scripts/check-node-test-count\.py", source))
    assert hits, "the tool no longer mentions its own invocation at all"
    for hit in hits:
        prefix = source[max(0, hit.start() - len("uv run --no-sync ")) : hit.start()]
        assert prefix == "uv run --no-sync ", (
            "a hint in scripts/check-node-test-count.py spells the invocation "
            "without the project runner: "
            f"...{source[max(0, hit.start() - 40) : hit.end() + 20]!r}"
        )

    # Drift state, for real: the printed line must be the runnable one.
    assert 'print(f"\\nFix with: {INVOCATION} --write")' in source


def test_agent_md_documents_the_canonical_invocation(mod) -> None:
    doc = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    assert mod.INVOCATION in doc, (
        "Agent.md must document the Node-count tool with the runnable form - that "
        "line is how a host learns the tool exists"
    )


def test_real_tree_is_consistent() -> None:
    """Integration: the tool reports OK on the checked-in tree.

    This is the test that actually talks to vitest and node --test, which is the
    whole point of the tool - the static guard next door can only reason about
    source text. It needs **both** halves of the toolchain: the renderer's
    `node_modules` and `npm` itself on PATH. Either one absent means the host cannot
    ask the runners, which is a skip naming what is missing (e.g. in a bare CI
    checkout of the pytest job) and never a failure - a red here would be read as a
    drifted count.
    """
    mod = _load_module()
    blocked = _cannot_ask_the_runners(mod)
    if blocked is not None:
        pytest.skip(f"{blocked}: cannot ask the runners")
    renderer = mod.measured_renderer()
    gui = mod.measured_gui()
    documented = mod.documented_counts((REPO_ROOT / "Agent.md").read_text(encoding="utf-8"))
    assert documented == (renderer, gui), (
        f"Agent.md documents {documented} but the runners executed {(renderer, gui)} "
        "- run scripts/check-node-test-count.py --write"
    )


def test_ci_gate_uses_the_check_mode_not_the_preview(mod) -> None:
    """The CI step must call the bare form, whose exit code actually gates.

    Measured 2026-09-11, while wiring this tool into `test.yml`: on a drifted doc
    the bare invocation exits **1**, `--write` exits 0 (it repaired), and
    `--dry-run` exits **0** too - it prints `FAIL` and then reports what a repair
    *would* do. A gate written as `--dry-run` is therefore green forever, which is
    the failure mode this whole file exists to prevent: a check that reads as
    coverage while checking nothing.

    Asserted at the level of the exit codes *and* the workflow text, because
    either half alone can drift: the codes could stay right while the step adds a
    flag, or the step could be correct while a refactor changes the codes.
    """
    # Exit-code contract, driven with an injected drift.
    import pathlib
    import tempfile

    doc = REPO_ROOT / "Agent.md"
    text = doc.read_text(encoding="utf-8")
    drifted = mod.GUI_LINE.sub(lambda m: m.group("head") + "1" + m.group("tail"), text, count=1)
    assert drifted != text, "the GUI count line must patch for this test to mean anything"

    with tempfile.TemporaryDirectory() as tmp:
        fake = pathlib.Path(tmp) / "Agent.md"
        fake.write_text(drifted, encoding="utf-8")
        mod.DOC = fake
        mod.measured_renderer = lambda: 514
        mod.measured_gui = lambda: 100

        assert mod.main([]) == 1, "the bare form must fail on drift - CI gates on it"
        assert mod.main(["--dry-run"]) == 0, (
            "if this ever becomes 1, the comment above is stale and the workflow "
            "step may safely use either form"
        )

    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    hits = [
        line.strip()
        for line in workflow.splitlines()
        if "check-node-test-count.py" in line
    ]
    assert hits, (
        "no CI step runs scripts/check-node-test-count.py, so the static model in "
        "tests/test_doc_counts.py is never corroborated by the real runners - the "
        "exact gap #1126 built this tool to close"
    )
    for hit in hits:
        assert "--dry-run" not in hit and "--write" not in hit, (
            f"the CI step must use the bare check form: {hit!r} - `--dry-run` exits "
            "0 on drift and `--write` would rewrite the repo under CI"
        )


# --- which tree was measured -------------------------------------------------


def test_the_tree_is_the_checkout_you_are_standing_in(mod, monkeypatch, tmp_path):
    """Same defect as the doc-count tool's, measured 2026-09-11.

    Unblocking a PR means working in a git worktree, where running the main
    checkout's copy of this script reported the *main* tree's numbers. `--write`
    in that position edits that other checkout - a confirm-step silently
    corrupting a tree the caller was not looking at.
    """
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Agent.md").write_text("Renderer: `npm test` (1: x)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert mod._resolve_root() == tmp_path.resolve(), (
        "the tool must measure the checkout the caller is standing in"
    )
    assert mod._resolve_root() != SCRIPT.parent.parent


def test_a_directory_that_is_not_a_checkout_falls_back_to_the_script_root(
    mod, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    assert mod._resolve_root() == SCRIPT.parent.parent.resolve()


# --- host portability: Windows argv + non-locale decoding (issue #1132) -------
#
# The tool's whole purpose is to be run *by the host* after touching a Node test
# file, but its CI gate runs on `ubuntu-latest` only (`test-windows` runs pytest
# and iscc), so two defects survived every green run: `npm` cannot be started by
# bare name on Windows (subprocess appends only `.exe`, while npm ships as
# `npm.CMD`), and decoding with the locale codec turns node's `ℹ` summary glyph
# into a swallowed reader-thread UnicodeDecodeError whose `None` stdout then
# crashed on the concatenation. Both were measured on the merged tree - the
# first produced `cannot run 'npm': [WinError 2]`, the second
# `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`.
#
# The probes below are platform-independent: the argv half monkeypatches
# `shutil.which`, and the decoding half runs a real child that writes a byte
# invalid under *any* UTF-8 locale, so the failure shape reproduces on
# Linux/macOS too rather than only on the machine that filed the issue.


@pytest.fixture
def fake_cwd(tmp_path):
    """A cwd whose `node_modules` exists, so `_run`'s pre-check is satisfied.

    The new probes are about argv resolution and decoding, not about the
    toolchain - but they must not borrow `RENDERER_ROOT` to run, because CI's
    pytest job runs *before* the `npm ci` step, so the real renderer has no
    `node_modules` there and every probe would fail the pre-check instead of
    reaching the behaviour under test (measured on both the ubuntu and
    windows-2025 jobs: 5 failed with `...renderer has no node_modules`). A
    temporary directory makes the probes independent of whether the host has
    installed the Node toolchain.
    """
    (tmp_path / "node_modules").mkdir()
    return tmp_path


def test_run_resolves_the_command_through_which(mod, monkeypatch, fake_cwd) -> None:
    """`npm` must reach the runner by its resolved path, not as a bare name."""
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/resolved/{name}")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    mod._run(["npm", "test"], fake_cwd)

    assert seen == [["/resolved/npm", "test"]], (
        f"the runner was invoked as {seen} - an unresolved bare `npm` is a file "
        "that does not exist on Windows (npm.CMD)"
    )


def test_run_leaves_an_unresolvable_command_alone(mod, monkeypatch, fake_cwd) -> None:
    """A name `which` cannot find keeps its spelling, so the error names it.

    Inventing a path here would replace a precise `cannot run 'nope'` with a
    confusing failure about a file nobody asked for.
    """
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: (seen.append(list(cmd)), _Proc())[1])
    mod._run(["nope", "test"], fake_cwd)

    assert seen == [["nope", "test"]]


def test_missing_runner_still_names_the_command(mod, monkeypatch, fake_cwd) -> None:
    """The FileNotFoundError path survives the resolution (negative control)."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    def _boom(cmd, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    with pytest.raises(mod.NodeCountError, match="cannot run 'npm'"):
        mod._run(["npm", "test"], fake_cwd)


def test_run_decodes_output_that_is_not_valid_utf8(mod, fake_cwd) -> None:
    """A byte the locale codec cannot decode must not lose the whole output.

    This is the measured Windows failure (cp936 vs node's `ℹ` U+2139): the
    decode error happened inside subprocess's reader thread, `threading`
    swallowed it, `proc.stdout` came back `None`, and main() - which catches
    `NodeCountError` and `OSError` only - printed a traceback instead of the
    reason the tool already knows how to print.
    """
    child = "import sys; sys.stdout.buffer.write(b'\\xb9 tests 7\\nfail 0\\n')"
    out = mod._run([sys.executable, "-c", child], fake_cwd)
    assert "tests 7" in out, f"output was lost: {out!r}"
    assert mod.NODE_TESTS.search(out), (
        "the summary no longer parses once the undecodable glyph is replaced"
    )


def test_unreadable_output_raises_the_tools_own_error(mod, monkeypatch, fake_cwd) -> None:
    """`None` streams are reported, never concatenated.

    The guard half of the same defect: if a stream really is unusable, the tool
    must say so in its own vocabulary - a `TypeError` escaping main() is not a
    diagnosis.
    """

    class _Proc:
        returncode = 0
        stdout = None
        stderr = None

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    with pytest.raises(mod.NodeCountError, match="no readable output"):
        mod._run(["npm", "test"], fake_cwd)


def test_a_bare_name_starts_the_real_runner(mod, fake_cwd) -> None:
    """No stub: the argv half must work against a real runner, not a mock.

    Reported by a reference implementation on Windows (pm25coder, 2026-09-10):
    every probe above stubs the path under test (`shutil.which`,
    `subprocess.run`), so they pin the *shape* of the fix rather than that a
    named runner starts at all. GitHub's ubuntu and windows-2025 images both put
    Node on PATH, so this one probe would have gone red pre-fix on Windows and
    green post-fix - and its absence is why the defect shipped with five green
    probes: with `which` mocked, the platform is exactly what stops being
    visible. `test_run_resolves_the_command_through_which` asserts the tool calls
    `which`; this asserts the result is usable.

    Skipped, not failed, where no runner is installed: this repo's pytest job can
    run before `npm ci`, and a missing toolchain is not a defect in `_run`.
    """
    if mod.shutil.which("npm") is None:
        pytest.skip("npm is not on PATH")
    out = mod._run(["npm", "--version"], fake_cwd)  # bare name, as the tool calls it
    assert re.match(r"\d+\.\d+", out.strip()), (
        f"a bare `npm` must start a real runner through the resolution; got {out!r}"
    )


def test_main_catches_every_failure_its_run_can_produce(mod, monkeypatch, capsys) -> None:
    """The end-to-end shape: a bad decode exits 2 with a message, not a traceback."""
    monkeypatch.setattr(
        mod, "measured_renderer", lambda: (_ for _ in ()).throw(
            mod.NodeCountError("`npm test` produced no readable output")
        )
    )
    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "no readable output" in captured.err
    assert "Traceback" not in captured.err

