#!/usr/bin/env python3
"""Content in the live install tree that the history a release is built from never held.

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
copy - the only home it ever had. Re-measured against the tag the install reports (`v0.3.7`)
by the convention below (`cyc20261007-230326`), **2** of the 372 shared files differ from it
- `emrg/server/competition_prompt.md` and `emrg/server/prompts/vibe_check.j2`. This paragraph
said "exactly one hand-edit" when it was written, and the undercount is the defect: the second
file is the half the **judge** reads, so the reading that hid it left the host's mandate
enforceable in the round but not in the verdict (#1898). No count is restated here to be
trusted - the tool derives it, and the numbers above are what it answered.

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
Per file the install tree and this checkout both carry, is the file's content in the history
a **release is built from**? If it is not, the next install/upgrade destroys it: the file was
written by hand, or its only other home is a snapshot no release ships.

**Which convention "the content" means is asked of git, not decided here.** Git stores a
file's *cleaned* bytes, and cleaning is a property of the path: this repository pins
`*.cmd`/`*.bat`/`*.ps1` to LF blobs with a CRLF checkout (`.gitattributes`
`text eol=crlf`), and a host with `core.autocrlf=true` normalizes every text file. So the
id this tool compares is `git hash-object --path=<relative path> <file>`'s, which is the
same convention that produced the blobs in it. Hashing the raw bytes instead answers a
different question and calls a line-ending difference an edit - measured 2026-10-07
against both `core.autocrlf=true` and a `eol=crlf` attribute, and caught by `test-windows`
on this tool's first version (run `37588646750`).

That is also why the comparison is not a shortcut computed in-process: a raw
`sha1("blob <len>\\0" + bytes)` is faster (measured: 1.2s for this host's 372 shared files,
one subprocess each) but it is the wrong function.

Membership is asked of the objects reachable from the refs a **release is built from** -
`refs/heads/*`, `refs/remotes/*`, `refs/tags/*` - rather than of a tag or of `HEAD`, because
the edit is the thing being detected and the version the install happens to be is not: an
install one release behind is ordinary and every one of its files still matches the commit
that shipped it. It is deliberately **not** `--all`, which is a different question with a
different answer. That namespace also holds this machinery's own bookkeeping refs, and
`refs/emrg/rescue/*` above all: `scripts/recover-worktree.py` writes a snapshot there exactly
because a dirty tree held work that exists nowhere else. Content reaching only such a ref is
not history any release ships - which is the point, not a technicality - so counting it made
the reading silently weaker in the one case that matters. Measured: the install tree's
`emrg/server/prompts/vibe_check.j2` holds bytes pinned into
`refs/emrg/rescue/20261006T035349Z` and reachable from no branch or tag, and the tool
answered "1 file(s)" where the tag comparison finds 2, so the file whose live copy the next
upgrade really does destroy was the one it stayed silent about. Drift whose bytes survive
only in such a ref is therefore reported - and the report **names the ref**, because that is
where the edit is still recoverable from.

**Which release that is gets its own reading, because the one above cannot see it.** Everything
above is true of a tree five releases old - its files match the commits that shipped them - so
the green verdict is silent about the daemon rendering that release's rules rather than the
newest ones. Measured 2026-10-20: this host's install tree carries
`emrg/server/evolution_prompt.md`'s pre-narrowing blob (the one `v0.3.14`-`v0.3.16` ship), five
releases past the host's 2026-10-16 ruling that narrowed the abstention clause, and the reading
above answered `0`. Every evolution cycle that day read the superseded rule out of the prompt
the daemon rendered, and none of them could see it: nothing measured it. So a `behind:` line is
printed under **both** verdicts - which release this tree is, which release is newest, and the
paths that differ. Being behind is not drift (nothing is lost) and not clean (it is what runs),
so it is stated rather than scored, and the exit code stays the drift reading's alone.

The walk itself is asked with `--missing=print` and read for the objects it does list. A
clone can hold a ref whose history it cannot fully read: measured on this host 2026-10-08
(`cyc20261008-001036`), where `git rev-list --all --objects` exits 128 on `bad tree object
219ff46e...` and so does every narrower walk, `HEAD` alone included - the hole is in the
history, and every membership question here is about history. Left intolerant that is exit
`2` on the very host this tool exists for. Tolerating it can only *remove* a candidate, so a
file whose content sits under a missing tree reads as drift: the same false-positive
direction, and the same safe one, as content that was committed and then orphaned. The count
of absent objects is printed so that a narrowed set is never read as the whole one.

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
  deleted since the install was built is not drift by content, because its absence is a
  version difference rather than an edit. It is **not** removed by the upgrade, though, and
  this docstring said it was until the claim was measured: on 2026-10-17 a tree reporting
  `0.3.16` still carried `emrg/gui/renderer/js/` - 11 files deleted from the source on
  2026-08-27 and absent from that tag's own tree. So they are **named as leftovers**, with a
  count and their paths, and they stay out of the exit code: the verdict is about content no
  commit holds, and a leftover is a different fact - a stale tree that a reader (a cycle, an
  audit, a session) can still be answered from.
* The membership test is over objects reachable from the refs a release is built from, so
  content that was committed and then orphaned - a deleted branch, a rewritten commit -
  reads as an edit. That is a false positive in the strict sense and the safe direction:
  both cases mean "this content is not in the history you can see".
* A file whose content reaches **only** a bookkeeping ref (`refs/emrg/rescue/*`,
  `refs/stash`, `refs/emrg-tmp/*`, `refs/emrg-check/*`) is drift like any other, and its
  line names the ref it is still recoverable from - because "an upgrade will delete this"
  is true of it, and that ref is the only place the bytes are kept.
* Objects absent from this clone are not walked, and the count is printed when it is not
  zero. The set is therefore the history this clone can see, which is the only history it
  can promise a file is safe in.
* It compares **content, under git's own line-ending convention**: a file whose only
  difference from the committed blob is CRLF versus LF reads as *no* drift, because
  `hash-object --path=<rel>` cleans it exactly as `git add` did. That is deliberate - such
  a file holds nothing a human added, and reporting it would flood the reading on any host
  whose attributes or `autocrlf` transform line endings.
* An edit that happens to reproduce a committed blob exactly is not drift - by
  construction, since the committed copy is then a home for it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import traceback
from pathlib import Path


#: Where the running daemon's package and prompt templates live. Not a git clone: the
#: installer writes it and an upgrade replaces it whole.
DEFAULT_INSTALL_DIR = "~/.emrg/install/source"


def _git_stored_id(root: Path, path: Path, relative: str) -> str:
    """The blob id git assigns to `path`'s content **at this path in this checkout**.

    Asked of git rather than computed here, and the `--path` is the load-bearing half: it
    is what makes the answer the one the committed blobs were created with. Git stores a
    file's *cleaned* bytes, and what "cleaned" means is a property of the path - this
    repository pins `*.cmd`/`*.bat`/`*.ps1` to LF blobs with a CRLF checkout
    (`.gitattributes` `text eol=crlf`, because a LF-only `.cmd` is misparsed by cmd.exe),
    and a host with `core.autocrlf=true` normalizes every text file. Hashing the raw bytes
    instead would report every such file as drift on such a host - noise, which is how a
    reading stops being read.

    Measured 2026-10-07 (cycle `cyc20261007-182502`) on synthetic repositories: a CRLF file
    whose committed blob is LF answers the committed id through `hash-object
    --path=<rel>` under **both** `core.autocrlf=true` and `*.cmd text eol=crlf`, while the
    raw construction answers a different id under both. The first version of this tool used
    the raw construction and `test-windows` caught it (run `37588646750`).
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "hash-object", f"--path={relative}", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            detail[0] if detail else f"git hash-object failed for {relative}"
        )
    return proc.stdout.strip()


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


