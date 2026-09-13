"""Tests for scripts/check-merge-tree-health.py - does a merge land a healthy tree?

Background (cycle cyc20260912-040220)
-------------------------------------
Eleven PRs were open, each green in CI, each `MERGEABLE`. Every gate the repo had
answered a question *about a PR*: are the votes current, can the base reach
master, is the CI verdict fresh, what would this dirty. None answered the one
that decides whether master is healthy a minute later: **does the tree produced
by merging this PR pass the guards the repo enforces on master?**

It does not, for a whole class of pairs. Two PRs that each add tests and each
rewrite Agent.md's documented count to the value true *for itself* merge with **no
conflict** - git keeps one copy of the line - while the merged tree collects more
tests than it documents:

    #1133 alone   : documents 1500, collects 1500   -> consistent
    #1140 alone   : documents 1500, collects 1500   -> consistent
    merged together: documents 1500, collects 1506  -> guard FAIL

The tests below reproduce that mechanism rather than a fixed string: the stub
guard **counts test files in its own tree** and compares that with the number
Agent.md states. Two branches each add one test file and each set the number to
their own total (both "2"), so the merge of the count line is clean and the
merged tree has three files - the same shape as the live finding, arrived at
hermetically.

Pinned in both directions (#455 - never infer from the failure case alone):

* a clean merge whose merged tree passes the guard is HEALTHY;
* a clean merge whose merged tree fails it is UNHEALTHY - the finding;
* a genuine conflict is CONFLICT, not a failure: there is no merged tree to judge;
* a guard that cannot be run at all is a measurement error, never "healthy" - the
  asymmetry that matters, since an unanswerable question reported as health is how
  a broken tree reaches master.

Hermetic: everything runs against real local git repos (cheap, no network, no
GitHub), and the guard lives *in the tree under test*, so the verdict is always a
measurement of the merged tree rather than a model of it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-tree-health.py"
GUARD_PATH = "scripts/check-doc-count.py"
DOC_PATH = "Agent.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_tree_health", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


def _write(repo: Path, relpath: str, body: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# The stub guard measures its own tree: it counts `tests/test_*.py` and compares
# with the number Agent.md states. That is what makes the union matter - a model
# that read the two sides could not see the merged tree's extra file.
#
# Its report carries the *current* guard's shape. Measured 2026-09-14
# (`cyc20260914-033026`): these stubs printed the pre-#1158 wording (`documents N
# but M are collected`) that the real guard stopped printing on 2026-09-13, so this
# tool's parser could not read a real finding and reported the tail of the guard's
# advice as the finding. `test_the_real_guard_failure_line_is_what_this_tool_requires`
# pins the wording to the real guard, so the fixture cannot drift from it again.
GUARD = '''#!/usr/bin/env python3
import pathlib, re, sys

doc = pathlib.Path("Agent.md").read_text(encoding="utf-8")
m = re.search(r"tests/ -v` \\((\\d+)\\)", doc)
if not m:
    print("no documented count found", file=sys.stderr)
    sys.exit(2)
documented = int(m.group(1))
found = len(list(pathlib.Path("tests").glob("test_*.py")))
if documented == found:
    print("tree: .")
    print("OK: no tracked file states the Python test count")
    sys.exit(0)
print("tree: .")
print(f"FAIL: 1 tracked file(s) state the Python test count ({documented} documented, {found} collected)")
print()
print("Fix with: uv run --no-sync python3 scripts/check-doc-count.py --write")
sys.exit(1)
'''

GUARD_UNRUNNABLE = """#!/usr/bin/env python3
import sys
print("could not parse a collected count from pytest output", file=sys.stderr)
sys.exit(2)
"""

# A guard that crashes: an unhandled exception exits 1, the same code the stub
# above uses for a finding, and nothing in its report says a verdict was reached.
# This is not hypothetical - the guard runs in a pristine export with
# `sys.executable`, so any unimportable dependency lands here.
GUARD_CRASHES = """#!/usr/bin/env python3
import definitely_not_a_module
"""


def _doc(n: int) -> str:
    return f"Python: `uv run pytest tests/ -v` ({n})\n"


def _seed(repo: Path, tests: int) -> None:
    """A repo whose tree is self-consistent with `tests` test files."""
    _init_repo(repo)
    _write(repo, GUARD_PATH, GUARD)
    _write(repo, DOC_PATH, _doc(tests))
    for i in range(tests):
        _write(repo, f"tests/test_seed{i}.py", f"def test_seed{i}():\n    assert True\n")
    _commit(repo, "base")


def _add_test(repo: Path, name: str, doc_count: int) -> None:
    """Add one test file and set the documented count, as a real PR does."""
    _write(repo, f"tests/test_{name}.py", f"def test_{name}():\n    assert True\n")
    _write(repo, DOC_PATH, _doc(doc_count))


def _guard_verdict(mod, repo: Path, tmp_path: Path, commit: str):
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    return mod._guard_verdict(tree, tmp_path / "work", cwd=str(repo))


# --- the decision point: the merged tree's guard, not the sides -----------------


def test_a_tree_whose_guard_passes_is_healthy(mod, tmp_path) -> None:
    repo = tmp_path / "ok"
    _seed(repo, 1)
    head = _git(repo, "rev-parse", "HEAD")
    passed, report = _guard_verdict(mod, repo, tmp_path, head)
    assert passed is True
    assert "no stored count" in report


def test_a_tree_whose_guard_fails_is_unhealthy(mod, tmp_path) -> None:
    """The finding itself: the tree is self-inconsistent."""
    repo = tmp_path / "stale"
    _seed(repo, 1)
    # Two test files, but the doc still says one - exactly the stale-union state.
    _write(repo, "tests/test_extra.py", "def test_extra():\n    assert True\n")
    head = _commit(repo, "add an unaccounted test")
    passed, report = _guard_verdict(mod, repo, tmp_path, head)
    assert passed is False
    # The finding is quoted from the guard's own line, not re-derived here.
    assert "1 tracked file(s) state the test count" in report


def test_a_guard_that_cannot_run_is_a_measurement_error_not_a_pass(mod, tmp_path) -> None:
    """An unanswerable question must never be reported as health."""
    repo = tmp_path / "unrunnable"
    _init_repo(repo)
    _write(repo, GUARD_PATH, GUARD_UNRUNNABLE)
    _write(repo, DOC_PATH, _doc(1))
    head = _commit(repo, "base")
    with pytest.raises(mod.MeasurementError):
        _guard_verdict(mod, repo, tmp_path, head)


def test_a_tree_without_the_guard_is_a_measurement_error(mod, tmp_path) -> None:
    repo = tmp_path / "bare"
    _init_repo(repo)
    _write(repo, DOC_PATH, _doc(1))
    head = _commit(repo, "base")
    with pytest.raises(mod.MeasurementError):
        _guard_verdict(mod, repo, tmp_path, head)


def test_a_guard_that_crashes_is_a_measurement_error_not_a_finding(mod, tmp_path) -> None:
    """Exit 1 is what an unhandled exception produces as well as a finding.

    Without the guard's own failure line there is no verdict to report, and
    "unhealthy" would be a finding about a tree nobody measured - the one
    direction this tool must never take, because its whole output is a verdict on
    whether a merge lands a healthy tree.
    """
    repo = tmp_path / "crashed"
    _init_repo(repo)
    _write(repo, GUARD_PATH, GUARD_CRASHES)
    _write(repo, DOC_PATH, _doc(1))
    head = _commit(repo, "base")
    with pytest.raises(mod.MeasurementError) as excinfo:
        _guard_verdict(mod, repo, tmp_path, head)
    # The error names the guard and the tree it was run from, so a reader can
    # re-run exactly that instead of guessing.
    assert GUARD_PATH in str(excinfo.value)


def test_the_real_guard_failure_line_is_what_this_tool_requires(mod, tmp_path) -> None:
    """Pin the marker to the real guard, so parser and guard cannot drift apart.

    The stubs above are fixtures: they prove the parse, not that the guard still
    prints a line this tool recognises as its failure line. Wording drift is
    exactly what happened once already (the pre-#1158 `documents N but M are
    collected`), and its effect is silent in one direction: a real finding would
    become a measurement error rather than a report.
    """
    repo = tmp_path / "real"
    _init_repo(repo)
    _write(repo, GUARD_PATH, (REPO_ROOT / GUARD_PATH).read_text(encoding="utf-8"))
    _write(repo, "tests/test_seed0.py", "def test_seed0():\n    assert True\n")
    # A stored count: since #1181 that alone is the guard's finding, whatever the
    # tree collects.
    _write(repo, DOC_PATH, _doc(1))
    head = _commit(repo, "a tree that stores the count")

    passed, report = _guard_verdict(mod, repo, tmp_path, head)

    assert passed is False, report
    assert "state the test count" in report, report


def test_the_real_guard_that_stores_no_count_is_healthy(mod, tmp_path) -> None:
    """The other direction through the real guard: no stored count, no finding."""
    repo = tmp_path / "real-ok"
    _init_repo(repo)
    _write(repo, GUARD_PATH, (REPO_ROOT / GUARD_PATH).read_text(encoding="utf-8"))
    _write(repo, "tests/test_seed0.py", "def test_seed0():\n    assert True\n")
    _write(repo, DOC_PATH, "# Doc\nPython: `uv run pytest tests/ -v`\n")
    head = _commit(repo, "a tree that stores nothing")

    passed, report = _guard_verdict(mod, repo, tmp_path, head)

    assert passed is True, report
    assert "no stored count" in report


def test_a_healthy_report_that_is_not_recognised_is_not_claimed_as_a_check(
    mod, tmp_path
) -> None:
    """Exit 0 without the guard's own OK line: report no more than it said.

    The report is what a reviewer reads, so "no stored count" is a claim about the
    tree - making it when the guard never said it would be this tool inventing a
    verdict, which is the same defect class as reporting a crash as a finding, one
    exit code over. Measured 2026-09-14 (`cyc20260914-033026`): a mutant that always
    claimed the recognised line survived the whole suite.
    """
    repo = tmp_path / "quiet"
    _init_repo(repo)
    _write(repo, GUARD_PATH, "#!/usr/bin/env python3\nprint('tree: .')\n")
    _write(repo, DOC_PATH, _doc(1))
    head = _commit(repo, "a guard whose report is not recognised")

    passed, report = _guard_verdict(mod, repo, tmp_path, head)

    assert passed is True
    assert report == "guard OK"


# --- the merge question ---------------------------------------------------------


def _two_pr_repo(tmp_path: Path, theirs_doc: int = 2) -> tuple[Path, str, str]:
    """Master and a branch that each add a test file and set the count to 2.

    Both sides state the *same* number, so the count line merges cleanly and the
    merged tree has three test files while the doc says two. Returns
    (repo, master, head).
    """
    repo = tmp_path / "m"
    _seed(repo, 1)

    _git(repo, "checkout", "-q", "-b", "feature")
    _add_test(repo, "branch", theirs_doc)
    head = _commit(repo, "branch adds a test")

    _git(repo, "checkout", "-q", "master")
    _add_test(repo, "master", 2)
    master = _commit(repo, "master adds a test")
    return repo, master, head


def _drive(mod, repo: Path, master: str, head: str, tmp_path: Path, monkeypatch):
    """Point the tool at local objects, bypassing gh/fetch entirely."""
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: ref)
    return mod.check_pr(1, master, tmp_path / "work", cwd=str(repo))


def test_a_clean_merge_that_lands_a_failing_tree_is_unhealthy(
    mod, tmp_path, monkeypatch
) -> None:
    """The class this tool exists for: no conflict, but the tree is inconsistent.

    Each side's own tree is self-consistent (both state 2 and hold 2 test files).
    Only the merged tree is wrong - it holds three. Reasoning from the two sides
    would clear this merge; asking the merged tree does not.
    """
    repo, master, head = _two_pr_repo(tmp_path)
    # Precondition: the merge really is clean, so this is not the conflict case.
    assert mod._merge_tree_paths(master, head, cwd=str(repo)) == []

    state, report = _drive(mod, repo, master, head, tmp_path, monkeypatch)
    assert state == "unhealthy", report
    assert "1 tracked file(s) state the test count" in report


def test_each_side_alone_is_healthy_so_only_the_union_is_broken(
    mod, tmp_path, monkeypatch
) -> None:
    """Direction that makes the finding specific: both sides pass on their own."""
    repo, master, head = _two_pr_repo(tmp_path)
    for label, commit in (("master", master), ("branch", head)):
        passed, report = _guard_verdict(mod, repo, tmp_path, commit)
        assert passed is True, f"{label} alone should be self-consistent: {report}"


def test_a_corresponding_clean_merge_that_is_consistent_is_healthy(
    mod, tmp_path, monkeypatch
) -> None:
    """The negative case: a merge whose union the doc accounts for is fine.

    Only the branch adds a test, and it sets the count to the total that the
    merged tree will have, so the merge is both clean and consistent.
    """
    repo = tmp_path / "okmerge"
    _seed(repo, 1)
    _git(repo, "checkout", "-q", "-b", "feature")
    _add_test(repo, "branch", 2)
    head = _commit(repo, "branch adds a test")
    _git(repo, "checkout", "-q", "master")
    _write(repo, "README.md", "unrelated\n")
    master = _commit(repo, "master touches something else")

    state, report = _drive(mod, repo, master, head, tmp_path, monkeypatch)
    assert state == "healthy", report


def test_a_conflicting_merge_is_a_conflict_not_a_health_verdict(
    mod, tmp_path, monkeypatch
) -> None:
    """No merged tree exists, so this must not be reported as unhealthy."""
    repo = tmp_path / "conflict"
    _seed(repo, 1)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, DOC_PATH, _doc(7))
    head = _commit(repo, "branch states 7")
    _git(repo, "checkout", "-q", "master")
    _write(repo, DOC_PATH, _doc(9))
    master = _commit(repo, "master states 9")

    state, report = _drive(mod, repo, master, head, tmp_path, monkeypatch)
    assert state == "conflict", report
    assert "Agent.md" in report


# --- a mutable ref name must never reach merge-tree -----------------------------


def test_rev_parse_resolves_a_name_to_a_commit(mod) -> None:
    """The invariant `check-merge-order.py` shipped broken: a *name* reaching
    `merge-tree` answers about whatever the last fetch left behind. Asserted over
    the argv the tool builds, which is the thing that would have caught it."""
    seen: list[list[str]] = []
    real = mod._run

    def spy(argv, cwd=None):
        seen.append(list(argv))
        return real(argv, cwd=cwd)

    mod._run = spy
    try:
        try:
            mod._rev_parse("HEAD")
        except mod.MeasurementError:
            pass
    finally:
        mod._run = real
    assert seen, "no git invocation was recorded"
    assert seen[0][:2] == ["git", "rev-parse"], seen[0]
    assert "merge-tree" not in seen[0]


# --- CLI contract ---------------------------------------------------------------


def test_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    assert "--base" in proc.stdout


def test_an_unresolvable_base_fails_loud_rather_than_reporting_health() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "refs/does-not-exist"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 2
    assert "could not measure" in (proc.stdout + proc.stderr)


def test_agent_md_documents_the_canonical_invocation() -> None:
    text = (REPO_ROOT / DOC_PATH).read_text(encoding="utf-8")
    assert "scripts/check-merge-tree-health.py" in text


# --- the base is refreshed before it is measured ---------------------------------
#
# The heads half of this tool's question is always current (every head is fetched
# from `refs/pull/<N>/head`); the base half was not, so a clean merge was judged
# against whatever the local ref happened to hold. Measured on this repo
# (`cyc20260913-231848`) over the 25 head refs saved in the clone: 22 conflict
# against both a stale and a fresh base (no tree - safe), and of the 3 that merge
# cleanly **all 3 land a different tree** once the base advances. The doc-count guard
# this tool runs passed in both trees there, so the measured damage is the wrong tree
# (8 files of master's own version bump flowing in), not a wrong verdict.
#
# A name is also ambiguous in a way a SHA is not: `refs/heads/<name>` is consulted
# before `refs/remotes/<name>`, so a stray local `origin/master` (which git itself
# creates when a fetch destination is written unqualified) shadows the remote ref.


def test_every_remote_tracking_spelling_is_refreshed(mod, monkeypatch) -> None:
    """`refs/remotes/origin/<branch>` is the same mutable ref as `origin/<branch>`.

    Pinned at the argv level so both spellings are visible in one place: a predicate
    that admits only the short one is exactly the shape the siblings shipped, and it
    is invisible to a test that only ever passes the short spelling.
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: (calls.append(argv), _Done())[1])

    for base in ("origin/master", "refs/remotes/origin/master"):
        calls.clear()
        mod._refresh_base(base)
        assert len(calls) == 1, (base, calls)
        joined = " ".join(calls[0])
        # Fully qualified destination: a bare `origin/master` makes git create a local
        # branch of that name, which would then shadow the remote-tracking ref.
        assert "+refs/heads/master:refs/remotes/origin/master" in joined, (base, calls)


def test_a_sha_or_local_ref_base_is_never_fetched(mod, monkeypatch) -> None:
    """Only a remote-tracking name is refreshed; a SHA and a local branch are literal."""
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: (calls.append(argv), None)[1])

    for ref in (
        "0" * 40,
        "localbase",
        "refs/heads/x",
        "FETCH_HEAD",
        "refs/heads/origin/master",      # the shadowing stray
        "refs/remotes/upstream/master",  # another remote
        "refs/tags/v1.0.0",
    ):
        mod._refresh_base(ref)

    assert calls == [], calls


def test_a_branch_whose_name_ends_in_head_is_refreshed_not_refused(mod, monkeypatch) -> None:
    """`<branch>/HEAD` is a legal branch name, so that spelling is an ordinary ref.

    The discriminator is `git symbolic-ref`'s exit code, never the `/HEAD` suffix:
    `git check-ref-format --branch feature/HEAD` accepts the name, so
    `refs/remotes/origin/feature/HEAD` is the tracking ref of a branch called
    `feature/HEAD` - and a predicate keyed on the name refused a base that only needed
    filling in (the defect the siblings fixed in `cyc20260913-225642`).
    """
    calls: list[list[str]] = []

    class _Plain:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd=None):
        calls.append(argv)
        done = _Plain()
        done.returncode = 1 if "symbolic-ref" in argv else 0
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/feature/HEAD", "refs/remotes/origin/feature/HEAD"):
        calls.clear()
        mod._refresh_base(base)
        fetches = [c for c in calls if "fetch" in c]
        assert len(fetches) == 1, (base, calls)
        joined = " ".join(fetches[0])
        assert "+refs/heads/feature/HEAD:refs/remotes/origin/feature/HEAD" in joined, (
            base,
            calls,
        )


def test_a_head_spelling_is_refreshed_through_its_symref(mod, monkeypatch) -> None:
    """`origin/HEAD` is fetched at the ref it points to, not at its own name.

    `refs/heads/HEAD` does not exist upstream (the fetch fails) and writing into a
    symref cannot be locked at all - git refuses and leaves it unchanged - so
    "refresh it in place" is not an option git offers.
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd=None):
        calls.append(argv)
        done = _Done()
        if "symbolic-ref" in argv:
            done.stdout = "refs/remotes/origin/trunk\n"
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/HEAD", "refs/remotes/origin/HEAD"):
        calls.clear()
        mod._refresh_base(base)
        fetches = [c for c in calls if "fetch" in c]
        assert len(fetches) == 1, (base, calls)
        assert "refs/heads/trunk:refs/remotes/origin/trunk" in " ".join(fetches[0]), (base, calls)


def test_a_head_spelling_that_resolves_outside_origin_is_a_measurement_error(
    mod, monkeypatch
) -> None:
    """A symref that does not lead to `origin`'s tracking refs is refused."""

    class _Elsewhere:
        returncode = 0
        stdout = "refs/heads/master\n"
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: _Elsewhere())

    for base in ("origin/HEAD", "refs/remotes/origin/HEAD"):
        with pytest.raises(mod.MeasurementError) as excinfo:
            mod._refresh_base(base)
        assert "symbolic ref" in str(excinfo.value), (base, excinfo.value)


def test_main_refreshes_the_base_before_measuring(mod, monkeypatch, capsys) -> None:
    """`main` must actually call `_refresh_base` - a correct helper nobody calls is dead.

    Pinned separately because the tests above exercise the helper directly: deleting
    the call from `main` leaves them all green while the defect returns in full.
    """
    seen: list[str] = []
    monkeypatch.setattr(mod, "_refresh_base", lambda ref: seen.append(ref))

    class _Done:
        returncode = 0
        stdout = "0" * 40
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: _Done())
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: "refs/x")
    monkeypatch.setattr(mod, "_merge_tree_paths", lambda a, b, cwd=None: [])
    monkeypatch.setattr(mod, "_merged_tree_sha", lambda a, b, cwd=None: "0" * 40)
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir, cwd=None: (True, "guard OK"))

    rc = mod.main(["--base", "origin/master", "1"])

    assert seen == ["origin/master"], seen
    assert rc == 0, rc


def _stale_clone(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """A clone whose `refs/remotes/origin/master` is one commit behind the remote.

    Real git, no network: the remote is a local bare repo. Returns
    (bare, seed, repo, stale, advanced) - `stale` is what the clone holds, `advanced`
    what the remote moved on to, so any arm can be re-armed by writing `stale` back.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "master")

    seed = tmp_path / "seed"
    _init_repo(seed)
    _write(seed, "a.txt", "one\n")
    _commit(seed, "first")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "master")

    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(repo)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    stale = _git(repo, "rev-parse", "refs/remotes/origin/master")

    _write(seed, "a.txt", "one\ntwo\n")
    advanced = _commit(seed, "the remote moves on")
    _git(seed, "push", "-q", "origin", "master")

    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, "precondition"
    return bare, seed, repo, stale, advanced


def test_a_stale_base_is_refreshed_with_real_git(mod, tmp_path, monkeypatch) -> None:
    """The defect's effect, with real git: the ref the tool then reads is the remote's.

    It is the arm that fails before the fix - `_refresh_base` returned immediately, so
    the tool measured the stale commit and printed `origin/master` over it - and it is
    re-armed between spellings so one arm cannot hand the next an already-current ref.
    """
    _bare, _seed, repo, stale, advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    for base in ("origin/master", "refs/remotes/origin/master", "origin/HEAD",
                 "refs/remotes/origin/HEAD"):
        _git(repo, "update-ref", "refs/remotes/origin/master", stale)
        assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, base
        mod._refresh_base(base)
        assert _git(repo, "rev-parse", "refs/remotes/origin/master") == advanced, base
    assert _git(repo, "rev-parse", "master") == stale, (
        "the refresh moves the remote-tracking ref only, never a local branch"
    )


def test_a_stale_base_lands_a_different_tree_so_the_refresh_matters(
    mod, tmp_path, monkeypatch
) -> None:
    """Why the refresh is not cosmetic: the base decides the tree this tool judges.

    The branch point is behind, the remote moves on, and the PR head does not contain
    that move - the ordinary state of a queued PR. Folding the head onto the stale ref
    and onto the true one gives **different trees**, so without the refresh the gate
    answers "is this merge healthy" about a tree nobody is going to land. This is the
    measurement over this clone's history, reproduced hermetically.
    """
    _bare, _seed, repo, stale, advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    # Bring the remote's newer commit into the object store **without** moving the
    # tracking ref: the point of the arm is to compare the two bases, so the stale ref
    # has to survive. `--refmap=` is what stops the clone's configured refspec from
    # also updating `refs/remotes/origin/*` (an empty command-line refspec alone does
    # not - git applies the configured one as well, measured here).
    _git(repo, "fetch", "-q", "--refmap=", "origin",
         "+refs/heads/master:refs/emrg-test/advanced")
    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, "still stale"

    # a PR head branched off the original master, created in the clone so the object
    # exists where the merge is folded
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "feature.txt", "the PR's own file\n")
    head = _commit(repo, "the PR adds a file")
    _git(repo, "checkout", "-q", "master")

    def merged_tree(base_ref: str) -> str:
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", base_ref, head],
            cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout.splitlines()[0].strip()

    tree_stale = merged_tree(stale)
    tree_fresh = merged_tree("refs/emrg-test/advanced")
    assert tree_stale != tree_fresh, (
        "precondition for this test: the two bases must land different trees"
    )

    # with the fix, the ref the tool resolves is the remote's, so it judges the real tree
    mod._refresh_base("origin/master")
    assert mod._rev_parse("origin/master") == advanced
    resolved = merged_tree(mod._rev_parse("origin/master"))
    assert resolved == tree_fresh
    assert resolved != tree_stale, (
        "measuring the stale ref would have produced a different tree - the wrong-tree reading"
    )


def test_a_shadowing_local_branch_cannot_replace_the_base(mod, tmp_path, monkeypatch) -> None:
    """`origin/master` is resolved by its **full name**, so a stray cannot shadow it.

    git's precedence list consults `refs/heads/<name>` before `refs/remotes/<name>`, and
    git itself creates a local `origin/master` when a fetch destination is written
    unqualified - so the bare spelling can silently denote the wrong commit. The
    measured sibling case printed a two-cycle-old commit as `origin/master`.
    """
    _bare, _seed, repo, _stale, advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    mod._refresh_base("origin/master")
    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == advanced, "precondition"

    # a different commit, reachable only as a local branch - the stray git itself would
    # create if a fetch destination were written unqualified
    _write(repo, "local.txt", "local\n")
    stray = _commit(repo, "a local commit that must not be measured as origin/master")
    _git(repo, "update-ref", "refs/heads/origin/master", stray)

    # git's own precedence picks the local branch for the bare name - the trap
    assert _git(repo, "rev-parse", "origin/master") == stray, "precondition: the trap exists"
    # the tool resolves by full name, so it does not
    assert mod._qualify_ref("origin/master") == "refs/remotes/origin/master"
    assert mod._rev_parse("origin/master") == advanced, (
        "the qualified lookup must answer with the remote-tracking commit, not the stray"
    )
    assert mod._rev_parse("refs/heads/origin/master") == stray, "the stray is still a ref"


def test_a_name_that_denotes_only_a_local_branch_is_refused(mod, tmp_path, monkeypatch) -> None:
    """A short name with no remote-tracking ref behind it is not the remote branch."""
    _bare, _seed, repo, _stale, _advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/heads/origin/missing", head)
    assert not mod._ref_exists("refs/remotes/origin/missing")

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._qualify_ref("origin/missing")
    assert "denotes only the local branch" in str(excinfo.value)


def test_a_shadowing_stray_is_reported_so_it_can_be_removed(
    mod, tmp_path, monkeypatch, capsys
) -> None:
    """Resolving by full name keeps the answer right, so the shadow only *warns*.

    The warning is the load-bearing part: the same stray silently misleads every other
    short-name reader in the checkout (`git checkout origin/master`), and this tool
    cannot fix those - it can only say what to delete. A warning that is not asserted
    is a warning that can be deleted without any test noticing.
    """
    _bare, _seed, repo, _stale, _advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    mod._refresh_base("origin/master")
    _git(repo, "update-ref", "refs/heads/origin/master", _git(repo, "rev-parse", "HEAD"))

    assert mod._qualify_ref("origin/master") == "refs/remotes/origin/master"
    err = capsys.readouterr().err
    assert "ambiguous" in err, err
    assert "git branch -D origin/master" in err, err


def test_a_base_that_cannot_be_refreshed_is_a_measurement_error(mod, monkeypatch) -> None:
    """A failed fetch is exit 2, never a quiet fall-back to the stale commit.

    The fail-open direction is the dangerous one: continuing against a base that could
    not be verified is how this tool would report health for a tree nobody will land.
    """
    calls: list[list[str]] = []

    class _Fail:
        returncode = 128
        stdout = ""
        stderr = "fatal: couldn't find remote ref"

    def fake_run(argv, cwd=None):
        calls.append(argv)
        done = _Fail()
        if "symbolic-ref" in argv:
            done.returncode = 1
            done.stderr = ""
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/master", "origin/HEAD"):
        with pytest.raises(mod.MeasurementError) as excinfo:
            mod._refresh_base(base)
        assert "could not refresh" in str(excinfo.value), (base, excinfo.value)
    assert any("fetch" in c for c in calls), ("the fetch must be attempted", calls)

