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
fragment that *is*. So an UNJUDGEABLE report also prints the assertion lines the run
echoed - pytest's `>` source line and its `E` explanation, marker stripped, capped at
`_ASSERTION_CANDIDATES` - and any of them can be handed straight back as `--expect`.
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
4. run the target, under the interpreter `_pytest_interpreter` resolves once for both
   runs (the invoking one, or the checkout's own `.venv` when that one cannot import
   pytest - the rule is asked of `scripts/check-merge-plan-suite.py`, which owns it):
   `HOME`/`TMPDIR`/`TMP`/`TEMP` are pinned to a fresh temporary
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
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
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

#: pytest's own words when the interpreter the target was run with cannot import it.
#: Both spellings, because the two ways that happens print differently: `-m pytest` with
#: the module absent prints `<python>: No module named pytest` (measured 2026-10-05 on
#: this host's python3.13), and an ImportError raised while collecting prints the quoted
#: `ModuleNotFoundError: No module named 'pytest'`. A matcher for one of the two would
#: miss half the readings it exists for, which is what the `identity imported` /
#: `3 identities imported` lesson in `emrg/server/evolution_prompt.md` §1.1 records.
#:
#: The module is **captured and compared**, not spelled into the pattern, because this
#: sentence names every module a run could not import: `No module named 'pytest_asyncio'`
#: is a missing *plugin* - pytest imports and a plugin does not - and a literal `pytest`
#: with no right boundary read it as "the interpreter cannot import pytest", which is a
#: cause nobody measured (veto, measured 2026-10-06 by `cyc20261006-131455`; the three
#: plugin names it listed all fired). Reading the name out is what makes the third cause
#: separable at all - `_missing_module` is the reader, and its callers decide what the
#: name means.
_NO_MODULE = re.compile(r"No module named '?([A-Za-z_][\w.]*)'?")


def _missing_module(out: str) -> str:
    """The module this run's output says it could not import, or `""` if it named none.

    A trailing `.` is stripped: `No module named pytest.` is the same reading as
    `No module named pytest` (the period belongs to the sentence around it, not to the
    name), while `pytest.core` keeps its dot and is therefore *not* pytest itself - the
    distinction the veto turned on, where an unanchored matcher could not tell them apart.
    """
    match = _NO_MODULE.search(out)
    return match.group(1).rstrip(".") if match else ""

#: pytest's two echoed forms of the assertion that failed: the source line it writes
#: with `>`, and its explanation, written with `E` - which carries the values
#: substituted (`E   assert 0 == 1`). Both are text the run really printed, so either
#: can be handed straight back as `--expect`.
#:
#: This exists for one verdict. UNJUDGEABLE means the `--expect` fragment did not
#: appear in the output, and the caller's next move is to find one that does - which,
#: without this, is a second pytest run typed by hand. Measured 2026-09-26
#: (`cyc20260926-140150`): two arms in one cycle came back UNJUDGEABLE on a fragment
#: copied from the test's *message*, and both were re-run by hand to find the assertion
#: that fires first.
#:
#: The `assert` keyword is required so the explanation lines pytest writes *about* a
#: failure (`+  where 0 = ...`, `AttributeError: ...`) are not offered as candidates:
#: they are not assertions, and a caller who pasted one would get UNJUDGEABLE again.
_ECHOED_ASSERTION = re.compile(r"^[>E]\s+(?P<text>.*\bassert\b.*)$")

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


