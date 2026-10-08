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
states that run's own step cannot report at all: **no run** for the tag, and a run that
is **still running**.

A tag is not a window:

Measured 2026-09-29, one day after this guard landed: the workflow holds **163** runs, so
a reading built on "the 50 most recent runs" stops at v0.2.58 -- and v0.2.57, published
with its full asset set since 2026-08-20, read as `FAULT: no run exists ... nothing was
published for this tag`, whose remedy (`git push origin :refs/tags/v0.2.57`) would have
deleted the tag of a live release. A question the instrument cannot answer had been
reported as a definite fault. Two changes answer it. The run list is filtered **by the
tag's own branch** (`--branch <tag>`; measured to return v0.2.57's run, 32322632199, while
the 50 newest stop at v0.2.58), so how much history the workflow has accumulated cannot
hide a tag's run -- and the client-side head-branch comparison stays, so an ignored filter
cannot widen the match either. And if no run comes back even then, the release is asked
about **before** any verdict: a release for the tag makes the build half *unmeasured*
(rc 2, do not re-push), never a fault with a destructive remedy.

A remedy has to be a command that answers:

Measured 2026-10-04 on v0.3.8's red run (36956685533), while this tool's own FAULT was
being read: the remedy used to send the reader to `gh run view <id> --log-failed`, and on
that host the command returns **0 bytes with exit 0** -- for this run, for a green one
(36658495939, v0.3.7) and for a recent `Test` run, so it is the host's log path and not
this run's. An empty answer that exits 0 reads as "there is nothing to see", which is the
same shape as reporting "could not measure" as a pass, and it is worst exactly here: the
reader has just been told the release is missing. `gh api` is unaffected (that job's log
is 689 KB), so the remedy now names `scripts/read-run-failure.py`, which asks the API,
prints the failing job's **step** and the block up to its `##[error]`, and keeps "no
failed job" (rc 1) apart from "the log could not be read" (rc 2). One implementer of the
reading, two consumers: this tool names the job, that one explains it. The old command is
not repeated here as a thing to try -- a remedy that offers both hands the reader the
broken one at the moment they are least able to tell.

The reads are public, so a token is not a precondition for them:

Measured 2026-10-08, on the host that runs the release chain: `gh` is installed but not
authenticated, and it answers every call below with a refusal to *try* --
`To get started with GitHub CLI, please run: gh auth login`, exit 4. That made this
step unrunnable exactly where it is used. With v0.3.9 tagged, one unauthenticated GET
each returned the tag's run (`37723424917`, success), its release (published, 9 assets,
neither draft nor prerelease) and `releases/latest` (`v0.3.9`) -- so the answer this tool
exists to print was three public reads away, while the tool printed
`not measurable` (rc 2) and told the reader to go and authenticate.

A release verifier that only answers when a token happens to be configured is blind at
the one moment it matters: when the host is reading a published release by hand, and when
the tag's own run is the only CI that ever exercises signing and notarization. So a call
`gh` **refused to try** is re-asked of `api.github.com` with no credentials
(`_public_api`), and the answer is translated into the field names the callers read
(`_via_public_api`): `id`/`databaseId`, `head_branch`/`headBranch`, `html_url`/`url`,
`display_title`/`displayTitle`. `gh` still goes first, because only it can read a private
repo -- and a call `gh` *made* and that failed is never re-asked, because 404 and 500 are
answers about the remote rather than refusals to ask.

The reading names its own basis (`read via: ...`). An authenticated reading and a public
one are the same answer only while the repo is public, and `--repo` points this tool at
other repos; a reading that cannot say which client answered has hidden which question it
answered.

Exit codes, the family's contract:

    0  the tagged run is green AND the release for the tag is published: not a draft,
       not a prerelease, with at least one asset
    1  a fault, named with its remedy: no run for the tag (asked by the tag's own branch,
       not by a window) and no release for the tag either; the run failed (the failing
       jobs are listed); no release exists for the tag although its run is green; the
       release is a draft, is a prerelease, or carries no asset
    2  not measurable -- never 0. Neither channel answered: `gh` is missing, and a call
       it refused to try found no answer anonymously either; or the tagged run has not
       concluded yet; or no run for the tag could be read although a release for the tag
       does exist, which leaves the build half unmeasured
       and forbids the re-push remedy. A run that is still running is
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
import re
import subprocess
import sys
import urllib.error
import urllib.request

