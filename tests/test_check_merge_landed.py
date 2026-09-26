"""Tests for scripts/check-merge-landed.py - did a merge land the tree its votes named?

Background (cycle cyc20260925-174000)
------------------------------------
Every other gate in this family asks its question *before* the merge, and one of them
is asked as the vote is spent: `cast-vote.py` refuses a body whose tree claim is not
the tree this merge would land. Nothing read the claim against the merge that then
happened. The landing tree is a function of **(base, head)**, and the base moves - a
parallel cycle merging another PR in between makes the landed tree a union no vote was
about and no gate measured, while every signal stays green.

Measured while writing the tool, on this cycle's own merge: PR #1613's counted votes
named `7eec9629ed11`, the merge commit `f0287610` carries tree
`7eec9629ed11e3f7a68c35707420b802764ed3c1`, and they are equal - so the tool reports
`NAMED` for a merge that behaved, which is the state a tool like this must be able to
produce before its failure state means anything.

Pinned in both directions (#455 - never infer from the finding alone):

* the merge commit's own tree is what is read, **not the head's** - the control test
  builds a head whose tree differs from the landed one, and a tool that compared the
  head would call both a match (#1133/#1140's shape);
* a landed tree no review names is `DIVERGED`, rc 1, with both trees printed;
* a merged PR whose reviews name no tree is `UNCHECKED` - reported as its own state and
  said not to be a pass, because "nothing to compare" and "agrees" are different
  answers;
* an unmerged PR is `PENDING`: nothing has landed, so nothing is compared;
* an unanswerable question is rc 2 - a merge commit this clone cannot obtain, or a
  review response whose projection did not apply - never a pass.

Hermetic: real local git repositories in `tmp_path`, and a `gh` stand-in that routes by
endpoint. No network, no GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-landed.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_landed", SCRIPT)
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
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _point_at(mod, monkeypatch, repo: Path) -> None:
    """Point this tool *and* the sibling it borrows its reading from at `repo`.

    The claim reading is `cast-vote.py`'s `named_trees`, and it resolves each hex token
    against the checkout *that tool* lives in (`_REPO_ROOT`, derived from its own file).
    In production the two roots are the same directory by construction — both are
    `scripts/…` — so this asserts that agreement **before** overriding it: a future edit
    that gave the sibling another root would make this tool read claims out of a tree
    nobody voted in, and that is a failure this test should announce rather than hide by
    patching both ends.
    """
    assert mod.CAST_VOTE == mod.SCRIPTS_DIR / "cast-vote.py", (
        "the tool must borrow the sibling from the repo it lives in"
    )
    assert (
        mod.cast_vote()._REPO_ROOT == mod.REPO_ROOT
    ), "the two tools disagree about which checkout they read, and both halves must read the same one"
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    monkeypatch.setattr(mod.cast_vote(), "_REPO_ROOT", repo)


@pytest.fixture
def merged_repo(tmp_path: Path, mod, monkeypatch):
    """A repo where a PR was merged onto a base that moved: head tree != landed tree.

    The shape is the one this tool exists for, and it is built the way it really
    happens. A branch is cut from `master`, `master` then receives an unrelated
    commit (a parallel cycle merging something else), and the branch is merged into
    the moved master. The merge commit's tree therefore contains both sides, while
    the branch's own head carries only its half.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, "a.txt", "base\n", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    head = _commit(repo, "feature.txt", "feature\n", "the PR")
    _git(repo, "checkout", "-q", "master")
    _commit(repo, "other.txt", "parallel\n", "a parallel cycle merged something else")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge the PR", "feature")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    _point_at(mod, monkeypatch, repo)
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "merge_commit": merge_commit,
        "head_tree": _git(repo, "rev-parse", f"{head}^{{tree}}"),
        "base_tree": _git(repo, "rev-parse", f"{base}^{{tree}}"),
        "landed_tree": _git(repo, "rev-parse", f"{merge_commit}^{{tree}}"),
    }


