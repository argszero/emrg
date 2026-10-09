#!/usr/bin/env python3
"""Which open PRs would land another open PR's commits?

The class this exists for
-------------------------
Measured 2026-10-06: PR #1879's head carried #1877's head commit, because the branch
was cut from that PR's branch. `pulls/1879/commits` returned four commits, the first of
them `b1e50447` - "emrg: grep counts only the files it really read", which is #1877's
work and its own PR's declaration.

    b1e50447  emrg: grep counts only the files it really read      <- #1877's head
    ff847a52  emrg: the empty-tree leg holds every pointable guard <- #1879's own
    ...

A reviewer found it by reading the API by hand and vetoed the PR (2026-10-06T16:09:37Z,
cycle `cyc20261006-215800`): "merging this head as it stands lands #1876's fix under a
PR that declares only `Closes #1878`". **No reading in this repository caught it**, and
the harm is the chain R5 exists for - the fix for one issue lands on a PR that does not
declare it, leaving the second claim on an issue that is already finished.

What the harm is measured to be, in both states
-----------------------------------------------
The same head, landable at two different bases. Before #1877 landed, master was
`2e1a6e44`:

    git merge-tree --write-tree --merge-base=2e1a6e44 2e1a6e44 c8b5bb5c  -> bb7d6d750872
      emrg/tools/grep_tool.py                            44 ++++-
      tests/test_a_tree_reading_guard_names_its_tree.py 141 ++++++++++   <- this PR's own
      tests/test_grep_tool.py                            82 ++++++-

Three paths, two of them #1876's fix riding under #1879's declaration. After #1877
landed (master `65df80ac`) the same head was re-measured:

    git merge-tree --write-tree --merge-base=65df80ac 65df80ac c8b5bb5c  -> bb7d6d750872
      tests/test_a_tree_reading_guard_names_its_tree.py 141 ++++++++++   <- only its own

One path. So the state this tool reports is a state of **the queue at the moment it is
read**, and it decays by itself when the carried PR lands - which is why the condition
below is "another PR is still open" rather than "the commit is not on master".

Why the condition is "open", and what a SHA can and cannot say
-------------------------------------------------------------
Measured 2026-10-07 (cycle `cyc20261007-045148`), asking the compare endpoint about
three heads, the two landings being indistinguishable by SHA:

    compare/master...b1e50447  --jq '{status,ahead_by,behind_by}' -> diverged, ahead 1
    compare/master...65df80ac  (the same work, squash-landed as #1877) -> behind, ahead 0
    compare/master...a7f3b03c  (master's tip)                          -> identical

`b1e50447`'s content **is** on master and the compare endpoint says `diverged, ahead_by 1`
- a squash landing leaves no commit behind, so no ancestor test can see it. A guard that
read "the carried commit is not an ancestor of master" as "that work is not landed" would
therefore report #1879's post-#1877 state - a state in which the landing changes one path -
as a fault, forever. That is why the fault condition is the *queue* (the carried commit is
the head of a PR that is still open) and not the graph.

The compare endpoint is still used, in the one direction it can be trusted: it can prove a
carried commit **is** landed (`behind`/`identical` - the commit is an ancestor of master)
and never that it is not.

What this tool does not answer
------------------------------
Whether the carrier's merge would change the carried PR's paths. On the two states above
that is the whole difference (3 paths against 1), and it is
`scripts/check-merge-landing-diff.py`'s question - it diffs the tree the merge produces.
This tool reports the fact and the two remedies, and the landing instrument prices them.

Exit codes: `0` every open PR carries only its own commits, `1` at least one carries
another open PR's head, `2` the queue could not be read. A `2` is never a pass.

Usage: scripts/check-stacked-prs.py [--repo owner/name]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_DEFAULT = "argszero/emrg"

#: How this file's python tools are invoked (`Agent.md`, "Test Commands"). Printed
#: remedies carry it, for the reason `review-queue.py`'s constant gives: a bare
#: `scripts/<name>.py` is mode 644 here and exits 126.
RUNNER = "uv run --no-sync python3"

#: The states in which a carried commit is provably already on master. Read from the
#: compare endpoint (measured 2026-10-07): `behind` is an ancestor of master, `identical`
#: is master's tip. Everything else - `ahead`, `diverged` - is *unknown*, and a squash
#: landing answers `diverged` while its content is on master, so no other state may be
#: read as landed.
LANDED_STATES = ("behind", "identical")


class MeasurementError(RuntimeError):
    """A reading this tool could not take - never a verdict.

    Raised rather than returned so `main` answers `2` with the cause, the code the family
    uses for "could not measure": an unhandled exception here would exit `1`, which in
    this family's tables is a *fault*.
    """


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    The program name is prepended here so a call site cannot forget it (the mistake
    `check-merge-freshness.py` records: a helper that omitted it ran the POSIX `pr`
    utility instead).
    """
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise MeasurementError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n{proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(f"gh answered with something that is not JSON: {exc}") from exc


def _gh_lines(args: list[str]) -> list[dict]:
    """Every page of a list endpoint, as one list, through a caller-owned `--jq` filter.

    `--paginate` with a `.[] | {...}` filter emits one JSON object per line across pages,
    parsed per line because concatenated page arrays are not valid JSON. The filter belongs
    to the caller, so a call site cannot end up with two `--jq` flags and a projection that
    silently did not apply (`check-vote-count.py`'s measured incident).
    """
    proc = subprocess.run(
        ["gh", *args, "--paginate"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise MeasurementError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)} --paginate\n"
            f"{proc.stderr.strip()}"
        )
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MeasurementError(f"gh answered with a line that is not JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise MeasurementError(f"gh answered a list endpoint with {type(row).__name__}")
        out.append(row)
    return out


def _open_pulls(repo: str) -> list[dict]:
    """Every open PR, as the fields this tool reads.

    The projection must have applied: without it the fields arrive under their raw names
    and `head.sha` reads as empty, which would make every PR look like it carries nothing -
    a clean report from a broken read.
    """
    rows = _gh_lines(
        [
            "api",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq",
            ".[] | {number, title, head_sha: .head.sha, head_ref: .head.ref, "
            "base_ref: .base.ref}",
        ]
    )
    for row in rows:
        if not row.get("number") or not row.get("head_sha"):
            raise MeasurementError(
                "the open-PR projection did not apply: a row came back without a number "
                f"or a head sha ({row!r}) - this is not a queue with no carriers"
            )
    return rows


def _commits_of(repo: str, number: int) -> list[dict]:
    """The commits a PR adds, as `pulls/<N>/commits` reports them (merge base relative)."""
    return _gh_lines(
        [
            "api",
            f"repos/{repo}/pulls/{number}/commits?per_page=100",
            "--jq",
            ".[] | {sha, subject: (.commit.message | split(\"\\n\")[0])}",
        ]
    )


def _commit_state(repo: str, sha: str) -> str:
    """The compare endpoint's status for master against this commit, or `""`.

    Asked only about a commit that is another open PR's head, and only to let the one
    provable answer through (`LANDED_STATES`). A failure here is not a failure of the
    reading: the row keeps its fault and says the state was not read.
    """
    payload = _gh_json(
        ["api", f"repos/{repo}/compare/master...{sha}", "--jq", "{status: .status}"]
    )
    if not isinstance(payload, dict):
        raise MeasurementError(f"the compare endpoint answered {type(payload).__name__}")
    return str(payload.get("status") or "")


@dataclass
class Row:
    """One open PR, and which other open PRs' commits it would land."""

    number: int
    title: str
    head_sha: str
    #: `(other PR's number, its head sha, its subject, its compare state or "")` per
    #: carried head - the other PR's *own* head, never an arbitrary shared commit.
    carried: list[tuple[int, str, str, str]] = field(default_factory=list)
    #: Carried heads whose PR is open but whose commit is provably on master, so landing
    #: them cannot carry that work again. Kept apart from `carried` rather than dropped:
    #: a row that silently lost a finding would read the same as one that never had it.
    already_landed: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.carried


def judge(repo: str, pulls: list[dict], commits_by_pr: dict[int, list[dict]]) -> list[Row]:
    """One row per open PR: whose head commits it carries, and whether that PR is open.

    The subject is the *head* of another open PR, not any commit shared between two
    branches. Two branches cut from the same stale master share commits, and neither is
    landing the other's work - the harm this reports needs the carried commit to be the
    other PR's **head**, i.e. work that PR is still holding.
    """
    head_owner: dict[str, dict] = {}
    for pull in pulls:
        head_owner[str(pull["head_sha"])] = pull

    rows: list[Row] = []
    for pull in pulls:
        number = int(pull["number"])
        row = Row(
            number=number,
            title=str(pull.get("title") or ""),
            head_sha=str(pull["head_sha"]),
        )
        for commit in commits_by_pr.get(number, []):
            sha = str(commit.get("sha") or "")
            owner = head_owner.get(sha)
            if owner is None or int(owner["number"]) == number:
                continue
            subject = str(commit.get("subject") or "")
            state = _commit_state(repo, sha)
            if state in LANDED_STATES:
                row.already_landed.append((int(owner["number"]), sha, subject))
            else:
                row.carried.append((int(owner["number"]), sha, subject, state))
        rows.append(row)
    return rows


def _remedy(repo: str, row: Row) -> str:
    """What the reader can do, built from the row rather than from a template."""
    parts = []
    for other, sha, subject, state in row.carried:
        parts.append(
            f"#{row.number} carries #{other}'s head {sha[:8]} (\"{subject}\"): merging it "
            f"would land #{other}'s work under #{row.number}'s declaration, so land #{other} "
            "first - then merge master into this branch (`git fetch origin master`, "
            "`git merge FETCH_HEAD`, `git push origin <branch>`), which is what makes this "
            f"PR's landing its own again. Measured on the shape this tool was written from: "
            "three paths changed while the carried PR was open, one after it landed. The "
            f"merge that changes what, for #{row.number}, is "
            f"`{RUNNER} scripts/check-merge-landing-diff.py {row.number}` "
            f"(compare state read: {state or 'not read'})"
        )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-stacked-prs.py",
        description=(
            "Report every open PR whose branch carries another open PR's head commit, so "
            "its merge would land work whose PR is still open."
        ),
    )
    parser.add_argument("--repo", default=REPO_DEFAULT, help="owner/name")
    args = parser.parse_args(argv)

    try:
        pulls = _open_pulls(args.repo)
        commits_by_pr = {int(p["number"]): _commits_of(args.repo, int(p["number"])) for p in pulls}
        rows = judge(args.repo, pulls, commits_by_pr)
    except MeasurementError as exc:
        print(f"cannot determine the stacked PRs: {exc}", file=sys.stderr)
        return 2

    print(f"repo: {args.repo}, {len(rows)} open PR(s)")
    for row in rows:
        mark = "stacked" if row.carried else "ok"
        print(f"#{row.number} {mark} - {row.title}")
        for other, sha, subject, state in row.carried:
            print(f"    carries #{other} head {sha[:8]} (\"{subject}\") - compare {state or 'unread'}")
        for other, sha, subject in row.already_landed:
            print(
                f"    carries #{other} head {sha[:8]}, whose work is already on master "
                f"(\"{subject}\") - nothing to land twice, reported and not counted"
            )

    bad = [row for row in rows if not row.clean]
    if not bad:
        print(
            f"OK: {len(rows)} open PR(s), none carries another open PR's head commit"
        )
        return 0

    for row in bad:
        print(_remedy(args.repo, row), file=sys.stderr)
    print(
        f"{len(bad)} of {len(rows)} open PR(s) would land another open PR's work",
        file=sys.stderr,
    )
    return 1


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table (a stacked PR), while `2` is the code for "the question could not be
    answered" - so a caller that reads the code, or a cycle that reads this report, would
    read a crash as a finding. Measured 2026-10-07 while arming this file: with the
    open-PR projection check disabled, a row without `head_sha` reaches `judge` and raises
    `KeyError`, and without this wrapper the tool exits `1` - "there is a stacked PR" - for
    a queue it never read.

    This file loads no sibling by file path, so it is not in the list
    `tests/test_a_crash_is_a_measurement_error.py` pins to byte-identical entry points;
    `DEVELOPMENT.md` says a gate like this "reaches the same rule by its own route and says
    so where it does", and the rule is pinned here for this tool by its own suite
    (`tests/test_check_stacked_prs.py::test_a_crash_answers_could_not_measure`).
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - reported as unmeasurable, never swallowed
        traceback.print_exc()
        print(
            f"{Path(__file__).name}: could not measure - {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2  # cause: tool-failed


if __name__ == "__main__":
    sys.exit(_entry())
