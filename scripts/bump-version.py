#!/usr/bin/env python3
"""Bump every EMRG version source in one shot.

Why this exists (rant 2026-09-10T14:07:19, release v0.2.94)
----------------------------------------------------------
Bumping the release version is a mechanical edit across **8 version
declarations in 8 files**. It has gone wrong repeatedly:

* #408 — `emrg/gui/package.json` was forgotten; the release shipped with a
  mismatched GUI version and had to be deleted, retagged and rebuilt.
* #1065 — `emrg/gui/package-lock.json` (root + ``packages[""]``) had no
  guard at all, so a missed bump produced no error.
* v0.2.94 — ``uv run`` silently rewrote every registry URL in ``uv.lock``
  to a local mirror, turning a 1-line version bump into 556 lines of
  environment noise that had to be reverted by hand.

``tests/test_version_sync.py`` catches drift *after* the edit, and
``tests/test_doc_counts.py`` guards the docs — but neither tells you
*what to edit*, and neither can stop ``uv`` from churning ``uv.lock``.
This script is the missing host-side counterpart: it edits all 8 sources
deterministically, refuses to run if any anchor is missing, and never
touches ``uv.lock`` beyond the ``name = "emrg"`` version line.

Usage
-----
    python3 scripts/bump-version.py 0.2.94           # bump every source
    python3 scripts/bump-version.py 0.2.94 --dry-run # preview, write nothing
    python3 scripts/bump-version.py --check          # report drift, no writes

``--check`` closes the CI/host symmetry loop: the host can self-verify
before pushing, instead of discovering drift after a wasted build round.

After bumping, the release flow is (see Agent.md "Releasing"):

    1. ``uv run --no-sync pytest tests/test_version_sync.py -q``  # guard
    2. commit on ``feature/release-vX.Y.Z`` → PR → 3 LGTMs → merge
    3. ``git tag vX.Y.Z && git push origin vX.Y.Z``  # triggers Build Release
    4. confirm all 4 platform legs green in ``build-release.yml``

Note: always run the test suite with ``uv run --no-sync`` after a bump so
``uv`` does not regenerate ``uv.lock`` against a mirror.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The single source of truth: emrg/__init__.py's __version__.
BASE_FILE = "emrg/__init__.py"
BASE_PATTERN = re.compile(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"')

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


# (relative path, anchor regex, expected occurrence count)
#
# Each regex must match *exactly* the version declaration it is meant to
# rewrite — never a dependency's version. ``package-lock.json`` legitimately
# carries the app version twice (root field + ``packages[""]``) while the rest
# of its 300+ ``"version"`` fields belong to dependencies, so the count is
# asserted rather than assumed.
VERSION_SOURCES: list[tuple[str, re.Pattern[str], int]] = [
    (BASE_FILE, BASE_PATTERN, 1),
    ("pyproject.toml", re.compile(r'^version\s*=\s*"\d+\.\d+\.\d+"', re.M), 1),
    ("emrg/gui/package.json", re.compile(r'"version"\s*:\s*"\d+\.\d+\.\d+"'), 1),
    # Anchored on the preceding "name": "emrg-gui" line. package-lock.json
    # holds 334 "version" fields (one per dependency) — a bare version matcher
    # would rewrite all of them, so the app version must be identified by the
    # lockfile's own package name instead of by position or indentation.
    (
        "emrg/gui/package-lock.json",
        re.compile(r'"name": "emrg-gui",\n\s*"version": "\d+\.\d+\.\d+"'),
        2,
    ),
    ("uv.lock", re.compile(r'name = "emrg"\nversion = "\d+\.\d+\.\d+"'), 1),
    ("packaging/build-runtime.sh", re.compile(r'\|\| echo "?\d+\.\d+\.\d+"?'), 1),
    ("packaging/make-installer.sh", re.compile(r'\|\| echo "?\d+\.\d+\.\d+"?'), 1),
    ("packaging/make-run-installer.sh", re.compile(r'\|\| echo "?\d+\.\d+\.\d+"?'), 1),
]

# Distinct files touched (8 sources live in 8 files; package-lock holds two).
FILE_COUNT = len({path for path, _, _ in VERSION_SOURCES})


class BumpError(RuntimeError):
    """Raised when an anchor is missing or ambiguous — fail loud, never guess."""


def read_current_version(root: Path | None = None) -> str:
    """Return the authoritative version from emrg/__init__.py."""
    root = REPO_ROOT if root is None else root
    text = (root / BASE_FILE).read_text(encoding="utf-8")
    m = BASE_PATTERN.search(text)
    if not m:
        raise BumpError(f"no __version__ found in {BASE_FILE}")
    return m.group(1)


def _find_versions(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Extract every semver embedded in each match of ``pattern``."""
    found: list[str] = []
    for m in pattern.finditer(text):
        for ver in re.findall(r"\d+\.\d+\.\d+", m.group(0)):
            found.append(ver)
    return found


