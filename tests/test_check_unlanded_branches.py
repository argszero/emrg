"""Tests for scripts/check-unlanded-branches.py - is this branch's work already landed?

Background (cycle cyc20261003-103411)
-------------------------------------
17 branches of one instance's work were audited by hand, one at a time, in a scratch
worktree: does it still merge, does its work exist on master, is it dead. The script
answers all three in one command. Its first version answered the third by "does master
hold the names and paths this branch adds" - and called seven branches that only *edit*
lines `SUPERSEDED` vacuously. Ground truth (copying each branch's own test files onto a
master worktree and running them) said **four of the seven were still live**: a 57%
false-positive rate on the one verdict a reader acts on by deleting a branch.

The lessons pinned here, each with the state it protects:

* an **UNCLASSIFIED** branch is never `SUPERSEDED` - vacuous truth is not a measurement
  (this is the regression the file exists for);
* `SUPERSEDED` survives with a **conclusive** producer instead: ancestry, which cannot be
  wrong about a pure edit;
* a **conflict outranks** the inability to classify, because it is a fact that was
  measured rather than a question that could not be answered;
* **every declared state has a producer** - an unreachable verdict is a verdict no
  reader can ever act on, and this module shipped one for a few minutes;
* "nothing matched" and "master is unreadable" are **rc 2**, never a clean report: an
  unread question is not a clean one.

Hermetic: real local git repos in `tmp_path`, no network, no GitHub, no `gh`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-unlanded-branches.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_unlanded_branches", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the module declares a dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
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
    # An empty identity may be inherited from the environment; pin it so the repo is
    # the only source of the commits these tests read.
    _git(path, "config", "commit.gpgSign", "false")


def _write(repo: Path, relpath: str, body: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------------------
# Scenarios. Each builds a repository whose ONLY branch state is the one under test, so
# a scenario's verdict cannot be an accident of another branch's shape.
# --------------------------------------------------------------------------------------


def _repo_adding_a_name(repo: Path) -> None:
    """A branch that introduces a function master does not have."""
    _init_repo(repo)
    _write(repo, "mod.py", "def a():\n    return 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "fix/adds-name")
    _write(repo, "mod.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    _commit(repo, "adds b")
    _git(repo, "checkout", "-q", "master")


def _repo_editing_a_line(repo: Path) -> None:
    """A branch that fixes a bug by editing a line: adds no name and no path."""
    _init_repo(repo)
    _write(repo, "mod.py", "def a():\n    return 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "fix/edits-a-line")
    _write(repo, "mod.py", "def a():\n    return 42\n")
    _commit(repo, "the fix")
    _git(repo, "checkout", "-q", "master")
    # master moves on independently, so the branch is NOT an ancestor of it and the
    # ancestry reading cannot rescue the classification.
    _write(repo, "other.py", "X = 1\n")
    _commit(repo, "master moves on")


def _repo_contained(repo: Path) -> None:
    """A branch whose tip master already contains - fully landed."""
    _init_repo(repo)
    _write(repo, "mod.py", "def a():\n    return 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "fix/landed")
    _write(repo, "mod.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    _commit(repo, "the work")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "fix/landed")
    # master advances past the merge, so the branch tip is a strict ancestor, not equal.
    _write(repo, "later.py", "Y = 2\n")
    _commit(repo, "after")


def _repo_conflicting_edit(repo: Path) -> None:
    """A branch that adds nothing new AND conflicts with master."""
    _init_repo(repo)
    _write(repo, "mod.py", "def a():\n    return 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "fix/conflicting")
    _write(repo, "mod.py", "def a():\n    return 2\n")
    _commit(repo, "branch side")
    _git(repo, "checkout", "-q", "master")
    _write(repo, "mod.py", "def a():\n    return 3\n")
    _commit(repo, "master side")


SCENARIOS = {
    "adds-name": (_repo_adding_a_name, "fix/adds-name"),
    "edits-a-line": (_repo_editing_a_line, "fix/edits-a-line"),
    "contained": (_repo_contained, "fix/landed"),
    "conflicting": (_repo_conflicting_edit, "fix/conflicting"),
}


@pytest.fixture
def scenario(tmp_path, monkeypatch, mod):
    """Build one scenario's repo and point the module at it."""

    def build(name: str):
        builder, branch = SCENARIOS[name]
        repo = tmp_path / name / "repo"
        builder(repo)
        monkeypatch.setattr(mod, "REPO_ROOT", repo)
        return repo, branch

    return build


def _run(mod, capsys, pattern: str, master: str = "master"):
    rc = mod.main([pattern, "--master", master])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# --------------------------------------------------------------------------------------
