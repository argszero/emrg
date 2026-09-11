"""Tests for scripts/check-merge-freshness.py - is a PR's CI verdict still current?

Background (cycle cyc20260911-083721)
-------------------------------------
On 2026-09-11 PR #1137 was `MERGEABLE/CLEAN` with both CI jobs green, and the
merge still produced a tree that failed two guards. The cause is worth pinning in
tests because the failure is silent and the obvious signals are all green:

* GitHub builds `Merge <head> into <merge-base>` for `pull_request`, so the verdict
  is about the head merged onto the *branch point*. When master moves, the branch
  point is no longer master, and the green run describes a tree that cannot be
  merged - yet it stays green, because master moving is not a branch push and
  fires no `synchronize` event.
* `gh pr view --json mergeable` says `CLEAN` throughout, because it answers "does
  this textually merge", which is the property that failed.

Both states are pinned here, never inferred from the failure case alone (#455):

* **fresh** - the head contains master's tip and has a passing run for that exact
  SHA; rc 0, and the tool is safe to gate a merge on.
* **stale** - four distinguishable ways (diverged, no run at all, still running,
  concluded non-success), each with its own reason, so a caller can tell "rebase
  it" apart from "fix it".

The fourth case is the one a naive implementation gets wrong: a head that contains
master but has *zero* CI runs is `no checks reported`, not a pass. Ancestry alone
is not enough to call a verdict current, so it is tested as its own state.

Nothing here touches the network: `_gh_json` is replaced, and the replacement is
asserted to receive the arguments the real helper would (so a test cannot pass by
never calling it).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-freshness.py"

HEAD = "a" * 40
OTHER = "b" * 40
BASE = "c" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_freshness", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the module declares a dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation
    # time. A module that is not registered there raises AttributeError inside
    # dataclasses itself - an error that names neither this test nor the cause.
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeGh:
    """A stand-in for `_gh_json` that answers from a routing table.

    Records every call so a test can assert the query was actually made - a test
    whose fake is never called would pass while the code under test queried
    nothing at all.
    """

    def __init__(self, pr_view: dict, compare: dict, runs: list[dict] | None):
        self.pr_view = pr_view
        self.compare = compare
        self.runs = runs
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> object:
        self.calls.append(list(args))
        # Every gh invocation must name its program at the call site (the helper
        # prepends it); assert the shape rather than trusting it.
        assert args, "gh was called with no arguments"
        if args[:2] == ["pr", "view"]:
            return self.pr_view
        if args[0] == "api":
            if any("actions/runs" in a for a in args):
                return {"runs": self.runs if self.runs is not None else []}
            assert any(a.startswith("repos/") and "/compare/" in a for a in args), args
            return self.compare
        raise AssertionError(f"unexpected gh call: {args}")


def _compare(status: str, ahead: int, behind: int, base: str = BASE) -> dict:
    return {"status": status, "ahead_by": ahead, "behind_by": behind, "merge_base": base}


def _view(sha: str = HEAD, branch: str = "feature/x") -> dict:
    return {"number": 1, "title": "t", "headRefOid": sha, "headRefName": branch}


def _run_(sha: str = HEAD, conclusion: str = "success", at: str = "2026-09-11T00:00:00Z") -> dict:
    """A workflow run, as the actions/runs payload reports it.

    `name` is not decoration: the tool pins the verdict to the `Test` workflow, so
    a fixture without it is a run the tool is right to ignore.
    """
    return {"headSha": sha, "name": "Test", "conclusion": conclusion, "createdAt": at}


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh_json", fake)


def _run(mod, monkeypatch, fake: FakeGh, argv: list[str] | None = None) -> int:
    _install(mod, monkeypatch, fake)
    return mod.main(argv if argv is not None else ["1"])


# --- fresh -----------------------------------------------------------------


def test_fresh_when_head_contains_master_and_has_a_passing_run(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert "FRESH" in out
    assert "STALE" not in out
    assert len(fake.calls) == 3, "expected exactly the three queries, not a short-circuit"


def test_fresh_when_master_has_not_moved_at_all(mod, monkeypatch, capsys):
    """`identical` means master's tip *is* the head - trivially current."""
    fake = FakeGh(_view(), _compare("identical", 0, 0), [_run_()])
    assert _run(mod, monkeypatch, fake) == 0
    assert "FRESH" in capsys.readouterr().out


# --- stale: the four distinguishable ways ----------------------------------


def test_stale_when_the_head_does_not_contain_master(mod, monkeypatch, capsys):
    """The #1137 case: the verdict was green but about an older master."""
    fake = FakeGh(
        _view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()]
    )
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "no longer be merged" in out
    # The reason must name the branch point, or a reader cannot tell which master
    # the verdict was actually about.
    assert "cb651a4" in out


