#!/usr/bin/env python3
"""Is the release for this tag published, and did its build pass?

`Agent.md` names two host-side counterparts for the release chain and both answer a
question about the *tree*: `scripts/bump-version.py --check` (all eight version
declarations agree) and `scripts/check-release-tag.py` (the tag names the version the
tagged tree declares). The third step of that chain -- "the tagged Build Release run is
green across the platform matrix, and the GitHub Release is published as Latest (not
draft, not prerelease) with the full asset set" -- had no counterpart at all: it was
read by hand, from `gh run list` and `gh release view`, or not read.

That step is where the measured incidents live. v0.2.7 cut nine red Build Release runs
while the Test workflow stayed green, because the tag is the **only** trigger of
`build-release.yml` -- so a tag pushed without a run reports nothing anywhere. v0.3.1
published a release that stayed a **draft** with one asset missing (`other side closed`
after 8 of 9 uploads): a draft is invisible to `releases/latest`, so the auto-upgrade
chain cannot see it, and the upgrade session is retriggered every tick forever. The run
was red, but "red" did not say what to fix, and confirming the recovery was another
hand-run of `gh release view`.

What this reads:

* the `Build Release` run whose head branch is the tag -- its status and conclusion,
  and, when it failed, the **names of the jobs that failed** rather than the run's;
* the release the API returns for the tag: `draft`, `prerelease`, and the asset names
  and count;
* and it prints which tag GitHub calls Latest.

What it deliberately does **not** do is compare the asset set against the build.
`build-release.yml`'s own final step already asserts published-and-complete-and-Latest
from the artifacts that run built, and it runs under `if: !cancelled()` so that it
reports the very failure it exists for (the v0.3.1 draft). A second implementer of that
comparison would be a second thing to keep in step, and a release whose CI said
`published, not prerelease, 9 asset(s)` needs no second opinion. What this adds is the
reading from **outside** the run, by a later cycle or by the host, including the two
states that run's own step cannot report at all: **no run exists** for the tag, and a
run that is **still running**.

Exit codes, the family's contract:

    0  the tagged run is green AND the release for the tag is published: not a draft,
       not a prerelease, with at least one asset
    1  a fault, named with its remedy: no run exists for the tag; the run failed (the
       failing jobs are listed); no release exists for the tag although its run is
       green; the release is a draft, is a prerelease, or carries no asset
    2  not measurable -- never 0. `gh` is missing, unauthenticated, or refused the
       call, or the tagged run has not concluded yet. A run that is still running is
       exactly the state this reading must not call a pass: the release does not exist
       until the run's own release job has created it, and a guard that passes because
       it could not read its subject is the defect this family exists to catch.

Usage:

    uv run --no-sync python3 scripts/check-release-published.py v0.3.5
    python3 scripts/check-release-published.py v0.3.5 --repo owner/name
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

#: The repo whose releases are read when `--repo` is not given. The same default the
#: sibling API-reading guards use, and printed before any verdict: the subject of this
#: reading is the remote queue, so the local tree's name would answer nothing.
DEFAULT_REPO = "argszero/emrg"

#: The one workflow that publishes a release, and the reason a tag with no run under it
#: is a fault rather than a gap: nothing else creates a release.
WORKFLOW = "build-release.yml"

#: Enough runs to find an old tag's own run without paginating. A tag older than this
#: one is reported as having no run, which is the honest reading of what was looked at.
RUN_LIMIT = 50


class GhError(RuntimeError):
    """A `gh` invocation that did not answer.

    `returncode` is carried rather than folded into the message because 404 is not a
    failure to measure: for `releases/tags/<tag>` it is the definite answer "there is no
    release for this tag". Every other code means the question could not be asked.
    """

    def __init__(self, returncode: int | None, args: list[str], detail: str) -> None:
        super().__init__(f"gh {' '.join(args)} failed (rc={returncode}): {detail}")
        self.returncode = returncode
        self.detail = detail


def _gh(args: list[str]) -> str:
    """Run `gh`, failing loud: an unreadable release is not a published one."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GhError(None, args, f"gh is not available on this machine ({exc})") from exc
    if proc.returncode != 0:
        raise GhError(proc.returncode, args, proc.stderr.strip())
    return proc.stdout


