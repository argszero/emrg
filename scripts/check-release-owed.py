#!/usr/bin/env python3
"""Does this tree declare a version that no tag carries?

The release chain asks its version question five times, and every one of the five
starts from a tag that already exists: `check-release-tag.py` compares one tag with
the tagged tree, `check-release-published.py` asks whether one tag is published,
`check-release-orphan-tags.py` enumerates tags that are already there, and
`check-release-artifact-version.py` / `check-release-prompt.py` read a built product.
None asks whether a tag **should** exist -- so a tree that declares a version nothing
has tagged is invisible to all five, and that is the one failure in this chain that
gets worse with every merge and turns nothing red.

Measured 2026-11-18 (`cyc20261118-095848`) on master `d5b17fea`:

* `emrg/__init__.py` declares `0.3.59`;
* `git describe --tags` answers `v0.3.58`;
* `git rev-list --count v0.3.58..HEAD` answers **9**.

Nine merged commits -- including two that change what every installation reads -- were
on master and reached no host, because a host only receives them from a published
release. The condition had been noticed by hand twice, once per release, each time
re-deriving the same three facts. This tool is that reading, mechanised.

Versions compare by **numeric parts**, never by string and never by tag date: `0.3.10`
is newer than `0.3.9`, and a tag's commit date says nothing about which version is
later. A tag that does not parse as three dotted numbers is counted and reported, never
sorted in as though it were one -- otherwise an unrelated tag named `latest` would
silently become "the newest release".

Only a tag **that can publish a release** counts: `build-release.yml` fires on
`tags: ["v*"]`, and its `verify-tag` job rejects a lightweight tag before any platform
builds, so neither a `v`-less name nor a lightweight tag can carry a version. Each is
reported by name as a near miss rather than accepted, because accepting either answers
`nothing is owed` for work that reached no host -- and both shapes have happened here
(`v0.2.97` went out lightweight).

It is a **screen for a host, not a CI gate.** On an unreleased master it is red by
design, so wiring it into CI would make the release cadence a permanently failing check
and teach its readers to ignore it. Its role is the one `bump-version.py --check` and
`check-release-tag.py` already play: the command run before cutting a release.

Exit codes, the family's contract:

    0  nothing is owed: the declared version is the newest tag's version
    1  the declared version is not the newest tag's version, and **which way** decides the
       remedy: *newer* means a release is owed (both versions and the commit count are
       printed), *older* means this checkout is behind a release that already exists and
       must not be tagged -- publishing an older version would put it ahead of a newer one.
       Reporting the second as the first, measured 2026-11-18 (`cyc20261118-102744`), sent a
       reader to `git tag -a v0.3.59` on a tree whose newest release was `v0.3.60`.
    2  not measurable: no version declaration could be read, or the repository could
       not be read at all (no `git`, not a work tree). Never 0 -- a guard that passes
       because it could not read its subject is the defect this one exists to catch.

An empty tag list is a **measurement**, not an unmeasured one: `git tag --list` answered,
and the answer is that nothing carries the declaration. So that case is exit 1, where a
repository that cannot be read at all is exit 2.

Usage:

    uv run --no-sync python3 scripts/check-release-owed.py
    uv run --no-sync python3 scripts/check-release-owed.py --root /path/to/checkout
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: The tree read when `--root` is not given: the checkout this script lives in.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the version is declared. The same file and the same anchored shape
#: `check-release-tag.py` and `tests/test_version_sync.py` read, so this reader and
#: those cannot disagree about what a declaration looks like.
VERSION_SOURCE = Path("emrg") / "__init__.py"
_DECLARATION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

#: The version shape a tag may carry: three dotted numbers, optional `v`.
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

#: The shape that actually **triggers a release**: `build-release.yml` fires on
#: `tags: ["v*"]` and nothing else. A tag spelled `0.3.59` never builds, never publishes and
#: never reaches a host -- so it cannot be what "carries" a version, however much it looks
#: like one. Measured 2026-11-18 (`cyc20261118-104130`): a tree declaring `0.3.59` whose only
#: tag was `0.3.59` answered `nothing is owed`, which is the exact false clean this tool
#: exists to prevent -- the work had shipped to nobody and the guard said it had.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class Unmeasurable(Exception):
    """The question could not be put to this tree at all -- exit 2's cause, never a pass."""


