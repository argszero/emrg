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
* **each runnable guard must report no defect about this checkout** — rc 0, or, for the
  one member whose subject a bare checkout genuinely lacks, the documented
  `could not measure` (the paragraph below the limits says which, and why). Naming the
  tree is not the same as watching the answer, and the gap between the two is what
  shipped v0.3.6: `check_nonlocal.py` printed `rc=1` about this very checkout on
  v0.3.6's commit (the `_approval_pending` crash the host reported, issue #1759) while
  every test in this file passed, because nothing here read an exit code. The leg is on
  the *family* rather than on each guard's own test file so that the next guard to join
  `RUN_HERE` cannot arrive without its verdict being asserted.

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

The verdict leg reads each member's exit code against that member's *own* contract, and
one contract has three honest answers: `check-memory-index.py` reads the `.emrg/`
indexes, which are gitignored **host data**, so a tree that carries none answers
`could not measure: no memory index under …` with rc **2** — the code its own `--help`
documents for "nothing could be measured" — and a host whose index is over the rule
answers rc **1**, a true statement about data this repository neither ships nor controls.
That member is therefore held to its own contract rather than to rc 0: rc 0 while its
subject is there and within the rule, rc 1 with its own finding line while it is over,
rc 2 with that reason while it is absent — and the leg **measures the subject's presence
itself** (`SUBJECT_MAY_BE_ABSENT`) rather than taking any answer on trust, then checks the
exit code against the report the guard printed, so a crash cannot pass as a verdict. Every
other member must be rc 0 unconditionally: for them rc 1 is the defect this leg exists
for, and it is a failure in both environments.

Corrected 2026-09-30 (`cyc20260930-074010`), and the correction is the lesson: the
paragraph that stood here claimed this guard "exits 0" in a checkout without the indexes,
and called that measurement. It exits 2. On that claim the leg was red in CI (run
36645953605, `assert 2 == 0`) while green on the host's checkout, where the index exists —
"a no-op in CI" was really "unchecked in CI", and the wrong half shipped. A claim about
another tool's exit code is a reading, never a recollection: run it in the tree whose
shape you are describing. A bare checkout is `git clone` with no `.emrg/` in it, and every
member's code was re-measured there before this leg was rewritten.
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
    "check_unbound_reads.py",
)

#: The one `RUN_HERE` member whose *subject* the tree under test can genuinely lack, and
#: the places it looks for that subject when it is given no path — the guard's own message
#: names both, and
#: `tests/test_check_memory_index.py::test_the_default_subject_is_both_levels_the_tree_carries`
#: pins them. They live under `.emrg/`, which is gitignored, so a bare checkout carries
#: none of them and the guard answers `could not measure` (rc 2) rather than a verdict.
#: Listed here so the leg below can *measure* the absence it reads that answer against,
#: instead of assuming it: with a subject present, the same rc 2 is a failure.
SUBJECT_MAY_BE_ABSENT = {
    "check-memory-index.py": (
        ".emrg/memory/MEMORY.md",
        ".emrg/sessions/*/memory/MEMORY.md",
    ),
}


def _carries(subjects: tuple[str, ...]) -> bool:
    """Whether this checkout holds any of these subject paths."""
    return any(next(REPO_ROOT.glob(pattern), None) is not None for pattern in subjects)

#: The one guard run here that names a *given* tree rather than its own checkout, so it
#: is asked a different question below: its line must follow its argument.
TAKES_A_TREE_ARGUMENT = "check-citation-resolves.py"

#: The guards that can be pointed at a tree of the caller's choosing, and the argument
#: that points each at one — the shape the empty-tree leg below needs and cannot derive,
#: because "accepts a root" is not visible in the source the way a `tree: ` line is.
#: `TAKES_A_TREE_ARGUMENT` is here as the member whose tree *is* its argument;
#: `check_unbound_reads.py` takes the same kind of input behind an optional flag, which
#: is why it is also in `RUN_HERE` (with no flag it names its own checkout);
#: `check-workflows.py` takes one behind `--root` and is classified in the
#: *cannot-be-run-here* bucket, which is why membership here is checked against the whole
#: classification rather than against `RUN_HERE` alone (it was added 2026-10-06,
#: `cyc20261006-214703`: the table was one merge old and already one member short, while
#: `check-workflows.py` refused both shapes correctly — rc 2 with
#: `not measurable: no workflow file under …`). A guard that gains a root argument has to
#: be added here, and this table is a claim like any other: nothing measures it against
#: the argv each guard really accepts.
POINTABLE_AT_A_TREE = {
    "check-citation-resolves.py": (),
    "check_unbound_reads.py": ("--root",),
    "check-workflows.py": ("--root",),
}

