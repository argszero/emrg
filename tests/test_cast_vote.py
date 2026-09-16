"""Tests for scripts/cast-vote.py - casting a vote the counter will actually count.

Background (cycle cyc20260916-020149)
-------------------------------------
`check-merge-freshness.py` correctly tells a reader with votes at risk to record
the landing-tree reading as a *review*, since reviews are the only channel
`check-vote-count.py` reads. On 2026-09-16 that advice was followed to the letter
and the vote was still lost:

    gh pr review 1255 --comment --body-file review1255.md   # rc 0, prints nothing
    scripts/check-vote-count.py 1255
    #   18:19:15Z VOID (no cycle id) - no cycle id in the vote body

The counter reads the voting cycle out of the body, so a body without one is a
vote nobody cast. Both halves of that loss are pinned here, because they are the
two properties that are invisible at the moment of posting:

* **attribution** - the body must carry exactly one cycle id. None is void; two
  is worse (the counter takes the first match, so the owner becomes an accident
  of prose order). `--cycle` may check that reading but cannot replace it, since
  the counter never sees the flag.
* **counting** - a posted review can still fail to count (a second vote from a
  cycle already in the run contributes nothing), and only the counter knows.

So the assertions come in both directions, never inferred from the failure case
alone (#455): every refusal is asserted to have posted *nothing*, and every
success is asserted to have actually reached `gh` and then read the counter back
(rather than reporting agreement with itself).

Nothing here touches the network: `_gh` and the sibling counter are both
replaced, and the replacements are asserted to receive the arguments the real
ones would, so no test can pass by never calling them.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cast-vote.py"
COUNTER_SCRIPT = REPO_ROOT / "scripts" / "check-vote-count.py"

HEAD = "a" * 40
CYCLE = "cyc20260916-020149"
OTHER_CYCLE = "cyc20260915-235900"
ID_REVIEW = "emrg: a flag with an attached value is the same flag (#1256)"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the sibling declares a dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation
    # time. An unregistered module raises AttributeError inside dataclasses
    # itself - an error that names neither this test nor the cause.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load(SCRIPT, "cast_vote")


@pytest.fixture
def counter_mod():
    return _load(COUNTER_SCRIPT, "check_vote_count_for_cast")


# ── the counter's payload, faked at its own interface ──────────────────────


@dataclass
class Vote:
    at: str
    kind: str
    cycle: str | None
    valid: bool
    why: str = ""


@dataclass
class Verdict:
    pr: int
    head_sha: str = HEAD
    valid_count: int = 0
    needed: int = 3
    votes: list[Vote] = field(default_factory=list)
    counted: list[bool] = field(default_factory=list)


def vote(cycle=CYCLE, *, kind="approve", counted=True, why="", at="2026-09-15T18:19:36Z"):
    return Vote(at=at, kind=kind, cycle=cycle, valid=counted, why=why)


def verdict_with(votes=(), counted=None, valid_count=0, pr=1):
    counted = [v.valid for v in votes] if counted is None else list(counted)
    return Verdict(pr=pr, votes=list(votes), counted=counted, valid_count=valid_count)


class FakeCounter:
    """Scripted `check_pr` readings, recording every call.

    The last verdict repeats, so a test can script "the first read sees nothing,
    the second sees the vote" without padding the list - and `calls` is asserted
    in every test, so a fake that is never called cannot pass for one that was.

    Keyword arguments are recorded too (the real counter takes
    `mergeability_wait`), for the same reason: the fake must be able to receive
    everything the real one would, or a call site that stopped passing a budget
    would pass by being invisible to this fake.
    """

    def __init__(self, *verdicts: Verdict):
        assert verdicts, "a FakeCounter needs at least one reading"
        self._verdicts = list(verdicts)
        self.calls: list[tuple[int, int]] = []
        self.kwargs: list[dict] = []

    def check_pr(self, pr: int, needed: int, **kwargs) -> Verdict:
        self.calls.append((pr, needed))
        self.kwargs.append(dict(kwargs))
        if len(self._verdicts) > 1:
            return self._verdicts.pop(0)
        return self._verdicts[0]


class FakeGh:
    """A stand-in for `_gh`: records the argv, answers with a fixed rc."""

    def __init__(self, rc: int = 0, stderr: str = ""):
        self.rc = rc
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        return _Proc(self.rc, self.stderr)


@dataclass
class _Proc:
    returncode: int
    stderr: str


@pytest.fixture
def body_file(tmp_path):
    def write(text: str) -> str:
        p = tmp_path / "vote.md"
        p.write_text(text, encoding="utf-8")
        return str(p)

    return write


def _run(mod, monkeypatch, counter: FakeCounter, gh: FakeGh, argv: list[str]):
    monkeypatch.setattr(mod, "votes_counter", lambda: counter)
    monkeypatch.setattr(mod, "_gh", gh)
    return mod.main(argv)


# ── attribution: a body the counter cannot read is never sent ──────────────


def test_a_body_without_a_cycle_id_is_refused_before_anything_is_posted(
    mod, monkeypatch, capsys, body_file
):
    """The measured loss: rc 0 from `gh`, and the count does not move.

    The refusal has to happen *before* the network call. A tool that posted and
    then reported the void vote would still have spent it - the counter excludes
    the review and nothing rolls that back.
    """
    counter = FakeCounter(verdict_with())
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file("✅ LGTM - no cycle id anywhere in this body")],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == [], "a body with no cycle id must reach no network call"
    assert counter.calls == [], "the count is not worth reading for a refused post"
    assert "no cycle id" in err
    # The message has to name why it is silent on the posting side, because that
    # is the part a reader cannot see: `gh pr review` prints nothing on success.
    assert "prints nothing" in err


def test_the_cycle_id_is_read_from_the_body_not_guessed(mod, monkeypatch, capsys, body_file):
    """The body is the authority; the vote is cast under the cycle it names."""
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote()], counted=[True], valid_count=2),
    )
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — ✅ LGTM on the landing tree")],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert len(gh.calls) == 1
    assert gh.calls[0][:3] == ["pr", "review", "1255"]
    assert "--comment" in gh.calls[0]
    assert out.startswith("#1255: review posted as cyc20260916-020149")
    assert "counted" in out and "2/3 valid votes" in out


def test_a_cycle_flag_that_disagrees_with_the_body_is_refused(mod, monkeypatch, capsys, body_file):
    """The flag checks the reading; it cannot speak for the body.

    `check-vote-count.py` never sees `--cycle`, so a flag the body contradicts
    would post a review attributed to whichever id the prose happens to contain -
    a vote under a cycle the operator did not choose.
    """
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        FakeCounter(verdict_with()),
        gh,
        [
            "1255",
            "--body-file",
            body_file(f"{CYCLE} — LGTM"),
            "--cycle",
            OTHER_CYCLE,
        ],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == []
    assert OTHER_CYCLE in err and CYCLE in err


def test_a_malformed_cycle_flag_is_refused(mod, monkeypatch, capsys, body_file):
    """A typo in `--cycle` would refuse every body containing the real id."""
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        FakeCounter(verdict_with()),
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — LGTM"), "--cycle", "cyc2026-09-16"],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == []
    assert "not a cycle id" in err


def test_two_cycle_ids_in_one_body_are_refused(mod, monkeypatch, capsys, body_file):
    """Two ids make the vote's owner an accident of prose order.

    A body that quotes another cycle (as a review does when it explains whose
    earlier vote it is superseding) reads as that cycle's vote if the id is
    quoted first. Better to refuse than to cast a vote under a cycle that did not
    write it.
    """
    gh = FakeGh()
    body = f"{OTHER_CYCLE} voted earlier; {CYCLE} — ✅ LGTM"
    rc = _run(mod, monkeypatch, FakeCounter(verdict_with()), gh, ["1255", "--body-file", body_file(body)])
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == []
    assert "more than one cycle id" in err
    assert OTHER_CYCLE in err and CYCLE in err


# ── counting: posted is not the same as counted ────────────────────────────


def test_a_posted_review_the_counter_does_not_count_exits_1(mod, monkeypatch, capsys, body_file):
    """The one state the caller cannot detect alone: rc 0 from gh, no vote.

    Exit 1 rather than 0, because "posted" is what every signal at the call site
    says, and the counter's reason is printed: a repeat vote and a pre-push review
    are different situations with different remedies. A *veto* is not one of them —
    a counted veto is registered, so it exits 0.
    """
    void = vote(counted=False, why="submitted before the head push (2026-09-15T18:13:26Z)")
    counter = FakeCounter(verdict_with(), verdict_with([void], counted=[False], valid_count=1))
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")])
    err = capsys.readouterr().err
    assert rc == 1
    assert len(gh.calls) == 1, "it was posted - that is exactly the problem"
    assert "POSTED and NOT counted" in err
    assert "submitted before the head push" in err


def test_a_counted_veto_is_reported_as_registered_not_as_spent(
    mod, monkeypatch, capsys, body_file
):
    """A veto counts as a verdict that resets the run, not as a vote for the PR.

    The counter reads a counted veto as `NO ... counts - resets the run`, so
    `_state_of` answers `"veto"` with `counted=True`. `confirm` used to
    short-circuit only on `"counted"` and `"void"`, so a veto that had registered
    fell through the retry loop and came out as a review that never appeared —
    exit 1 with "the vote was spent for nothing" for a vote that was on the record
    (measured 2026-09-17, `cyc20260917-043948`, on #1303 and #1305).

    The read count is asserted too: a definite answer must return on the first
    confirm read, not after the attempts are exhausted.
    """
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote(kind="veto", counted=True)], counted=[True], valid_count=0),
    )
    gh = FakeGh()
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        [
            "1255",
            "--body-file",
            body_file(f"{CYCLE} — ❌ needs fix"),
            "--attempts",
            "3",
            "--delay",
            "0",
        ],
    )
    out, err = capsys.readouterr()
    assert rc == 0, "the veto is on the record - nothing was lost"
    assert "VETO" in out
    assert "resets the run" in out
    assert "spent for nothing" not in err
    assert len(counter.calls) == 2, "1 pre-flight + 1 confirm read, then it returns"
    assert len(gh.calls) == 1, "the review was posted exactly once"


def test_a_failed_post_exits_2_and_never_reads_the_counter(mod, monkeypatch, capsys, body_file):
    """A `gh` failure is not a vote, so it must not be confirmed as one.

    Reading the counter here would report the *pre-existing* count and could
    print a cheerful "counted" for a review that was never sent.
    """
    counter = FakeCounter(verdict_with([vote(cycle=OTHER_CYCLE)], counted=[True], valid_count=1))
    gh = FakeGh(rc=1, stderr="gh: could not create review")
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")])
    err = capsys.readouterr().err
    assert rc == 2
    assert len(gh.calls) == 1
    assert counter.calls == [(1255, 3)], "no confirm read after a failed POST"
    assert "could not create review" in err


def test_a_cycle_that_already_counted_a_vote_here_is_refused(mod, monkeypatch, capsys, body_file):
    """Counting is per cycle, so a second vote contributes nothing - do not send it."""
    counter = FakeCounter(verdict_with([vote()], counted=[True], valid_count=2))
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")])
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == []
    assert "already has a counted vote" in err


def test_a_veto_from_this_cycle_is_refused(mod, monkeypatch, capsys, body_file):
    """A veto is this cycle's decision already made; a later LGTM would be a third state.

    The counter is the only place that knows a veto is a veto (it is `valid`), so
    the refusal is read from its `kind`, not inferred from the body.
    """
    veto = Vote(at="2026-09-15T18:00:00Z", kind="veto", cycle=CYCLE, valid=True)
    counter = FakeCounter(verdict_with([veto], counted=[True], valid_count=0))
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")])
    err = capsys.readouterr().err
    assert rc == 2
    assert gh.calls == []
    assert "already vetoed" in err


def test_a_void_earlier_review_does_not_block_the_current_vote(mod, monkeypatch, capsys, body_file):
    """A void earlier review is noted, not treated as a reason to refuse.

    This is the state this tool's own defect produced: the first review had no
    cycle id, the counter excluded it, and the cycle is still entitled to its one
    counted vote. Refusing here would make the defect permanent.
    """
    earlier = vote(counted=False, why="no cycle id in the vote body")
    counter = FakeCounter(
        verdict_with([earlier], counted=[False], valid_count=1),
        verdict_with([earlier, vote()], counted=[False, True], valid_count=2),
    )
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")])
    captured = capsys.readouterr()
    assert rc == 0
    assert len(gh.calls) == 1
    assert "no cycle id in the vote body" in captured.err, "the note says what the void one was"
    assert "counted" in captured.out


def test_the_settle_retry_re_reads_until_the_review_appears(mod, monkeypatch, capsys, body_file):
    """GitHub registers a review a moment after the POST returns.

    A single read can therefore miss it and report "never appeared" for a review
    that did arrive - the mutation that removes the retry is caught by asserting
    the number of reads, not just the exit code.
    """
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote()], counted=[True], valid_count=2),
    )
    gh = FakeGh()
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — LGTM"), "--attempts", "2", "--delay", "0"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert counter.calls == [(1255, 3), (1255, 3)], "one pre-flight read, one confirm read"
    assert "2/3 valid votes" in out


def test_a_review_that_never_appears_is_reported_not_guessed(mod, monkeypatch, capsys, body_file):
    """Bounded retries, then a loud "unmeasurable" - never a silent 0, and never "spent".

    The absence is a *different state* from a void vote, and only its wording separates
    them: both exit 1, and `confirm`'s note is the same either way, so the printed verdict
    is the only thing telling a reader "do not re-post, re-read" apart from "the vote was
    spent for nothing". Measured 2026-09-17 by an outside reviewer: replacing this branch's
    message with the void branch's left `tests/test_cast_vote.py` at 18 passed, i.e. the
    separation had no assertion behind it. Both halves are asserted here — the word that
    means unmeasurable, and the verdict it must not be confused with.
    """
    counter = FakeCounter(verdict_with())
    gh = FakeGh()
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — LGTM"), "--attempts", "3", "--delay", "0"],
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert len(counter.calls) == 4, "1 pre-flight + 3 confirm attempts"
    assert "never appeared" in err
    assert "unmeasurable" in err, (
        "the absence has to be reported as unmeasurable, not as a verdict - the review is "
        "on GitHub and cannot be un-posted"
    )
    assert "spent for nothing" not in err, (
        "this is the `void` branch's verdict, and the `none` state is not a spent vote: "
        "re-collapsing the two leaves a reader re-posting instead of re-reading"
    )


def test_dry_run_posts_nothing(mod, monkeypatch, capsys, body_file):
    """Every check, no POST - and it must not claim a vote was cast."""
    counter = FakeCounter(verdict_with())
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — LGTM"), "--dry-run"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert gh.calls == []
    assert len(counter.calls) == 1, "the count is still read - that is the check being made"
    assert "dry run" in out and "counted" not in out


def test_an_unreadable_body_file_exits_2_and_posts_nothing(mod, monkeypatch, capsys, tmp_path):
    """The first `rc 2` the table did not name (issue #1309): the body cannot be read.

    A mistyped or deleted `--body-file` is the most ordinary way to run this tool wrong, and
    the exit code is the only thing a wrapper sees. It must be the documented `2` — nothing
    was posted — and not `1`, which means a vote *was* spent.
    """
    missing = tmp_path / "no-such-body.md"
    counter = FakeCounter(verdict_with())
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", str(missing)])
    err = capsys.readouterr().err
    assert rc == 2, "an unreadable body posts nothing, so it is the 'nothing was posted' code"
    assert not missing.exists()
    assert gh.calls == [], "no review may be attempted without a body"
    assert counter.calls == [], "and the count is not even asked"
    assert "could not read" in err, (
        "the refusal has to say what it could not read, or the caller cannot tell this "
        "apart from the other rc 2 refusals"
    )


def test_an_unreadable_vote_count_exits_2_and_posts_nothing(mod, monkeypatch, capsys, body_file):
    """The second `rc 2` the table did not name (issue #1309): the counter raised.

    `check-vote-count.py` fails loud on an unmeasurable reading, and this tool runs *before*
    posting, so a counter that cannot be read must stop the vote rather than spend it. The
    distinction matters: `1` would tell a wrapper the vote went out and did not count.
    """

    class RaisingCounter:
        def __init__(self):
            self.calls = 0

        def check_pr(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("the counter could not reach GitHub")

    counter = RaisingCounter()
    gh = FakeGh()
    rc = _run(
        mod,
        monkeypatch,
        counter,
        gh,
        ["1255", "--body-file", body_file(f"{CYCLE} — LGTM")],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert counter.calls == 1
    assert gh.calls == [], "the vote is not spent on a count the tool could not read"
    assert "vote count could not be read" in err
    assert "the counter could not reach GitHub" in err, (
        "the counter's own reason is carried through: the remedy depends on it"
    )


def test_the_exit_code_table_names_every_rc_2_cause_the_module_can_reach(mod):
    """The table is a contract callers script against, so it is joined to the code.

    Issue #1309: the table enumerated four causes while the module returned `2` from five
    sites, and the two it omitted — an unreadable `--body-file`, and a vote count that could
    not be read — were the ones a caller is most likely to hit.

    The first cut of this test read the *count* of refusals out of the source and then
    checked a hand-written tuple of five phrases against the table. That closes #1309 and
    nothing after it: a sixth refusal, with a message family the table does not name, left
    this test green while the table went stale again — the same defect, one release later.
    Measured, not argued: a spoken sixth `return 2` sitting next to the five kept this test
    passing, so the claim in the docstring ("a new refusal path cannot be added without the
    table being updated") was false as implemented.

    The join therefore runs in both directions between three copies, none of which is a
    list the other two are trusted against:

    * every `return 2` must have a `print(..., file=sys.stderr)` ahead of it in the same
      block — a refusal that says nothing is unusable, and this asserts it mechanically;
    * every `return 2` must declare `# cause: <slug>` on the return itself, so a refusal
      cannot be added without stating which cause it is;
    * every declared slug must be in `RC2_CAUSES`; every slug in `RC2_CAUSES` must be
      reached by some `return 2` (a cause the code cannot produce is a promise the tool
      does not keep); and every slug must be named in the table's rc 2 entry, which is
      what a caller reads.

    Why a declared slug, and not a family derived from the message: several returns are one
    family reached from one site (no cycle id, two cycle ids, and a `--cycle` that
    disagrees are all the `cycle-id` cause), and two of the five carry a message that is
    *entirely* a variable, so no reading of the messages alone can name the family.
    """
    import ast
    import re

    # A cause slug: lower case, at least one hyphen. The hyphen is what separates a slug
    # from the other backticked tokens the entry carries (`gh`, `--body-file`, `--cycle`),
    # so it is not decoration — it is the parse.
    slug_in_table = re.compile(r"`([a-z]+(?:-[a-z]+)+)`")

    def rc2_entry(text: str) -> str:
        """The `2` entry of the exit-code table — its own lines, not the whole table.

        Scoped deliberately: the rest of the docstring backticks hyphenated things that are
        not causes (`check-vote-count.py`), and reading those as slugs would fail this test
        for a reason that has nothing to do with the exit codes.
        """
        lines = text.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if re.match(r"^\s+2\s\s", line)), None
        )
        assert start is not None, "the exit-code table must document the rc 2 code"
        entry = [lines[start]]
        for line in lines[start + 1:]:
            if not line.strip() or re.match(r"^\s+\d\s\s", line):
                break
            entry.append(line)
        return "\n".join(entry)

    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    section = (mod.__doc__ or "").split("Exit codes")[1]
    assert section, "the exit-code table is the contract this test measures"
    table = rc2_entry(section)
    causes = tuple(getattr(mod, "RC2_CAUSES", ()))
    assert causes, "the module enumerates its rc 2 causes so the table can be joined to them"
    assert all(slug_in_table.fullmatch(f"`{cause}`") for cause in causes), (
        f"every cause slug must be readable by the table's own parse, so the two lists can "
        f"be compared: {causes}"
    )

    def blocks(node: ast.AST):
        """Every statement block under `node`, each yielded exactly once.

        Driven by the fields rather than by a hand-written list of attributes: `body` is
        also how a `Lambda`/`IfExp` spells its *expression*, and an `except` handler is
        reachable both from `handlers` and as a child — hand-written traversal yielded
        those blocks twice, which is how the first version of this test reported the same
        refusal as both silent and spoken.
        """
        for _field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield value
        for child in ast.iter_child_nodes(node):
            yield from blocks(child)

    def stderr_message(stmt: ast.stmt) -> str | None:
        """The refusal text of a `print(..., file=sys.stderr)`, else None."""
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            return None
        call = stmt.value
        if getattr(call.func, "id", "") != "print":
            return None
        if not any(
            kw.arg == "file" and ast.unparse(kw.value) == "sys.stderr"
            for kw in call.keywords
        ):
            return None
        literal = call.args[0] if call.args else None
        parts = getattr(literal, "values", [literal])
        return "".join(
            part.value for part in parts
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )

    def declared_cause(line: str) -> str | None:
        """The `# cause: <slug>` a `return 2` carries, or None when it declares none."""
        match = re.search(r"#\s*cause:\s*([a-z][a-z0-9-]*)", line)
        return match.group(1) if match else None

    lines = source.splitlines()
    refusals: list[tuple[int, str, str | None]] = []
    for block in blocks(tree):
        for index, stmt in enumerate(block):
            if (
                isinstance(stmt, ast.Return)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value == 2
            ):
                message = stderr_message(block[index - 1]) if index else None
                refusals.append(
                    (stmt.lineno, message or "", declared_cause(lines[stmt.lineno - 1]))
                )

    silent = [line for line, message, _cause in refusals if not message]
    assert not silent, (
        "a `return 2` must be preceded by the print that says why — a refusal with no message "
        f"is unusable to the caller. Silent refusals at: {silent}"
    )

    undeclared = [line for line, _message, cause in refusals if cause is None]
    assert not undeclared, (
        "every `return 2` must declare `# cause: <slug>` on the return itself. Without that "
        "declaration a new refusal path can be added while the table silently stays "
        f"incomplete — issue #1309 again, one release later. Undeclared at: {undeclared}"
    )

    declared = {cause for _line, _message, cause in refusals}
    assert declared <= set(causes), (
        f"a refusal declares the cause {sorted(declared - set(causes))}, which `RC2_CAUSES` "
        "does not enumerate — the enumeration is what the table is checked against, so an "
        "unlisted slug would be named nowhere a caller can read"
    )
    assert declared == set(causes), (
        f"`RC2_CAUSES` enumerates {sorted(set(causes) - declared)}, which no `return 2` "
        "reaches: a cause the table offers and the code cannot produce is a promise the tool "
        "does not keep"
    )

    listed = set(slug_in_table.findall(table))
    assert listed == set(causes), (
        "the rc 2 entry and `RC2_CAUSES` are two copies of one list, joined in both "
        f"directions: the table names {sorted(listed)}, the module enumerates "
        f"{sorted(causes)}. A caller scripting on the code reads the table as the contract "
        "(issue #1309)"
    )


def test_the_body_is_sent_byte_for_byte(mod, monkeypatch, capsys, body_file):
    """The body is the caller's reading; the tool does not rewrite the vote."""
    body = f"{CYCLE} — ✅ LGTM\n\n| a | b |\n|---|---|\n| x | y |\n"
    path = body_file(body)
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote()], counted=[True], valid_count=1),
    )
    gh = FakeGh()
    rc = _run(mod, monkeypatch, counter, gh, ["1255", "--body-file", path])
    assert rc == 0
    assert Path(path).read_text(encoding="utf-8") == body
    assert gh.calls[0][-1] == path, "the file is passed to gh, not its text"


# ── the two regexes must agree: this tool refuses what the counter reads ────


def test_the_cycle_pattern_agrees_with_the_counter(mod, counter_mod):
    """A helper that accepted a different shape would post void-by-construction bodies.

    Pinned against the sibling's own pattern rather than a copy of the expected
    string: the requirement is *agreement*, and a test that hard-coded the shape
    twice would keep passing after one side changed.
    """
    assert mod._CYCLE_RE.pattern == counter_mod._CYCLE_RE.pattern
    samples = [
        CYCLE,
        f"vote from {CYCLE}",
        "no id at all",
        "cyc2026-09-16",
        "cyc20260916-02014",
        OTHER_CYCLE,
    ]
    for sample in samples:
        assert bool(mod.cycles_in(sample)) == bool(counter_mod._CYCLE_RE.search(sample)), sample


# ── the transient that used to abort a vote ────────────────────────────────


def test_the_mergeability_budget_reaches_the_counter_at_both_reads(
    mod, monkeypatch, capsys, body_file
):
    """Both reads hand the counter the same budget, and both are recorded.

    The refusal this removes was measured (`cyc20260916-074105`): GitHub reports
    mergeability as `UNKNOWN` for a minute or two after a push, the counter
    refuses it - correctly, it will not guess - and this tool then posted nothing.
    The voter's remedy was to sleep and re-run by hand, which is the work a
    bounded re-ask exists to do. Two reads matter: the pre-flight count (which
    decides whether to post at all) and the confirm read (which decides whether
    the post worked). A budget passed to only one of them would leave the other
    able to abort the same vote, and the fake records the keyword so a call site
    that stopped passing it fails here.
    """
    path = body_file(f"✅ LGTM - cycle `{CYCLE}`")
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote()], counted=[True], valid_count=1),
    )
    rc = _run(
        mod,
        monkeypatch,
        counter,
        FakeGh(),
        ["1255", "--body-file", path, "--mergeability-wait", "45"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert len(counter.calls) == 2, "one pre-flight read and one confirmation read"
    assert counter.kwargs == [
        {"mergeability_wait": 45.0},
        {"mergeability_wait": 45.0},
    ], counter.kwargs
    assert "counted" in out


def test_zero_is_a_supported_budget_and_asks_once(mod, monkeypatch, capsys, body_file):
    """`0` must stay legal: it is "ask once", the counter's own default.

    Pinned because the tempting way to make the waiting *feel* robust is a floor
    (`max(30, wait)`), which would silently make the fast path unreachable and
    hide a hung GitHub behind a minute of sleeping in every scripted use.
    """
    counter = FakeCounter(
        verdict_with(),
        verdict_with([vote()], counted=[True], valid_count=1),
    )
    rc = _run(
        mod,
        monkeypatch,
        counter,
        FakeGh(),
        ["1255", "--body-file", body_file(f"✅ LGTM - cycle `{CYCLE}`"), "--mergeability-wait", "0"],
    )
    assert rc == 0
    assert counter.kwargs == [{"mergeability_wait": 0.0}, {"mergeability_wait": 0.0}]