class FakeGh:
    """A `gh` stand-in: one PR view and one review list, routed by endpoint.

    Records every call, so a test cannot pass because the tool queried nothing.
    """

    def __init__(self, view: dict, reviews: list[dict]):
        self.view = view
        self.reviews = reviews
        self.calls: list[list[str]] = []

    def _json(self, args: list[str]) -> object:
        self.calls.append(list(args))
        assert args[0] == "api", f"unexpected gh call: {args}"
        target = next((a for a in args if a.startswith("repos/")), "")
        if target.endswith("/reviews"):
            return self.reviews
        assert "/pulls/" in target, args
        return self.view

    def _paginated(self, args: list[str]) -> list:
        self.calls.append(list(args))
        # The caller must own the filter: without `--jq` the projection would not
        # apply, and the tool is supposed to refuse that rather than read nothing.
        assert "--jq" in args, f"the caller's projection is missing: {args}"
        return list(self.reviews)


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh_json", fake._json)
    # The reviews read is the counter's helper; the sibling is loaded lazily inside
    # `tree_claims`, so the stub goes on the loader's return value.
    monkeypatch.setattr(mod, "counter", lambda: type("C", (), {"_gh_json_paginated": fake._paginated}))


def _review(body: str, at: str = "2026-09-25T09:00:00Z") -> dict:
    # `submitted_at` is the key the sibling's projection emits; the tool refuses a
    # review without it, which the last test pins.
    return {"at": at, "body": body}


def _view(merge_commit: str, state: str = "closed", merged: bool = True) -> dict:
    return {
        "state": state,
        "merged": merged,
        "merged_at": "2026-09-25T09:45:10Z" if merged else None,
        "merge_commit_sha": merge_commit,
        "head_sha": "0" * 40,
        "base_sha": "1" * 40,
    }


def test_the_merge_commit_tree_is_what_is_read_not_the_heads(mod, merged_repo, monkeypatch):
    """The control the whole tool rests on: a head's tree is not what landed.

    The fixture's head tree and landed tree are deliberately different. A tool that
    asked the head (or the API's `head.sha`) would call this merge `NAMED` while
    reading a tree that never landed - the #1133/#1140 shape, where each side is
    consistent and only the union is the subject.
    """
    fixture = merged_repo
    assert fixture["head_tree"] != fixture["landed_tree"], (
        "the fixture must produce a head whose tree differs from the merge's, or this "
        "test cannot tell the two readings apart"
    )
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [_review(f"✅ LGTM — cycle cyc20260925-174000\n\nlanding tree {fixture['head_tree'][:12]}")],
    )
    _install(mod, monkeypatch, fake)

    verdict = mod.check_pr(1613, "argszero/emrg")

    assert verdict.landed_tree == fixture["landed_tree"], (
        "the tool read a tree other than the merge commit's - the claim it compares "
        "against would then be about a tree that never landed"
    )
    assert verdict.reading == mod.DIVERGED, (
        "a head's tree was named, not the merge's, so this must be a divergence"
    )
    assert fake.calls, "the tool answered without querying anything"


def test_a_vote_naming_the_landed_tree_is_named(mod, merged_repo, monkeypatch):
    """And the same merge reads `NAMED` once the body names the tree that landed."""
    fixture = merged_repo
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [_review(f"✅ LGTM — cycle cyc20260925-174000\n\nlanding tree {fixture['landed_tree'][:12]}")],
    )
    _install(mod, monkeypatch, fake)

    verdict = mod.check_pr(1613, "argszero/emrg")

    assert verdict.reading == mod.NAMED
    assert verdict.landed_tree == fixture["landed_tree"]
    assert verdict.claimed == [fixture["landed_tree"]], (
        "the abbreviated tree in the body must be resolved to the full sha, or a match "
        "would be read as a miss"
    )