def _gh_json(args: list[str]) -> object:
    return json.loads(_gh(args))


def runs_for_tag(tag: str, repo: str) -> list[dict]:
    """Every `Build Release` run whose head branch is `tag`, newest first.

    The head branch is how the workflow records the tag it was triggered by (a tag push
    sets `GITHUB_REF_NAME` to the tag and the run's head branch to it too), so this is a
    comparison of the tag against the run's own record rather than against a title.
    """
    runs = _gh_json(
        [
            "run", "list",
            "--repo", repo,
            "--workflow", WORKFLOW,
            "--limit", str(RUN_LIMIT),
            "--json", "databaseId,headBranch,status,conclusion,displayTitle,url",
        ]
    )
    if not isinstance(runs, list):
        raise GhError(None, ["run", "list"], "the run list was not a list")
    return [r for r in runs if r.get("headBranch") == tag]


def failed_jobs(run_id: object, repo: str) -> tuple[list[str], str]:
    """The names of the failed jobs of `run_id`, and a reason when they cannot be read.

    `skipped` is not a failure: a job gated on a platform (`needs: build` where the
    matrix leg was skipped) concludes `skipped` on a successful run, and calling that a
    failure would invent one.
    """
    if run_id is None:
        return [], "the run did not report an id"
    try:
        payload = _gh_json(["run", "view", str(run_id), "--repo", repo, "--json", "jobs"])
    except GhError as exc:
        return [], f"the job list could not be read ({exc.detail})"
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return [], "the job list could not be read (no jobs in the answer)"
    broken = [
        str(j.get("name"))
        for j in jobs
        if isinstance(j, dict) and j.get("conclusion") not in (None, "success", "skipped")
    ]
    return broken, ""


def release_for_tag(tag: str, repo: str) -> dict | None:
    """The release the API returns for `tag`, or None when there is none.

    None is the definite absence (HTTP 404), not an unreadable answer: every other
    failure propagates, because "no release" and "could not ask" must not arrive as the
    same value.
    """
    try:
        payload = _gh_json(["api", f"repos/{repo}/releases/tags/{tag}"])
    except GhError as exc:
        if exc.returncode == 404:
            return None
        raise
    if not isinstance(payload, dict):
        raise GhError(None, ["api", f"releases/tags/{tag}"], "the answer was not an object")
    return payload


def latest_tag(repo: str) -> tuple[str, str]:
    """What GitHub calls the Latest release, or why that could not be read.

    Reported, never asserted here: *whether* this tag should be Latest depends on
    whether it is the newest published release, and that assertion -- with its
    pagination -- already lives in the workflow's final step. A tag that is not the
    newest is allowed to be published and rerun, and refusing it here would refuse the
    recovery path rather than a fault.
    """
    try:
        payload = _gh_json(["api", f"repos/{repo}/releases/latest"])
    except GhError as exc:
        return "", f"not readable ({exc.detail})"
    if isinstance(payload, dict) and isinstance(payload.get("tag_name"), str):
        return payload["tag_name"], ""
    return "", "not readable (no tag_name in the answer)"


def check(tag: str, repo: str) -> int:
    """Print the reading and return the exit code.

    The header is printed here, once, before anything can fail: every failure path
    below (and `main`'s) must leave a reader able to see what was asked, and a
    "not measurable" that does not name its subject is the kind of gap this family
    exists to close.
    """
    print(f"repo: {repo}")
    print(f"tag: {tag}")
    try:
        return _read(tag, repo)
    except GhError as exc:
        print(
            f"not measurable: {exc} -- this is not a pass. Check `gh auth status` and "
            f"run this again."
        )
        return 2
    except json.JSONDecodeError as exc:
        print(f"not measurable: gh returned something that is not JSON ({exc})")
        return 2


