#!/usr/bin/env python3
"""Run one mutation arm and judge it, separating "the test failed" from "nothing ran".

The class this exists for
-------------------------
A mutation arm is the only evidence that a test *depends on* the line it claims to
test: break the line, and the test must die. Every cycle here writes a few, by hand,
in a throwaway script under the session's `tmp/` - and the same three mistakes have
been made by hand, repeatedly, because the judgement "the run failed, so the arm is
killed" is wrong in ways that *look identical* to success:

* **nothing ran.** A node id that does not resolve - most often a class method named
  without its class, so `tests/test_x.py::test_y` instead of
  `tests/test_x.py::TestC::test_y` - makes pytest exit **4** (measured 2026-09-26:
  `USAGE_ERROR`). A harness that judges by `rc != 0` reports KILLED for an arm that
  executed no test at all. This is not hypothetical: a cycle recorded five of five
  arms "killed" while none had run, and this tool's first own use reproduced it three
  times in one sitting.
* **the mutation did not parse.** A source edit that produces invalid Python makes
  pytest *error* rather than fail - and which code it returns depends on **how the
  target is named**, not on the error (all three rows driven through this tool
  2026-10-03, `cyc20261003-202837`): a fixture that imports the module at run time
  gives **1**, the same code as a failing test; a collection-time import gives **2**
  for a path target (`tests/test_x.py`) and **4** for a node-id target
  (`tests/test_x.py::test_y` - the form this tool's own usage example passes), because
  pytest reports an uncollectable node as a usage error. So an error is
  indistinguishable from a real kill to a harness that only asks "did it fail?", and
  on the node-id form it is also indistinguishable, by exit code alone, from a
  *mistyped* target - which is why the mutated text is compiled here and the syntax
  error it names is what the report gives, instead of a guess read off the code.
* **the restore is not a snapshot.** Restoring a mutated file with `git checkout --`
  silently reverts *uncommitted* work: an arm run inside a cycle, whose change is not
  committed yet, deletes the very change the next arm mutates.

Measured exit codes, on this repository's pytest (2026-09-26), which is what the
judgement below is built on:

    passing target                                 0
    a test that failed                             1
    an *error* - a fixture that blew up loading   1   (measured: mutating a file the
      a module that cannot parse                       `mod` fixture imports)
    a collection-time error - a syntax error       2   (INTERRUPTED; a *path* target.
      in a file pytest imports while collecting          A node-id target gives 4 here
                                                          instead - pytest reports an
                                                          uncollectable node as a usage
                                                          error. This tool compiles the
                                                          mutated text, so it names the
                                                          syntax error either way.)
    a node id that does not resolve                4   (USAGE_ERROR)
    nothing collected (`-k` matching nothing)      5

The row that matters most is the third: **an error is exit 1, the same code as a
failure.** So `--expect` is not a nicety - without it, a mutation that breaks the
loading of the module under test is indistinguishable from a mutation a test
actually caught, and that is the whole failure mode this tool exists to remove.

The judgement therefore has three states, not two:

    rc 0                                  -> SURVIVED   (the mutation is not covered)
    rc 1 and the expected assertion text  -> KILLED     (the arm is evidence)
    anything else                         -> UNJUDGEABLE, with the reason named

The pre-flight is the other half of it: the target is run **before** the mutation, and
an arm whose target does not collect and pass then is refused outright. That is what
makes a later exit 4 attributable to the mutation rather than to a mistyped node id.

Naming the reason is not enough when the reason is "your `--expect` text is not in the
output": that verdict costs the caller a second pytest run typed by hand to find a
fragment that *is*. So an UNJUDGEABLE report also prints the failure lines the run
echoed - pytest's `>` source line and its `E` explanation, marker stripped, capped at
`_ASSERTION_CANDIDATES` - and any of them can be handed straight back as `--expect`,
whether the failure was an assertion or a raised exception (see `_ECHOED_LINE`).
Measured 2026-09-26 (`cyc20260926-140150`), the shape that costs the round: an arm whose
`--expect` was copied from the test's *message*, which pytest never prints because an
earlier assertion on the same test fires first (it echoed `assert 1 < 0`; that fragment
killed the arm in one step). The block is printed for this verdict only - for KILLED the
expectation already matched, and for the refusals there is nothing to search for.

What it does, in order
----------------------
1. snapshot the named file and resolve the mutation anchor (exactly one occurrence);
2. pre-flight: run the target unmutated - it must collect and pass (`--no-preflight`
   is the escape hatch for an arm whose target is already red, and says so in the
   verdict, because the attribution is then the reader's);
3. apply the replacement and assert the file really changed;
4. run the target: `HOME`/`TMPDIR`/`TMP`/`TEMP` are pinned to a fresh temporary
   directory **for the child only** (`emrg/server/evolution_prompt.md` states the
   rule: a temp-root home is itself a writable zone, so a *process-wide* pinned `HOME`
   turns a sandbox test red - a false red, not a regression), bytecode writing is
   switched off and the mutated file's own `__pycache__` is purged, because a `.pyc`
   whose `(mtime, size)` header still matches can answer for the mutation
   (`scripts/check-merge-plan-suite.py` carries that measurement);
5. restore from the snapshot and assert it came back **byte for byte**.

Why this is not in the `check*.py` family
-----------------------------------------
That family's rule is "a guard that reads a working tree names the tree it read before
it gives a verdict". This tool *modifies* the tree it names, so it is not a health
reading of one - but the convention's substance still applies and the cost is one
line, so the tree is printed first anyway.

Usage
-----
    uv run --no-sync python3 scripts/run-mutation-arm.py \
        --file scripts/check-pr-base.py \
        --old 'where = "named" if args.prs else "open"' \
        --new 'where = "open"' \
        --node 'tests/test_check_pr_base.py::TestTheSummaryCountsTheSelectionNotTheRepository::test_a_named_list_is_not_reported_as_the_open_set' \
        --expect '1 of the 1 named PR(s)'

`--node` is repeatable. `--json` prints one object instead of the prose report.

Exit codes
----------
    0  KILLED           the mutation made the target fail on the expected assertion
    1  SURVIVED         the target still passed with the mutation in place
    2  UNJUDGEABLE      the run does not separate the two (wrong node id, a mutation
                        that does not parse, nothing collected, an unrelated failure)
    3  TARGET-BROKEN    before mutating, the target did not collect or did not pass
    4  NO-MUTATION      the replacement left the file byte-identical, or the anchor
                        does not occur exactly once
    5  RESTORE-MISMATCH the file did not come back byte for byte

The file is restored on every path, including a failure inside this tool.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: pytest's exit codes, named where the judgement reads them, each measured on this
#: repository's pytest (2026-09-26). `PYTEST_TEST_FAILED` is also what pytest returns
#: for an *error* - a fixture that blew up loading a module that cannot parse - which
#: is why `--expect` is required rather than optional.
PYTEST_OK = 0
PYTEST_TEST_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS = 5

KILLED = "KILLED"
SURVIVED = "SURVIVED"
UNJUDGEABLE = "UNJUDGEABLE"
TARGET_BROKEN = "TARGET-BROKEN"
NO_MUTATION = "NO-MUTATION"
RESTORE_MISMATCH = "RESTORE-MISMATCH"

EXIT_KILLED = 0
EXIT_SURVIVED = 1
EXIT_UNJUDGEABLE = 2
EXIT_TARGET_BROKEN = 3
EXIT_NO_MUTATION = 4
EXIT_RESTORE_MISMATCH = 5

#: `1 passed in 0.02s`, `3 passed, 1 warning in 0.10s` - the count pytest prints.
_PASSED = re.compile(r"(\d+) passed")

#: pytest's two echoed forms of what failed: the source line it writes with `>`, and its
#: explanation, written with `E` - which carries the values substituted
#: (`E   assert 0 == 1`). Both are text the run really printed, so either can be handed
#: straight back as `--expect`.
#:
#: This exists for one verdict. UNJUDGEABLE means the `--expect` fragment did not
#: appear in the output, and the caller's next move is to find one that does - which,
#: without this, is a second pytest run typed by hand. Measured 2026-09-26
#: (`cyc20260926-140150`): two arms in one cycle came back UNJUDGEABLE on a fragment
#: copied from the test's *message*, and both were re-run by hand to find the assertion
#: that fires first.
#:
#: An `E` line is offered whether or not it contains the word `assert`. A mutation most
#: often breaks behaviour by **raising**, and pytest's explanation of a raised exception
#: carries no assertion at all - it is `E   TypeError: object of type 'NoneType' has no
#: len()`. Requiring the keyword meant exactly those arms offered **nothing**: measured
#: 2026-10-04, feeding this function pytest's own output for a `TypeError` returned `[]`
#: while the `assert` form returned its line - so the one verdict whose whole job is to
#: hand the fragment back printed an empty report and the caller re-ran pytest by hand,
#: which is the cost this exists to remove. The claim that made the keyword look
#: necessary ("a caller who pasted one would come back with a second UNJUDGEABLE") is
#: false: `--expect` only has to appear in the run's output, and the `E` line does. That
#: is how the arm whose report came back empty was finally judged.
#:
#: What stays out is pytest's *context* for an assertion - the `+  where ...` lines,
#: which explain the values inside the assertion rather than naming the failure - and a
#: `>` source echo carrying no assertion, which names the mutated line the caller wrote
#: themselves.
_ECHOED_LINE = re.compile(r"^(?P<marker>[>E])\s+(?P<text>.*)$")

#: How many candidates the report prints, and how long each may be: the report is read
#: from a terminal, and the first few lines of a failure are where its assertion is.
_ASSERTION_CANDIDATES = 5
_ASSERTION_MAX_CHARS = 200


def _purge_bytecode(target: Path) -> list[str]:
    """Delete the bytecode caches that could answer for the mutated file.

    Mirrors `scripts/check-merge-plan-suite.py::_purge_bytecode`, in the one place an
    arm needs it, and for the reason measured there: CPython prefers a `.pyc` whose
    header `(mtime, size)` pair still matches, so an edit that keeps the file's length
    and lands in the same second can be invisible to the run that judges the arm.
    """
    removed: list[str] = []
    for parent in [target.parent, *target.parents[:3]]:
        cache = parent / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            removed.append(str(cache))
    return removed


def _run_target(node: list[str], cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the named pytest target, with the child-only environment pinning.

    `HOME`/`TMPDIR`/`TMP`/`TEMP` are the *arm's* temp root and are passed to this child
    only - pinning them for the whole tool (or worse, for the whole suite) is the
    false-red `emrg/server/evolution_prompt.md` records.
    """
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        TMPDIR=str(home),
        TMP=str(home),
        TEMP=str(home),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", *node, "-q", "-p", "no:cacheprovider"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def _failed_node(out: str) -> str:
    """The first `FAILED <node>` line pytest printed, or empty - evidence, not a count."""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            return stripped[len("FAILED ") :].split(" - ")[0].strip()
    return ""