#: The spelling the release workflow gives a published version, and the only refs the
#: "which release is this install tree" question may be answered from.
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def release_tags(root: Path) -> list[str]:
    """Every release tag this clone carries, newest first.

    Version order, not creation order: `--sort=-v:refname` is git's own version sort, which
    puts `v0.3.10` above `v0.3.9` where a lexical sort would put it below - the wrong sort does
    not crash, it names a real release that is simply not the newest. A tag this clone does not
    have cannot be named, and that is reported rather than guessed (see `behind_release`).
    """
    listing = _git(root, "tag", "--sort=-v:refname", "--list", "v*")
    return [name for name in (line.strip() for line in listing.splitlines()) if RELEASE_TAG_RE.match(name)]


def release_tree(root: Path, ref: str) -> dict[str, str]:
    """`{path: blob id}` for one release ref, from a single `ls-tree`.

    One call for the whole tree rather than a `rev-parse` per path: the question compares
    every shared file at once, and this host's install tree shares ~380 of them, which at one
    process each is a second of subprocesses per release examined.
    """
    blobs: dict[str, str] = {}
    for line in _git(root, "ls-tree", "-r", ref).splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


def behind_release(
    install_dir: Path, root: Path, shared: list[tuple[Path, str]]
) -> tuple[str | None, str | None, list[str], int]:
    """`(newest tag, the tag this tree is, paths that differ from the newest, shared count)`.

    The question the rest of this tool cannot answer, and the reason it is here: `scan` asks
    whether a file's content is reachable from *any* ref a release is built from. An install
    tree several releases old passes that test with a green verdict, because the old bytes are
    in an older release's history - which is a release ref. Measured 2026-10-20 on this host:
    the install tree carries `evolution_prompt.md`'s pre-narrowing blob (`af6e4ae5`, the one
    `v0.3.14`-`v0.3.16` ship) while master and the five releases since carry the narrowed rule,
    and the other reading answered `0` - "every file ... holds content the refs a release is
    built from still reach". The daemon renders that file into every evolution cycle, so the
    reading that said "shipped" said it about a superseded rule.

    So the second question is: **which release is this tree, and is a newer one out?** It is
    answered for the tree as a whole rather than file by file, and that distinction is
    measured: attributing each file to "the newest release that still carries these bytes"
    names a *later* tag for every file the next release did not touch, so a v0.3.16 tree
    reported `v0.3.16` for one path and `v0.3.18` for another and named no version at all. The
    tree's version is the newest tag whose content it matches on every shared path, which is
    one fact about one directory.

    A tree matching no tag is `None` and is not this reading's fault to explain: content in no
    tag is the *other* reading's subject (a hand edit, which destroys on upgrade), and mixing
    the two in one line leaves a reader unable to tell "an upgrade brings the rest" from "an
    upgrade destroys this".

    Absent files are out of scope: this compares the paths both trees carry, so a file the
    newest release added is something an upgrade creates, not a lag in what this tree has.

    `None` for the newest tag is "this clone carries no release tag", which the caller must
    report as unmeasurable rather than as currency - `[]` with a tag is "measured, nothing lags".
    """
    if not shared:
        return None, None, [], 0
    tags = release_tags(root)
    if not tags:
        return None, None, [], len(shared)

    ids = {relative: _git_stored_id(root, path, relative) for path, relative in shared}
    # The tree's own version: the newest release it matches on every shared path. Walked from
    # the newest so the first match is the answer, and the walk stops there.
    install_release: str | None = None
    for tag in tags:
        blobs = release_tree(root, tag)
        if all(blobs.get(relative) == blob for relative, blob in ids.items()):
            install_release = tag
            break

    newest = tags[0]
    newest_blobs = release_tree(root, newest)
    differing = sorted(
        relative
        for relative, blob in ids.items()
        if newest_blobs.get(relative) != blob
    )
    return newest, install_release, differing, len(shared)