def test_a_merge_no_review_names_is_reported_as_a_divergence(mod, merged_repo, monkeypatch, capsys):
    """`DIVERGED` prints both trees and exits 1 - never a quiet pass.

    The claim is a tree that really exists here (`master`'s tree at the moment the
    branch was cut, i.e. the base the vote would have measured before the parallel
    merge moved it) — not an invented sha, which `named_trees` would refuse to read as
    a claim at all and which would therefore test the other arm.
    """
    fixture = merged_repo
    assert fixture["base_tree"] != fixture["landed_tree"], "the fixture must offer a tree that is not the landed one"
    fake = FakeGh(_view(fixture["merge_commit"]), [_review(f"✅ LGTM — landing tree {fixture['base_tree'][:12]}")])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 1, "a merge landing an unnamed tree must not exit 0"
    assert mod.DIVERGED in out.out
    assert fixture["landed_tree"][:12] in out.out, "the landed tree must be printed"
    assert fixture["base_tree"][:12] in out.out, "the claimed tree must be printed beside it"
    assert "not a merge those votes" in out.err


def test_a_claim_this_clone_cannot_resolve_is_not_read_as_no_claim(
    mod, merged_repo, monkeypatch, capsys
):
    """A token that resolves to nothing is reported as unresolvable, not as absent.

    `named_trees` answers "trees named here", so a body whose tree this checkout does
    not hold returns an empty list - the same value as a body naming no tree. Those
    have different repairs (a shallow clone or pruned plan worktrees, versus a vote
    that was never about a landing tree), so the reason must name the right one, and a
    tool that merged the two would tell a reader the body names nothing when it names
    a tree.
    """
    fixture = merged_repo
    invented = "0123456789abcdef0123456789abcdef01234567"
    fake = FakeGh(_view(fixture["merge_commit"]), [_review(f"✅ LGTM — landing tree {invented[:12]}")])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 0, "an unresolvable claim is unmeasured, not a divergence"
    assert mod.UNCHECKED in out.out
    assert "cannot resolve to a tree" in out.out
    assert "names a tree" not in out.out, (
        "the body does name a token; saying it names nothing is a false statement about it"
    )


def test_the_same_tree_named_by_a_later_review_still_counts(mod, merged_repo, monkeypatch):
    """Any page of reviews counts, so a claim on the newest one is not missed."""
    fixture = merged_repo
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [
            _review("✅ LGTM — cycle cyc20260925-160939", at="2026-09-25T08:20:54Z"),
            _review(f"✅ LGTM — cycle cyc20260925-174000\n\n{fixture['landed_tree'][:12]}", at="2026-09-25T09:44:58Z"),
        ],
    )
    _install(mod, monkeypatch, fake)

    assert mod.check_pr(1613, "argszero/emrg").reading == mod.NAMED


def test_a_merged_pr_with_no_tree_claim_is_not_a_pass(mod, merged_repo, monkeypatch, capsys):
    """`UNCHECKED` is its own state: nothing was compared, and the report says so."""
    fixture = merged_repo
    fake = FakeGh(_view(fixture["merge_commit"]), [_review("✅ LGTM — cycle cyc20260925-174000")])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 0, "an unchecked merge is not a divergence - it is a question with nothing to answer it"
    assert mod.UNCHECKED in out.out
    assert "not" in out.err and "a pass" in out.err, (
        "an unchecked merge must be said not to be a pass; exiting 0 silently would "
        "read as one"
    )


def test_an_unmerged_pr_is_pending_and_compares_nothing(mod, merged_repo, monkeypatch, capsys):
    """Nothing has landed, so nothing is compared - and the reviews are never read."""
    fixture = merged_repo
    fake = FakeGh(_view(fixture["merge_commit"], state="open", merged=False), [])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 0
    assert mod.PENDING in out.out
    assert "nothing has landed" in out.out
    assert all(not str(c[-1]).endswith("/reviews") for c in fake.calls), (
        "an unmerged PR has no landed tree to compare a claim against, so the reviews "
        "must not be read at all"
    )


