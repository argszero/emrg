#!/usr/bin/env python3
"""Sync Agent.md's documented renderer/GUI test counts with the real runners.

Usage
-----
    uv run --no-sync python3 scripts/check-node-test-count.py           # report drift, exit 1
    uv run --no-sync python3 scripts/check-node-test-count.py --dry-run  # show the change
    uv run --no-sync python3 scripts/check-node-test-count.py --write    # rewrite Agent.md

`--write` and `--dry-run` are mutually exclusive: one repairs, the other must
not write, so the pair is rejected outright rather than silently resolved in
favour of one of them - the same rule (and the same measured reason) as
`scripts/check-doc-count.py`.

Why this exists
---------------
`Agent.md` carries two Node-suite counts beside the Python one:

    GUI: `cd emrg/gui && npm test` (100: 44 daemon_client + ...)
    Renderer: ... npm test` (514: 5 snapshot-store + 9 utils + ...)

`tests/test_doc_counts.py` guards both *statically* - it counts `it(`/`test(`
definitions per file, which is all the pytest job can do without node_modules.
But a static count is a **model** of the runner, and this repo has already been
burned three times by the model drifting from the runner:

* R2254 - renderer 445 -> 448 with the doc un-bumped;
* #1120 - two files sharing a label stem silently dropped one file's count;
* #1125 - the regex could not see `it.each(...)` / `test.skip(...)` at all.

The guard cannot tell "my model matches the runner" from "my model matches
itself", and the pytest job has no node_modules to check. This tool closes that
loop the other way round: it asks the real runners and reports what they
executed, so the number never comes from the model, from arithmetic, or from
memory of what the count "should" be. Run it after touching any Node test file.

Counting rules (each measured, not assumed)
-------------------------------------------
Renderer (`emrg/gui/renderer`, `vitest run`):
    the executed total is `Tests  N passed (N)`; a tree with failing or skipped
    renderer tests is refused rather than documented. The per-file breakdown is
    not written here - it needs each file's own count, and the static guard
    already pins it (this tool reports the *total* the runner executed, which is
    the half the static model cannot corroborate).

GUI (`emrg/gui/test`, `node --test "test/*.test.js"`):
    CI runs it with `EMRG_SKIP_INTEGRATION=1`, which registers **one extra
    entry** whose name is the skip *reason* (`integration.test.js` calls the
    module-level `skip(reason)`, #906). node counts it in `tests`, so the
    definition count is `tests - <module-level skip entries>`. That entry count
    is scanned from the suite files and asserted to be exactly 1: if it changes,
    this tool stops rather than documenting a shifted number.

Scope, stated rather than implied: this tool rewrites only the two Node-suite
headline counts. The Python count belongs to `scripts/check-doc-count.py`, and
the per-file breakdowns stay with the static guards. It does not run
`npm install`: a missing `node_modules` is reported as that reason, not as a
bogus count.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "Agent.md"
GUI_ROOT = REPO_ROOT / "emrg" / "gui"
RENDERER_ROOT = GUI_ROOT / "renderer"

# The one spelling of "run this tool" that every hint in this repo prints.
# Measured 2026-09-10 (main clone): this form exits 0, while the bare `python3`
# form exits 2 without measuring anything - the host's `python3` has no pytest.
INVOCATION = "uv run --no-sync python3 scripts/check-node-test-count.py"

GUI_TEST_SUFFIX = ".test.js"

# Agent.md's two lines, anchored on text rather than line numbers so an edit
# above them cannot silently move the target. The GUI anchor includes the
# backticked command, which also keeps it from matching the Renderer line.
GUI_LINE = re.compile(r"(?P<head>`cd emrg/gui && npm test` \()(?P<count>\d+)(?P<tail>: )")
RENDERER_LINE = re.compile(
    r"(?P<head>^Renderer: .*?npm test` \()(?P<count>\d+)(?P<tail>: )", re.M
)

VITEST_SUMMARY = re.compile(r"^\s*Tests\s+(\d+) passed\s+\((\d+)\)", re.M)
# ⚠️ Not `^(\W*)`: node prefixes its summary with `ℹ` (U+2139 INFORMATION
# SOURCE), which Python's Unicode-aware `\w` class *matches* - so `\W` is empty
# there and the pattern never fires (measured 2026-09-10: every summary line was
# skipped while the text was plainly on stdout). Use an explicit
# not-a-letter-or-digit class.
NODE_TESTS = re.compile(r"^[^A-Za-z0-9]*tests (\d+)$", re.M)
NODE_FAIL = re.compile(r"^[^A-Za-z0-9]*fail (\d+)$", re.M)
# A *call*, not a mention: the statement may be indented (integration.test.js
# calls it inside `if (SKIP) {`), while the prose mention in that same file's
# comment starts with `//`, so requiring `skip(` to be the first token on the
# stripped line separates the two (measured 2026-09-10: `^skip\(` found 0).
MODULE_SKIP_ENTRY = re.compile(r"^\s*skip\(", re.M)

# Both runners colour their summaries (`\x1b[2m Tests \x1b[22m \x1b[32m514 passed\x1b[39m`),
# so the escapes land *inside* the line a regex has to match. Measured
# 2026-09-10: without this strip, vitest's summary is unmatchable even though it
# is plainly on stdout. `\x1b\[[0-9;]*m` is the SGR subset both emit.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Runner output with colour escapes removed (parsers run on this)."""
    return _ANSI.sub("", text)