#: The ref namespaces a release is built from. Everything else a ref can live in
#: (`refs/emrg/rescue/*`, `refs/stash`, `refs/emrg-tmp/*`, `refs/emrg-check/*`, ...) is this
#: machinery's own bookkeeping, and a recovery snapshot above all: `scripts/recover-worktree.py`
#: writes one precisely where a dirty tree holds work that exists nowhere else, so nothing
#: there is history a release ships. Counting it as history is how a live hand-edit went
#: unreported (#1898).
SHIPPING_REF_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/tags/")


def _tolerant_objects(root: Path, *revisions: str) -> tuple[set[str], int]:
    """`(objects, missing)` reachable from `revisions`, tolerating objects this clone lacks.

    `<id> <path>` per line, but only the id is needed: the question is whether the blob
    exists in the history, not where it sat in it.

    `--missing=print` is what lets the walk finish. An object a clone does not have is
    printed as `?<id>` and stepped over, where the default (`--missing=error`) aborts the
    whole walk with exit 128 - measured 2026-10-08 (`cyc20261008-001036`) on this host, where
    `git rev-list --all --objects` dies on `bad tree object 219ff46e...` and so does every
    narrower walk including `HEAD` alone, because the hole is in the *history* and every
    membership question here is about history. Intolerant, that is exit `2` on the very host
    the tool serves, and a reading that can only say "could not measure" here measures
    nothing. `allow-any` finishes too but reports nothing, which is why the count is returned
    and printed: a reading that covers less than history must say so rather than leave a
    narrowed set to read as the whole one.
    """
    out = _git(root, "rev-list", *revisions, "--objects", "--missing=print")
    objects: set[str] = set()
    missing = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("?"):
            missing += 1
            continue
        objects.add(line.split(" ", 1)[0])
    return objects, missing


