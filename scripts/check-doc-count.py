#!/usr/bin/env python3
"""Keep the Python test count measured, not stored.

Usage
-----
    uv run --no-sync python3 scripts/check-doc-count.py                    # the rule: exit 1 if any file states the count
    uv run --no-sync python3 scripts/check-doc-count.py --measure          # print the count the tree actually collects
    uv run --no-sync python3 scripts/check-doc-count.py --resolve-conflict # clear a conflicted count line

Why the number is not written down
---------------------------------
`Agent.md` used to state the collected Python test count, and this tool kept it
in sync. Measured 2026-09-13 (`cyc20260913-132356`) on the live queue: **11 of
14 open PRs were conflicting, and all 11 conflicted on that single line**
(`Agent.md:122`), because every PR that adds a test had to rewrite the same
derived number. The merge *ordering* was not the problem and no guard could fix
it: a value conflict is loud and safe (two branches writing different numbers
conflict, and a human resolves it), while two branches writing the **same**
number merge cleanly and silently leave a stale value - measured that cycle:
`#1179` + `#1180` both wrote `1601` while the merged tree collected `1603`, a
clean merge the guard only caught on the merged tree.

A derived fact stored in a document is therefore both a conflict magnet and a
silent-corruption site. The fix is not a better guard for the stored number, it
is to stop storing it: the count is a *measurement*, so any place that needs it
runs the measurement (or names the command). `README.md`/`README.cn.md` already
went this way for the same reason (the Tests badge replaced their hardcoded
counts, rant 2026-08-11T19:50:37); this tool extends that rule to every tracked
file, so there is no last copy to keep true.

This is the host-side half of the CI guard
(`tests/test_doc_counts.py::test_no_tracked_file_states_the_python_test_count`),
in the same shape as `scripts/bump-version.py --check` for the version sources.
Both read the same rule out of this file, so the two cannot drift apart.

Scope, stated with its measurement
----------------------------------
Every tracked file is scanned except `tests/` and `scripts/`. That exclusion is
not a convenience: those two trees are where the rule and its probes *live*, so
they must be able to spell the claim to pin it. Measured 2026-09-13 over all 466
tracked files with this rule: all hits outside the docs were in `tests/` (71)
and `scripts/` (2) - fixtures and prose about the shape, not claims about this
tree. Every tracked `.md` is in scope, and a witness test asserts that, so an
exclusion cannot quietly grow to cover the file a real claim sits in.

The GUI/renderer counts on the following `Agent.md` lines are a different case
and stay: each carries a per-file *breakdown* (information no guard can derive),
they change only when GUI tests are added, and they are pinned to the real
runner by `scripts/check-node-test-count.py`. This rule covers the Python total,
which nothing added to a row of numbers.

The file list comes from `git ls-files` in a checkout and from a walk when there
is no `.git`. The second path is not a convenience: `scripts/check-merge-sequence.py`
judges a merged tree by extracting it with `git archive` into a temp directory
and running *that tree's own copy* of this guard, and such a tree is a pristine
export with no `.git`. There the two paths agree by construction (an export holds
only tracked content); in a checkout the tracked path always wins, so a broken
repository fails loud rather than quietly scanning something else.

Run it with the project interpreter: `--measure` shells out to pytest, so the
interpreter that answers must be one that can import it. *Which* interpreter that
is, is asked rather than assumed: `measure_interpreter` keeps this tool's own
interpreter when it can already import pytest, and otherwise uses the checkout's
own `.venv` - so the documented `uv run --no-sync python3 ...` spelling still
measures the tree on a host whose `python3` resolves outside the checkout, and a
tree where nothing reachable has pytest is refused with that reason rather than
reported as a bogus count (`_VENV_INTERPRETERS` and `measure_interpreter` below
carry the measurement).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def _resolve_root() -> Path:
    """The tree to measure: the checkout the caller is *standing in*.

    Derived from the cwd when that is a checkout, not from `__file__`. Measured
    2026-09-11, in exactly the situation this tool exists for: unblocking a PR
    means working in a git worktree, where running the main checkout's copy of
    this script reported `OK: Agent.md documents 1420` while the worktree's own
    `Agent.md` said 1401 - it had read the wrong tree and called it consistent.
    A writing mode in that position edits the *other* checkout, which is how a
    confirm-step silently corrupts a tree the caller was not looking at.

    Falling back to the script's own root keeps `python3 scripts/...` working from
    anywhere (the documented invocation), and a mismatch is stated rather than
    silently resolved: the caller gets told which tree answered, because "which
    tree did you measure" is the one thing this tool must never leave ambiguous.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "Agent.md").is_file() and (cwd / "scripts").is_dir():
        return cwd
    return here


