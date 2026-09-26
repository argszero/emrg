"""A guard that reads a working tree says which tree answered.

The rule, and where it came from
--------------------------------
`Agent.md` states it as a convention. It is not decoration: `check-merge-sequence.py`
reads a sibling guard's report and **refuses it outright** unless the report names the
tree it read and that name resolves to the tree the sequence tool asked about
(`TREE_IN_REPORT`), whose comment records the failure mode — run from a working
directory without `scripts/`, the guard "falls back to its own checkout and answers
about *that* tree, in the same words - a wrong tree reported as a consistent one".
`check-doc-count.py` and `check_nonlocal.py` carry the incident that produced the
convention (2026-09-11: a confident `OK` about a checkout the caller was not in).

Measured 2026-09-25 (`cyc20260925-182347`) over the `scripts/check-*.py` family: the
convention held for every tree-reading guard **except one**.
`check-citation-resolves.py` takes its tree as an *argument* (defaulting to `.`) and
printed the same verdict for this checkout, for a worktree, and for a tmp directory a
test had just built. It now prints `tree: <resolved root>` before any verdict, and this
file is the runner that makes the convention a rule rather than a claim.

That sweep was over the family **as classified**, and one guard it did not have to look
at was mis-classified the same day: `#1618` added `check-merge-landed.py` and put it in
the "answers about something other than a working tree" bucket, in an entry that says it
"reads merged trees from local git" — the bucket's premise contradicted by its own note.
Its tree half is local git in this checkout (`REPO_ROOT`, derived from the file), it
answers `UNCHECKED` where another clone answers `NAMED`, and it named nothing. Corrected
2026-09-26 (`cyc20260926-034521`): the tool prints `tree: <this checkout>` first (a
`tree` field under `--json`, so the JSON stays one document) and the entry moved to the
bucket whose premise it fits.

What is asserted, and the two legs that keep it from being decoration
---------------------------------------------------------------------
* **every `scripts/check*.py` is classified** below — run here, names its tree but
  cannot be run in this suite (with the reason), or does not read a working tree (with
  what it reads instead). A guard added later fails this file until it is classified,
  which is what stops the rule from decaying into the absence of anyone's objection;
* **the guards that can be run are run**, and the **first** line of stdout names the
  tree. The pair of assertions that follow is the control, and each half kills a
  plausible way of faking it: the three module-rooted guards must name *their own*
  checkout even when launched from an unrelated working directory (so a line that
  reports the caller's cwd dies), while the citation guard must name the tree it was
  *given* — a tmp root, and explicitly **not** this repository (so a line hardcoded to
  the repository root dies).

Named limits. The middle bucket is **classified, not verified**: its guards read a
working tree and name it, but cannot be run here (one needs the Node runners, which the
pytest job's environment does not have; another measures a separately installed
interpreter; the third needs `gh` and the network), so nothing below checks them, and
saying so is better than counting them as checked. One of the three is nonetheless
verified — `check-merge-landed.py` by `tests/test_check_merge_landed.py`, which runs it
against a `tmp_path` repository and a `gh` stand-in — so the limit now reads "not
verified **here**" for that member rather than "not verified". The first line is the
subject rather than "anywhere in the output" because that is what the convention says
and what a reader's eye reaches first — a line printed after a verdict has already been
given answers a question the reader did not ask.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

#: Run here, and the first line of stdout must be `tree: <this checkout>`. Each of
#: these takes no required argument, imports nothing outside the standard library
#: **and this checkout's own package**, and reads a working tree — the conditions
#: that make "run it and look" the way to ask the question instead of reading its
#: source for a print statement. `check-memory-index.py` is the second kind: its
#: two numbers come from `emrg.memory` and `emrg.server.daemon`, so it needs the
#: project interpreter (which this suite is), and it reads the `.emrg/` trees under
#: the checkout it stands in.
RUN_HERE = (
    "check-doc-count.py",
    "check-memory-index.py",
    "check-rant-citations.py",
    "check_nonlocal.py",
)

#: The one guard run here that names a *given* tree rather than its own checkout, so it
#: is asked a different question below: its line must follow its argument.
TAKES_A_TREE_ARGUMENT = "check-citation-resolves.py"

#: Reads a working tree and names it, but cannot be run in this suite. Classified rather
#: than dropped, because "not run" is the kind of exemption that widens silently.
NAMES_ITS_TREE_BUT_IS_NOT_RUN_HERE = {
    "check-node-test-count.py": (
        "needs the Node runners to compare their totals; the pytest job has no "
        "node_modules, so running it here would measure the environment, not the rule"
    ),
    "check-extension-load.py": (
        "measures a separately installed interpreter (~/.emrg/install/bin/python), "
        "which a clean CI checkout does not have"
    ),
    "check-merge-landed.py": (
        "needs `gh` and the network for the review half. It was classified as a "
        "NON-tree-reader when #1618 added it, by the bucket entry that says it 'reads "
        "merged trees from local git' - the premise of that bucket contradicting its "
        "own note. Its tree half is local git in the checkout its own file lives in "
        "(`REPO_ROOT`), so the rule applies; the difference from the two above is that "
        "this member's naming IS verified rather than classified - "
        "`tests/test_check_merge_landed.py` runs it against a `tmp_path` repository with "
        "a `gh` stand-in and pins the first line"
    ),
}

#: Answers about something other than a working tree, so the rule does not apply - each
#: with what it reads instead, because "not a tree reader" is a claim like any other.
NOT_TREE_READERS = {
    "check-vote-count.py": "reads reviews and mergeability from the GitHub API",
    "check-pr-base.py": "reads the base each PR declares on the GitHub API",
    "check-patch-files.py": "reads the patch files it is given as arguments",
    "check-merge-freshness.py": "requires PR numbers; answers per head and its base",
    "check-merge-order.py": "requires PR numbers; names the base it plans against",
    "check-merge-pairs.py": "requires PR numbers; names the base it folds onto",
    "check-merge-plan-suite.py": "requires PR numbers; names the base it plans against",
    "check-merge-sequence.py": "requires PR numbers; names the base it folds onto",
    "check-merge-tree-health.py": "requires PR numbers; prints the repo it works in",
    "check-merge-landing-diff.py": "requires PR numbers; names the base it diffs against",
}


def _guards_on_disk() -> set[str]:
    """Every guard in the family. `check*.py`, not `check-*.py`: one member
    (`check_nonlocal.py`) spells the delimiter differently, and a hyphen-only glob would
    have left it out of the rule without anyone deciding to — which is the way an
    exemption actually happens. This test's own stale-entry assertion caught it, and
    the pattern is widened rather than the entry dropped, because that file reads a
    working tree and names it."""
    return {path.name for path in SCRIPTS_DIR.glob("check*.py")}


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _first_line(proc: subprocess.CompletedProcess) -> str:
    out = (proc.stdout or "").splitlines()
    return out[0] if out else ""


def _named_root(proc: subprocess.CompletedProcess) -> str:
    """The path a `tree: <path>` line names, or `""` when there is no such line."""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("tree: "):
            return line[len("tree: ") :].strip()
    return ""


def test_every_guard_in_the_family_is_classified() -> None:
    """A new `scripts/check-*.py` must be classified here before it can pass.

    The rule this file holds (a tree-reading guard names its tree) is only a rule if a
    guard cannot arrive unclassified — otherwise the next guard that forgets the line
    is simply one nobody wrote a test for, which is how the guard this file's docstring
    names came to be missing it.
    """
    classified = (
        set(RUN_HERE)
        | {TAKES_A_TREE_ARGUMENT}
        | set(NAMES_ITS_TREE_BUT_IS_NOT_RUN_HERE)
        | set(NOT_TREE_READERS)
    )
    on_disk = _guards_on_disk()
    assert on_disk - classified == set(), (
        f"these guards are in scripts/ and are not classified above: "
        f"{sorted(on_disk - classified)} - add each to RUN_HERE (it reads a working "
        "tree and can be run without a toolchain), to "
        "NAMES_ITS_TREE_BUT_IS_NOT_RUN_HERE (it reads one but cannot be run here, with "
        "the reason), or to NOT_TREE_READERS (with what it reads instead)"
    )
    assert classified - on_disk == set(), (
        f"these are classified above and no longer exist: {sorted(classified - on_disk)} "
        "- a stale entry would keep the rule looking wider than it is"
    )


def test_a_tree_reading_guard_names_its_own_checkout_whatever_the_cwd(tmp_path) -> None:
    """The first line names the tree that answered — not the directory it was launched from.

    Launched from `tmp_path`, a guard that reported its caller's working directory would
    name `tmp_path`; one hardcoded to a checkout would name that checkout even when the
    checkout is not the subject. These three derive their root from their own file, so
    the tree that answered is this one in both runs, and the assertion is the pair.
    """
    for name in RUN_HERE:
        script = SCRIPTS_DIR / name

        here = _run([str(script)], cwd=REPO_ROOT)
        assert _first_line(here) == f"tree: {REPO_ROOT}", (
            f"{name}: first line is {_first_line(here)!r}, not `tree: {REPO_ROOT}` — a "
            "guard that reads a tree must say which one before it says anything else "
            f"(rc={here.returncode}, stderr={here.stderr.strip()[:200]!r})"
        )

        elsewhere = _run([str(script)], cwd=tmp_path)
        named = _named_root(elsewhere)
        assert named, (
            f"{name}: launched from {tmp_path} it printed no `tree: ` line at all "
            f"(rc={elsewhere.returncode}, stdout={elsewhere.stdout[:200]!r})"
        )
        assert Path(named).resolve() == REPO_ROOT, (
            f"{name}: launched from {tmp_path} it named {named!r} — this guard answers "
            "about its own checkout, and a line that follows the caller's cwd would "
            "make the same report true of two different trees"
        )


def test_the_guard_that_takes_a_tree_names_the_tree_it_was_given(tmp_path) -> None:
    """The citation guard's line follows its **argument** — the leg a constant dies on.

    This guard is the one whose tree is chosen by its caller, so its subject is not this
    repository: pointed at a directory a test built, it must say so. A line hardcoded to
    the repository root would pass every other assertion in this file and fail here.
    """
    script = SCRIPTS_DIR / TAKES_A_TREE_ARGUMENT
    subject = tmp_path / "some-tree"
    subject.mkdir()

    proc = _run([str(script), str(subject)], cwd=REPO_ROOT)

    named = _named_root(proc)
    assert named, (
        f"{TAKES_A_TREE_ARGUMENT}: no `tree: ` line in its report "
        f"(rc={proc.returncode}, stdout={proc.stdout[:200]!r})"
    )
    assert Path(named).resolve() == subject.resolve(), (
        f"{TAKES_A_TREE_ARGUMENT}: it was given {subject} and named {named} — the line "
        "must name the tree that answered, or it is a constant wearing the "
        "convention's clothes"
    )
    assert Path(named).resolve() != REPO_ROOT, (
        "the control is only discriminating while the tree it was given is not this "
        "repository — otherwise both halves of this file's control name the same path"
    )


def test_the_citation_guards_line_is_not_merely_the_default(tmp_path) -> None:
    """Run with no argument, that guard names the tree it defaults to (`.`), not this one.

    The default is what a cycle gets when it runs the guard by hand from a shell, so the
    line has to be right there too — and it is the run that shows the guard's subject is
    "the tree you are standing in" rather than "the repository the file lives in".
    """
    script = SCRIPTS_DIR / TAKES_A_TREE_ARGUMENT
    proc = _run([str(script)], cwd=tmp_path)

    named = _named_root(proc)
    assert Path(named).resolve() == tmp_path.resolve(), (
        f"{TAKES_A_TREE_ARGUMENT}: run from {tmp_path} with no argument it named "
        f"{named!r} — its default root is `.`, and that is the tree it read"
    )