def test_stale_when_there_is_no_ci_run_for_this_head(mod, monkeypatch, capsys):
    """Ancestry is not enough: `no checks reported` is not a pass.

    This is the state a dropped push event leaves behind, and it is the one an
    ancestry-only implementation would wrongly call fresh.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr()
    assert rc == 1
    assert "NO Test run" in out.out
    assert "no checks reported" in out.out


def test_stale_when_a_run_exists_only_for_a_different_sha(mod, monkeypatch, capsys):
    """Keyed on the SHA, not the branch.

    A branch pushed twice has runs for both heads; reading the older one as the
    current verdict is the same mistake one step smaller.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(OTHER)])
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1
    assert "NO Test run" in capsys.readouterr().out


def test_a_passing_run_from_another_workflow_is_not_the_verdict(mod, monkeypatch, capsys):
    """The claim is "the *tests* passed", so the workflow is part of the query.

    Today `test.yml` is the only workflow `pull_request` triggers, so accepting
    any passing run happens to give the right answer - which is exactly the kind
    of coincidence that stops being true silently, the first time a second
    workflow is added to a branch. Pinned on a fixture whose only difference from
    the fresh case is the workflow name.
    """
    other_workflow = _run_()
    other_workflow["name"] = "Build Release"
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [other_workflow])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "NO Test run" in out, out


def test_stale_and_distinguished_when_ci_is_still_running(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="pending")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "still pending" in out


def test_a_failing_verdict_is_reported_as_failing_not_as_stale(mod, monkeypatch, capsys):
    """Re-running CI will not help, so the wording must not suggest a rebase does."""
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="failure")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "failing verdict, not a stale one" in out


def test_the_newest_run_for_the_head_wins(mod, monkeypatch, capsys):
    """Two runs on one SHA: the freshest conclusion decides, not the first seen."""
    fake = FakeGh(
        _view(),
        _compare("ahead", 1, 0),
        [
            _run_(conclusion="failure", at="2026-09-11T00:00:00Z"),
            _run_(at="2026-09-11T01:00:00Z"),
        ],
    )
    rc = _run(mod, monkeypatch, fake)
    assert rc == 0
    assert "FRESH" in capsys.readouterr().out


# --- fail loud, never guess ------------------------------------------------


def test_an_unrecognised_compare_status_is_refused_not_called_fresh(mod, monkeypatch, capsys):
    """A new GitHub status must not silently read as fresh.

    The freshness sets are named rather than written as `status == "ahead"` for
    exactly this case: anything unrecognised has to fail loud.
    """
    fake = FakeGh(_view(), _compare("some_new_status", 1, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unrecognised compare status" in err


def test_a_gh_failure_exits_2_with_the_reason(mod, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("gh failed (rc=1): gh api repos/x/compare/...\nsome stderr")

    monkeypatch.setattr(mod, "_gh_json", boom)
    rc = mod.main(["1"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "gh failed" in err


def test_the_helper_invokes_the_gh_program_by_name(mod, monkeypatch):
    """`_gh_json` must prepend the program name itself.

    Measured 2026-09-11: a call site that passed `["pr", "view", ...]` to a helper
    which also omitted the program name ran the POSIX `pr` utility, whose failure
    message (`pr: cannot open view`) names neither gh nor the real mistake.
    """
    import subprocess as sp

    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Done()

    monkeypatch.setattr(sp, "run", fake_run)
    mod._gh_json(["pr", "view", "1"])
    assert seen and seen[0][0] == "gh", seen
    assert seen[0][1:] == ["pr", "view", "1"]


def test_json_mode_is_machine_readable(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["stale"] is False
    assert payload[0]["head"] == HEAD
    assert payload[0]["merge_base"] == BASE


def test_no_function_has_an_unused_parameter() -> None:
    """Every declared parameter must be read somewhere in its own body.

    Added because this script shipped one: `_latest_run_for_head(head, branch,
    number)` never read `number`, left over from a draft that used it to build the
    error message. A dead parameter is not cosmetic here - it tells the next reader
    the function needs the PR number to do its job, which is exactly the kind of
    false signal about a *merge-gate* helper that this repo treats as a defect.

    Parsed with `ast` rather than a regex, and checked against the whole function
    body including nested scopes, so a parameter read only inside a closure still
    counts as used. `self` is excluded (methods), as are names prefixed with `_`.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        declared = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if args.vararg:
            declared.append(args.vararg.arg)
        if args.kwarg:
            declared.append(args.kwarg.arg)
        named = set(declared) - {"self"}
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        for param in sorted(named):
            if param not in used:
                offenders.append(f"{node.name}({param})")
    assert not offenders, (
        "unused parameter(s) - each declares a dependency the body does not have: "
        f"{offenders}"
    )