REPO_ROOT = _resolve_root()

# Trees that hold the rule and its probes rather than claims about the tree.
# See "Scope" in the module docstring: measured, and witnessed by a test.
EXCLUDED_PREFIXES = ("tests/", "scripts/")

# The one spelling of "run this tool" that every hint in this repo prints: this
# module's hints, the pytest guard's failure message, and Agent.md's doc line.
# Measured 2026-09-10 (cyc20260910-191242) in the main clone: this form exits 0,
# while the bare `python3` form an older hint used exits 2 without measuring
# anything - the host's `python3` has no pytest, so a hint spelled that way sends
# the reader straight into a second failure. A hint is only worth printing if it
# runs; keep the spelling here and let tests/test_check_doc_count.py prove the
# other sites agree with it.
#
# Scope of that measurement, added 2026-09-13 (cyc20260913-122923) after walking
# into it: "this form exits 0" is a property of a **synced** checkout. The
# measurement above was taken in the main clone, which is synced. In a fresh
# worktree the same spelling exits 2 without measuring anything, because
# `uv run --no-sync` has created an empty `.venv` there and nothing in it can
# import pytest - so the spelling is necessary but not sufficient, and the hint it
# appears in must not be printed when the environment, not the interpreter choice,
# is what is missing. See the pytest-missing branch in `measured_count`.
#
# That note said both `python` and `python3` "resolve to it", which is the POSIX
# half of a two-platform fact: measured 2026-10-05 (cyc20261005-034541) on
# Windows, uv's venv provides `python.exe` and no `python3.exe`, so `python3`
# leaves the venv and resolves on PATH. What fails in that fresh worktree is the
# environment either way - but *which* interpreter answers is not something a
# spelling decides, which is why `measure_interpreter` asks rather than assumes.
INVOCATION = "uv run --no-sync python3 scripts/check-doc-count.py"

# The stored form, as it appeared in Agent.md: the command followed by the count
# in parentheses. This is the shape a PR that adds a test had to rewrite, and the
# one a merge could silently leave stale.
STORED_COUNT = re.compile(r"(?P<command>uv run pytest tests/ -v`)(?P<claim> \(\d+\))")

# How a count is written down. A claim is recognised by its *shape*, not by its
# value: the value is what is wrong, so a rule keyed on the number could not see
# the stale copies it exists to find.
#
# The prose forms come from issue #1158's measurement of the same defect one
# level down (`DEVELOPMENT.md` advertised "currently 681 items" while the tree
# collected 1590 - a claim off by 2.3x in a file no check read). They are
# lexical, so they can over-trigger on a doc that happens to phrase a number
# that way; that direction is the safe one (a loud "delete this" beats a silent
# drift) and today's tree has no such phrasing at all once the two real claims
# are gone.
COUNT_CLAIMS = (
    ("stored next to the test command", STORED_COUNT),
    ("parenthesised count", re.compile(r"\(\s*(?:currently\s+)?\d+\s+(?:items|tests|passed)\s*\)")),
    ("'currently N' claim", re.compile(r"\bcurrently\s+\d+\s+(?:items|tests|passed)\b")),
    ("bare 'N items' claim", re.compile(r"\b\d{3,}\s+(?:items|tests|passed)\b")),
    ("count after a pytest command", re.compile(r"pytest[^\n]{0,60}\(\s*\d+\s*\)")),
)

COLLECTED = re.compile(r"(\d+) tests? collected")

# A full conflict block: `<<<<<<< label`, both sides, `>>>>>>> label`. Kept as one
# pattern with named sides so the resolver can be driven without a real merge.
CONFLICT_BLOCK = re.compile(
    r"^<<<<<<<[^\n]*\n(?P<ours>.*?)^=======\n(?P<theirs>.*?)^>>>>>>>[^\n]*\n",
    re.S | re.M,
)