# The regression this file exists for
# --------------------------------------------------------------------------------------


def test_a_branch_that_only_edits_lines_is_unclassified_never_superseded(scenario, mod, capsys):
    """The measured defect: a pure edit was vacuously 'everything master holds'.

    It adds no name and no path, so the name comparison answered 'master has it all'
    for a branch whose fix is not on master at all.
    """
    scenario("edits-a-line")
    rc, out, err = _run(mod, capsys, "refs/heads/fix/*")

    assert mod.UNCLASSIFIED in out
    assert mod.SUPERSEDED not in out, out
    assert "UNCLASSIFIED 1" in out
    # rc 0 says only that nothing was found dead or dirty - and the reason it is not a
    # pass is said out loud rather than left for the reader to infer.
    assert rc == 0
    assert "UNCLASSIFIED" in err
    assert "test files" in err


def test_the_unclassified_note_names_how_many_and_the_stronger_reading(scenario, mod, capsys):
    scenario("edits-a-line")
    _, _, err = _run(mod, capsys, "refs/heads/fix/*")
    assert "1 branch(es) are" in err
    assert "copying the branch's changed test files onto a master worktree" in err


# --------------------------------------------------------------------------------------
# The states, each with a producer
# --------------------------------------------------------------------------------------


def test_a_branch_adding_a_name_master_lacks_is_unlanded(scenario, mod, capsys):
    scenario("adds-name")
    rc, out, _ = _run(mod, capsys, "refs/heads/fix/*")

    assert mod.UNLANDED in out
    assert rc == 0
    # the report is actionable: it names the symbol master lacks
    assert "mod.py::b" in out


def test_a_branch_whose_tip_master_contains_is_superseded_by_ancestry(scenario, mod, capsys):
    scenario("contained")
    rc, out, _ = _run(mod, capsys, "refs/heads/fix/*")

    assert mod.SUPERSEDED in out
    assert rc == 1
    assert "master already contains this tip" in out


def test_a_conflict_outranks_the_inability_to_classify(scenario, mod, capsys):
    """A measured fact beats a question that could not be answered.

    This branch adds no name and no path either - without the precedence rule it would
    read `UNCLASSIFIED`, and a reader deciding what to fold would be told 'cannot say'
    about a branch whose actual condition is 'does not merge'.
    """
    scenario("conflicting")
    rc, out, _ = _run(mod, capsys, "refs/heads/fix/*")

    assert mod.CONFLICTING in out
    assert mod.UNCLASSIFIED not in out, out
    assert rc == 1


def test_every_declared_state_has_a_producer(mod, scenario, capsys):
    """A verdict nothing can ever reach is a verdict no reader can act on.

    This module shipped exactly that for a few minutes: `SUPERSEDED` survived the fix as
    a printed state with no code path that could return it, and the docstring still
    advertised it. Enumerating the states against the scenarios is how that is caught
    rather than noticed.
    """
    declared = {mod.UNLANDED, mod.CONFLICTING, mod.SUPERSEDED, mod.UNCLASSIFIED}
    produced = set()
    for name in SCENARIOS:
        repo, branch = scenario(name)
        produced.add(mod.read_branch(branch, "master").state)

    assert declared - produced == set(), f"no scenario produces {declared - produced}"


def test_an_unreadable_ref_is_unknown_and_never_a_verdict(mod, tmp_path, monkeypatch, capsys):
    scenario_repo = tmp_path / "r"
    _init_repo(scenario_repo)
    _write(scenario_repo, "mod.py", "def a():\n    return 1\n")
    _commit(scenario_repo, "base")
    monkeypatch.setattr(mod, "REPO_ROOT", scenario_repo)

    reading = mod.read_branch("refs/heads/does-not-exist", "master")
    assert reading.state == mod.UNKNOWN
    assert reading.note


# --------------------------------------------------------------------------------------
# An unread question is not a clean one
# --------------------------------------------------------------------------------------


def test_nothing_matching_the_pattern_is_rc2_not_a_clean_report(scenario, mod, capsys):
    scenario("adds-name")
    rc, out, err = _run(mod, capsys, "refs/heads/nonexistent/*")

    assert rc == 2
    assert "no ref matches" in out + err
    assert "0 branch" not in out
    assert "UNLANDED" not in out


def test_master_that_cannot_be_resolved_is_rc2(scenario, mod, capsys):
    scenario("adds-name")
    rc, out, err = _run(mod, capsys, "refs/heads/fix/*", master="no-such-master")

    assert rc == 2
    assert "could not be resolved" in out + err