def _passed_count(out: str) -> int:
    """How many tests passed, read from pytest's own summary line (0 when it says none)."""
    found = _PASSED.findall(out)
    return int(found[-1]) if found else 0


def _assertion_lines(out: str) -> list[str]:
    """The lines pytest echoed as the failure, marker stripped - candidates for `--expect`.

    Order is the run's, duplicates are collapsed (a failure reports each assertion
    once, but a parametrised id can repeat a line in the summary), and the list is
    capped: this is offered as a fragment to retype, not as a copy of the report.

    Both of pytest's explanations qualify, an assertion and a raised exception alike -
    see `_ECHOED_LINE`, which measures why the second one is not optional.

    Empty stays a real answer and is not an error: a run that failed without echoing a
    failure line at all - a collection error, a conftest that raised before any test
    body ran - has nothing to offer, and saying so by printing nothing is better than
    inventing a candidate. What changed is which runs those are: a test that *ran* and
    raised is no longer one of them.
    """
    seen: list[str] = []
    for line in out.splitlines():
        match = _ECHOED_LINE.match(line)
        if match is None:
            continue
        text = match.group("text").strip()
        if not text or text.startswith("+"):
            # pytest's `+  where ...` block: it explains the values *inside* an
            # assertion, it is not the line that failed, and a caller handed one would
            # be pasting context rather than a failure.
            continue
        if match.group("marker") == ">" and "assert" not in text:
            # A `>` line is the failing *source* line. Worth offering when it carries
            # the assertion; a bare source echo is the mutated line's own text, which
            # the caller wrote and every arm for that line would print.
            continue
        text = text[:_ASSERTION_MAX_CHARS]
        if text not in seen:
            seen.append(text)
        if len(seen) >= _ASSERTION_CANDIDATES:
            break
    return seen


