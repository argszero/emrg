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
    source text. It needs node_modules, so it skips (loudly) when they are
    absent, e.g. in a bare CI checkout of the pytest job.
    """
    mod = _load_module()
    if not (mod.RENDERER_ROOT / "node_modules").exists():
        pytest.skip(f"no node_modules under {mod.RENDERER_ROOT}: cannot ask the runners")
    renderer = mod.measured_renderer()
    gui = mod.measured_gui()
    documented = mod.documented_counts((REPO_ROOT / "Agent.md").read_text(encoding="utf-8"))
    assert documented == (renderer, gui), (
        f"Agent.md documents {documented} but the runners executed {(renderer, gui)} "
        "- run scripts/check-node-test-count.py --write"
    )
