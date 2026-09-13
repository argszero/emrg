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
    print(f"OK: Agent.md documents {found} collected Python tests")
    sys.exit(0)
print("tree: .")
print(f"FAIL: Agent.md documents {documented} Python tests but {found} are collected")
print()
print("Fix with: uv run --no-sync python3 scripts/check-doc-count.py --write")
sys.exit(1)
'''

GUARD_UNRUNNABLE = """#!/usr/bin/env python3
import sys
print("could not parse a collected count from pytest output", file=sys.stderr)
sys.exit(2)
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
    assert "documents 1" in report


def test_a_tree_whose_guard_fails_is_unhealthy(mod, tmp_path) -> None:
    """The finding itself: the tree is self-inconsistent."""
    repo = tmp_path / "stale"
    _seed(repo, 1)
    # Two test files, but the doc still says one - exactly the stale-union state.
    _write(repo, "tests/test_extra.py", "def test_extra():\n    assert True\n")
    head = _commit(repo, "add an unaccounted test")
    passed, report = _guard_verdict(mod, repo, tmp_path, head)
    assert passed is False
    # Both numbers are quoted from the guard, not re-derived here.
    assert "documents 1" in report and "2 are collected" in report


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
    assert "documents 2" in report and "3 are collected" in report


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