#: The spellings the family's refusals really use. The first two are what this leg's own
#: measurement produced on 2026-10-06 (`cyc20261006-214703`), by pointing each member in
#: `POINTABLE_AT_A_TREE` at an empty directory **and** at a path that is not there:
#: `check-citation-resolves.py` and `check_unbound_reads.py` print `could not measure`,
#: `check-workflows.py` prints `not measurable`. The leg below asked for the first of those
#: only, so it could not have covered the third member at all — a matcher written for one
#: output form is the defect this repository has already paid for (issue #461, the singular
#: `identity imported` that missed `3 identities imported`).
#:
#: `unmeasurable` is carried but is **not** one of those six readings — no member of this
#: table prints it, in either shape (reviewed 2026-10-06, `cyc20261006-215800`; verified
#: here). It is here because the family does print it elsewhere — `check-patch-files.py`,
#: `check-rant-citations.py` and `check-extension-load.py` — so a guard joining the table
#: with that wording would be reported as printing no reason at all, which would be a false
#: finding about a truthful guard. The comment says which is measured and which is carried
#: because the two are different claims about the same tuple.
REFUSAL_WORDS = ("could not measure", "not measurable", "unmeasurable")


def _refusal_reason(out: str) -> str:
    """The reason a refusal printed after its spelling, or `""` when it printed none.

    Read from the **start of a line**, not from anywhere in the output: the leg's own
    words are that "the unmeasurable code has to carry the reason", and a substring search
    is satisfied by a spelling that appears inside a *verdict* line, or inside a docstring
    echoed by `--help`. Requiring the `:` the family's refusals use is what makes the
    assertion's sentence true of the thing it asserts (reviewed 2026-10-06,
    `cyc20261006-215800`).
    """
    for line in out.splitlines():
        for spelling in REFUSAL_WORDS:
            prefix = f"{spelling}:"
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
    return ""


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
    "check-workflows.py": (
        "lints this tree's `.github/workflows/` with the actionlint build CI pins, which "
        "a clean checkout - and this host - does not ship (measured 2026-10-04: no "
        "actionlint, shellcheck, node or npm on PATH). It reads a working tree and names "
        "it, and that naming IS verified rather than classified: "
        "`tests/test_check_workflows.py::test_the_tree_it_read_is_named_first` runs it as "
        "a subprocess against a `tmp_path` tree and asserts the first line, which holds "
        "with or without the binary because the tree line is printed before the search"
    ),
    "check-install-drift.py": (
        "its subject is the host's installed tree (`~/.emrg/install/source`), not this "
        "checkout: the verdict is about a *pair* of trees, and a bare checkout carries no "
        "install, so running it here would measure the host rather than the rule - and on "
        "a host with a hand-edited install tree its honest answer is rc 1, which is that "
        "host's drift rather than this tree's defect. It reads a working tree (this "
        "checkout, through git) and names both halves, and that naming IS verified rather "
        "than classified: `tests/test_check_install_drift.py::test_the_report_names_both_trees_before_the_verdict` runs it "
        "as a subprocess against a `tmp_path` install tree and checkout and pins both lines"
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
    "check-release-tag.py": (
        "takes a required release tag, so the bare run this suite performs is not its "
        "invocation - the tag it is asked about does not exist until the host cuts it, so "
        "there is nothing for a default to mean. It does read a working tree (the checkout "
        "its own file lives in, `REPO_ROOT`) and names it before the verdict, and that "
        "naming IS verified rather than classified: `tests/test_check_release_tag.py` runs "
        "it against `tmp_path` trees and asserts the resolved tree is printed before the "
        "verdict, so the rule is measured here even though the member is not in the bare "
        "loop above"
    ),
}