def declared_version(root: Path) -> str:
    """The tree's declared version, or `Unmeasurable` when it does not name exactly one.

    Two declarations are unmeasurable rather than a choice: the file names two versions,
    so it does not name one. `tests/test_no_duplicate_sources.py` owns that question and
    this reader must not answer it by picking the first.
    """
    try:
        text = (root / VERSION_SOURCE).read_text(encoding="utf-8")
    except OSError as exc:
        raise Unmeasurable(f"no version declaration could be read from {VERSION_SOURCE}: {exc}")
    found = _DECLARATION.findall(text)
    if len(found) != 1:
        raise Unmeasurable(
            f"{VERSION_SOURCE} carries {len(found)} `__version__ = \"...\"` declaration(s), "
            f"so it does not name one version"
        )
    version = found[0].strip()
    if not version:
        raise Unmeasurable(f"{VERSION_SOURCE} declares an empty version")
    return version


def _git(root: Path, *args: str) -> str:
    """Run one git command in `root` and return its stdout, or raise `Unmeasurable`."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise Unmeasurable(f"git could not be run in {root}: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise Unmeasurable(f"`git {' '.join(args)}` failed in {root}: {detail}")
    return proc.stdout


def _require_a_work_tree_of_its_own(root: Path) -> None:
    """`root` must be the repository's top level, not a directory inside another one.

    `git` walks **up** to find a repository, so pointing this tool at any subdirectory of
    any checkout makes it read that enclosing repository's tags and answer about a tree
    it was not asked about -- measured 2026-11-18 by the test that gives it a plain
    directory inside this checkout: it reported this checkout's 158 tags for a directory
    that is not a work tree at all. A reading about the wrong repository is worse than no
    reading, so the mismatch is refused.
    """
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != root:
        raise Unmeasurable(
            f"{root} is inside the repository at {top}, not a work tree of its own -- "
            f"the tags read would be that repository's, so this tree's release state "
            f"cannot be read from here. Point --root at a work tree's top level."
        )


def tags(root: Path) -> tuple[list[tuple[tuple[int, int, int], str]], list[str], list[str], list[str]]:
    """Every **release** tag, newest last, plus three buckets of names that are not one.

    A tag carries a version only when all three hold:

    * it matches `_RELEASE_TAG` - `build-release.yml` triggers on `tags: ["v*"]` and nothing
      else, so a `v`-less tag builds nothing;
    * it is **annotated**. The workflow's `verify-tag` job reads `object.type` from the API
      and exits 1 unless it is `tag`, so a lightweight tag stops the run before any platform
      builds and no release is published. This repository has already shipped one: `v0.2.97`
      was lightweight while every release since `v0.2.94` is annotated, and the instruction
      that produced it was the bare `git tag v<x.y.z>` (`tests/test_release_tag_form.py`
      records the measurement). `%(objecttype)` answers the same question locally;
    * therefore neither `v`-less nor lightweight can be what "carries" a version, and
      counting either would answer `nothing is owed` for work that reached no host.

    The buckets are kept apart because the remedies differ: a version-shaped tag missing its
    `v`, a version-shaped tag that is lightweight, and an unrelated name (`latest`) which is
    only noise.

    Sorted by numeric parts, because string order puts `v0.3.9` after `v0.3.10`. One
    `for-each-ref` call answers both name and type, rather than N `cat-file` calls.
    """
    _require_a_work_tree_of_its_own(root)
    text = _git(root, "for-each-ref", "refs/tags", "--format=%(refname:short)%09%(objecttype)")
    annotated: set[str] = set()
    for line in text.splitlines():
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) == 2 and parts[1] == "tag":
            annotated.add(parts[0])
    names = [line.rstrip("\r\n").split("\t")[0] for line in text.splitlines() if line.strip()]

    parsed: list[tuple[tuple[int, int, int], str]] = []
    lightweight: list[str] = []
    vless: list[str] = []
    skipped: list[str] = []
    for name in names:
        release = _RELEASE_TAG.match(name)
        if release is not None:
            if name in annotated:
                parsed.append((tuple(int(part) for part in release.groups()), name))
            else:
                lightweight.append(name)
            continue
        if _TAG.match(name) is not None:
            vless.append(name)
            continue
        skipped.append(name)
    parsed.sort()
    return parsed, lightweight, vless, skipped


def _name_list(names: list[str], limit: int = 5) -> str:
    """A few names, then a count -- because this checkout has 79 lightweight tags.

    Measured 2026-11-18 (`cyc20261118-105553`) on this repository: naming all 79 pushed the
    `A RELEASE IS OWED` line off the visible width, hiding the only actionable sentence
    behind a list nobody reads to the end. The count is the fact; the names are examples.
    """
    ordered = sorted(names)
    if len(ordered) <= limit:
        return ", ".join(ordered)
    return f"{', '.join(ordered[:limit])} and {len(ordered) - limit} more"


def check(root: Path) -> int:
    """Print the reading and return the exit code."""
    print(f"tree: {root}")
    try:
        declared = declared_version(root)
        parsed, lightweight, vless, skipped = tags(root)
    except Unmeasurable as exc:
        print(f"not measurable: {exc}")
        return 2
    print(f"declared: {declared} ({VERSION_SOURCE})")

    # A version-shaped declaration is compared as parts too, so `0.3.10` is not read as
    # older than `0.3.9`; a declaration that is not three dotted numbers cannot be
    # ordered against the tags at all.
    declared_match = _TAG.match(declared)
    if declared_match is None:
        print(
            f"not measurable: the declared version {declared!r} is not three dotted "
            f"numbers, so it cannot be ordered against a tag"
        )
        return 2

    skipped_note = f", {len(skipped)} tag(s) not version-shaped and not compared" if skipped else ""
    vless_note = (
        f", {len(vless)} version-shaped tag(s) with no `v` and therefore no build: "
        f"{_name_list(vless)}"
        if vless
        else ""
    )
    light_note = (
        f", {len(lightweight)} lightweight tag(s) that verify-tag rejects and so publish "
        f"nothing: {_name_list(lightweight)}"
        if lightweight
        else ""
    )
    if not parsed:
        print(f"tags: none that would publish a release{light_note}{vless_note}{skipped_note}")
        causes = []
        if vless:
            causes.append(
                f"the version-shaped tag(s) {_name_list(vless)} carry it, but "
                f"`build-release.yml` triggers on `v*` only, so a tag spelled without the `v` "
                f"builds nothing and publishes nothing"
            )
        if lightweight:
            causes.append(
                f"the tag(s) {_name_list(lightweight)} carry it, but they are "
                f"lightweight and `verify-tag` requires an annotated tag, so the run stops "
                f"before any platform builds"
            )
        carried = (" -- " + "; and ".join(causes)) if causes else ""
        print(
            f"A RELEASE IS OWED: the tree declares {declared} and no tag that publishes a "
            f"release carries it{carried}"
        )
        return 1

    newest = parsed[-1][1]
    print(
        f"tags: {len(parsed)} that would publish a release, newest {newest}"
        f"{light_note}{vless_note}{skipped_note}"
    )

    declared_parts = tuple(int(part) for part in declared_match.groups())
    newest_parts = parsed[-1][0]

    if declared == newest.lstrip("v"):
        print(f"OK: nothing is owed - the tree declares {declared} and {newest} carries it")
        return 0

    if declared_parts < newest_parts:
        # The tree is OLDER than the newest tag: a checkout behind the release, or a branch
        # cut before it. Nothing merged unreleased, and the remedy is not a tag - tagging an
        # older version would publish it as the latest, ahead of a newer one that already
        # exists. Keep telling this apart from "owed": reporting it as owed sends a reader to
        # cut a release of a version that is already superseded.
        print(
            f"THIS CHECKOUT IS BEHIND: the tree declares {declared} and the newest tag is "
            f"{newest}, so nothing here is waiting to be published -- the tree predates a "
            f"release that already exists. Update the checkout to {newest} (or to master) "
            f"before reading its release state; do NOT tag {declared}, because that would "
            f"publish an older version as the newest release, ahead of {newest}."
        )
        return 1

    try:
        count_text = _git(root, "rev-list", "--count", f"{newest}..HEAD").strip()
        count: int | None = int(count_text)
    except (Unmeasurable, ValueError):
        # The comparison is still a fact without the count, so it is reported with the
        # count named as unread rather than withheld.
        count = None
    reach = (
        f"{count} commit(s) on HEAD are not in {newest}"
        if count is not None
        else f"the commit count between {newest} and HEAD could not be read"
    )
    print(
        f"A RELEASE IS OWED: the tree declares {declared} and the newest tag is {newest} "
        f"-- {reach}, so that work has merged and reaches no host until it is published. "
        f"Cut the release: bump to the version you mean to ship, merge it, then "
        f"`git tag -a v{declared} -m \"emrg v{declared}\"` and push the tag (the tag is the "
        f"only trigger of build-release.yml). Run `scripts/check-release-tag.py v{declared}` "
        f"first, and `scripts/check-release-published.py v{declared}` afterwards."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A tree that declares a version no tag carries owes a release.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the tree to read (default: the checkout this script lives in)",
    )
    args = parser.parse_args(argv)
    return check(args.root.resolve())


if __name__ == "__main__":
    sys.exit(main())