def _syntax_error(text: str, name: str) -> str:
    """Why `text` is not Python, or "" when it parses.

    Measured 2026-10-03 (`cyc20261003-202837`): a mutation that breaks the syntax makes
    pytest fail to collect the target, and the code it returns depends on the target
    form - **4** for a node id (`tests/test_x.py::test_y`, the form this tool's usage
    example passes), **2** for a path. The exit-4 branch used to read that as "the
    target no longer resolves" and send the caller to re-check a node id the pre-flight
    had already proved good. Compiling the mutated text is the determinate answer, it
    costs one call, and it is available *before* the run rather than inferred from a
    code that means two things.
    """
    try:
        compile(text, name, "exec")
    except SyntaxError as exc:
        where = f"line {exc.lineno}" if exc.lineno else "an unknown line"
        return f"{exc.msg} at {where}"
    except ValueError as exc:
        # NUL bytes and the like: not a SyntaxError, still not importable.
        return str(exc)
    return ""


def _why_unjudgeable(rc: int, expect: str, out: str, syntax_error: str = "") -> str:
    if syntax_error:
        # Determinate, and it comes first: the mutated text does not parse, which is
        # *this arm's* doing, so no code the run returned needs interpreting.
        return (
            f"the mutated file no longer parses - {syntax_error}. pytest exited {rc} "
            "because the target could not be imported, not because a test failed on the "
            "line this arm breaks; no test ran, so this is not a kill"
        )
    if rc == PYTEST_USAGE_ERROR:
        return (
            "pytest exited 4 (usage error) AFTER a pre-flight that collected and passed, "
            "and the mutated file still parses - check the target spelling (a class "
            "method needs its class: tests/test_x.py::TestC::test_y). No test ran, so "
            "this is not a kill"
        )
    if rc == PYTEST_INTERRUPTED:
        return (
            "pytest exited 2 (interrupted): collection itself failed, so no test ran "
            "against the mutation - the usual cause is that the mutated file is imported "
            "while tests are collected and no longer parses. Not a kill"
        )
    if rc == PYTEST_NO_TESTS:
        return "pytest exited 5: no test was collected, so nothing judged the mutation"
    if rc == PYTEST_TEST_FAILED:
        return (
            f"the target failed (or errored), but not on the assertion this arm names: "
            f"{expect!r} does not appear in the output. pytest returns 1 for a failed "
            "test and for a fixture that cannot load the mutated module alike, so a "
            "harness that only asks whether the run failed calls both of them kills"
        )
    return f"pytest exited {rc}, which separates neither outcome"