#: Answers about something other than a working tree, so the rule does not apply - each
#: with what it reads instead, because "not a tree reader" is a claim like any other.
NOT_TREE_READERS = {
    "check-vote-count.py": "reads reviews and mergeability from the GitHub API",
    "check-pr-base.py": "reads the base each PR declares on the GitHub API",
    "check-issue-links.py": (
        "reads the open issues and PRs and their timelines on the GitHub API - the "
        "subject is the remote queue, so there is no local tree whose name would "
        "answer anything (the repo it read is printed first instead)"
    ),
    "check-stacked-prs.py": (
        "reads every open PR's head and commit list from the GitHub API and reports which "
        "open PR's head commit another one would land - the subject is the remote queue, so "
        "no local tree's name would answer anything (the repo it read is printed first "
        "instead, as check-issue-links.py does)"
    ),
    "check-patch-files.py": "reads the patch files it is given as arguments",
    "check-merge-freshness.py": "requires PR numbers; answers per head and its base",
    "check-merge-order.py": "requires PR numbers; names the base it plans against",
    "check-merge-pairs.py": "requires PR numbers; names the base it folds onto",
    "check-merge-plan-suite.py": "requires PR numbers; names the base it plans against",
    "check-merge-sequence.py": "requires PR numbers; names the base it folds onto",
    "check-merge-tree-health.py": "requires PR numbers; prints the repo it works in",
    "check-merge-landing-diff.py": "requires PR numbers; names the base it diffs against",
    "check-notary-credentials.py": (
        "reads the three notarization variables from the environment and then asks Apple's "
        "notary service (`xcrun notarytool history`) whether they are accepted - the subject "
        "is a credential and the answer comes from the network, so no local tree would "
        "answer anything. It is a host-side preflight: the rule this file holds is about a "
        "guard naming the tree it reports on, and this one reports on a credential"
    ),
    "check-release-published.py": (
        "takes a required release tag and reads that tag's release and its `build-release.yml` "
        "run from the GitHub API - the subject is the remote release queue, so no local tree "
        "would answer anything (the repo it read is printed first instead, as "
        "check-issue-links.py does)"
    ),
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


def _classified() -> set[str]:
    """Every guard this file accounts for, in any bucket.

    One home for the set, because two assertions ask about it and the reviewer of the leg
    that first wrote it in two places was right that they can drift: the sibling check below
    asks "is every guard on disk classified", and the empty-tree leg asks "is every pointable
    guard in a bucket that can hold a tree reader". The first wants this union; the second
    wants `_tree_reading_buckets()`, and having them share one definition is what keeps the
    two questions about the same classification (reviewed 2026-10-06, `cyc20261006-215800`).
    """
    return (
        set(RUN_HERE)
        | {TAKES_A_TREE_ARGUMENT}
        | set(NAMES_ITS_TREE_BUT_IS_NOT_RUN_HERE)
        | set(NOT_TREE_READERS)
    )


def _tree_reading_buckets() -> set[str]:
    """The buckets a guard that can be pointed at a tree may sit in.

    `NOT_TREE_READERS` is excluded deliberately: a member there answers about something
    that is not a working tree, so a guard listed as pointable **and** classified as not
    reading a tree is a contradiction, and this is the set that can still catch it. The
    wider `_classified()` is the right question for the sibling check ("every script on
    disk is classified"), which is a different one (reviewed 2026-10-06,
    `cyc20261006-230355`).
    """
    return set(RUN_HERE) | {TAKES_A_TREE_ARGUMENT} | set(NAMES_ITS_TREE_BUT_IS_NOT_RUN_HERE)


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
    classified = _classified()
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


def test_every_runnable_guard_comes_back_clean_on_this_checkout() -> None:
    """The leg that was missing when a released client crashed on every Enter.

    `check_nonlocal.py` exists for exactly one defect class, was correct, and printed
    `rc=1` about this checkout on v0.3.6's commit — the `_approval_pending` read that
    made the TUI unusable (issue #1759). Nothing failed: no workflow ran the guard, and
    no test read its exit code. Naming the tree (the assertions above) would not have
    caught it either — the guard named its tree *correctly* while reporting the defect.

    So this asks every runnable member for its verdict on the tree being tested, and reads
    that verdict against the guard's *own* contract rather than against a single expected
    number:

    * **rc 0** — no defect about this tree. The only acceptable answer for a member whose
      subject is this repository's own source, whatever the checkout holds;
    * **rc 2 with `could not measure`** — accepted **only** from the one member in
      `SUBJECT_MAY_BE_ABSENT`, and **only** while this checkout really carries none of its
      subjects. The absence is measured here (the same two paths the guard looks at, per
      its own message and its own test file) so the allowance cannot become a place a
      broken guard hides: the same rc 2 with an index present fails below;
    * **rc 1 with its own finding line** — accepted, for that same member and only while
      its subject is present, because that member's subject is **gitignored host data**:
      `check-memory-index.py` reads the `.emrg/` indexes, which this repository neither
      ships nor controls, so "the indexes under this checkout are within the rule" is a
      claim about the host rather than about the tree this family guards. Its exit code is
      still read for internal consistency — rc 0 must carry the `OK:` line and rc 1 the
      finding line — so a crash, a half-run, or a guard whose report disagrees with its
      exit code still fails. The member's *boundary* behaviour (a row of exactly
      `INDEX_TITLE_MAX_CHARS` then one past it, `cap` lines then one more) is read in full
      by `tests/test_check_memory_index.py`, which builds both states from the constants;
      this leg is about the family's verdicts, not a second home for that.

    rc 1 from any other member — a rule this family enforces violated by the tree under
    test — is a failure in both environments, which is what keeps this leg biting on the
    defect it was written for. See the module docstring for the exit code this leg got
    wrong once, and how.

    Corrected 2026-10-01 (`cyc20261001-095229`): the third answer was added when the row
    predicate stopped reading only `- ` — the fix that lets `check-memory-index.py` read a
    Markdown-table index at all. With `- ` alone the guard could not report rc 1 for a
    table index under *any* host data, so this contract had no third case to state; now
    it can, and this host's own index really is over the row bound (four rows, the
    longest 1,139 chars), which made this leg red here while CI — a bare clone, no
    `.emrg/`, rc 2 — stayed green.
    """
    for name in RUN_HERE:
        proc = _run([str(SCRIPTS_DIR / name)], cwd=REPO_ROOT)
        out = proc.stdout + proc.stderr
        subjects = SUBJECT_MAY_BE_ABSENT.get(name)
        if subjects is None:
            assert proc.returncode == 0, (
                f"{name} reports a defect on this checkout (rc={proc.returncode}) — a rule "
                "this family enforces is violated by the tree under test:\n"
                f"{out}"
            )
            continue
        if not _carries(subjects):
            assert proc.returncode == 2 and "could not measure" in out, (
                f"{name}: this checkout carries none of its subjects "
                f"({', '.join(subjects)}), so the only honest verdict it can give here is "
                "`could not measure` — rc 2, the code its own --help documents — and it "
                f"must say so in words. Got rc={proc.returncode}:\n{out}"
            )
            continue
        assert proc.returncode in (0, 1), (
            f"{name}: with its subject present, the two verdicts it documents for itself "
            f"are 0 (within the rule) and 1 (over it). Got rc={proc.returncode}:\n{out}"
        )
        within = "OK:" in out
        assert within == (proc.returncode == 0), (
            f"{name}: rc={proc.returncode} while its report "
            f"{'says the indexes are within the rule' if within else 'carries no OK line'} "
            f"— the exit code and the reading must be one answer:\n{out}"
        )
        assert proc.returncode == 0 or "over a number the rule names" in out, (
            f"{name}: rc=1 must carry that guard's own finding line, or the exit code is "
            f"not a verdict this leg can read:\n{out}"
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


def _refusal(proc: subprocess.CompletedProcess) -> str:
    """The combined output of a refusal, for the two legs below to assert on together."""
    return (proc.stdout or "") + (proc.stderr or "")


def test_a_guard_pointed_at_a_tree_it_read_nothing_from_refuses_to_pass(tmp_path) -> None:
    """An empty tree is not a clean one, for whichever guard is asked about it.

    The family's rule — a question a guard cannot answer is `could not measure`, never a
    pass — was prose in `check-citation-resolves.py`'s exit-code paragraph and had no
    executor in either guard that can be pointed at a tree of the caller's choosing
    (measured 2026-10-06, `cyc20261006-165503`): the citation guard printed its green
    line `every citation names a node id pytest collects` with rc 0 for an empty
    directory, and `check_unbound_reads.py` printed its `OK:` line for an empty one and
    for a path that does not exist at all. The fix is a branch in each; this leg is what
    makes a third guard take the branch rather than being trusted to have copied it.

    Both shapes are asked of every member, because they are both "I read no file": a
    directory that exists and holds nothing, and a path that is not a directory at all.
    """
    classified = _tree_reading_buckets()
    unclassified = set(POINTABLE_AT_A_TREE) - classified
    assert not unclassified, (
        f"{sorted(unclassified)} is listed as pointable at a tree and is in no bucket "
        "that can hold one, so nothing else in this file holds it to the family's rules"
    )
    for name, flag in POINTABLE_AT_A_TREE.items():
        empty = tmp_path / f"empty-{name}"
        empty.mkdir()
        absent = tmp_path / f"absent-{name}"
        for label, target in (("an empty directory", empty), ("a path that is not there", absent)):
            proc = _run([str(SCRIPTS_DIR / name), *flag, str(target)], cwd=REPO_ROOT)
            out = _refusal(proc)
            assert proc.returncode == 2, (
                f"{name} answered rc={proc.returncode} for {label} ({target}) — `0` is "
                f"\"measured and clean\", and a tree it read no file from is neither:\n{out}"
            )
            assert _refusal_reason(out), (
                f"{name}: the unmeasurable code has to carry the reason, or it is not an "
                f"answer a reader can act on — no line starts with one of {REFUSAL_WORDS} "
                f"followed by a reason:\n{out}"
            )
            assert "OK:" not in out and "collects" not in out, (
                f"{name} printed a clean verdict as well as refusing — the two halves "
                f"disagree:\n{out}"
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


def test_the_refusal_reader_takes_only_a_line_that_starts_with_the_spelling() -> None:
    """The reader the leg asserts through, pinned in both directions.

    The leg's own words are that "the unmeasurable code has to carry the reason". The
    matcher this replaced searched the whole output for the spelling, which a *verdict*
    line satisfies — so the assertion could pass on a guard that refused in words the term
    reader cannot act on (reviewed 2026-10-06, `cyc20261006-215800`). Both halves are
    pinned here because the leg's arms can only show the third member's shape; this is the
    reader every member is read through.
    """
    assert _refusal_reason("could not measure: no test module was read\n") == (
        "no test module was read"
    )
    assert _refusal_reason("not measurable: no workflow file under /tmp/x\n") == (
        "no workflow file under /tmp/x"
    )
    assert _refusal_reason("VERDICT: not measurable: a reason\n") == "", (
        "a spelling inside another line is a mention, not the refusal this leg reads"
    )
    assert _refusal_reason("not measurable:\n") == "", (
        "the spelling with no reason after it is what the leg exists to refuse"
    )
    assert _refusal_reason("") == ""


def test_the_legs_bucket_set_is_narrower_than_the_sibling_checks() -> None:
    """A pointable guard cannot be one that answers about something other than a tree.

    The leg first read the four-bucket union, which `NOT_TREE_READERS` is a member of, so a
    guard listed as both pointable *and* classified as not reading a tree would have been
    accepted — the one contradiction this membership check can still catch (reviewed
    2026-10-06, `cyc20261006-230355`). The wider set stays the sibling check's question:
    "is every guard on disk classified", which is a different one.
    """
    assert _tree_reading_buckets() <= _classified()
    assert not (_tree_reading_buckets() & set(NOT_TREE_READERS)), (
        "a bucket that cannot hold a tree reader is in the set the leg checks membership "
        "against, so the check can no longer fail for the reason it exists"
    )
    assert set(NOT_TREE_READERS) <= _classified()
