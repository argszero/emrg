#!/usr/bin/env python3
"""Which tags carry no GitHub Release object at all?

`scripts/check-release-published.py` answers one tag well: its build run, its release,
draft/prerelease/assets, and the per-cause remedy for each. That is the right shape for a
tag someone is asking about, and it is the wrong shape for the question no one thinks to
ask -- *is any tag on this repository published-less right now?* -- because it can only be
put to one tag at a time, and nobody enumerates a repository's tags by hand.

Measured 2026-11-12 (`cyc20261112-025715`), which is why this file exists. Tag `v0.3.51`
had been pushed and its release job had been **skipped**, so four platform builds were
green with nothing published behind them. That was discovered by reading one run's jobs,
not by asking about the repository -- and when the fleet question was finally put by hand
the next cycle, the answer was **three** orphan tags, not one: `v0.3.51`, `v0.3.34` and
`v0.3.8`. A per-tag tool had been run on none of the other two, because no reading ever
named them.

So this is a screen and not a second opinion. It reads the two collections and reports the
difference; the moment a tag is named, the per-tag tool is what explains it, and this
file's remedy hands the reader exactly that command. **One implementer of a per-tag
reading, two consumers** -- the same arrangement `check-release-published.py` already
describes for `scripts/read-run-failure.py`, and for the same reason: a second
implementation of the cause logic is a second thing to keep in step, and the fleet view
does not need it to answer its own question.

A tag is not a window
---------------------
Both collections are read **paginated**, and the counts actually read are printed before
any verdict -- that print is where a count belongs, because it moves between runs while
this paragraph does not. Here both collections outgrow a page at GitHub's default size, so
a list read with that page size, or with a `--limit` window, would report *every tag past
the first page* as an orphan. The failure would be loud and wrong in the most expensive
direction -- it would name real published releases as missing and invite someone to re-push
their tags.

Draft releases are included in an authenticated list (the account carries `repo`), so a
draft does **not** read as an orphan here; it reads as its own fault, which is what it is:
a draft is invisible to `releases/latest`, so no host can upgrade to it.

Why a second call confirms every candidate
------------------------------------------
The screen's answer for "no release" is *the tag did not appear in the releases list*. That
is an absence read from a **collection**, and a collection can be short for reasons that
have nothing to do with the tag: a truncated page, a filter GitHub applied, a release
deleted between the two calls. So every candidate is put to the per-tag endpoint
(`repos/<repo>/releases/tags/<tag>`) before it is reported, and only a **404** there is
the definite answer. Anything else raises, and the run is `not measurable` -- because a
false orphan names a published release as missing.

The 404 arrives as an unreadable process code, not as a status: `gh api` exits **1** for
404 exactly as it does for 500 (measured 2026-09-29). The status therefore has to be parsed
out of gh's own message, which is the same distinction `check-release-published.py` draws
and for the same reason -- "there is no release" and "the question could not be asked" must
never arrive as one value.

A tag that will never have a release
------------------------------------
Two tags on this repository carry no release and never will: `v0.3.8` and `v0.3.34` each lost
their build, and a published successor replaced each one. They are recorded in
`ABANDONED_TAGS` with the failed run and the successor that replaced it, because the
alternative is a screen that answers `1` on every run for the rest of the repository's life --
and an exit code that is correct, unactionable and permanent is an exit code its reader learns
to ignore. That costs exactly the reading this screen was built to make cheap: an orphan
nobody thought to ask about.

The record is the narrowest thing that suppresses anything.

* It reclassifies **one verdict on one tag**: "no release at all". A recorded tag that later
  carries a draft, a prerelease or an asset-less release still faults, because that is a
  different sentence with a different remedy.
* A recorded tag is still **confirmed** by the per-tag endpoint. The record reclassifies a
  verdict; it does not take a shortcut around the reading that produced it.
* A third tag faults whether or not it is in the record. This is a list of tags, never a
  class of them.
* A recorded row that a release **contradicts** is a fault: the record claimed no release
  would ever exist for that tag, and one does. Removing the row is the whole remedy, which is
  what keeps the record from becoming a permanent blanket.
* A recorded row whose tag has **left the collection** suppresses nothing, so it is printed
  as dormant and does not fault. Faulting there would be the same disease this record exists
  to cure -- permanent, correct, and unactionable.

Exit codes, the family's contract:

    0  every tag carries a release that is published: not a draft, not a prerelease, with
       at least one asset -- or carries none at all and is recorded in `ABANDONED_TAGS`,
       which is printed on its own line and never silently
    1  a fault, named per tag with its remedy: a tag with no release at all, or a release
       that is a draft, is a prerelease, or carries no asset; and a recorded row that no
       longer describes the repository
    2  not measurable -- never 0. `gh` is missing, unauthenticated or refused a call; the
       repository returned **no tags at all**, which is a scan that read nothing rather
       than a clean result; or a candidate could not be confirmed either way

Usage:

    uv run --no-sync python3 scripts/check-release-orphan-tags.py
    uv run --no-sync python3 scripts/check-release-orphan-tags.py --repo owner/name
    uv run --no-sync python3 scripts/check-release-orphan-tags.py --tag v0.3.51
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

#: The repo read when `--repo` is not given, and printed before any verdict: the subject is
#: a remote collection, so the local tree's name would answer nothing.
DEFAULT_REPO = "argszero/emrg"

#: `gh api` reports the HTTP status inside its own message -- `gh: Not Found (HTTP 404)` --
#: and exits 1 for every HTTP failure, so the status is parsed out of the message.
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")

#: A page of 100 is GitHub's own maximum, so this is the fewest calls the read can take.
_PER_PAGE = 100

#: Tags whose release will never exist, each with the reason it is permanent. A row here
#: reclassifies **one** verdict on **one** tag -- "no release at all" -- and nothing else: it
#: never suppresses a draft, a prerelease or an asset-less release on the same tag, it never
#: covers a tag it does not name, and a row a release contradicts is a fault of its own.
#: The docstring's "A tag that will never have a release" states why.
ABANDONED_TAGS: dict[str, str] = {
    "v0.3.8": (
        "build-release run 36956685533 concluded failure on its macOS arm64 job; v0.3.9, "
        "published 2026-10-08, superseded it, so retrying the build now would publish a "
        "version older than the one hosts already run"
    ),
    "v0.3.34": (
        "build-release run 41625081155 concluded failure on both Linux AppImage jobs; "
        "v0.3.35, published 2026-11-03, superseded it, so retrying the build now would "
        "publish a version older than the one hosts already run"
    ),
}


class GhError(RuntimeError):
    """A `gh` invocation that did not answer.

    `http_status` is carried rather than folded into the message because 404 is not a
    failure to measure: for `releases/tags/<tag>` it is the definite answer "no release
    exists for this tag". Every other status, and None, means the question could not be
    asked.
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