def test_a_merge_commit_this_clone_cannot_get_is_unmeasurable(mod, tmp_path, monkeypatch, capsys):
    """rc 2, never `NAMED`: an unanswerable question is reported as one."""
    repo = tmp_path / "empty"
    _init_repo(repo)
    _commit(repo, "a.txt", "a\n", "base")
    _point_at(mod, monkeypatch, repo)
    missing = "9" * 40
    fake = FakeGh(_view(missing), [_review("✅ LGTM — cycle cyc20260925-174000")])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 2
    assert "error:" in out.err
    assert missing[:12] in out.err, "the reason must name the commit it could not obtain"


def test_a_review_without_its_projection_is_unmeasurable(mod, merged_repo, monkeypatch, capsys):
    """The caller-owns-the-filter rule, enforced where it would fail silently.

    A response that lost the projection answers `body`/`submitted_at` under their raw
    names, so `at` reads empty - and a tool that carried on would find no claim in a
    body that has one, reporting `UNCHECKED` for a merge it never read.
    """
    fixture = merged_repo
    fake = FakeGh(_view(fixture["merge_commit"]), [{"body": "irrelevant"}])
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    assert rc == 2
    assert "projection" in out.err


def test_the_json_report_carries_both_trees(mod, merged_repo, monkeypatch, capsys):
    """The machine-readable half: the landed tree and every claimed one."""
    fixture = merged_repo
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [_review(f"✅ LGTM — landing tree {fixture['landed_tree'][:12]}")],
    )
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["reading"] == mod.NAMED
    assert payload[0]["landed_tree"] == fixture["landed_tree"]
    assert payload[0]["claimed_trees"] == [fixture["landed_tree"]]


def test_it_names_the_clone_it_read_before_any_verdict(mod, merged_repo, monkeypatch, capsys):
    """The tree half is local git, so the report says *which* local git answered.

    Every token this tool resolves comes out of this checkout's object store, and the
    reading changes with it: the body in the unresolvable-claim test above reads
    `UNCHECKED` here and `NAMED` in a clone that holds the
    object, at the same exit code and in the same words. So the checkout is said out
    loud, first, before any verdict — the `tree: ` line its module-rooted siblings
    (`check-doc-count.py`, `check_nonlocal.py`) carry, which
    `tests/test_a_tree_reading_guard_names_its_tree.py` requires of them and, until now,
    classified this tool as not needing ("answers about something other than a working
    tree", in the bucket entry that also says it "reads merged trees from local git").

    The fixture is the control: it points `REPO_ROOT` at a `tmp_path` repository, so a
    line hardcoded to this repository, or one reporting the caller's cwd, dies here.
    """
    fixture = merged_repo
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [_review(f"✅ LGTM — landing tree {fixture['landed_tree'][:12]}")],
    )
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613"])

    out = capsys.readouterr()
    lines = out.out.splitlines()
    first = lines[0] if lines else ""
    assert rc == 0
    assert first == f"tree: {fixture['repo']}", (
        f"the first line is {first!r} - a guard that reads a working tree names the one "
        "that answered before it gives a verdict"
    )
    assert str(REPO_ROOT) not in first, (
        "the line must follow the checkout the tool read, not the one this test runs from"
    )