def test_the_summary_counts_every_branch_read(scenario, mod, capsys):
    scenario("adds-name")
    repo = mod.REPO_ROOT
    _git(repo, "branch", "fix/second", "master")
    _, out, _ = _run(mod, capsys, "refs/heads/fix/*")
    assert "of 2 branch(es)" in out


# --------------------------------------------------------------------------------------
# The name reading itself
# --------------------------------------------------------------------------------------


def test_names_are_collected_from_anywhere_in_the_file(mod):
    """A name added inside a class or a conditional is still a name the branch added."""
    source = (
        "def top():\n"
        "    pass\n"
        "\n"
        "\n"
        "class C:\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "if True:\n"
        "    def conditional():\n"
        "        pass\n"
        "\n"
        "\n"
        "async def coro():\n"
        "    pass\n"
    )
    assert mod._module_level_names(source) == {"top", "C", "method", "conditional", "coro"}


def test_unparseable_source_yields_no_names_rather_than_inventing_them(mod):
    """A branch whose Python does not parse has nothing to compare - say so."""
    assert mod._module_level_names("def broken(:\n") == set()


def test_a_branch_whose_python_does_not_parse_is_still_reported(mod, tmp_path, monkeypatch, capsys):
    """It must not crash the audit, and it must not borrow names from the broken file."""
    repo = tmp_path / "broken"
    _init_repo(repo)
    _write(repo, "mod.py", "def a():\n    return 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "fix/broken")
    _write(repo, "mod.py", "def a(:\n")
    _commit(repo, "broken")
    _git(repo, "checkout", "-q", "master")
    monkeypatch.setattr(mod, "REPO_ROOT", repo)

    rc, out, _ = _run(mod, capsys, "refs/heads/fix/*")
    assert rc in (0, 1)
    assert "Traceback" not in out


# --------------------------------------------------------------------------------------
# The pattern is an argument, not a guess
# --------------------------------------------------------------------------------------


def test_the_pattern_selects_which_branches_are_audited(scenario, mod, capsys):
    """Parallel instances push to the same remote under the same prefixes."""
    scenario("adds-name")
    repo = mod.REPO_ROOT
    _git(repo, "branch", "other/untouched", "master")

    _, only_fix, _ = _run(mod, capsys, "refs/heads/fix/*")
    assert "fix/adds-name" in only_fix
    assert "other/untouched" not in only_fix

    _, only_other, _ = _run(mod, capsys, "refs/heads/other/*")
    assert "other/untouched" in only_other
    assert "fix/adds-name" not in only_other


def test_the_report_names_the_tree_it_read_before_any_verdict(scenario, mod, capsys, tmp_path):
    """The `scripts/check*.py` family convention, and the leg
    `tests/test_a_tree_reading_guard_names_its_tree.py` points at when it classifies this
    member as "classified, its naming verified here".

    The subject is git refs belonging to the checkout the file lives in - not the caller's
    working directory - so the line must be that checkout, and it must come first, on the
    rc 2 paths as well as the reporting one.
    """
    repo, _ = scenario("adds-name")
    _, out, _ = _run(mod, capsys, "refs/heads/fix/*")
    assert out.splitlines()[0] == f"tree: {repo}", out

    # the same line on the path that measures nothing: a caller reading rc 2 still has to
    # know which tree could not answer
    _, out2, _ = _run(mod, capsys, "refs/heads/nonexistent/*")
    assert out2.splitlines()[0] == f"tree: {repo}", out2


def test_the_line_is_the_guards_own_checkout_not_the_callers_cwd(scenario, mod, capsys, tmp_path, monkeypatch):
    """A line that followed the process's working directory would make one report true of
    two different trees."""
    repo, _ = scenario("adds-name")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    _, out, _ = _run(mod, capsys, "refs/heads/fix/*")
    assert out.splitlines()[0] == f"tree: {repo}"
    assert str(other) not in out.splitlines()[0]


def test_the_default_pattern_is_a_remote_namespace_not_a_local_one(mod):
    """Documented behaviour: the tool audits fetched refs by default, not local branches."""
    import inspect

    source = inspect.getsource(mod.main)
    assert "refs/remotes/origin/fix/*" in source


def test_the_module_root_is_read_at_call_time_not_bound_as_a_default(mod):
    """A default argument is fixed at def time; a test repointing REPO_ROOT needs neither.

    Without this the whole file would be untestable off-repo, and - worse - a caller who
    changed REPO_ROOT would silently keep auditing the real checkout.
    """
    import inspect

    signature = inspect.signature(mod._git)
    assert signature.parameters["cwd"].default is None
    assert "REPO_ROOT" in inspect.getsource(mod._git)