def shipping_objects(root: Path) -> tuple[set[str], int]:
    """The objects reachable from the refs a release is built from, and how many are absent."""
    return _tolerant_objects(root, "--branches", "--tags", "--remotes")


def non_shipping_refs(root: Path) -> list[str]:
    """Every ref a release is **not** built from, in git's own order."""
    names = (line.strip() for line in _git(root, "for-each-ref", "--format=%(refname)").splitlines())
    return [name for name in names if name and not name.startswith(SHIPPING_REF_PREFIXES)]


def refs_holding(root: Path, refs: list[str], blobs: set[str]) -> dict[str, list[str]]:
    """Which of `refs` reach each of `blobs`, stopping as soon as every blob is named.

    This is the half that makes the report actionable: "the next upgrade deletes this" is
    only useful with somewhere to recover the bytes from, and a bookkeeping ref is where
    they survive. A ref that cannot be walked at all is skipped rather than failing the
    reading, since it can only make a report less specific, never a verdict wrong.
    """
    found: dict[str, list[str]] = {}
    remaining = set(blobs)
    for ref in refs:
        if not remaining:
            break
        try:
            objects, _ = _tolerant_objects(root, ref)
        except RuntimeError:
            continue
        for blob in sorted(remaining):
            if blob in objects:
                found.setdefault(blob, []).append(ref)
                remaining.discard(blob)
    return found


