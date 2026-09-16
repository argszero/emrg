"""Guard: a scratch directory a test creates inside the repository must be invisible to git.

The exposure, measured 2026-09-17 in this repository
----------------------------------------------------
Five sites in `tests/test_bash_tool_sandbox.py` run a POSIX shell in a private
`mkdtemp(dir=<this directory>)` and remove it in a `finally`. A `finally` does not run
under SIGKILL, and the residue is **tree dirt**: killing the corpus test inside its loop
(measured: kill at 0.35 s, the test's own window is ~0.5 s) left
`tests/emrg-corpus-fapk6xox/`, `git status` listed it as `??`, `git check-ignore` answered
*not ignored*, and the dirty-tree criterion — `TaskHandler._dirty_tree_would_lose_work_sync`,
the guard that decides whether a cycle keeps its tier — answered `loses=True`:

    tests/emrg-corpus-fapk6xox/ could not be read to compare

so the **next** evolution cycle keeps the read-only tier because a *test* was interrupted.
Removing that one directory, the same criterion answers `loses=False` (`the tree is clean`,
`git status` empty). That makes this the expensive class this repo has already paid for once
(33 consecutive zero-commit cycles, issue #1237), charging a cycle for a scratch directory
holding no work at all.

The criterion is not the thing to change — an untracked *directory* genuinely cannot be
compared to `HEAD`, so unique work and scratch look alike to it, and `loses=True` is the
safe answer there. The names are the thing to change, and `.gitignore` now carries them
(`/tests/emrg-*`, whose comment states the same measurement). This file is the mechanic that
keeps a new site from reintroducing the exposure, because a comment cannot.

The rule
--------
For every `mkdtemp(dir=…)` call in `tests/` whose `dir=` expression is **derived from
`__file__`** — i.e. a path *inside this repository*, since the module lives in `tests/` — the
name that call can create must be matched by git's ignore rules. A root that is not
`__file__`-derived (a `tmp_path` fixture, the default system temp) cannot dirty the tree and
is out of scope; that distinction is read from the syntax rather than kept in a list, so a
new site is found by *writing the call*, not by remembering to edit this file.

Two honest boundaries, stated rather than implied:
* a site with no literal `prefix=` cannot be measured statically (the name `mkdtemp` invents
  is random), so it is reported as an unmeasurable site with the remedy — never as a pass;
* the check asks git about a *synthesised* name (`<prefix>x`), not about a directory that
  exists, which is what makes it runnable on a clean tree (`git check-ignore` answers for
  paths that do not exist — verified: rc=1 for an unignored path, rc=0 for an ignored one).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def _sites() -> list[tuple[Path, int, str | None, str]]:
    """Every `mkdtemp(dir=…)` call in `tests/`, as (file, line, prefix, dir source).

    `dir source` is the expression as written (`ast.unparse`), which is what decides whether
    the site is in scope: `__file__` in it — directly or through a name assigned to such an
    expression in the same module — means the root is a path inside the repository.
    """
    sites: list[tuple[Path, int, str | None, str]] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        file_derived: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and "__file__" in ast.dump(node.value):
                file_derived.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called != "mkdtemp":
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if "dir" not in kw:
                continue
            source = ast.unparse(kw["dir"])
            is_file_derived = "__file__" in source or (
                isinstance(kw["dir"], ast.Name) and kw["dir"].id in file_derived
            )
            if not is_file_derived:
                continue
            prefix = kw.get("prefix")
            prefix = (prefix.value if isinstance(prefix, ast.Constant)
                      and isinstance(prefix.value, str) else None)
            sites.append((path, node.lineno, prefix, source))
    return sites


def _unignored(candidates: list[str]) -> list[str]:
    """The candidates git's ignore rules do not match, asked one at a time.

    `git check-ignore -q` exits 0 for a matched path and 1 for an unmatched one, and it
    answers for paths that do not exist — so no directory has to be created to ask.
    """
    bad: list[str] = []
    for rel in candidates:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", rel],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            bad.append(rel)
    return bad


def test_every_scratch_root_a_test_creates_in_the_repo_is_gitignored():
    """The invariant, with the instrument's own liveness asserted first.

    A scan that silently stopped finding sites would pass while measuring nothing, which is
    the failure mode a guard is supposed to have instead of its callers, so the count is
    asserted against the five sites the rule was written for rather than trusted.
    """
    sites = _sites()
    assert len(sites) >= 5, (
        f"the scan found {len(sites)} in-repo `mkdtemp(dir=…)` site(s) in tests/, fewer than "
        "the five this rule was measured against — either the sites moved or this scan is "
        "dead, and a guard that measures nothing passes for the wrong reason"
    )

    unmeasurable = [f"{p.relative_to(REPO_ROOT)}:{n}" for p, n, prefix, _src in sites
                    if not prefix]
    assert not unmeasurable, (
        "these sites create an in-repo scratch directory under a name mkdtemp invents, so no "
        f"instrument can ask git whether it is ignored: {unmeasurable}. Give the call a "
        "literal `prefix=` whose names `.gitignore` matches (e.g. the `/tests/emrg-*` rule), "
        "or move its root out of the repository."
    )

    candidates = [f"tests/{prefix}x" for _p, _n, prefix, _src in sites]
    bad = _unignored(candidates)
    assert not bad, (
        "these names a test can create inside the repository are not ignored by git, so an "
        f"interrupted run leaves tree dirt that costs the next cycle its tier: {bad}. Add "
        "the prefix to `.gitignore` (scoped to /tests/, with the measurement in the comment) "
        "or move the scratch root out of the repository."
    )


def test_the_ignore_check_discriminates_both_ways():
    """The instrument read in both states, because `_unignored` returning [] is the pass.

    An empty list is also what a broken call returns, so the pair is the evidence: the same
    function must reject a name the current rule ignores and name one it does not.
    """
    assert _unignored(["tests/emrg-corpus-fapk6xox"]) == [], (
        "the residue measured on 2026-09-17 is still ignored by /tests/emrg-* — if this "
        "fails the rule was narrowed, and the five sites are exposed again"
    )
    assert _unignored(["tests/scratch-not-covered-x"]) == ["tests/scratch-not-covered-x"], (
        "the check must report an unmatched path, or every site below it passes by default "
        "(the first draft of this control used `tests/emrg-…`, which the rule under test "
        "matches — a control that cannot fail is not one)"
    )
    assert _unignored(["emrg-corpus-x"]) == ["emrg-corpus-x"], (
        "the rule is scoped to /tests/ on purpose: the same name at the repository root must "
        "stay visible to git, or the pattern swallows files it was never about"
    )
