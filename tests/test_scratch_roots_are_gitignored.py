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
name that call can create must be matched by git's ignore rules. That distinction is read
from the syntax rather than kept in a list, so a new site is found by *writing the call*, not
by remembering to edit this file.

Every other `dir=` is classified deliberately, into one of two buckets — because "not
`__file__`-derived" and "cannot be read" are not the same answer, and the first version of
this scan collapsed them:

* **provably outside the repository** — a root the syntax shows to be a temp location
  (`tmp_path` / `tmp_path_factory` fixtures, `tempfile.gettempdir()`, `TemporaryDirectory()`,
  `os.environ` / `getenv`) cannot dirty the tree, so it is out of scope. That is decided over
  the **whole** expression — every name it is built from has to be temp-rooted, and a name
  bound from a temp call (`with tempfile.TemporaryDirectory() as td:`) is followed, to a
  fixed point — rather than in the expression's bare-name position. The first version read
  only `dir=tmp_path`, so `tmp_path / "sub"`, the way a scratch dir gets a subdirectory, was
  reported as a root nobody could read: it failed a tree for doing what the failure message
  below tells it to do (measured 2026-09-17 by an outside reviewer on #1303);
* **unmeasurable** — anything else is reported with a remedy, never passed. This is the hole
  a two-hop root fell through: `REPO_ROOT = Path(__file__)…` then `TESTS_DIR = REPO_ROOT /
  "tests"`, the idiom *this* module uses for its own root, was invisible to a one-hop scan
  and was silently treated as a root that cannot dirty the tree — so a real in-repo site
  escaped while the guard reported green. Provenance is now a fixed point over the module's
  assignments, so a root is followed however many hops it takes.

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


#: Fixture names whose root is a temp location by construction (pytest's `tmp_path` family).
_TEMP_FIXTURES = frozenset({"tmp_path", "tmp_path_factory"})

#: Substrings that mark a `dir=` expression as a temp location rather than a repo path.
_TEMP_MARKERS = ("gettempdir", "TemporaryDirectory", "mkdtemp", "environ", "getenv", "expanduser")


def _file_derived_names(tree: ast.AST) -> set[str]:
    """The names in `tree` that hold a path derived from `__file__`, to a fixed point.

    One pass over the module's assignments was the first version, and one hop is evadable by
    the idiom this very module uses for its own root: `REPO_ROOT = Path(__file__)…` is
    file-derived, but `TESTS_DIR = REPO_ROOT / "tests"` mentions only `REPO_ROOT`, so a site
    rooted at `TESTS_DIR` was invisible to the scan — and, worse than invisible, it fell into
    the out-of-scope branch, i.e. a real in-repo root was reported as one that cannot dirty
    the tree. Iterating to a fixed point follows a chain of any length (measured: the
    two-hop shape evaded the one-hop scan, and the guard module's own style is two hops).
    """
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    derived: set[str] = set()
    while True:
        grown = False
        for node in assignments:
            value_names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if not ("__file__" in value_names or (value_names & derived)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in derived:
                    derived.add(target.id)
                    grown = True
        if not grown:
            return derived


def _parameters(node: ast.AST) -> set[str]:
    """One function's parameter names, so a fixture-supplied root can be recognised."""
    a = node.args
    names = {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _bound_names(node: ast.AST) -> set[str]:
    """Names a *binding* in `node` shadows — assignment, `with … as`, `for … in`."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            names.update(t.id for t in sub.targets if isinstance(t, ast.Name))
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            if isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
    return names


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """`{child: parent}` for the whole module, so a call can find its enclosing defs."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _fixture_seed(call: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[str]:
    """The fixture names that can reach `call` — its *own* enclosing scopes' parameters.

    Seeded from every function's parameters module-wide instead, a local variable that
    merely *shares a name* with a fixture reads as a temp location: measured 2026-09-17 on
    head `0e27465f`, `tmp_path = Path("/abs/…")` in one function plus `def test_x(tmp_path)`
    elsewhere made `dir=tmp_path / "sub"` answer **outside** — a root the syntax does not
    show to be a temp directory, passed instead of reported, which is the value this guard's
    doctrine forbids. On the parent head the same shape was reported, so the fix's own arm
    is what widened it. A binding in the *same* scope shadows the parameter, so bound names
    are subtracted rather than trusted.
    """
    seed: set[str] = set()
    node: ast.AST = call
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seed |= {p for p in _parameters(node) if p in _TEMP_FIXTURES}
            seed -= _bound_names(node)
    return seed


def _temp_rooted_names(tree: ast.AST, seed: set[str]) -> set[str]:
    """Every name in `tree` a temp location is behind, to a fixed point.

    `dir=td` does not show that `td` came from `with tempfile.TemporaryDirectory() as td:`,
    and the docstring calls that family provably outside — so the *name* has to be followed,
    however many hops it takes, for the same reason `_file_derived_names` is: one hop was
    the evasion there. The `tmp_path` / `tmp_path_factory` fixtures are parameters rather
    than assignments, so the fixed point is seeded from `seed` — which is computed per call
    by `_fixture_seed`, never from the whole module's parameter list, or a name that merely
    looks like a fixture would be enough to pass an unreadable root.
    """
    bindings: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.append((target.id, node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                target = item.optional_vars
                if isinstance(target, ast.Name):
                    bindings.append((target.id, item.context_expr))
    temp = set(seed)
    while True:
        grown = False
        for name, value in bindings:
            if name in temp:
                continue
            mentioned = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            if mentioned & temp or any(m in ast.unparse(value) for m in _TEMP_MARKERS):
                temp.add(name)
                grown = True
        if not grown:
            return temp


def _scan(tree: ast.AST, path: Path) -> tuple[list[tuple[Path, int, str | None, str]],
                                              list[tuple[Path, int, str]]]:
    """`(in-scope sites, unmeasurable sites)` for one parsed module.

    A `dir=` is **in scope** when its expression mentions `__file__`, or is a name the module
    assigns from something file-derived (to a fixed point). It is **provably outside** when
    the syntax shows a temp location: a `tmp_path`/`tmp_path_factory` fixture name *that
    reaches this call* (its own enclosing scopes' parameters, minus names a binding shadows),
    a name bound from a temp call, or an expression carrying a temp marker — read over the
    whole expression, so `tmp_path / "sub"` is outside rather than unreadable. A root built
    only partly from temp names is *not* outside: every name it mentions has to be
    temp-rooted. Everything else — an unresolvable name, a path built from something the scan
    cannot follow — is **unmeasurable**, reported rather than passed.
    """
    derived = _file_derived_names(tree)
    parents = _parents(tree)
    in_scope: list[tuple[Path, int, str | None, str]] = []
    unmeasurable: list[tuple[Path, int, str]] = []
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
        arg = kw["dir"]
        names_in_arg = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
        if "__file__" in source or (isinstance(arg, ast.Name) and arg.id in derived):
            prefix = kw.get("prefix")
            prefix = (prefix.value if isinstance(prefix, ast.Constant)
                      and isinstance(prefix.value, str) else None)
            in_scope.append((path, node.lineno, prefix, source))
            continue
        temp_rooted = _temp_rooted_names(tree, _fixture_seed(node, parents))
        harmless = any(marker in source for marker in _TEMP_MARKERS) or (
            bool(names_in_arg) and names_in_arg <= temp_rooted
        )
        if not harmless:
            unmeasurable.append((path, node.lineno, source))
    return in_scope, unmeasurable


def _sites() -> list[tuple[Path, int, str | None, str]]:
    """Every in-scope `mkdtemp(dir=…)` call in `tests/`, as (file, line, prefix, dir source)."""
    sites: list[tuple[Path, int, str | None, str]] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        in_scope, _ = _scan(tree, path)
        sites.extend(in_scope)
    return sites


def _unmeasurable_sites() -> list[tuple[Path, int, str]]:
    """Every `mkdtemp(dir=…)` call in `tests/` whose root this scan cannot classify."""
    found: list[tuple[Path, int, str]] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _, unmeasurable = _scan(tree, path)
        found.extend(unmeasurable)
    return found


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

    unclassifiable = [
        f"{p.relative_to(REPO_ROOT)}:{n} (dir={src})"
        for p, n, src in _unmeasurable_sites()
    ]
    assert not unclassifiable, (
        "this scan cannot tell whether these scratch roots are inside the repository, so it "
        f"must not treat them as harmless: {unclassifiable}. A root reached through a chain "
        "of assignments is followed to a fixed point, so a two-hop `__file__` path is in "
        "scope; a root that is neither `__file__`-derived nor a temp location has to be "
        "written so the scan can see it (give it a name derived from `__file__`, or root it "
        "at `tmp_path` — a subdirectory under it counts, so `tmp_path / \"sub\"` is read as "
        "a temp location — and move it out of the repository)."
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


def test_the_scan_follows_a_root_to_any_depth_and_reports_what_it_cannot_read():
    """The classifier driven directly, once per bucket — the arms the guard exists for.

    The scan's liveness was asserted by counting sites, but a count cannot show that the
    *interesting* shapes are classified: the one-hop root it was written against, the
    two-hop root that evaded it (the idiom this module itself uses), a fixture root that
    is genuinely out of scope, and a root nothing can resolve — which must be reported
    rather than passed, because "could not measure" and "clean" must not be the same value.
    Synthetic sources rather than files: the classification is the thing under test.

    The out-of-scope family is driven in the spellings the *docstring* names rather than
    only in its bare form: an outside reviewer measured on 2026-09-17 that `tmp_path` was
    recognised while `tmp_path / "sub"` and a `TemporaryDirectory()` alias were reported
    unreadable, so the guard failed a tree for doing what its own remedy says. The
    counterpart arm is `partly_temp`: a temp name *mixed with* an unknown one has to stay
    unreadable, so widening the check cannot become a way for a repo path to hide.
    """
    one_hop = '''
import os, tempfile
root = os.path.dirname(os.path.abspath(__file__))
d = tempfile.mkdtemp(dir=root, prefix="emrg-one-")
'''
    two_hop = '''
import tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
d = tempfile.mkdtemp(dir=TESTS_DIR, prefix="emrg-two-")
'''
    three_hop = '''
import tempfile
from pathlib import Path
HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
SUB = ROOT / "tests" / "nested"
d = tempfile.mkdtemp(dir=SUB, prefix="emrg-three-")
'''
    fixture_root = '''
import tempfile
def test_x(tmp_path):
    d = tempfile.mkdtemp(dir=tmp_path, prefix="anything-")
'''
    fixture_subdir = '''
import tempfile
def test_x(tmp_path):
    d = tempfile.mkdtemp(dir=tmp_path / "sub", prefix="anything-")
'''
    tempdir_alias = '''
import tempfile
def test_x():
    with tempfile.TemporaryDirectory() as td:
        d = tempfile.mkdtemp(dir=td, prefix="anything-")
'''
    partly_temp = '''
import tempfile
def test_x(tmp_path, other_dir):
    d = tempfile.mkdtemp(dir=tmp_path / other_dir, prefix="anything-")
'''
    shadowed_name = '''
import tempfile
from pathlib import Path
def test_uses_the_fixture(tmp_path):
    pass
def test_other():
    tmp_path = Path("/abs/other-project/tests")
    d = tempfile.mkdtemp(dir=tmp_path, prefix="anything-")
'''
    shadowed_name_composite = '''
import tempfile
from pathlib import Path
def test_uses_the_fixture(tmp_path):
    pass
def test_other():
    tmp_path = Path("/abs/other-project/tests")
    d = tempfile.mkdtemp(dir=tmp_path / "sub", prefix="anything-")
'''
    shadowed_in_its_own_scope = '''
import tempfile
from pathlib import Path
def test_x(tmp_path):
    tmp_path = Path("/abs/somewhere/tests")
    d = tempfile.mkdtemp(dir=tmp_path, prefix="anything-")
'''
    shadowed_beside_a_nested_def = '''
import tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
tmp_path = REPO_ROOT / "tests"
def test_x():
    def helper(tmp_path):
        return tmp_path

    helper("x")
    d = tempfile.mkdtemp(dir=tmp_path / "sub", prefix="anything-")
'''
    fixture_in_a_nested_def = '''
import tempfile
def test_x():
    def helper(tmp_path):
        d = tempfile.mkdtemp(dir=tmp_path / "sub", prefix="anything-")

    helper("x")
'''
    unresolvable = '''
import tempfile
def test_x(some_dir):
    d = tempfile.mkdtemp(dir=some_dir, prefix="anything-")
'''

    def classify(source: str):
        tree = ast.parse(source)
        return _scan(tree, Path("synthetic.py"))

    for label, source, prefix in (
        ("one hop", one_hop, "emrg-one-"),
        ("two hops (the evasion)", two_hop, "emrg-two-"),
        ("three hops", three_hop, "emrg-three-"),
    ):
        in_scope, unmeasurable = classify(source)
        assert [p for _f, _n, p, _s in in_scope] == [prefix], (
            f"a root reached in {label} must be classified as in-repo, not dropped: "
            f"in_scope={in_scope} unmeasurable={unmeasurable}"
        )
        assert not unmeasurable, f"{label} is measurable — it must not be reported unknown"

    in_scope, unmeasurable = classify(fixture_root)
    assert not in_scope and not unmeasurable, (
        "a `tmp_path` root is a temp location: out of scope, and measurable enough to know so"
    )

    for label, source in (
        ("a `tmp_path` root with a subdirectory", fixture_subdir),
        ("a name bound from `TemporaryDirectory()`", tempdir_alias),
    ):
        in_scope, unmeasurable = classify(source)
        assert not in_scope and not unmeasurable, (
            f"{label} is a temp location as well, and the remedy below tells a "
            "contributor to write exactly this — reporting it unreadable fails a tree "
            "for obeying the failure message (measured 2026-09-17): "
            f"in_scope={in_scope} unmeasurable={unmeasurable}"
        )

    in_scope, unmeasurable = classify(partly_temp)
    assert not in_scope, "a root built partly from an unknown name is not evidence of a repo root"
    assert len(unmeasurable) == 1, (
        "one temp name in the expression does not make the whole root a temp location: "
        "every name it is built from has to be temp-rooted, or the check could be widened "
        f"until a repo path hides behind a fixture name (got {unmeasurable})"
    )

    for label, source in (
        ("a bare name that shadows a fixture", shadowed_name),
        ("a composite root built on a shadowing name", shadowed_name_composite),
        ("a fixture name rebound in its own scope", shadowed_in_its_own_scope),
        ("a fixture-named parameter of a def *nested beside* the call", shadowed_beside_a_nested_def),
    ):
        in_scope, unmeasurable = classify(source)
        assert not in_scope and len(unmeasurable) == 1, (
            f"{label} is not a temp location the syntax shows: the fixture name is a "
            "parameter of a *different* function, and a binding in this scope shadows it. "
            "Seeding from every function's parameters module-wide made this answer 'outside' "
            "— an unreadable root passed rather than reported (measured 2026-09-17 on head "
            f"`0e27465f`, and on the bare shape on its parent too): "
            f"in_scope={in_scope} unmeasurable={unmeasurable}"
        )

    # ...and the counterpart, so narrowing the seed to the *enclosing* scopes cannot become a
    # way to report a real fixture root: here the nested def IS the call's enclosing scope.
    in_scope, unmeasurable = classify(fixture_in_a_nested_def)
    assert not in_scope and not unmeasurable, (
        "a `tmp_path` parameter of the function the call sits in is a temp location, however "
        "deeply that function is nested — reading only module-level defs would report it, "
        f"failing a tree for a temp root: in_scope={in_scope} unmeasurable={unmeasurable}"
    )

    in_scope, unmeasurable = classify(unresolvable)
    assert not in_scope, "an unresolvable root is not evidence of a repo root"
    assert len(unmeasurable) == 1, (
        f"an unresolvable root must be reported as unmeasurable, got {unmeasurable}"
    )
    _file, lineno, source = unmeasurable[0]
    assert source == "some_dir", "the report names the expression it could not read"
    assert "mkdtemp" in unresolvable.splitlines()[lineno - 1], (
        "the reported line is the call itself, not a nearby one"
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