def _apply(text: str, old: str, new: str) -> str | None:
    """The mutated text, or None when the anchor is not uniquely present."""
    if text.count(old) != 1:
        return None
    return text.replace(old, new, 1)


class Arm:
    """One arm's state, so the verdict is computed before anything is printed."""

    def __init__(self, args: argparse.Namespace, cwd: Path, target: Path, original: str):
        self.args = args
        self.cwd = cwd
        self.target = target
        self.original = original
        self.preflight = "skipped" if args.no_preflight else "pending"
        self.failed_node = ""
        self.restored: bool | None = None
        self.verdict = ""
        self.why = ""
        self.code = EXIT_UNJUDGEABLE
        #: the observed rc of the post-mutation run, and how many tests passed in it -
        #: the evidence behind the verdict, printed so a reader can check it rather
        #: than trust it.
        self.mutated_rc: int | None = None
        self.mutated_passed: int | None = None
        #: the failure lines the post-mutation run echoed, as candidates for
        #: `--expect` - printed when the verdict is UNJUDGEABLE, which is the one
        #: state where the caller has to retype the fragment. Not only assertions:
        #: a mutation that breaks the code by raising is explained by a line with no
        #: `assert` in it at all.
        self.assertions: list[str] = []
        #: why the mutated text is not Python, or "" - read before the run, so the
        #: verdict never has to infer the cause from an exit code that means two
        #: different things depending on how the target was named.
        self.syntax_error = ""

    def decide(self, verdict: str, why: str, code: int) -> None:
        self.verdict, self.why, self.code = verdict, why, code

    def as_dict(self) -> dict[str, object]:
        return {
            "tree": str(self.cwd),
            "file": str(self.target),
            "label": self.args.label,
            "node": list(self.args.node),
            "expect": self.args.expect,
            "preflight": self.preflight,
            "failed_node": self.failed_node,
            "mutated_rc": self.mutated_rc,
            "mutated_passed": self.mutated_passed,
            "assertions": list(self.assertions),
            "syntax_error": self.syntax_error,
            "restored": self.restored,
            "verdict": self.verdict,
            "why": self.why,
            "exit": self.code,
        }


