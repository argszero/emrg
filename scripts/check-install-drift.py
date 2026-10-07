#!/usr/bin/env python3
"""Content in the live install tree that no commit of this checkout has ever held.

The reading
-----------
The daemon runs the **installed** package, not this checkout: `_resolve_task_template`
(`emrg/server/scheduler.py`) returns `Path(__file__).parent / <template>`, and that
`__file__` is under `~/.emrg/install/source/`. So for every built-in task type the file
that actually renders is the install tree's copy, and the install tree is not a git
clone - it is a directory an installer wrote and an upgrade overwrites whole.

That makes "a file in the install tree whose content exists in no commit" a precise and
load-bearing reading: it is content somebody edited **in place**, outside version
control, that the next install/upgrade replaces with the release. Nothing measured this,
and the cost is measured too.

The incident (2026-10-07, cycle `cyc20261007-130240`)
-----------------------------------------------------
The host had asked, by name, for a rule to be written in `emrg/server/competition_prompt.md`
(host, 2026-10-06T10:40:46; quotable with `scripts/find-host-message.py`). Measured on
this host: the tracked file has no such rule, `git log --all -S` finds the rule's wording
in no ref, and the rule exists as a hand-appended `### 0.0` block in the install tree's
copy - the only home it ever had. Measured against the tag the install reports (`v0.3.7`),
the install's copy is the tag's bytes **plus** that block: every other prompt file in the
tree is byte-identical to `v0.3.7`, so the tree is `v0.3.7` with exactly one hand-edit.

Two independent confirmations that this is not a one-off:

* the tag `v0.3.8` (cut 2026-10-02, the release the tag chain is part-way through) carries
  **0** occurrences of the rule - so the upgrade that installs it does not merely fail to
  ship the host's mandate, it **deletes** it;
* the competition task's own rants of 2026-10-07 (`2026-10-07T09:44:31`, and
  `2026-10-07T12:10:07` for the same file) record that it tried to make its prompt edits
  in that tree and was refused, ending "the install tree copy is the one that takes
  effect, not the evolution-tree copy" - which is true, and is why the edit has to reach
  the source checkout and ship.

The question this answers
-------------------------
Per file the install tree and this checkout both carry, is the file's exact content
*somewhere* in this repository's history? If it is not, the file was written by hand and
the next upgrade destroys it.

Membership is asked of `git rev-list --all --objects` - the objects reachable from the
refs - rather than of a tag or of `HEAD`, because the edit is the thing being detected and
the version the install happens to be is not: an install one release behind is ordinary
and every one of its files still matches the commit that shipped it.

Exit codes
----------
``0``  every shared file's content is in this repository's history.
``1``  at least one is not; each is printed, with the path to edit instead.
``2``  the question could not be answered - no install tree to read, no checkout, `git`
failed, or **not one file was checked**, which would make a green verdict a reading over
an empty set.

Named limits
------------
* Only files the checkout **tracks** are checked: a file the repository has moved or
  deleted since the install was built is skipped (and counted), because its absence is a
  version difference rather than an edit, and it is removed by the upgrade either way.
* The membership test is over objects reachable from the refs, so content that was
  committed and then orphaned - a deleted branch, a rewritten commit - reads as an edit.
  That is a false positive in the strict sense and the safe direction: both cases mean
  "this content is not in the history you can see".
* It compares **content**, so an edit that happens to reproduce a committed blob exactly
  is not drift - by construction, since the committed copy is then a home for it.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import traceback
from pathlib import Path


#: Where the running daemon's package and prompt templates live. Not a git clone: the
#: installer writes it and an upgrade replaces it whole.
DEFAULT_INSTALL_DIR = "~/.emrg/install/source"


def _git_blob_sha(path: Path) -> str:
    """The blob hash `git hash-object <path>` would print, computed here.

    `hash-object` is one subprocess per file and this scan reads every file the two trees
    share; the hash is a documented construction (`sha1("blob <len>\\0" + bytes)`), and
    `tests/test_check_install_drift.py::test_the_hash_is_the_one_git_computes` holds this
    function to `git hash-object`'s answer on real bytes, so the shortcut cannot drift
    away from the thing it stands in for.
    """
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _git(root: Path, *argv: str) -> str:
    """Run `git -C <root> ...`, raising `RuntimeError` with git's own words."""
    proc = subprocess.run(
        ["git", "-C", str(root), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[0] if detail else f"git {' '.join(argv)} failed")
    return proc.stdout


def tracked_files(root: Path) -> set[str]:
    """Every path this checkout tracks, as posix-relative text."""
    return {name for name in _git(root, "ls-files", "-z").split("\0") if name}


def reachable_objects(root: Path) -> set[str]:
    """Every object id reachable from this checkout's refs.

    `<id> <path>` per line, but only the id is needed: the question is whether the blob
    exists in the history, not where it sat in it.
    """
    objects: set[str] = set()
    for line in _git(root, "rev-list", "--all", "--objects").splitlines():
        line = line.strip()
        if line:
            objects.add(line.split(" ", 1)[0])
    return objects


def scan(install_dir: Path, root: Path) -> tuple[list[tuple[str, int]], list[str], int]:
    """Return `(drifted, skipped, checked)` for the files the two trees share.

    `drifted` is `(path, size)` for a shared file whose content is in no reachable
    object; `skipped` is the install-relative paths this checkout no longer tracks;
    `checked` is how many files the question was actually asked about, which `main`
    refuses to report a green verdict over when it is 0.
    """
    tracked = tracked_files(root)
    objects = reachable_objects(root)
    drifted: list[tuple[str, int]] = []
    skipped: list[str] = []
    checked = 0
    for path in sorted(install_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(install_dir).as_posix()
        if relative not in tracked:
            skipped.append(relative)
            continue
        checked += 1
        if _git_blob_sha(path) not in objects:
            drifted.append((relative, path.stat().st_size))
    return drifted, skipped, checked


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table, while `2` is the code for "the question could not be answered". A caller
    that checks the code - which is how this family composes, one gate running another or
    reading its `rc` - would otherwise read a crash as a verdict. Byte-identical in every
    tool of the family, and `tests/test_a_crash_is_a_measurement_error.py` pins that.
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


def main(argv: list[str] | None = None) -> int:
    """Report the install tree's files whose content no commit of the checkout holds.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: the exit code the docstring states.
    """
    # The `tree:`/`install:` lines must reach a merged reader before any verdict, and
    # stdout is block-buffered through a pipe while stderr is not - the family rule,
    # pinned by `tests/test_guard_report.py`.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Files in the live install tree whose content no commit of this checkout "
            "holds: content an upgrade replaces."
        )
    )
    parser.add_argument(
        "--install-dir",
        default=DEFAULT_INSTALL_DIR,
        help=(
            "the installed tree the daemon runs from "
            f"(default: {DEFAULT_INSTALL_DIR})"
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="the checkout to ask (default: this script's own)",
    )
    args = parser.parse_args(argv)

    install_dir = Path(args.install_dir).expanduser()
    root = Path(args.root).expanduser().resolve() if args.root else Path(__file__).resolve().parent.parent

    # Both trees are named before any verdict: this tool answers about a pair, and a
    # reader who cannot see which pair was read cannot tell a clean install tree from a
    # checkout the caller was not in.
    print(f"tree: {root}")
    print(f"install: {install_dir}")

    if not install_dir.is_dir():
        print(
            f"could not measure: no install tree at {install_dir} - the question is about "
            "the installed copy, so there is nothing to compare",
            file=sys.stderr,
        )
        return 2
    if not (root / ".git").exists():
        print(
            f"could not measure: {root} is not a git checkout, so no history can say "
            "whether a file's content was ever committed",
            file=sys.stderr,
        )
        return 2
    try:
        drifted, skipped, checked = scan(install_dir, root)
    except RuntimeError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    if checked == 0:
        # The family's empty-set refusal (2026-10-06, `cyc20261006-165503`): a verdict of
        # `0` says the files were checked, and a tree sharing no path with this checkout
        # was not checked at all. Measured on an empty directory before the branch was
        # written: the green line, rc 0.
        print(
            f"could not measure: {install_dir} and {root} share no tracked path "
            f"({len(skipped)} install file(s) skipped as untracked here), so there was "
            "nothing to ask the question of - `0` says the files were checked, and none "
            "was.",
            file=sys.stderr,
        )
        return 2

    if not drifted:
        print(
            f"every file the install tree shares with this checkout holds content this "
            f"checkout's history has ({checked} file(s) checked, {len(skipped)} skipped "
            "as untracked here)"
        )
        return 0

    for relative, size in drifted:
        print(f"{relative} ({size} bytes) - no commit of this checkout holds this content")
    print(f"{len(drifted)} file(s) hold content no commit has; {checked} checked")
    print(
        "    this was edited in place. The install tree is not a git clone and an "
        "install/upgrade replaces it whole, so the next one destroys it: move the change "
        "into the source checkout and ship it, or it is lost."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(_entry())