# `merge.conflictStyle = diff3` (and `zdiff3`) inserts a `||||||| <base>` section
# between the two sides - measured 2026-09-11 on this machine with a real
# `git merge`: the base line is a third copy of the conflicted line. The subtle
# part, also measured: `CONFLICT_BLOCK` does *not* simply fail to match this
# layout - it matches while swallowing the base line into `ours`, so the two
# captured sides are `"<ours>\n||||||| <base>"` and `"<theirs>"`. The block is
# therefore misread as a content conflict: the reader is told the sides "differ
# by more than the count", which is false (all three differ only in the number)
# and points them at hand-picking a side. Detecting the layout *before* matching
# turns that into an accurate refusal. Do not reorder these two checks.
CONFLICT_BASE_SECTION = re.compile(r"^\|\|\|\|\|\|\|[^\n]*\n", re.M)


class DocCountError(Exception):
    """The tree is not in a shape this tool can act on."""


def measured_revision() -> str:
    """The revision this tree is at, in the words the report prints.

    Printed beside the count, because a count is a number *about a tree* and the
    two are only comparable together: measured 2026-09-28 (`cyc20260928-150510`),
    a cycle compared its own checkout's count with a worktree gate's count,
    attributed the one-test difference to the harness, and recorded that as a
    finding - the two runs were of **different trees**, and the reading that
    would have shown it (their revisions) was on neither line. The tree line
    already answers "which checkout"; this answers "which revision of it", which
    is the half that moves while a cycle works.

    Degrades to a stated absence rather than a guess: a checkout that is not a
    git repository, or has no `git` on PATH, says so on the line, because a
    revision that silently reads as "same as last time" is the defect this
    exists to prevent.
    """
    try:
        branch = _git("symbolic-ref", "--short", "-q", "HEAD")
        head = _git("rev-parse", "HEAD")
    except OSError as exc:
        return f"revision unreadable ({exc.__class__.__name__})"
    if not head:
        return "revision unreadable (git rev-parse said nothing)"
    # `symbolic-ref` exits non-zero for a detached HEAD, which is a state, not a
    # failure - the same distinction the merge gates keep when they name a tree.
    where = branch or "detached HEAD"
    return f"at {head[:8]} ({where})"