def _report(arm: Arm, as_json: bool) -> None:
    if as_json:
        print(json.dumps(arm.as_dict(), indent=2, sort_keys=True))
        return
    if arm.args.label:
        print(f"arm: {arm.args.label}")
    # The tree first, before any verdict: this tool writes to the tree it names, and
    # a reader has to be able to tell which checkout was modified.
    print(f"tree: {arm.cwd}")
    print(f"file: {arm.target}")
    print(f"target: {', '.join(arm.args.node)}")
    print(f"preflight: {arm.preflight}")
    print(f"mutated run: rc={arm.mutated_rc} passed={arm.mutated_passed}")
    print(f"failed node: {arm.failed_node or '-'}")
    print(f"restored byte-for-byte: {arm.restored}")
    print(f"verdict: {arm.verdict} - {arm.why}")
    if arm.verdict == UNJUDGEABLE and arm.assertions:
        # Printed only for the verdict whose reader has to retype the fragment. For
        # KILLED the expectation already matched, and for the other states the reason
        # is a refusal rather than a search - so this stays out of the common report.
        print("the run did echo these failure lines, any of which can be --expect:")
        for text in arm.assertions:
            print(f"  {text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run-mutation-arm.py",
        description=(
            "Run one mutation arm and judge it: KILLED only when the named target "
            "really failed on the expected assertion"
        ),
    )
    parser.add_argument("--file", required=True, help="the file to mutate")
    parser.add_argument("--old", required=True, help="the text to replace (must occur once)")
    parser.add_argument("--new", required=True, help="what to replace it with")
    parser.add_argument(
        "--node",
        required=True,
        action="append",
        help="pytest target (node id or path); repeatable",
    )
    parser.add_argument(
        "--expect",
        required=True,
        help=(
            "text that only THIS arm's failure prints; a failure without it is not this "
            "arm's. The failing assertion's own source line is the most reliable choice, "
            "because pytest always echoes it (e.g. 'assert 2 == 3', or "
            "'assert (tree / \"subject.py\").read_text'). A fragment copied from the "
            "test's *message* can be missed when an earlier assertion in the same test "
            "fails first - and when that happens the verdict is UNJUDGEABLE and the "
            "report prints the failure lines the run did echo, so the retry is one "
            "step rather than a second run typed by hand. A mutation that makes the "
            "code raise is explained the same way: its candidate is pytest's `E` "
            "line carrying the exception"
        ),
    )
    parser.add_argument("--label", default="", help="what this arm breaks, for the report")
    parser.add_argument("--cwd", default="", help="the tree to run in (default: this checkout)")
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip the pre-mutation run (for an arm whose target is already red)",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object instead")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve() if args.cwd else Path(__file__).resolve().parent.parent
    target = Path(args.file)
    if not target.is_absolute():
        target = cwd / target

    original = target.read_text(encoding="utf-8") if target.is_file() else None
    arm = Arm(args, cwd, target, original or "")
    if original is None:
        # Every verdict leaves through `_report`, including this one: an exit code with
        # no prose is the failure this family keeps naming.
        arm.decide(UNJUDGEABLE, f"no such file: {target}", EXIT_UNJUDGEABLE)
        _report(arm, args.json)
        return arm.code

    mutated = _apply(original, args.old, args.new)
    if mutated is None:
        occurrences = original.count(args.old)
        arm.decide(
            NO_MUTATION,
            f"the anchor occurs {occurrences} time(s) in {target.name}; a replacement "
            "needs exactly one site, or the arm mutates something other than what it "
            "names",
            EXIT_NO_MUTATION,
        )
        _report(arm, args.json)
        return arm.code
    if mutated == original:
        arm.decide(NO_MUTATION, "the replacement left the file byte-identical", EXIT_NO_MUTATION)
        _report(arm, args.json)
        return arm.code

    # Read from the mutated text, before anything runs: a mutation that breaks the
    # syntax is this arm's own doing, and it is the one cause whose exit code varies
    # with how the target was named (`_syntax_error` records that measurement).
    if target.suffix == ".py":
        arm.syntax_error = _syntax_error(mutated, target.name)

    home = Path(tempfile.mkdtemp(prefix="emrg-arm-"))
    mutated_on_disk = False
    try:
        if not args.no_preflight:
            proc = _run_target(args.node, cwd, home)
            passed = _passed_count(_combined(proc))
            if proc.returncode != PYTEST_OK or passed < 1:
                # A refusal is a verdict like any other, so it goes through the same
                # report path: an unattributed non-zero exit is the failure this tool
                # exists to prevent, and that includes this tool's own.
                arm.preflight = f"refused (rc={proc.returncode}, {passed} passed)"
                arm.decide(
                    TARGET_BROKEN,
                    f"before any mutation the target exited {proc.returncode} with "
                    f"{passed} passed. An arm can only attribute a failure to its "
                    "mutation if the target collected and passed first - check the node "
                    "id (a class method needs its class: "
                    "tests/test_x.py::TestC::test_y)",
                    EXIT_TARGET_BROKEN,
                )
            else:
                arm.preflight = f"{passed} passed"

        if not arm.verdict:
            try:
                target.write_text(mutated, encoding="utf-8")
                mutated_on_disk = True
                _purge_bytecode(target)
                proc = _run_target(args.node, cwd, home)
                out = _combined(proc)
                arm.mutated_rc = proc.returncode
                arm.mutated_passed = _passed_count(out)
                arm.failed_node = _failed_node(out)
                arm.assertions = _assertion_lines(out)
                if proc.returncode == PYTEST_OK:
                    arm.decide(
                        SURVIVED,
                        "the target still passed with the mutation in place, so it does "
                        "not depend on the line this arm breaks",
                        EXIT_SURVIVED,
                    )
                elif proc.returncode == PYTEST_TEST_FAILED and args.expect in out:
                    arm.decide(
                        KILLED,
                        f"the target failed on the expected assertion ({args.expect!r})",
                        EXIT_KILLED,
                    )
                else:
                    arm.decide(
                        UNJUDGEABLE,
                        _why_unjudgeable(
                            proc.returncode, args.expect, out, arm.syntax_error
                        ),
                        EXIT_UNJUDGEABLE,
                    )
            finally:
                if mutated_on_disk:
                    target.write_text(original, encoding="utf-8")
                    arm.restored = target.read_text(encoding="utf-8") == original
                else:
                    arm.restored = target.read_text(encoding="utf-8") == original
    finally:
        shutil.rmtree(home, ignore_errors=True)

    if arm.restored is False:
        arm.decide(
            RESTORE_MISMATCH,
            f"{target} did not come back byte for byte - restore it by hand before "
            "anything else reads this tree",
            EXIT_RESTORE_MISMATCH,
        )
    _report(arm, args.json)
    return arm.code


if __name__ == "__main__":
    raise SystemExit(main())