def _read(tag: str, repo: str) -> int:
    """The readings, on a subject whose name is already printed."""
    runs = runs_for_tag(tag, repo)
    if not runs:
        print(
            f"FAULT: no `{WORKFLOW}` run exists for {tag} (looked at the {RUN_LIMIT} most "
            f"recent runs). That workflow is the only thing that creates a release, so "
            f"nothing was published for this tag. Re-trigger it by pushing the tag again "
            f"(`git push origin :refs/tags/{tag} && git push origin {tag}`) and watch the "
            f"run it starts."
        )
        return 1

    run = runs[0]
    run_id = run.get("databaseId")
    status = run.get("status")
    conclusion = run.get("conclusion")
    print(f"build run: {run_id} {status}/{conclusion}")
    if run.get("url"):
        print(f"run url: {run['url']}")

    if status != "completed":
        print(
            f"not measurable: the run for {tag} is still `{status}`, so the release it "
            f"would create does not exist yet -- this is not a pass. Read it again with "
            f"`gh run watch {run_id}` or a later run of this guard."
        )
        return 2

    if conclusion != "success":
        broken, reason = failed_jobs(run_id, repo)
        if broken:
            print(f"failed jobs: {', '.join(broken)}")
        elif reason:
            print(f"failed jobs: {reason}")
        print(
            f"FAULT: the `{WORKFLOW}` run for {tag} concluded `{conclusion}`, so nothing "
            f"was published for this tag (or the release it created is incomplete: the "
            f"run's own final step is what confirms published/complete/Latest, and it "
            f"runs even when the upload failed). Read the failing job's log with "
            f"`gh run view {run_id} --log-failed`, then re-run with "
            f"`gh run rerun {run_id} --failed`."
        )
        return 1

    print("build run green: its final step asserts published, complete and Latest, so a "
          "green run carries that assertion; the readings below are the same state read "
          "from outside the run")

    release = release_for_tag(tag, repo)
    if release is None:
        print(
            f"FAULT: no release exists for tag {tag}, although its build run is green. "
            f"A green run with no release is the state the workflow's final step exists "
            f"to report; if it was created and then deleted, re-run the release job "
            f"(`gh run rerun {run_id}`)."
        )
        return 1

    draft = release.get("draft")
    prerelease = release.get("prerelease")
    assets = release.get("assets")
    names = sorted(
        str(a.get("name"))
        for a in (assets if isinstance(assets, list) else [])
        if isinstance(a, dict)
    )
    print(f"release: draft={draft} prerelease={prerelease} assets={len(names)}")
    for name in names:
        print(f"  asset: {name}")

    latest, why = latest_tag(repo)
    if latest:
        print(f"github Latest: {latest}" + (" (this tag)" if latest == tag else ""))
    else:
        print(f"github Latest: {why}")

    faults: list[str] = []
    if draft is not False:
        faults.append(
            "it is a DRAFT (or its draft flag could not be read), so it is invisible to "
            "releases/latest and no host can upgrade to it -- publish it with "
            f"`gh release edit {tag} --draft=false`"
        )
    if prerelease is not False:
        faults.append(
            "it is marked PRERELEASE, so the upgrade chain will not treat it as a "
            f"release -- clear it with `gh release edit {tag} --prerelease=false`"
        )
    if not names:
        faults.append(
            "it carries no asset at all, so every platform's installer is missing -- "
            f"re-run the run (`gh run rerun {run_id}`); an interrupted upload is what "
            "the v0.3.1 release stopped on"
        )
    if faults:
        print("FAULT: the release for " + tag + " is not usable:")
        for fault in faults:
            print(f"  - {fault}")
        return 1

    print(
        f"OK: {tag} built green and is published -- not a draft, not a prerelease, "
        f"{len(names)} asset(s)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Is the release for this tag published, and did its build pass?",
    )
    parser.add_argument("tag", help="the release tag, e.g. v0.3.5")
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"owner/name to read (default: {DEFAULT_REPO})",
    )
    args = parser.parse_args(argv)
    return check(args.tag, args.repo)


if __name__ == "__main__":
    sys.exit(main())