def _git(*argv: str) -> str:
    """One `git` read in this tree, or `""` when it did not answer.

    `""` rather than an exception for a non-zero exit: the caller's whole use of
    this is the report line, and a tree without git still has a countable suite.
    An `OSError` (no `git` binary) is left to the caller, because that is a
    different absence from "this is not a repository".
    """
    proc = subprocess.run(
        ["git", *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


# Where a checkout keeps the environment `uv sync` populates, relative to the
# checkout root. Both spellings are tried on every platform, Windows first: a
# candidate that is not there is skipped by `is_file()`, so the order costs
# nothing and no `sys.platform` branch is needed - which is also why this is
# testable on either leg. The same pair, for the same reason, is in
# `check-merge-plan-suite.py`: the two tools answer one question, "which
# interpreter measures a tree in this checkout", and two spellings of the project
# environment are two answers free to drift apart.
_VENV_INTERPRETERS: tuple[Path, ...] = (
    Path(".venv") / "Scripts" / "python.exe",
    Path(".venv") / "bin" / "python",
)

# The answer, once asked: which interpreter measures this tree is a property of
# this machine and of this run, and finding it spawns a probe process.
_MEASURE_INTERPRETER: str | None = None


def can_import_pytest(python: str) -> bool:
    """Whether `python` can import pytest - asked, never assumed from its name.

    `--measure`'s first act is `-m pytest`, so the probe runs that import in the
    interpreter in question and reads the exit code. Measured 2026-10-05 on this
    host: `uv run --no-sync python3` reaches `~/.emrg/install/bin/python3.exe`,
    which answers rc 1 with `No module named pytest`, while this checkout's
    `.venv` python answers rc 0 - two paths whose names tell a reader nothing
    about which is which.

    An interpreter this machine cannot start at all - a removed path, a
    `.venv/bin/python` shell script on Windows, a file without the execute bit -
    answers *no* rather than raising: "this one cannot measure the tree" is a
    fact about the candidate, and an `OSError` out of a search is a crash where a
    fallback was asked for. `check-merge-plan-suite.py` probes its candidates the
    same way, for the same reason.
    """
    try:
        return subprocess.run(
            [python, "-c", "import pytest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).returncode == 0
    except OSError:
        return False


def tried_interpreters(checkout: Path) -> list[str]:
    """Every interpreter `measure_interpreter` can reach, this tool's own first.

    One definition, because two readers need the same list in the same order: the
    search tries them in this order, and the refusal names the ones it tried - a
    remedy that names no invocation leaves the reader where they were.
    """
    return [sys.executable] + [
        str(checkout / relative) for relative in _VENV_INTERPRETERS
    ]


def measure_interpreter(own: str | None = None, checkout: Path | None = None) -> str:
    """The interpreter that measures this tree: this tool's own, or the checkout's `.venv`.

    Which interpreter runs the collection is not a detail: a spelling that reaches
    an interpreter without pytest measures nothing, and what the caller gets is
    `rc 2`, not a count. Measured 2026-10-05 on this host, the documented
    `uv run --no-sync python3 scripts/check-doc-count.py --measure` reaches the
    EMRG install's `python3.exe` and measures nothing, while this checkout's
    `.venv` holds pytest - uv's venv provides `python.exe` and no `python3.exe`,
    so `python3` leaves the venv and resolves on PATH. The tool used to read that
    as a refusal; the answer it can act on is the environment this checkout
    already has, so that is tried before refusing.

    `check-merge-plan-suite.py` asks the same question (its `_suite_interpreter`),
    and both must answer it the same way: "which interpreter judges a tree in this
    checkout" is one fact about one machine.

    `own` and `checkout` exist so a test can ask about a layout it owns instead of
    about this machine's. An invocation whose interpreter can already import
    pytest is answered with itself and no candidate is probed, so the documented
    invocation keeps measuring exactly what it measured before.
    """
    global _MEASURE_INTERPRETER
    cacheable = own is None and checkout is None
    if cacheable and _MEASURE_INTERPRETER is not None:
        return _MEASURE_INTERPRETER
    interpreter = sys.executable if own is None else own
    resolved = interpreter
    if not can_import_pytest(interpreter):
        root = REPO_ROOT if checkout is None else checkout
        for relative in _VENV_INTERPRETERS:
            candidate = root / relative
            if candidate.is_file() and can_import_pytest(str(candidate)):
                resolved = str(candidate)
                break
    if cacheable:
        _MEASURE_INTERPRETER = resolved
    return resolved


def interpreter_is_the_checkouts(interpreter: str, checkout: Path) -> bool | None:
    """Whether `interpreter` is the one this checkout's own `.venv` provides.

    Asked by the refusal, because it is the one thing that explains a failed
    invocation the search itself cannot fix: when the interpreter that ran this
    tool is not this checkout's own environment, re-running the same spelling
    changes nothing, and the reader is owed that sentence rather than left to
    infer it from two path strings.

    `None` when the checkout has no `.venv` to compare against: the question has
    no subject there, which is not the same answer as "no".

    :param interpreter: the path of the interpreter that ran this tool.
    :param checkout: the tree this tool is reading.
    :returns: True when the interpreter lives in `checkout/.venv`, False when it
        lives somewhere else, None when there is nothing to compare against.
    """
    venv = checkout / ".venv"
    if not venv.is_dir():
        return None
    try:
        exe = Path(interpreter).resolve()
    except OSError:
        return None
    return venv.resolve() in exe.parents


def missing_pytest_remedy(
    interpreter: str, checkout: Path, tried: list[str]
) -> str:
    """Which cause was measured, and the remedy that fits it.

    Reached only after `measure_interpreter` has tried every interpreter this
    checkout offers, so what is left to say is about the *environment*: nothing
    reachable from here can import pytest, and every candidate is named, because a
    remedy that names no invocation leaves the reader where they were.

    Measured 2026-10-05 (cyc20261005-034541), against what this branch used to
    print: it asserted "an unsynced checkout, not a wrong-interpreter problem",
    generalising one 2026-09-13 reading of a fresh worktree into the only case.
    On this Windows host the other case is what happens - `.venv` held pytest
    (9.1.1) while `sys.executable` was the EMRG install's `bin\\python3.exe`,
    because uv's venv provides `python.exe` and no `python3.exe`, so
    `uv run --no-sync python3 ...` leaves the venv and resolves on PATH. That case
    is now answered by the search (`measure_interpreter`) rather than described
    here, and the old sentence's second claim - that `uv sync` cannot fix it - is
    gone with it: an environment that was never built is what `uv sync` is for,
    whichever interpreter the caller's spelling happened to reach.

    :param interpreter: the interpreter that ran this tool.
    :param checkout: the tree this tool is reading.
    :param tried: every interpreter the search reached, in the order it tried them.
    :returns: the paragraph the pytest-missing branch appends to its message.
    """
    venv = checkout / ".venv"
    if venv.is_dir():
        state = (
            f"This checkout's `.venv` is there but unsynced, so it holds no "
            "pytest"
        )
    else:
        state = (
            "This checkout has no `.venv` at all, so its environment was never "
            "created here"
        )
    foreign = ""
    if interpreter_is_the_checkouts(interpreter, checkout) is False:
        foreign = (
            f" The interpreter that ran this tool ({interpreter}) is not this "
            f"checkout's own environment (`{venv}`), so re-running the spelling "
            "that produced this message cannot help by itself."
        )
    return (
        f"{state}, and no other interpreter this tool can reach has pytest "
        f"either. Tried: {', '.join(tried)}.{foreign} Run `uv sync` in {checkout} "
        "first, or run this tool from a checkout whose environment is already "
        "synced."
    )


def measured_count() -> int:
    """How many tests pytest actually collects in this tree.

    `encoding="utf-8"` / `errors="replace"` rather than the locale codec: the
    identical defect was measured in `scripts/check-node-test-count.py` (issue
    #1132, where a locale mismatch left `proc.stdout` as `None` after the decode
    error was swallowed by subprocess's reader thread, and the concatenation
    raised a bare `TypeError` past every handler). Any collected id or warning
    carrying a non-ASCII byte would do the same here on a cp936 host, so the
    decoding is pinned before that can happen.

    The interpreter is `measure_interpreter`'s answer, not `sys.executable`: a
    count measured by an interpreter that is not the one the caller named is still
    a count of this tree, but *which* interpreter answered is part of the reading
    when the two differ, so the difference is printed rather than left to be
    discovered from a number.
    """
    interpreter = measure_interpreter()
    if interpreter != sys.executable:
        # Said out loud, because which interpreter judged a tree is part of the
        # reading: this tool's own interpreter cannot import pytest, and the
        # count that follows comes from the checkout's environment instead.
        print(
            f"measuring interpreter: {interpreter} - this tool's own interpreter "
            f"({sys.executable}) cannot import pytest",
            file=sys.stderr,
        )
    proc = subprocess.run(
        [interpreter, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout is None or proc.stderr is None:
        raise DocCountError(
            "pytest --collect-only produced no readable output "
            "(its output could not be decoded)"
        )
    if proc.returncode != 0:
        detail = (proc.stdout[-2000:] + proc.stderr[-2000:]).strip()
        # Two causes, and this branch used to print one of them as the only one
        # ("an unsynced checkout, not a wrong-interpreter problem"): the
        # 2026-09-13 measurement (cyc20260913-122923) of a fresh review worktree
        # - `uv run --no-sync` had left an empty `.venv` there (`site-packages`
        # holding only `_virtualenv.pth` and `_virtualenv.py`) - is one of them.
        # Measured 2026-10-05 (cyc20261005-034541): on this Windows host the
        # other one is what happens, and the sentence was false - `.venv` held
        # pytest while `sys.executable` was a `python3` outside the checkout. The
        # search above answers that case now; what is left here is the one no
        # search can fix, and `missing_pytest_remedy` says only what it read.
        if "No module named pytest" in detail:
            raise DocCountError(
                "pytest is not installed in the interpreter that ran the "
                f"collection ({interpreter}), so no test was collected:\n"
                + detail
                + "\n\n"
                + missing_pytest_remedy(
                    interpreter, REPO_ROOT, tried_interpreters(REPO_ROOT)
                )
            )
        raise DocCountError(
            f"pytest --collect-only failed (rc={proc.returncode}):\n"
            + detail
            + "\n\nhint: run this with the project interpreter, e.g."
            f" `{INVOCATION} --measure`"
        )
    match = COLLECTED.search(proc.stdout)
    if not match:
        raise DocCountError(
            "could not parse a collected count from pytest output:\n"
            + proc.stdout[-2000:].strip()
        )
    return int(match.group(1))


def claims_in(text: str) -> list[tuple[int, str, str]]:
    """Every stored count in `text`, as (line number, shape, the line).

    Takes text rather than a path so the rule can be *driven* against a doc that
    stores the count and against prose that merely mentions the command - a rule
    only ever pointed at the real tree is not known to discriminate, and the
    tree's own docs are the negative half the guard needs.
    """
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for shape, pattern in COUNT_CLAIMS:
            if pattern.search(line):
                found.append((lineno, shape, line.strip()))
                break
    return found


def _tracked_files() -> list[str]:
    """`git ls-files` in this tree: what the repository carries.

    Tracked rather than walked, because a claim is a fact about the repo: an
    untracked scratch file must not be able to red the rule, and a tracked file
    must not be able to hide behind an ignore rule.
    """
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise DocCountError(
            f"could not list tracked files in {REPO_ROOT} "
            f"(git ls-files rc={proc.returncode}): {proc.stderr.strip()}"
        )
    if proc.stdout is None:
        raise DocCountError("git ls-files produced no readable output")
    return sorted(
        name
        for name in proc.stdout.splitlines()
        if name and not name.startswith(EXCLUDED_PREFIXES)
    )


# Directories a *non-checkout* tree may still carry from a build. Only the
# fallback below consults them: an export has no untracked content at all, so the
# two paths see the same files there. Stated rather than silent, because "which
# files did you scan" is the one thing this rule must not answer vaguely.
_BUILD_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__", ".emrg"}


def _exported_files() -> list[str]:
    """The fallback for a tree with no `.git`: walk what is there.

    Measured need, not speculation: `scripts/check-merge-sequence.py` judges a
    merged tree by extracting it with `git archive` into a temp directory and
    running *the tree's own copy* of this guard there. That tree is a pristine
    export and has no `.git`, so `git ls-files` fails by construction - and a
    guard that cannot list its files must not silently pass.

    A walk is the right answer exactly there and nowhere else: an export contains
    only tracked content, so walking it and listing its tracked files are the same
    set. In a checkout the tracked path is used instead (see `scanned_files`), so
    an untracked scratch file can never red the rule.
    """
    found: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(part in _BUILD_DIRS for part in relative.parts):
            continue
        name = relative.as_posix()
        if name.startswith(EXCLUDED_PREFIXES):
            continue
        found.append(name)
    return sorted(found)


def scanned_files() -> list[str]:
    """The files this rule covers, tree-relative and sorted.

    Two paths, and which one runs is decided by one measurable fact: whether this
    tree is a git checkout. A checkout is always scanned as *tracked files*; a
    pristine export (no `.git`) is walked. A checkout whose listing fails is an
    error rather than a walk, so a broken repository cannot quietly change what
    "the repo states" means.
    """
    if (REPO_ROOT / ".git").exists():
        return _tracked_files()
    return _exported_files()


def offenders() -> list[tuple[str, int, str, str]]:
    """(file, line number, shape, line) for every stored count in the tree."""
    found: list[tuple[str, int, str, str]] = []
    for name in scanned_files():
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary or unreadable: a claim cannot live in a file this rule
            # cannot read, and failing the whole scan over, say, a PNG would
            # make the guard unusable rather than strict.
            continue
        for lineno, shape, line in claims_in(text):
            found.append((name, lineno, shape, line))
    return found


def resolve_conflict(text: str) -> str:
    """Resolve a conflicted count line by removing the count, not picking a side.

    Refuses unless the conflict is *exactly* the count line and the two sides are
    the same text once the count is removed. Both halves are load-bearing:

    * "exactly the count line" - if any other part of the document is conflicted,
      resolving here would silently drop whichever lines lost;
    * "the same text modulo the count" - if the sides differ in their *wording*
      too, this is a content conflict and one of the two texts is a real choice
      (in the 2026-09-13 queue every in-flight branch's line differed from the
      new count-free form in wording as well, so this mode correctly refuses
      those: taking the count-free form there is a judgement, and the tool's job
      is not to make judgements silently).

    Where it does apply, the resolution follows from the rule itself rather than
    from either side: the number is a measurement, so the resolver *removes* the
    claim and keeps everything else. Returns the resolved text; the caller writes
    it only after this function has proved no claim survives.
    """
    if CONFLICT_BASE_SECTION.search(text):
        raise DocCountError(
            "the conflict uses the diff3 layout (`||||||| <base>`), which this "
            "tool does not parse: it cannot tell whether the conflict is the "
            "count line, so it must not resolve it. Re-merge with git's "
            "default layout (`git config merge.conflictStyle merge` and "
            "recreate the conflict), or resolve by hand"
        )
    matches = list(CONFLICT_BLOCK.finditer(text))
    if not matches:
        raise DocCountError(
            "no conflict block found; nothing to resolve (run the tool with no "
            "arguments to check the tree)"
        )
    if len(matches) > 1:
        raise DocCountError(
            f"{len(matches)} conflict blocks found; this tool only resolves the "
            "count line - resolve the others by hand"
        )

    block = matches[0]
    ours, theirs = block.group("ours"), block.group("theirs")
    if not (STORED_COUNT.search(ours) or STORED_COUNT.search(theirs)):
        raise DocCountError(
            "the conflict is not the count line; this tool only resolves that "
            "line - resolve this one by hand"
        )

    def stripped(side: str) -> str:
        return STORED_COUNT.sub(lambda m: m.group("command"), side)

    if stripped(ours) != stripped(theirs):
        raise DocCountError(
            "the two sides differ by more than the count, so this is a content "
            "conflict and the tool must not choose a side (one side changes text "
            "other than the number, or the block spans more than the count line). "
            "Read it: the count-free side is the shape to keep, but which side "
            "carries the wording you want is a judgement"
        )

    resolved = text[: block.start()] + stripped(ours) + text[block.end() :]
    if CONFLICT_BLOCK.search(resolved) or ">>>>>>>" in resolved:
        raise DocCountError(
            "conflict markers remain outside the count line; resolve by hand"
        )
    remaining = claims_in(resolved)
    if remaining:
        raise DocCountError(
            "resolving left another stored count at line "
            f"{remaining[0][0]}: {remaining[0][2]} - resolve by hand"
        )
    return resolved


def _measure_mode() -> int:
    """`--measure`: print the count the tree in front of you collects."""
    try:
        count = measured_count()
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"measured: {count} collected Python tests {measured_revision()}")
    return 0


def _resolve_conflict_mode() -> int:
    """`--resolve-conflict`: clear the conflicted count line, keep the structure.

    No number is written, because no number is stored: the resolution is the
    count-free form of the line.
    """
    target = REPO_ROOT / "Agent.md"
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {target}: {exc}", file=sys.stderr)
        return 2

    try:
        resolved = resolve_conflict(text)
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        target.write_text(resolved, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {target}: {exc}", file=sys.stderr)
        return 2

    print(f"resolved {target.name}: conflict block removed, count claim dropped")
    print(f"Next: uv run --no-sync pytest tests/test_doc_counts.py -q")
    return 0


def main(argv: list[str] | None = None) -> int:
    # A merged reader must see the `tree:` line before any verdict, and this
    # family's docstrings promise that order. stdout is block-buffered when it is
    # a pipe (how a cycle reads this report: `2>&1 | tail`) while stderr is not,
    # so without this every stderr line overtakes the tree line. Behaviour and
    # pin: tests/test_guard_report.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage\n-----", 1)[0].strip(),
        epilog=__doc__.split("Usage\n-----", 1)[-1].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--measure",
        action="store_true",
        help="print the count this tree collects (the measurement a doc should name, not store)",
    )
    mode.add_argument(
        "--resolve-conflict",
        action="store_true",
        help=(
            "resolve a conflicted count line by dropping the claim: keeps the "
            "structure, writes no number (refuses if the conflict is anywhere "
            "else, or if the sides differ by more than the count)"
        ),
    )
    args = parser.parse_args(argv)

    # Say which tree answered. A tool whose whole job is "check the tree in front
    # of you" must not leave "which tree" ambiguous - the 2026-09-11 defect was
    # precisely a confident `OK` about a checkout the caller was not in.
    print(f"tree: {REPO_ROOT}")

    if args.measure:
        return _measure_mode()
    if args.resolve_conflict:
        return _resolve_conflict_mode()

    try:
        found = offenders()
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not found:
        print(
            "OK: no tracked file states the Python test count "
            "(it is measured, not stored)"
        )
        return 0

    files = {name for name, _, _, _ in found}
    print(
        f"FAIL: {len(files)} tracked file(s) state the Python test count "
        f"({len(found)} claim(s))"
    )
    for name, lineno, shape, line in found:
        print(f"  {name}:{lineno} [{shape}] {line[:160]}")
    print(
        "\nThe count is a measurement, so it must not be written down: a stored\n"
        "number goes stale and every PR that adds a test has to rewrite it (that\n"
        "one line caused 11 of 11 conflicts on 2026-09-13, and two PRs writing the\n"
        "same value merged cleanly while the merged tree collected a different\n"
        f"number). Delete the number and name the command instead.\n\nMeasure it with: {INVOCATION} --measure"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