def check(expected: str | None = None, root: Path | None = None) -> list[str]:
    """Return a list of drift descriptions (empty when everything agrees).

    ``root`` is resolved at call time (never bound as a default argument), so
    callers — including tests — can redirect the whole module at one point.
    """
    root = REPO_ROOT if root is None else root
    base = expected or read_current_version(root)
    problems: list[str] = []
    for rel, pattern, count in VERSION_SOURCES:
        path = root / rel
        if not path.exists():
            problems.append(f"{rel}: MISSING FILE")
            continue
        text = path.read_text(encoding="utf-8")
        versions = _find_versions(text, pattern)
        if len(versions) != count:
            problems.append(
                f"{rel}: expected {count} version declaration(s) matching the "
                f"anchor, found {len(versions)}"
            )
            continue
        wrong = [v for v in versions if v != base]
        if wrong:
            problems.append(f"{rel}: {', '.join(sorted(set(wrong)))} != {base}")
    return problems


def bump(
    new_version: str, root: Path | None = None, dry_run: bool = False
) -> list[str]:
    """Rewrite every version source to ``new_version``; return changed paths.

    Only the version literal inside each matched anchor is replaced, so the
    surrounding formatting (quote style, trailing ``> "$DIST/version.txt"``,
    lockfile structure) is preserved byte-for-byte.
    """
    root = REPO_ROOT if root is None else root
    if not SEMVER.match(new_version):
        raise BumpError(f"not a semver x.y.z: {new_version!r}")

    old_version = read_current_version(root)
    if old_version == new_version:
        return []

    changed: list[str] = []
    for rel, pattern, count in VERSION_SOURCES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        versions = _find_versions(text, pattern)
        if len(versions) != count:
            raise BumpError(
                f"{rel}: expected {count} version declaration(s) matching the "
                f"anchor, found {len(versions)} — the file layout changed; "
                f"update VERSION_SOURCES in scripts/bump-version.py"
            )
        stale = [v for v in versions if v != old_version]
        if stale:
            raise BumpError(
                f"{rel}: contains {sorted(set(stale))} but {BASE_FILE} says "
                f"{old_version} — sources are already inconsistent; run "
                f"`python3 scripts/bump-version.py --check` first"
            )

        def _swap(m: re.Match[str]) -> str:
            return m.group(0).replace(old_version, new_version)

        new_text, n = pattern.subn(_swap, text)
        if n != count:  # defensive: subn must agree with finditer
            raise BumpError(f"{rel}: replaced {n} occurrence(s), expected {count}")
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
        changed.append(rel)

    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump every EMRG version source (8 declarations in 8 files).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----", 1)[-1].strip(),
    )
    parser.add_argument("version", nargs="?", help="new version, x.y.z")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift across all sources without writing anything",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change, write nothing"
    )
    args = parser.parse_args(argv)

    try:
        current = read_current_version()
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if args.version and SEMVER.match(args.version):
            base = args.version
            print(f"checking all {FILE_COUNT} files against {base} …")
        else:
            base = current
            print(f"checking all {FILE_COUNT} files against {base} ({BASE_FILE}) …")
        problems = check(base)
        if problems:
            print(f"\n✗ {len(problems)} drift(s) found:")
            for p in problems:
                print(f"  - {p}")
            print("\nFix with: python3 scripts/bump-version.py <version>")
            return 1
        print(f"✓ all {len(VERSION_SOURCES)} version sources agree on {base}")
        return 0

    if not args.version:
        parser.error("a version is required unless --check is used")

    try:
        changed = bump(args.version, dry_run=args.dry_run)
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not changed:
        print(f"already at {args.version} — nothing to do")
        return 0

    verb = "would update" if args.dry_run else "updated"
    print(f"{verb} {args.version} (from {current}) — {len(changed)} files:")
    for rel in changed:
        print(f"  - {rel}")

    if args.dry_run:
        print("\n(dry run — no files written)")
        return 0

    print(
        "\nNext:\n"
        "  1. uv run --no-sync pytest tests/test_version_sync.py -q\n"
        f"  2. commit on feature/release-v{args.version} → PR → 3 LGTMs → merge\n"
        f"  3. git tag v{args.version} && git push origin v{args.version}\n"
        "  4. confirm all 4 platforms green in build-release.yml\n"
        "\nNote: use `uv run --no-sync` so uv does not regenerate uv.lock."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