def test_the_json_report_names_the_same_tree_without_breaking_the_document(
    mod, merged_repo, monkeypatch, capsys
):
    """`--json` carries the same fact as a field, because it is still one document.

    The prose line would be a second kind of line in a stream a machine consumer parses,
    so the JSON half states the clone inside the payload instead. Both halves are pinned
    because "names its tree" has to hold wherever the reader is.
    """
    fixture = merged_repo
    fake = FakeGh(
        _view(fixture["merge_commit"]),
        [_review(f"✅ LGTM — landing tree {fixture['landed_tree'][:12]}")],
    )
    _install(mod, monkeypatch, fake)

    rc = mod.main(["1613", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["tree"] == str(fixture["repo"])


#: Run by a subprocess with `stdout` a pipe, so the buffering question is the real one.
#: `_gh` is replaced rather than `gh` on PATH: the property under test is which stream
#: ordering the reader gets, and the network boundary is the one thing that must be
#: stubbed for a test to be hermetic. Nothing below the `_gh` call is reached for an
#: unresolvable number, so the failure is produced before any git work.
_PIPED_DRIVER = '''\
import importlib.util, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_merge_landed_piped", Path(sys.argv[1]))
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _unreachable(args):
    raise RuntimeError(
        "gh failed (rc=1): gh " + " ".join(args) + "\\ngh: Not Found (HTTP 404)"
    )


mod._gh = _unreachable
raise SystemExit(mod.main(["999999"]))
'''

#: The remedy as `main()` carries it, spelled exactly as the fault injection removes it:
#: a copy that failed to lose it would make the negative half below vacuous.
_REMEDY_BLOCK = """\
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

"""


def _piped_first_lines(script: Path, driver: Path) -> list[str]:
    """Run `script` under a pipe, the reading mode the ordering promise is made for."""
    proc = subprocess.run(
        [sys.executable, str(driver), str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout or "").splitlines()


def test_the_tree_line_precedes_a_failure_under_a_pipe(tmp_path):
    """The `tree: ` line must come before the failure *for the text a reader gets*.

    The two conditions in `main()`'s comment - "before the first verdict" and "before the
    first failure" - are what a cycle relies on when it reads this report through
    `2>&1 | cat -n`, and a cycle is the reader this tool was written for. Pinned through
    a pipe because that is the only mode where the two orders differ: measured on this
    branch by `cyc20260926-091529`, and reproduced by `cyc20260926-094103` where
    `PYTHONUNBUFFERED=1` printed the tree line first - the program's own order.

    Both directions, so the assertion is about the remedy and not about something else
    that happens to be true (#455): with the block `main()` carries, the tree line is
    first; with that block removed from a copy of the file, the `error:` line is. A
    remedy-free copy that is really remedy-free is asserted before it is read, or the
    negative half would pass for the wrong reason.
    """
    driver = tmp_path / "piped_driver.py"
    driver.write_text(_PIPED_DRIVER, encoding="utf-8")

    with_remedy = tmp_path / "with-remedy" / "scripts" / "check-merge-landed.py"
    with_remedy.parent.mkdir(parents=True)
    with_remedy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    lines = _piped_first_lines(with_remedy, driver)
    assert lines, "the piped run produced no output at all, so nothing was measured"
    assert lines[0] == f"tree: {with_remedy.parent.parent}", (
        f"the first line a piped reader gets is {lines[0]!r}, not the tree line - a "
        f"verdict still overtakes the line that says which clone answered: {lines!r}"
    )
    assert any(line.startswith("error: ") for line in lines[1:]), (
        f"this test needs a failure *after* the tree line to be about, and none was "
        f"produced: {lines!r}"
    )

    stripped = with_remedy.read_text(encoding="utf-8")
    assert _REMEDY_BLOCK in stripped, (
        "the fault injection no longer matches the remedy in `main()`, so the negative "
        "half below would compare a file that still carries it"
    )
    without = tmp_path / "without-remedy" / "scripts" / "check-merge-landed.py"
    without.parent.mkdir(parents=True)
    without.write_text(stripped.replace(_REMEDY_BLOCK, "", 1), encoding="utf-8")

    control = _piped_first_lines(without, driver)
    assert control and not control[0].startswith("tree: "), (
        f"without the remedy the tree line came first anyway ({control!r}), so this test "
        "is not measuring the remedy"
    )
    assert any(line.startswith("error: ") for line in control), (
        f"the remedy-free control produced no failure to be early: {control!r}"
    )