#: The repo whose releases are read when `--repo` is not given. The same default the
#: sibling API-reading guards use, and printed before any verdict: the subject of this
#: reading is the remote queue, so the local tree's name would answer nothing.
DEFAULT_REPO = "argszero/emrg"

#: The one workflow that publishes a release, and the reason a tag with no run under it
#: is a fault rather than a gap: nothing else creates a release.
WORKFLOW = "build-release.yml"

#: How many of the tag's *own* runs are read. The list is asked for by the tag
#: (`--branch <tag>`), so this bounds one tag's runs rather than the workflow's whole
#: history: a tag older than any window is still found by its own name. A re-pushed tag
#: leaves more than one run behind, and the newest is the one that counts.
RUN_LIMIT = 50

#: Identifies this tool to api.github.com, which refuses requests without one -- the same
#: constant the sibling `scripts/read-run-failure.py` carries, named per tool so a
#: rate-limit question can be traced to the reader that spent it.
USER_AGENT = "emrg-check-release-published"

#: One anonymous request's budget. Anonymous reads share a 60/hour bucket on the host, so
#: a hung request is not worth waiting out; the reading is retried, never waited on.
_TIMEOUT = 30

#: How `gh` declines to try at all when no credential is configured: it prints this and
#: exits 4. Named because it is the one failure that says nothing about the remote --
#: measured 2026-10-08, `gh run list` and `gh api` both answer it here, while the same
#: reads return 200 unauthenticated -- and so the one failure this tool must not report as
#: "could not measure" without asking the public API first.
_GH_NOT_AUTHENTICATED = "gh auth login"

#: Which channels answered, so the reading names its own basis. A host that sees a
#: reading must be able to tell an authenticated one from a public one: they are the same
#: question only while the repo is public, and this tool is pointed at other repos by
#: `--repo`.
_TRANSPORT: set[str] = set()


def _note_transport(which: str) -> None:
    _TRANSPORT.add(which)


def transports_used() -> str:
    """The channels that answered, in a stable order, for the reading to print."""
    order = ["gh", "api.github.com (no token)"]
    used = [name for name in order if name in _TRANSPORT]
    return ", ".join(used) if used else "nothing answered"


#: `gh api` reports the HTTP status inside its own message -- `gh: Not Found (HTTP 404)`
#: -- and exits 1 for every HTTP failure. The process code therefore cannot tell 404 from
#: 500, so the status is parsed out of the message (measured 2026-09-29 on
#: `releases/tags/v9.9.9` and on an unauthenticated call).
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class GhError(RuntimeError):
    """A `gh` invocation that did not answer.

    `http_status` is carried rather than folded into the message because 404 is not a
    failure to measure: for `releases/tags/<tag>` it is the definite answer "there is no
    release for this tag". Every other status means the question could not be asked, and
    so does None -- a call that is not HTTP-shaped at all (`run list` refusing a workflow
    name, or `gh` missing from the machine).
    """

    def __init__(
        self,
        returncode: int | None,
        args: list[str],
        detail: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"gh {' '.join(args)} failed (rc={returncode}): {detail}")
        self.returncode = returncode
        self.detail = detail
        self.http_status = http_status


def _flag(args: list[str], name: str) -> str | None:
    """The value `name` is bound to in a `gh` argument list, or None when it is absent."""
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _public_path(args: list[str]) -> str | None:
    """The api.github.com path that answers the same question as this `gh` call.

    Every read this tool makes is a public endpoint, so each has an equivalent; None is
    the definite "no equivalent is known", which is raised rather than guessed, because a
    guessed path would answer a different question in the same shape.
    """
    if args[:1] == ["api"]:
        return args[1] if len(args) > 1 else None
    if args[:2] == ["run", "list"]:
        repo = _flag(args, "--repo")
        workflow = _flag(args, "--workflow")
        branch = _flag(args, "--branch")
        if not (repo and workflow and branch):
            return None
        limit = _flag(args, "--limit") or str(RUN_LIMIT)
        return (
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            f"?branch={branch}&per_page={limit}"
        )
    if args[:2] == ["run", "view"]:
        repo = _flag(args, "--repo")
        if not repo or len(args) < 3:
            return None
        return f"repos/{repo}/actions/runs/{args[2]}/jobs"
    return None


