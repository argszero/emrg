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

It is a **screen for a host, not a CI gate.** On an unreleased master it is red by
design, so wiring it into CI would make the release cadence a permanently failing check
and teach its readers to ignore it. Its role is the one `bump-version.py --check` and
`check-release-tag.py` already play: the command run before cutting a release.

Exit codes, the family's contract:

    0  nothing is owed: the declared version is the newest tag's version
    1  a release is owed: the tree declares a version no tag carries -- both versions
       and the commit count between them are printed
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

#: A release tag: three dotted numbers, optional `v`. Deliberately narrow -- the
#: version shape `scripts/bump-version.py` rewrites and `check-release-tag.py` accepts.
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


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


def tags(root: Path) -> tuple[list[tuple[tuple[int, int, int], str]], list[str]]:
    """Every release-shaped tag, newest last, plus the names that did not parse.

    Sorted by numeric parts, because string order puts `v0.3.9` after `v0.3.10`.
    """
    _require_a_work_tree_of_its_own(root)
    text = _git(root, "tag", "--list")
    parsed: list[tuple[tuple[int, int, int], str]] = []
    skipped: list[str] = []
    for name in (line.strip() for line in text.splitlines()):
        if not name:
            continue
        match = _TAG.match(name)
        if match is None:
            skipped.append(name)
            continue
        parsed.append((tuple(int(part) for part in match.groups()), name))
    parsed.sort()
    return parsed, skipped


def check(root: Path) -> int:
    """Print the reading and return the exit code."""
    print(f"tree: {root}")
    try:
        declared = declared_version(root)
        parsed, skipped = tags(root)
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
    if not parsed:
        print(f"tags: none version-shaped{skipped_note}")
        print(
            f"A RELEASE IS OWED: the tree declares {declared} and no tag carries it -- "
            f"the tag list was read and is empty of version tags, so nothing has been "
            f"published for this tree"
        )
        return 1

    newest = parsed[-1][1]
    print(f"tags: {len(parsed)} version-shaped, newest {newest}{skipped_note}")

    if declared == newest.lstrip("v"):
        print(f"OK: nothing is owed - the tree declares {declared} and {newest} carries it")
        return 0

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
