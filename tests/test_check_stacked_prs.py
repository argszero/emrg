"""`check-stacked-prs.py`: the reading that reports a PR carrying another open PR's head.

Both directions, because a check is only evidence if it can fail: the carrier is reported
when the carried PR is open, and the same shape is *not* a fault once that PR has landed
(the state #1879 was in after #1877 merged, measured 2026-10-07: the landing changes one
path instead of three).

The fixtures are the API's own shapes, taken from the live endpoints this cycle:
`pulls?state=open` projects `{number, title, head_sha, head_ref, base_ref}`, and
`pulls/<N>/commits` projects `{sha, subject}` with the subject taken to the end of its
first line. The commit list of a real stacked PR is what makes the positive arm real:
measured on #1879, `pulls/1879/commits` returns four commits whose first is #1877's head
`b1e50447` and whose last is the PR's own.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-stacked-prs.py"

#: A stacked pair, as the API reported it for #1879 and #1877 (recorded 2026-10-07).
OLD_HEAD = "b1e5044726e3d5fd6a4a5d0e2e1d3a4b5c6d7e8f"
OWN_HEAD = "c8b5bb5c35c70ec75a0ede53654de290e6623541"
OTHER_PR = 1877
CARRIER = 1879


def _load_module():
    spec = importlib.util.spec_from_file_location("check_stacked_prs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolves annotations through
    # `sys.modules[cls.__module__]` at class-creation time, and an unregistered module
    # raises inside dataclasses itself with a message naming neither this file nor why.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeGh:
    """A stand-in for the module's two gh readers, answering from a routing table.

    Routes on the *endpoint* rather than on the jq filter: the filter is the caller's and
    the point of the arrangement is that a projection which did not apply comes back
    looking like a subject with no rows, so the fake has to be able to answer both.
    """

    def __init__(self, pulls, commits, statuses=None, break_on=""):
        self.pulls = pulls
        self.commits = commits
        self.statuses = statuses or {}
        self.break_on = break_on
        self.calls: list[list[str]] = []
        #: The module's own "could not measure" type, filled in by `_install`: the fake has
        #: to raise what the tool catches, or it would prove only that *a* RuntimeError is
        #: caught. `MeasurementError` derives from RuntimeError, so the wrong class is the
        #: one a reader would not notice.
        self.error_cls: type[Exception] = RuntimeError

    def _route(self, args: list[str]) -> object:
        self.calls.append(list(args))
        endpoint = next((a for a in args if a.startswith("repos/")), "")
        if self.break_on and self.break_on in endpoint:
            raise self.error_cls(f"gh failed (rc=1): {endpoint}")
        if "/pulls?" in endpoint:
            return list(self.pulls)
        if endpoint.endswith("/commits?per_page=100"):
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            return list(self.commits.get(number, []))
        if "/compare/" in endpoint:
            sha = endpoint.split("...")[1]
            return {"status": self.statuses.get(sha, "ahead")}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def __call__(self, args: list[str]) -> object:
        return self._route(args)

    def lines(self, args: list[str]) -> list[dict]:
        answer = self._route(args)
        assert isinstance(answer, list)
        return answer


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    fake.error_cls = mod.MeasurementError
    monkeypatch.setattr(mod, "_gh_json", lambda args: fake(args))
    monkeypatch.setattr(mod, "_gh_lines", lambda args: fake.lines(args))


def _pull(number: int, head: str, title: str = "t") -> dict:
    return {
        "number": number,
        "title": title,
        "head_sha": head,
        "head_ref": f"fix/pr-{number}",
        "base_ref": "master",
    }


def _commit(sha: str, subject: str) -> dict:
    return {"sha": sha, "subject": subject}


def _run(mod, monkeypatch, fake: FakeGh, argv: list[str] | None = None) -> int:
    _install(mod, monkeypatch, fake)
    return mod.main(argv if argv is not None else ["--repo", "argszero/emrg"])


def test_a_carrier_is_reported_when_the_other_pr_is_open(mod, monkeypatch, capsys):
    """The defect this tool was written for, on the payload #1879 really returned.

    #1879's commit list begins with #1877's head commit, because the branch was cut from
    that PR's branch. While #1877 is open, merging #1879 lands #1876's fix under a PR that
    declares only #1878 - which is what the reviewer vetoed it for, by hand.
    """
    fake = FakeGh(
        pulls=[_pull(CARRIER, OWN_HEAD), _pull(OTHER_PR, OLD_HEAD)],
        commits={
            CARRIER: [
                _commit(OLD_HEAD, "emrg: grep counts only the files it really read"),
                _commit(OWN_HEAD, "emrg: the empty tree leg holds every guard"),
            ],
            OTHER_PR: [_commit(OLD_HEAD, "emrg: grep counts only the files it really read")],
        },
        statuses={OLD_HEAD: "diverged"},
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 1, "a PR that would land another open PR's work is a fault, not a pass"
    assert f"#{CARRIER} stacked" in cap.out
    assert f"carries #{OTHER_PR} head {OLD_HEAD[:8]}" in cap.out
    assert f"#{OTHER_PR} ok" in cap.out, (
        "the carried PR is not the fault - it holds its own work; the carrier is"
    )
    assert "would land another open PR's work" in cap.err
    # The remedy has to be runnable as printed: a bare `scripts/x.py` is mode 644 here.
    assert mod.RUNNER in cap.err
    assert f"{mod.RUNNER} scripts/check-merge-landing-diff.py {CARRIER}" in cap.err


def test_a_queue_with_no_stack_is_clean(mod, monkeypatch, capsys):
    """Two PRs cut from the same master share no head commits, and that is the clean run."""
    fake = FakeGh(
        pulls=[_pull(1, "a" * 40), _pull(2, "b" * 40)],
        commits={1: [_commit("a" * 40, "one")], 2: [_commit("b" * 40, "two")]},
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 0
    assert "OK: 2 open PR(s), none carries another open PR's head commit" in cap.out
    assert "#1 ok" in cap.out and "#2 ok" in cap.out


def test_a_pr_does_not_report_itself(mod, monkeypatch, capsys):
    """`pulls/<N>/commits` always contains the PR's own head - the self-skip is load-bearing.

    Without it every PR in every queue would be reported as carrying a head commit, and
    the reading would be noise that fires on everything.
    """
    fake = FakeGh(
        pulls=[_pull(1, "a" * 40)],
        commits={1: [_commit("a" * 40, "one"), _commit("a" * 40, "one again")]},
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 0
    assert "stacked" not in cap.out


def test_a_carried_head_whose_pr_is_closed_is_not_a_fault(mod, monkeypatch, capsys):
    """The state #1879 was in after #1877 landed - and it is why the condition is the queue.

    The carried commit is still in the commit list (squash landed it, so the SHA is not on
    master: `compare/master...b1e50447` answers `diverged, ahead_by 1`, measured 2026-10-07),
    and the landing then changes one path rather than three. Nothing in the graph can say
    that; the *open set* can, and this arm pins that the graph is not asked to.
    """
    fake = FakeGh(
        pulls=[_pull(CARRIER, OWN_HEAD)],
        commits={CARRIER: [_commit(OLD_HEAD, "emrg: grep counts only the files it really read")]},
        statuses={OLD_HEAD: "diverged"},
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 0, (
        "the carried PR is closed, so this PR lands only its own work - reporting the "
        "diverged SHA as a fault is the 'the commit is not an ancestor, so the work is "
        "unlanded' inference the squash landing falsifies"
    )
    assert "OK:" in cap.out
    assert not [c for c in fake.calls if "/compare/" in " ".join(c)], (
        "no compare call is owed when no open PR owns the carried commit"
    )


def test_a_carried_head_already_on_master_is_reported_and_not_counted(mod, monkeypatch, capsys):
    """The one answer the compare endpoint can prove: `behind` means the commit is master's ancestor.

    That commit's work is on master, so landing it again is not what this tool is about -
    but the row says so rather than dropping the finding, because a row that silently lost
    a finding reads exactly like one that never had it.
    """
    fake = FakeGh(
        pulls=[_pull(CARRIER, OWN_HEAD), _pull(OTHER_PR, OLD_HEAD)],
        commits={CARRIER: [_commit(OLD_HEAD, "emrg: already landed")]},
        statuses={OLD_HEAD: "behind"},
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 0
    assert "already on master" in cap.out
    assert f"#{OTHER_PR} head {OLD_HEAD[:8]}" in cap.out, (
        "the fact is still reported - only the fault is withheld"
    )
    assert "stacked" not in cap.out


def test_a_broken_lookup_is_not_a_clean_queue(mod, monkeypatch, capsys):
    """An unreadable queue is not an empty one: rc 2, and no OK line."""
    fake = FakeGh(pulls=[], commits={}, break_on="/pulls?")
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 2
    assert "cannot determine the stacked PRs" in cap.err
    assert "OK:" not in cap.out, "a read that failed may not print a clean verdict"


def test_a_compare_that_cannot_be_read_keeps_the_fault(mod, monkeypatch, capsys):
    """A narrowing that failed is not a reading that failed - and the two failures are told apart.

    The compare endpoint is asked for one thing only: to let a *provably landed* head
    through (`LANDED_STATES`). Its answer can therefore suppress a finding and never create
    one, so its silence leaves the fault standing - which is what the code already does when
    the state is simply not in `LANDED_STATES`.

    Both directions here, because the signal that has to discriminate is *which* call
    failed. Measured 2026-10-07 on the code this arm was written for: with the compare broken
    and the queue readable, the tool answered `cannot determine the stacked PRs` (rc 2) and
    printed no row at all - an unreadable queue for a queue that had answered, withholding a
    finding it had already taken. The second half is the control: with the queue broken the
    answer is still rc 2, so the two arms are not both reporting "unread".
    """
    fake = FakeGh(
        pulls=[_pull(CARRIER, OWN_HEAD), _pull(OTHER_PR, OLD_HEAD)],
        commits={CARRIER: [_commit(OLD_HEAD, "emrg: the carried work")]},
        break_on="/compare/",
    )
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 1, "the carrying queue was read; a compare failure may not unmake that"
    assert f"#{CARRIER} stacked" in cap.out
    assert f"carries #{OTHER_PR} head {OLD_HEAD[:8]}" in cap.out
    assert "compare unread" in cap.out, "the row says which half it could not read"
    assert "compare state read: not read" in cap.err, (
        "the remedy repeats the state it was measured from, so a reader can weigh the finding"
    )
    assert "cannot determine the stacked PRs" not in cap.err, (
        "answering 'could not measure' withholds a finding the tool had already taken"
    )

    # The control: the *queue* failing is still the unmeasurable answer.
    fake = FakeGh(pulls=[], commits={}, break_on="/pulls?")
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 2, "a queue that was never read answers 2, never a finding"
    assert "cannot determine the stacked PRs" in cap.err


def test_the_open_pr_projection_is_read_not_assumed(mod, monkeypatch, capsys):
    """A projection that did not apply comes back with no `head_sha`, and every PR then looks clean.

    That is the shape `check-vote-count.py`'s caller-owned-filter rule was written for: the
    tool cannot tell "no carriers" from "the field I read was never asked for", so it refuses
    to answer instead of reporting a clean queue.
    """
    fake = FakeGh(pulls=[{"number": 5, "title": "t"}], commits={})
    rc = _run(mod, monkeypatch, fake)
    cap = capsys.readouterr()
    assert rc == 2
    assert "projection did not apply" in cap.err
    assert "OK:" not in cap.out


def test_a_crash_answers_could_not_measure(mod, monkeypatch, capsys):
    """A crash may not leave as this tool's `1`, because `1` means "there is a stacked PR".

    Measured while arming this file (2026-10-07): with the projection check disabled, a row
    without `head_sha` reaches `judge` and raises `KeyError`. Without the wrapper that is an
    unhandled exception, Python exits `1`, and a caller reading the code reads "a PR carries
    another one's work" about a queue the tool never read. Both halves are asserted: the
    code, and the line that names the cause.
    """

    def boom(*args, **kwargs):
        raise KeyError("head_sha")

    monkeypatch.setattr(mod, "main", boom)
    rc = mod._entry()
    cap = capsys.readouterr()
    assert rc == 2, "a crash is the unmeasurable code, never the code that means a finding"
    assert "check-stacked-prs.py: could not measure - KeyError: 'head_sha'" in cap.err, (
        "the summary line has to name the tool and the cause, or a reader sees only a "
        "traceback and cannot tell which reading failed"
    )


def test_the_entry_point_returns_what_main_answered(mod, monkeypatch):
    """The other direction: a verdict is passed through, not turned into an unmeasurable."""
    monkeypatch.setattr(mod, "main", lambda *a, **k: 1)
    assert mod._entry() == 1, (
        "a wrapper that swallowed the verdict would make every fault unreadable"
    )
    monkeypatch.setattr(mod, "main", lambda *a, **k: 0)
    assert mod._entry() == 0


def test_a_commit_list_is_read_past_the_first_page(mod, monkeypatch):
    """A PR with more than one page of commits must not lose the page the carried head is on.

    `--paginate` with a per-line filter emits one JSON object per line across pages, so the
    reader is exercised at the shape gh really produces: two pages' worth of lines, parsed
    as one list, with nothing silently dropped at the page boundary.
    """
    pages = "\n".join(
        json.dumps({"sha": f"{i:040d}", "subject": f"commit {i}"}) for i in range(150)
    )
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert "--paginate" in argv, "the pagination flag is what makes the read complete"
        return type("P", (), {"returncode": 0, "stdout": pages, "stderr": ""})()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rows = mod._gh_lines(["api", "repos/x/y/pulls/1/commits?per_page=100", "--jq", ".[] | {}"])
    assert len(rows) == 150, "every line of every page is one commit, and none is dropped"
    assert rows[0]["sha"] == f"{0:040d}" and rows[-1]["sha"] == f"{149:040d}"
    assert calls and calls[0][-1] == "--paginate"