def _run_target(
    node: list[str], cwd: Path, home: Path, python: str
) -> subprocess.CompletedProcess[str]:
    """Run the named pytest target, with the child-only environment pinning.

    `python` is the interpreter the arm is judged under, resolved by
    `_pytest_interpreter` - not `sys.executable` unconditionally. The two differ only
    when the interpreter this tool was invoked with cannot import pytest at all, which
    is never a deliberate choice: an arm cannot be judged by an interpreter that has no
    runner, so that case is a refusal today and is the wrong answer to the question the
    caller asked (see `_pytest_interpreter` for the measurement).

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
        [python, "-m", "pytest", *node, "-q", "-p", "no:cacheprovider"],
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
    """The assertion lines pytest echoed, marker stripped - candidates for `--expect`.

    Order is the run's, duplicates are collapsed (a failure reports each assertion
    once, but a parametrised id can repeat a line in the summary), and the list is
    capped: this is offered as a fragment to retype, not as a copy of the report.

    Empty is a real answer and is not an error: a run that failed without echoing an
    assertion - a collection error, a fixture that raised - has nothing to offer, and
    saying so by printing nothing is better than inventing a candidate.
    """
    seen: list[str] = []
    for line in out.splitlines():
        match = _ECHOED_ASSERTION.match(line)
        if match is None:
            continue
        text = match.group("text").strip()[:_ASSERTION_MAX_CHARS]
        if text and text not in seen:
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


#: The gate that already owns the answer to *which interpreter runs pytest here*.
#: Loaded from its file rather than copied, the same way `check-merge-plan-suite.py`
#: loads `check-merge-sequence.py` and both load `merge_tree.py`: a rule with two
#: implementations is a rule free to drift apart, and this one has already been answered
#: twice on one host. Measured 2026-10-05 (`cyc20261005-212445`), under a bare `python3`
#: (this host's resolves to the *installed* interpreter, which has no pytest):
#:
#:     check-merge-plan-suite._suite_interpreter() -> <checkout>/.venv/bin/python
#:     run-mutation-arm: TARGET-BROKEN, "check the node id"
#:
#: One question, two answers - the gate resolved an interpreter that can run the thing it
#: measures, and this tool refused the arm while naming a cause that was not the cause.
_GATE = Path(__file__).resolve().parent / "check-merge-plan-suite.py"


#: The answer, once loaded: the gate is asked per run, and re-executing its module on
#: every call would be both wasteful and wrong for a reader who needs to see that this
#: tool has one source for the rule (`_pytest_interpreter`'s callers include the tests
#: that pin the delegation).
_GATE_MODULE: object | None = None


def _load_gate():
    """The gate module, loaded from its file once and kept.

    No failure is handled *here*, on purpose: a gate that is missing, unreadable or will
    not compile raises out of `exec_module`, and `main` turns that into the UNJUDGEABLE
    verdict (`_why_no_interpreter`) rather than a traceback. The `if spec is None or
    spec.loader is None` guard that used to sit here was **unreachable for exactly that
    case**, which made it a refusal that could never fire: `spec_from_file_location`
    returns a spec *and* a `SourceFileLoader` for a path that does not exist (measured
    2026-10-06, `cyc20261006-020931`: `ModuleSpec(name='x', loader=<SourceFileLoader …>,
    origin='/definitely/not/here.py')`), so the `RuntimeError` was dead code and the live
    path was an uncaught `FileNotFoundError`.
    """
    global _GATE_MODULE
    if _GATE_MODULE is not None:
        return _GATE_MODULE
    spec = importlib.util.spec_from_file_location("check_merge_plan_suite", _GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _GATE_MODULE = module
    return module


def _pytest_interpreter(own: str | None = None, root: Path | None = None) -> str:
    """The interpreter the arm is judged under: the caller's, or one that can import pytest.

    Asked of the gate, never re-implemented here. The gate's rule is "this interpreter if
    it can import pytest, else the checkout's own `.venv`", and it is the right rule for an
    arm for the same reason it is right for the suite: an arm judged by an interpreter with
    no runner has measured nothing, and refusing is not more honest than using the
    interpreter the checkout ships. The resolved path is reported, so the evidence names
    the environment it came from rather than leaving it implied.
    """
    return _load_gate()._suite_interpreter(own, root)


def _why_no_interpreter(exc: BaseException) -> str:
    """Why the arm could not be judged at all, because the rule could not be asked.

    The interpreter is not resolved here: it is *asked of* `_GATE`, a file beside this
    tool (`_pytest_interpreter`). A gate that is missing, unreadable or will not compile
    therefore leaves the question unanswered - and an arm whose interpreter is unknown has
    measured nothing, which is what this verdict means rather than a refusal about the
    target. Nothing has been run and nothing has been written when this is printed.

    This exists because the alternative was measured, not imagined (2026-10-06,
    `cyc20261006-020931`): with the gate unloadable the tool exited **1** - which this file
    defines as `EXIT_SURVIVED` - after a traceback and with no `verdict:` line, so a caller
    reading the exit code read "the target still passed with the mutation in place" out of
    a run that never started.
    """
    return (
        f"the interpreter to judge the arm under could not be resolved: "
        f"{type(exc).__name__}: {exc}. This tool asks {_GATE.name}, beside it, for the rule, "
        "so that gate has to be present and loadable; nothing has been run and nothing has "
        "been mutated. An arm whose interpreter could not be resolved has measured nothing, "
        "which is what this verdict means - check the gate first "
        "(uv run --no-sync python3 scripts/check-merge-plan-suite.py --help)"
    )


def _why_target_broken(rc: int, passed: int, out: str, interpreter: str) -> str:
    """Why the pre-flight refused, read from the run's own output.

    Three causes reach this refusal with different remedies, so the reason is read rather
    than assumed. Measured 2026-10-05 (`cyc20261005-191637`), on this host: running this
    tool with an interpreter that cannot import pytest - a bare `python3` here resolves
    to the *installed* interpreter, not the checkout's - gave `rc=1, 0 passed`, and the
    message sent the caller to re-check a node id that the same tool, under the
    checkout's runner, runs green (`uv run --no-sync pytest <node>` -> `1 passed`). The
    output said which of the two it was all along, and this function reads it.

    The third is the same sentence naming a different module, and it was the veto's
    subject (2026-10-06, `cyc20261006-131455`): a run whose output says
    `No module named 'pytest_asyncio'` had measurable pytest and one missing plugin, and
    the two-cause reader called it "the interpreter cannot import pytest". A report that
    names a cause has to have read it, so the module is named and only `pytest` itself
    takes the interpreter branch.

    `interpreter` is the one the run *actually used*, so the message is about the thing
    that ran: it must not name `sys.executable` when the resolver substituted a different
    one. Every branch names it, so the causes stay separable from the report even when the
    detector does not fire.
    """
    missing = _missing_module(out)
    if missing == "pytest":
        line = next(text for text in out.splitlines() if _NO_MODULE.search(text)).strip()
        return (
            f"before any mutation the target exited {rc} with {passed} passed, because "
            f"the interpreter this tool ran it with cannot import pytest: {line}. This "
            f"tool runs the target with the interpreter it resolved ({interpreter}), so "
            "the node id is not the cause - and because the resolver already fell back to "
            "the checkout's own `.venv`, more than one interpreter has been tried: "
            "install pytest (`uv sync`) or run the arm through the checkout's runner "
            "(uv run --no-sync python3)"
        )
    if missing:
        line = next(text for text in out.splitlines() if _NO_MODULE.search(text)).strip()
        return (
            f"before any mutation the target exited {rc} with {passed} passed, because "
            f"the run could not import a module: {line}. The target never collected, so "
            f"the node id is not the cause; the module named there is, and it is not "
            f"pytest itself - the run named {missing!r}, which is read apart from "
            f"pytest because a module that will not import is not an interpreter that "
            f"lacks pytest. Install it in the environment this tool runs with "
            f"({interpreter}), or run the arm through the checkout's runner "
            "(uv run --no-sync python3)"
        )
    return (
        f"before any mutation the target exited {rc} with {passed} passed. An arm can "
        "only attribute a failure to its mutation if the target collected and passed "
        "first - check the node id (a class method needs its class: "
        "tests/test_x.py::TestC::test_y). The run was made with the interpreter this "
        f"tool resolved ({interpreter}); if that is not the one you meant, run the arm "
        "through the checkout's runner(s) rather than a bare python3, which can resolve "
        "to the installed interpreter"
    )


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
        #: the assertion lines the post-mutation run echoed, as candidates for
        #: `--expect` - printed when the verdict is UNJUDGEABLE, which is the one
        #: state where the caller has to retype the fragment.
        self.assertions: list[str] = []
        #: why the mutated text is not Python, or "" - read before the run, so the
        #: verdict never has to infer the cause from an exit code that means two
        #: different things depending on how the target was named.
        self.syntax_error = ""
        #: the interpreter both runs used, resolved before either of them. Printed
        #: because the resolver can substitute the checkout's `.venv` for the caller's
        #: interpreter, and evidence that does not name its environment is evidence a
        #: reader has to guess about.
        self.interpreter = ""

    def decide(self, verdict: str, why: str, code: int) -> None:
        self.verdict, self.why, self.code = verdict, why, code

    def as_dict(self) -> dict[str, object]:
        return {
            "tree": str(self.cwd),
            "file": str(self.target),
            "label": self.args.label,
            "node": list(self.args.node),
            "expect": self.args.expect,
            "interpreter": self.interpreter,
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
    print(f"interpreter: {arm.interpreter or '(not resolved - the arm stopped first)'}")
    print(f"preflight: {arm.preflight}")
    print(f"mutated run: rc={arm.mutated_rc} passed={arm.mutated_passed}")
    print(f"failed node: {arm.failed_node or '-'}")
    print(f"restored byte-for-byte: {arm.restored}")
    print(f"verdict: {arm.verdict} - {arm.why}")
    if arm.verdict == UNJUDGEABLE and arm.assertions:
        # Printed only for the verdict whose reader has to retype the fragment. For
        # KILLED the expectation already matched, and for the other states the reason
        # is a refusal rather than a search - so this stays out of the common report.
        print("the run did echo these assertion lines, any of which can be --expect:")
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
            "report prints the assertion lines the run did echo, so the retry is one "
            "step rather than a second run typed by hand"
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
    # Resolved once, before either run, so the pre-flight and the judged run cannot be
    # about two different interpreters - and reported, because the evidence has to name
    # the environment it came from when the resolver substitutes one.
    #
    # A resolver that cannot answer is a **verdict**, not a traceback. The gate is a file
    # loaded from disk, so it can be missing, unreadable or not compiling - a parallel
    # cycle mid-edit on it is enough - and an uncaught exception here exits 1, which this
    # file defines as `EXIT_SURVIVED` (measured 2026-10-06, `cyc20261006-020931`: a gate
    # with one broken line appended, and a tool standing alone without one, each exited 1
    # with no `verdict:` line). Every other outcome in this tool leaves through `_report`;
    # so does this one, and the verdict is UNJUDGEABLE because an arm whose interpreter is
    # unknown has measured nothing.
    try:
        python = _pytest_interpreter()
    except Exception as exc:  # noqa: BLE001 - the cause is named, not swallowed
        arm.decide(UNJUDGEABLE, _why_no_interpreter(exc), EXIT_UNJUDGEABLE)
        shutil.rmtree(home, ignore_errors=True)
        _report(arm, args.json)
        return arm.code
    arm.interpreter = python
    try:
        if not args.no_preflight:
            proc = _run_target(args.node, cwd, home, python)
            passed = _passed_count(_combined(proc))
            if proc.returncode != PYTEST_OK or passed < 1:
                # A refusal is a verdict like any other, so it goes through the same
                # report path: an unattributed non-zero exit is the failure this tool
                # exists to prevent, and that includes this tool's own. Which of the
                # causes it was is read from the output, not assumed - see
                # `_why_target_broken`.
                arm.preflight = f"refused (rc={proc.returncode}, {passed} passed)"
                arm.decide(
                    TARGET_BROKEN,
                    _why_target_broken(
                        proc.returncode, passed, _combined(proc), python
                    ),
                    EXIT_TARGET_BROKEN,
                )
            else:
                arm.preflight = f"{passed} passed"

        if not arm.verdict:
            try:
                target.write_text(mutated, encoding="utf-8")
                mutated_on_disk = True
                _purge_bytecode(target)
                proc = _run_target(args.node, cwd, home, python)
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


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table, while `2` is the code for "the question could not be answered". A caller
    that checks the code - which is how this family composes, one gate running another or
    reading its `rc` - would otherwise read a crash as a verdict. Byte-identical in every
    tool of the family, and `tests/test_a_crash_is_a_measurement_error.py` pins that.
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - reported as unmeasurable, never swallowed
        traceback.print_exc()
        print(
            f"{Path(__file__).name}: could not measure - {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2  # cause: tool-failed


if __name__ == "__main__":
    raise SystemExit(_entry())