def _public_api(path: str) -> str:
    """One read from api.github.com with no credentials at all.

    Its own function rather than inlined into `_gh`, because it is the second seam a test
    drives (patching `subprocess` would replace it for every other module in the process).

    A failed fetch is raised as the same `GhError` `_gh` raises, with the HTTP status
    carried: `release_for_tag` reads 404 off it as the definite absence, and an answer
    that arrived as a transport error would turn "no release exists" into "could not
    measure" -- the two states this family exists to keep apart.
    """
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    labelled = [f"api.github.com/{path}"]
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise GhError(
            None, labelled, f"HTTP {exc.code} from api.github.com", http_status=exc.code
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a reading that failed, not a crash
        raise GhError(
            None, labelled, f"api.github.com could not be reached ({type(exc).__name__}: {exc})"
        ) from exc
    return body


def _run_as_gh(run: dict) -> dict:
    """One REST run object under the field names `gh run list --json` uses.

    The translation is by name and not by hope: a caller that read `databaseId` off a raw
    REST page would read None, and a run whose id did not come back is precisely the
    "not measurable" this guard must not let arrive as a pass. `headBranch` matters most
    of all -- `runs_for_tag` filters on it, and a run listed but not matched reads as
    *this tag has no run*, the fault with the destructive remedy.
    """
    return {
        "databaseId": run.get("id"),
        "headBranch": run.get("head_branch"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "displayTitle": run.get("display_title"),
        "url": run.get("html_url"),
    }


def _via_public_api(args: list[str]) -> str:
    """The same call, answered anonymously, in the shape `gh` would have printed.

    Two of the shapes differ and one does not: `run view --json jobs` and `gh api` both
    hand back the REST body unchanged (a job's `name` and `conclusion` are spelled the
    same on both sides), while `run list --json` returns a bare list of runs whose fields
    `gh` renamed -- and it arrives wrapped in the endpoint's own `workflow_runs` envelope,
    which a caller expecting a list must not be handed.
    """
    path = _public_path(args)
    if path is None:
        raise GhError(None, args, "no anonymous equivalent is known for this call")
    body = _public_api(path)
    # Noted here rather than inside `_public_api`: this is the function that decides the
    # channel, and a note recorded a layer below the decision would vanish for any caller
    # that drove the fetch itself -- a reading whose basis is unrecorded is the state this
    # line exists to prevent.
    _note_transport("api.github.com (no token)")
    if tuple(args[:2]) != ("run", "list"):
        return body
    payload = json.loads(body)
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise GhError(
            None,
            args,
            "api.github.com's run list carried no `workflow_runs` list",
        )
    return json.dumps([_run_as_gh(r) for r in runs if isinstance(r, dict)])


def _gh(args: list[str]) -> str:
    """Run `gh`, failing loud: an unreadable release is not a published one.

    **`gh` first, then api.github.com with no credentials, and only when `gh` refused to
    try.** Measured 2026-10-08 on the host that runs the release chain: `gh` is installed
    but unauthenticated, so every call this tool makes answers

        To get started with GitHub CLI, please run:  gh auth login

    and exits 4 -- while the same tag's run, its release and `releases/latest` each
    return 200 to an unauthenticated GET. The documented third step of the release chain
    was therefore unrunnable in the environment the chain is run from, which is the
    moment this tool exists for.

    A call `gh` **made** and that failed is never re-asked: 404 and 500 are answers about
    the remote, not refusals to ask, and re-asking would spend the anonymous bucket to
    learn the same thing.
    """
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
        detail = proc.stderr.strip()
        if _GH_NOT_AUTHENTICATED in detail:
            return _via_public_api(args)
        status = _HTTP_STATUS.search(detail)
        raise GhError(
            proc.returncode,
            args,
            detail,
            http_status=int(status.group(1)) if status else None,
        )
    _note_transport("gh")
    return proc.stdout


def _gh_json(args: list[str]) -> object:
    return json.loads(_gh(args))


def runs_for_tag(tag: str, repo: str) -> list[dict]:
    """Every `Build Release` run whose head branch is `tag`, newest first.

    The head branch is how the workflow records the tag it was triggered by (a tag push
    sets `GITHUB_REF_NAME` to the tag and the run's head branch to it too), so this is a
    comparison of the tag against the run's own record rather than against a title. The
    same ref is what the API is asked to filter on (`--branch`): a window over *all* the
    workflow's runs answers about the tags inside it, not about the tag that was asked
    for, and the tag it leaves out reads as one that was never published.
    """
    runs = _gh_json(
        [
            "run", "list",
            "--repo", repo,
            "--workflow", WORKFLOW,
            "--branch", tag,
            "--limit", str(RUN_LIMIT),
            "--json", "databaseId,headBranch,status,conclusion,displayTitle,url",
        ]
    )
    if not isinstance(runs, list):
        raise GhError(None, ["run", "list"], "the run list was not a list")
    return [r for r in runs if r.get("headBranch") == tag]


def _no_run_for_tag(tag: str, repo: str) -> int:
    """No run came back for the tag: the release decides which absence this is.

    A run this guard could not read is not the same state as a tag nothing was ever
    published for, and the two must not arrive as the same verdict: only the second has a
    remedy, and the remedy for the second -- `git push origin :refs/tags/<tag>` -- would
    delete the tag of a live release. So the release is asked about first, and a release
    that exists turns this into an unmeasured build half (rc 2), never a fault.
    """
    release = release_for_tag(tag, repo)
    if release is not None:
        assets = release.get("assets")
        count = len(assets) if isinstance(assets, list) else 0
        print(
            f"not measurable: no `{WORKFLOW}` run for {tag} came back -- the list was "
            f"asked for by the tag's own branch (`--branch {tag}`), so this is not a "
            f"window limit -- but a release for the tag DOES exist: draft="
            f"{release.get('draft')} prerelease={release.get('prerelease')} assets={count}. "
            f"The build-run half is therefore unread, which is not a pass. Do NOT re-push "
            f"the tag: overwriting it would delete the tag of a published release and "
            f"re-run signing for a version already out. List the tag's own runs with "
            f"`gh run list --branch {tag}` and compare them against this release."
        )
        return 2
    print(
        f"FAULT: no `{WORKFLOW}` run exists for {tag} -- read by the tag's own branch, so "
        f"not a window over the workflow's runs -- and no release exists for the tag "
        f"either. That workflow is the only thing that creates a release, so nothing was "
        f"published for this tag. Re-trigger it by pushing the tag again "
        f"(`git push origin :refs/tags/{tag} && git push origin {tag}`) and watch the "
        f"run it starts."
    )
    return 1


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
    same value. The status itself has to be read out of gh's message -- `gh api` exits 1
    for 404 exactly as it does for 500 (measured 2026-09-29), so a test on the process
    code would never take this branch and every absent release would read as unmeasurable.
    """
    try:
        payload = _gh_json(["api", f"repos/{repo}/releases/tags/{tag}"])
    except GhError as exc:
        if exc.http_status == 404:
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
        code = _read(tag, repo)
    except GhError as exc:
        print(
            f"not measurable: {exc} -- this is not a pass. A call `gh` refused to try is "
            f"re-asked of api.github.com with no credentials, so this failure means the "
            f"read did not answer on either channel: check `gh auth status` and this "
            f"machine's network, then run this again."
        )
        code = 2
    except json.JSONDecodeError as exc:
        print(f"not measurable: the answer is not JSON ({exc})")
        code = 2
    # Printed whatever the verdict, and last so it never displaces the reading: an
    # authenticated reading and a public one are the same answer only while the repo is
    # public, and `--repo` points this tool at other repos.
    print(f"read via: {transports_used()}")
    return code


def _read(tag: str, repo: str) -> int:
    """The readings, on a subject whose name is already printed."""
    runs = runs_for_tag(tag, repo)
    if not runs:
        return _no_run_for_tag(tag, repo)

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
            f"runs even when the upload failed). Read the failing job's step and its "
            f"output with "
            f"`uv run --no-sync python3 scripts/read-run-failure.py {run_id}`, then "
            f"re-run with `gh run rerun {run_id} --failed`."
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