def shared_paths(install_dir: Path, root: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """`(install path, install-relative path)` for the paths both trees carry, and the rest.

    The walk both readings start from: a path is comparable only when this checkout tracks it,
    because the id of the other side is asked of git at *that* path.
    """
    tracked = tracked_files(root)
    shared: list[tuple[Path, str]] = []
    skipped: list[str] = []
    for path in sorted(install_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(install_dir).as_posix()
        if relative not in tracked:
            skipped.append(relative)
            continue
        shared.append((path, relative))
    return shared, skipped


#: How many lagging paths a `behind:` line names before counting the rest, matching the
#: leftovers section's shape: enough to recognise the family, not a wall of paths.
BEHIND_SHOWN = 10


def _report_release(
    install_dir: Path,
    root: Path,
    newest: str | None,
    install_release: str | None,
    differing: list[str],
    checked: int,
) -> None:
    """Print which release this tree is, and whether a newer one exists.

    Printed under **both** verdicts, because it is a second question and not a verdict: a tree
    that lags is not drift (nothing here is lost) and is not clean either (the daemon renders
    it). Leaving it out of the green branch is the defect this function exists to fix - the
    green line said "a release's refs still reach this content", which is true of any older
    release and therefore said nothing about being current.
    """
    if newest is None:
        print(
            f"behind: could not measure which release {install_dir} is - this checkout "
            "carries no release tag, and the refs a release is built from cannot answer it "
            "(an older release's bytes are in them too)"
        )
        return
    if not differing:
        print(
            f"behind: none - {install_dir} carries what {newest} ships "
            f"({checked} shared file(s) compared)"
        )
        return

    version = install_release or "no single release"
    print(
        f"behind: {install_dir} is the {version} copy and the newest release is {newest}; "
        f"{len(differing)} of {checked} shared file(s) differ, so whatever the daemon renders "
        "from this tree is that release's copy"
    )
    for relative in differing[:BEHIND_SHOWN]:
        print(f"    {relative}")
    if len(differing) > BEHIND_SHOWN:
        print(f"    ... and {len(differing) - BEHIND_SHOWN} more")
    print(
        f"    remedy: an upgrade to {newest} brings these. Until then a rule read from this "
        "tree is the installed release's rule, not the newest one's."
    )


def scan(
    install_dir: Path, root: Path
) -> tuple[list[tuple[str, int, str]], list[str], int, int]:
    """Return `(drifted, skipped, checked, missing)` for the files the two trees share.

    `drifted` is `(path, size, note)` for a shared file whose content no shipped object
    holds - the note saying why, and naming the bookkeeping ref it is still recoverable
    from when one holds it; `skipped` is the install-relative paths this checkout no longer
    tracks; `checked` is how many files the question was actually asked about, which `main`
    refuses to report a green verdict over when it is 0; `missing` is how many objects the
    membership walk found absent from this clone, which `main` prints when it is not zero.
    """
    shared, skipped = shared_paths(install_dir, root)
    shipped, missing = shipping_objects(root)

    drifted: list[tuple[str, int, str]] = []
    for path, relative in shared:
        blob = _git_stored_id(root, path, relative)
        if blob not in shipped:
            drifted.append((relative, path.stat().st_size, blob))

    holders = (
        refs_holding(root, non_shipping_refs(root), {blob for _, _, blob in drifted})
        if drifted
        else {}
    )
    reported: list[tuple[str, int, str]] = []
    for relative, size, blob in drifted:
        refs = holders.get(blob, [])
        if refs:
            note = (
                "held only by "
                + ", ".join(refs)
                + ", which no release builds from - the bytes are still there, in a "
                "snapshot of uncommitted work"
            )
        else:
            note = "no commit of this checkout holds this content"
        reported.append((relative, size, note))
    return reported, skipped, len(shared), missing


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
    """Report the install tree's files whose content no shipped commit of the checkout holds.

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
            "Files in the live install tree whose content the refs a release is built from "
            "do not hold (content an upgrade replaces), and which release that tree is "
            "(content an upgrade brings)."
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
        drifted, skipped, checked, missing = scan(install_dir, root)
        shared, _ = shared_paths(install_dir, root)
        newest, install_release, differing, _ = behind_release(install_dir, root, shared)
    except RuntimeError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    if missing:
        # A property of the reading and not of the files, so it is printed whatever the
        # verdict: the membership set is the history this clone can see, which is smaller
        # than the history its refs name whenever an object is absent.
        print(
            f"membership: {missing} object(s) reachable from the refs a release is built "
            "from are absent from this checkout and were not walked; content under a "
            "missing tree reads as drift"
        )

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

    caches = [path for path in skipped if "__pycache__/" in path]
    leftover = [path for path in skipped if "__pycache__/" not in path]
    if leftover:
        # The limit this tool used to state as a rationale ("its absence is a version
        # difference rather than an edit, and it is removed by the upgrade either way") was
        # measured false on 2026-10-17: a tree reporting `0.3.16` still carried
        # `emrg/gui/renderer/js/` — 11 files deleted from the source on 2026-08-27 and
        # absent from that tag's own tree. An upgrade copies a release in and removes
        # nothing, so what it leaves behind is exactly the class a reader of this tree keeps
        # finding, and silence about it is the defect.
        shown = leftover[:10]
        print(
            f"leftovers: {len(leftover)} file(s) under {install_dir} are paths this checkout "
            "no longer tracks. An install/upgrade copies a release in and removes nothing, so "
            "a file a later release dropped stays here and anything that reads this tree can "
            "still read it."
        )
        for relative in shown:
            print(f"    {relative}")
        if len(leftover) > len(shown):
            print(f"    ... and {len(leftover) - len(shown)} more")
        if caches:
            print(
                f"    ({len(caches)} interpreter cache file(s) under __pycache__ are not "
                "listed: no reader executes them as source)"
            )
        print(
            "    remedy: reinstall from the release, or delete these, so no reader (a cycle, "
            "an audit, a session) is answering a question about code no build ships. An "
            "upgrade alone will not do it."
        )

    if not drifted:
        print(
            f"every file the install tree shares with this checkout holds content the "
            f"refs a release is built from still reach ({checked} file(s) checked, "
            f"{len(leftover)} named as leftovers above, "
            f"{len(caches)} interpreter cache file(s) not listed)"
        )
        _report_release(install_dir, root, newest, install_release, differing, checked)
        return 0


    for relative, size, note in drifted:
        print(f"{relative} ({size} bytes) - {note}")
    print(f"{len(drifted)} file(s) hold content no shipped commit has; {checked} checked")
    print(
        "    this was edited in place. The install tree is not a git clone and an "
        "install/upgrade replaces it whole, so the next one destroys it: move the change "
        "into the source checkout and ship it, or it is lost."
    )
    _report_release(install_dir, root, newest, install_release, differing, checked)
    return 1


if __name__ == "__main__":
    raise SystemExit(_entry())