class NodeCountError(Exception):
    """The doc or the tree is not in a shape this tool can act on."""


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run a Node test runner and return its decoded output.

    Two host-hostile details, both measured on Windows (issue #1132) and both
    invisible from this repo's CI, which runs the gate on `ubuntu-latest` only
    while `test-windows` runs pytest and iscc:

    * **argv must be resolved.** `subprocess` appends only `.exe` on Windows, and
      npm ships as `npm.CMD`, so the bare `"npm"` in `measured_renderer()` reached
      `CreateProcess` as a file that does not exist: `cannot run 'npm': [WinError
      2]`. The tool whose docstring tells the reader to run it after touching a
      Node test file could not start the runner on the platform it was written
      for. `shutil.which` resolves that (and returns `/usr/bin/npm` unchanged on
      Linux/macOS, so the argv CI sees is identical); a name `which` cannot find
      is left alone so the existing `FileNotFoundError` path still names it.
    * **decoding cannot depend on the locale.** `text=True` alone decodes with the
      locale codec, and `node --test` prefixes its summary with `ℹ` (U+2139) -
      the very character `NODE_TESTS` is written to tolerate. Under cp936 that
      raises `UnicodeDecodeError` **inside the reader thread**, where `threading`
      swallows it, leaving `proc.stdout` as `None`; the failure then surfaced as
      `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'` on the
      concatenation below - a traceback the user cannot act on, for a condition
      the tool already knows how to report. `scripts/push-branch-from-api.py`
      documents this same GBK lesson at line 15; `errors="replace"` renders an
      undecodable byte as U+FFFD instead of losing the whole output.
    """
    resolved = shutil.which(cmd[0])
    if resolved:
        cmd = [resolved, *cmd[1:]]
    if not (cwd / "node_modules").exists():
        raise NodeCountError(
            f"{cwd} has no node_modules; run `npm install` there first - this "
            "tool measures the runners, it does not install them"
        )
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=full_env,
        )
    except FileNotFoundError as exc:
        raise NodeCountError(f"cannot run {cmd[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NodeCountError(f"`{' '.join(cmd)}` timed out after 900s") from exc
    # A `None` stream is what a swallowed decode error leaves behind - report it
    # as this tool's own error rather than letting the concatenation raise
    # TypeError past every handler in main().
    if proc.stdout is None or proc.stderr is None:
        raise NodeCountError(
            f"`{' '.join(cmd)}` in {cwd} produced no readable output "
            "(its output could not be decoded)"
        )
    output = _plain(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise NodeCountError(
            f"`{' '.join(cmd)}` in {cwd} failed (rc={proc.returncode}):\n"
            + output[-2000:].strip()
        )
    return output


def measured_renderer() -> int:
    """vitest's executed test total for the renderer suite."""
    output = _run(["npm", "test"], RENDERER_ROOT)
    match = VITEST_SUMMARY.search(output)
    if not match:
        raise NodeCountError(
            "could not parse vitest's summary line (`Tests  N passed (N)`):\n"
            + output[-2000:].strip()
        )
    passed, total = int(match.group(1)), int(match.group(2))
    if passed != total:
        raise NodeCountError(
            f"vitest reports {passed} passed of {total} renderer tests; this tool "
            "will not document a tree whose runner disagrees with itself"
        )
    return total


def module_skip_entries() -> int:
    """How many extra entries the GUI runner registers for module-level `skip(`."""
    base = GUI_ROOT / "test"
    files = sorted(base.glob(f"*{GUI_TEST_SUFFIX}"))
    assert files, f"no GUI test files found under {base}"
    return sum(len(MODULE_SKIP_ENTRY.findall(f.read_text(encoding="utf-8"))) for f in files)


def measured_gui() -> int:
    """node --test's executed *definition* count, as CI runs it."""
    output = _run(
        ["npm", "test"], GUI_ROOT, env={"EMRG_SKIP_INTEGRATION": "1"}
    )
    tests = NODE_TESTS.search(output)
    fails = NODE_FAIL.search(output)
    if not tests or not fails:
        raise NodeCountError(
            "could not parse node --test's summary (`tests N` / `fail N`):\n"
            + output[-2000:].strip()
        )
    failures = int(fails.group(1))
    if failures:
        raise NodeCountError(
            f"node --test reports {failures} failing GUI tests; this tool will not "
            "document a tree whose runner is red"
        )
    entries = module_skip_entries()
    if entries != 1:
        raise NodeCountError(
            f"expected exactly one module-level `skip(` reason entry in "
            f"emrg/gui/test/*{GUI_TEST_SUFFIX}, found {entries} - the runner's "
            "`tests` total no longer maps to the definition count by a constant, "
            "so this tool will not guess. Teach measured_gui() the new shape."
        )
    return int(tests.group(1)) - entries


def documented_counts(text: str) -> tuple[int, int]:
    """(renderer, GUI) headline counts as Agent.md claims them. Fails loud unless unambiguous."""
    renderer = list(RENDERER_LINE.finditer(text))
    gui = list(GUI_LINE.finditer(text))
    for matches, label in ((renderer, "Renderer"), (gui, "GUI")):
        if len(matches) != 1:
            raise NodeCountError(
                f"expected exactly one Agent.md {label} count line, found "
                f"{len(matches)}; this tool will not guess which one is real"
            )
    return int(renderer[0].group("count")), int(gui[0].group("count"))


def patch_count(pattern: re.Pattern[str], text: str, value: int) -> str:
    """Replace one headline count. Only that number may change."""
    match = pattern.search(text)
    if match is None:
        raise NodeCountError("the Agent.md count line disappeared before the patch")
    patched = text[: match.start("count")] + str(value) + text[match.end("count") :]
    mask = pattern.sub(lambda m: m.group("head") + "#" + m.group("tail"), text)
    masked_patched = pattern.sub(lambda m: m.group("head") + "#" + m.group("tail"), patched)
    if mask != masked_patched:
        raise NodeCountError(
            "refusing to write: the patch would change more than the count"
        )
    return patched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage\n-----", 1)[0].strip(),
        epilog=__doc__.split("Usage\n-----", 1)[-1].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite Agent.md")
    mode.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    args = parser.parse_args(argv)

    try:
        text = DOC.read_text(encoding="utf-8")
        renderer_doc, gui_doc = documented_counts(text)
        renderer_real = measured_renderer()
        gui_real = measured_gui()
    except NodeCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot read {DOC}: {exc}", file=sys.stderr)
        return 2

    drift = []
    if renderer_doc != renderer_real:
        drift.append(f"Renderer: documents {renderer_doc}, runner executed {renderer_real}")
    if gui_doc != gui_real:
        drift.append(f"GUI: documents {gui_doc}, runner executed {gui_real}")

    if not drift:
        print(
            f"OK: {DOC.name} documents {renderer_real} renderer + {gui_real} GUI tests "
            "(both runners agree)"
        )
        return 0

    for line in drift:
        print(f"FAIL: {line}")
    if not (args.write or args.dry_run):
        print(f"\nFix with: {INVOCATION} --write")
        return 1

    if args.dry_run:
        print(
            f"\n(dry run - would set renderer {renderer_doc} -> {renderer_real} "
            f"and GUI {gui_doc} -> {gui_real}, no files written)"
        )
        return 0

    try:
        patched = text
        if renderer_doc != renderer_real:
            patched = patch_count(RENDERER_LINE, patched, renderer_real)
        if gui_doc != gui_real:
            patched = patch_count(GUI_LINE, patched, gui_real)
        DOC.write_text(patched, encoding="utf-8")
    except NodeCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot write {DOC}: {exc}", file=sys.stderr)
        return 2

    print(
        f"\nupdated {DOC.name}: renderer {renderer_doc} -> {renderer_real}, "
        f"GUI {gui_doc} -> {gui_real}"
    )
    print("Next: uv run --no-sync pytest tests/test_doc_counts.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