def _gh(args: list[str]) -> str:
    """Run `gh`, failing loud: a collection that could not be read is not a clean one."""
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
        status = _HTTP_STATUS.search(detail)
        raise GhError(
            proc.returncode,
            args,
            detail,
            http_status=int(status.group(1)) if status else None,
        )
    return proc.stdout


def all_tags(repo: str) -> list[str]:
    """Every tag on the repository, newest first, read paginated.

    `--jq` is applied per page and the pages are concatenated as lines, so this never has
    to assume anything about how `gh` joins a multi-page array: each page prints one name
    per line and the lines are the tag list.
    """
    out = _gh(
        [
            "api",
            f"repos/{repo}/tags?per_page={_PER_PAGE}",
            "--paginate",
            "--jq",
            ".[].name",
        ]
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def all_releases(repo: str) -> dict[str, dict]:
    """Every release on the repository, keyed by the tag it names, read paginated.

    Draft releases are included: the account carries `repo`, and GitHub shows drafts to an
    account with push access. A draft is therefore reported as a draft rather than as an
    absent release -- the two have different remedies.
    """
    out = _gh(
        [
            "api",
            f"repos/{repo}/releases?per_page={_PER_PAGE}",
            "--paginate",
            "--jq",
            '.[] | [.tag_name, (.draft|tostring), (.prerelease|tostring), '
            '((.assets|length)|tostring)] | @tsv',
        ]
    )
    releases: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise GhError(None, ["api", "releases"], f"unexpected release row: {line!r}")
        name, draft, prerelease, assets = parts
        releases[name] = {
            "draft": draft == "true",
            "prerelease": prerelease == "true",
            "assets": int(assets),
        }
    return releases


def release_exists(tag: str, repo: str) -> bool:
    """Put one candidate to the per-tag endpoint: is there really no release for it?

    False is the definite absence (HTTP 404), not an unreadable answer: every other
    failure propagates, so "no release" and "could not ask" never arrive as one value.
    This is what keeps a truncated collection from printing a fault about a published tag.
    """
    try:
        _gh(["api", f"repos/{repo}/releases/tags/{tag}"])
    except GhError as exc:
        if exc.http_status == 404:
            return False
        raise
    return True


def _unusable(release: dict) -> list[str]:
    """The ways a release that exists can still be one no host can use."""
    faults: list[str] = []
    if release["draft"] is not False:
        faults.append("it is a DRAFT, so `releases/latest` cannot see it")
    if release["prerelease"] is not False:
        faults.append("it is marked PRERELEASE, so the upgrade chain skips it")
    if not release["assets"]:
        faults.append("it carries no asset, so no platform has an installer")
    return faults


def check(repo: str, only: str | None = None) -> int:
    """Print the reading and return the exit code, naming the subject before anything fails."""
    print(f"repo: {repo}")
    try:
        return _read(repo, only)
    except GhError as exc:
        print(
            f"not measurable: {exc} -- this is not a pass. Check `gh auth status` and run "
            f"this again."
        )
        return 2


def _read(repo: str, only: str | None) -> int:
    tags = all_tags(repo)
    if not tags:
        print(
            "not measurable: the repository returned no tags at all, so nothing was "
            "compared -- this is not a pass. A scan with an empty subject has measured "
            "nothing, which is not the verdict that nothing is wrong."
        )
        return 2
    releases = all_releases(repo)
    print(f"tags read: {len(tags)}")
    print(f"releases read: {len(releases)} (paginated, drafts included)")

    subject = [only] if only else tags
    if only and only not in tags:
        print(
            f"not measurable: `--tag {only}` was asked for, and that tag is not in the "
            f"{len(tags)} tags read. Either it does not exist or the collection was short; "
            f"nothing was compared."
        )
        return 2

    orphans: list[str] = []
    for tag in subject:
        if tag in releases:
            continue
        if not release_exists(tag, repo):
            orphans.append(tag)
        else:
            # The collection said absent and the per-tag endpoint says present: the
            # collection was short. That is not a fault about this tag, and it is not a
            # clean result either -- the scan's premise is gone.
            print(
                f"not measurable: {tag} did not appear in the {len(releases)} releases "
                f"read, yet `releases/tags/{tag}` answers that a release exists. The "
                f"collection was short of its own subject, so this scan compared an "
                f"incomplete list -- not a pass."
            )
            return 2

    # The record splits the orphans; it does not shorten the list before it is read.
    abandoned = [tag for tag in orphans if tag in ABANDONED_TAGS]
    faults = [tag for tag in orphans if tag not in ABANDONED_TAGS]

    # A row a release contradicts is a fault of the record itself: the record claimed no
    # release would ever exist for that tag, and one does, so the row is no longer true and
    # removing it is the remedy. The fault is one edit away from gone, which is what keeps
    # the record from becoming a blanket.
    #
    # A row whose tag has left the collection suppresses nothing at all, so it is printed as
    # dormant rather than faulting: a permanent fault here would be the same disease this
    # record exists to cure. A narrowed run audits only the rows whose tag it read.
    stale: list[tuple[str, str]] = []
    dormant: list[str] = []
    for tag in ABANDONED_TAGS:
        if only and tag != only:
            continue
        if tag in orphans:
            continue
        if tag in releases:
            stale.append((tag, "it now carries a release, so it is not an orphan"))
        elif tag not in tags:
            dormant.append(tag)

    unusable: list[tuple[str, list[str]]] = [
        (tag, _unusable(releases[tag]))
        for tag in subject
        if tag in releases and _unusable(releases[tag])
    ]

    for tag in faults:
        print(f"  tag: {tag} -- NO RELEASE")
    for tag in abandoned:
        print(
            f"  tag: {tag} -- ABANDONED, no release by recorded decision: "
            f"{ABANDONED_TAGS[tag]}"
        )
    for tag, why in stale:
        print(f"  baseline row: {tag} -- STALE: {why}; remove it from ABANDONED_TAGS")
    for tag in dormant:
        print(
            f"  record: {tag} -- DORMANT: it is not among the {len(tags)} tags read, so its "
            f"row suppresses nothing today"
        )
    for tag, tag_faults in unusable:
        print(f"  tag: {tag} -- release unusable: {'; '.join(tag_faults)}")

    if not faults and not unusable and not stale:
        if abandoned:
            print(
                f"OK: {len(subject) - len(abandoned)} of {len(subject)} tag(s) read carry a "
                f"published release -- not a draft, not a prerelease, with at least one "
                f"asset; the {len(abandoned)} abandoned above carry none by recorded decision"
            )
        else:
            print(
                f"OK: all {len(subject)} tag(s) read carry a published release -- not a "
                f"draft, not a prerelease, with at least one asset"
            )
        return 0

    print(
        f"FAULT: {len(faults)} tag(s) with no release and {len(unusable)} tag(s) with an "
        f"unusable release"
        + (f", plus {len(stale)} stale baseline row(s)" if stale else "")
        + f", out of {len(subject)} read"
        + (
            f" ({len(abandoned)} more carry no release by recorded decision and are not "
            f"counted)"
            if abandoned
            else ""
        )
        + ". A tag with no release behind it is a version someone was told was cut; the four "
        f"platform builds may even be green, because a build and a release are two different "
        f"jobs. Read each one with `uv run --no-sync python3 scripts/check-release-published.py "
        f"<tag>`, which names that tag's run and the remedy for its own cause -- this screen "
        f"reads the collection and cannot tell a skipped release job from a failed one."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Which tags carry no GitHub Release object at all?",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"owner/name to read (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="read one tag instead of the whole collection; a tag that is not there is "
        "not measurable rather than clean",
    )
    args = parser.parse_args(argv)
    return check(args.repo, args.tag)


if __name__ == "__main__":
    sys.exit(main())
