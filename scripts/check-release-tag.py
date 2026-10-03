#!/usr/bin/env python3
"""Does the release tag name the version the tagged tree declares?

The release pipeline derives everything it *says about* a release from the tag,
and everything *inside* the artifacts from the tree -- so the two halves of an
upgrade meet exactly at the version string, and nothing compared them.

Measured 2026-09-27 (cycle `cyc20260927-103600`) on master `4ca419f8`:

* `emrg/__init__.py:3` is the declaration `scripts/bump-version.py --check` names
  as its base and `tests/test_version_sync.py` reads with the same regex used here;
* `packaging/build-runtime.sh:134` writes the package's `version.txt` with
  `python -c "import emrg; ...; print(e.__version__)"` -- the *built tree's*
  version, never the tag's;
* `build-release.yml`'s `verify-tag` job checked the tag's **object type** only
  (the annotated-tag rule from the `v0.2.97` incident). `grep -n GITHUB_REF_NAME
  .github/workflows/*.yml` finds no comparison against any version, the `release`
  job's completeness check derives its expected asset set from the files the run
  itself built (`artifacts/*`) rather than from the tag, and `grep -rn
  "__version__" tests/` finds no test that puts a ref name beside the version.

So `git tag -a v0.4.0` cut on a tree that says `0.3.3` builds, signs, notarizes
and publishes a release *called* `v0.4.0` whose `version.txt` says `0.3.3`, and
the GitHub side looks entirely correct (published, not draft, not prerelease,
asset set matching the build). The cost lands on every host that installs it:
`emrg/server/upgrade.py::UpgradeManager.tick` compares the installed `version.txt`
with `target.lstrip("v")`, so the upgrade session is re-triggered every tick
forever, and no later release repairs it -- the comparison fails again the moment
the new version installs. That is the shape issue #1598 measured by hand (23
attempts, 132 minutes, byte-identical failures); the backoff #1600 added slows
such a loop (30 min base, 6 h ceiling) but never ends one.

The reading is deliberately the chain's own expression, `tag.lstrip("v")`: if the
guard and `UpgradeManager.tick` disagreed about what a tag means, the guard would
be measuring a release nobody can install.

What this answers, and what it does not. The tag against the tree's *declared*
version. That all eight version declarations agree with each other is a different
question with its own guards (`tests/test_version_sync.py`, host-side
`scripts/bump-version.py --check`) and is deliberately not re-answered here: one
declaration is enough to answer this one, and a second reader of the same eight
files would be a second thing to keep in step. The tag's *form* (annotated,
`v`-prefixed) belongs to `build-release.yml`'s annotated check and
`tests/test_release_tag_form.py`; a tag without the `v` prefix is therefore not
refused here.

Exit codes, the family's contract:

    0  the tag names the version the tagged tree declares
    1  a fault: the two name different versions -- both are printed, and the
       remedy is to either re-tag the right commit or bump the tree
    2  not measurable: no version declaration could be read (missing file, no
       anchored declaration, more than one, an empty read). Never 0 -- a guard
       that passes because it could not read its subject is the defect this one
       exists to catch, and the CI step must fail on it too.

Usage:

    uv run --no-sync python3 scripts/check-release-tag.py v0.3.3
    python3 scripts/check-release-tag.py v0.3.3 --root /path/to/checkout
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The tree that is read when `--root` is not given: the checkout this script
#: lives in, so the CI step reads the tagged tree and the host reads their own.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the version is declared. The same file and the same shape
#: `tests/test_version_sync.py` reads as its base version, anchored at the start
#: of a line so a prose mention of `__version__ = "x"` is not a declaration.
VERSION_SOURCE = Path("emrg") / "__init__.py"
_DECLARATION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def declared_version(root: Path) -> str | None:
    """The tree's declared version, or None when it cannot be read as one.

    None is *not* an empty version: it is the absence of a reading, and every
    caller must treat it as unmeasurable rather than as agreement. Two
    declarations are also None -- the file names two versions, so it does not
    name one -- and `tests/test_no_duplicate_sources.py` owns that question.
    """
    try:
        text = (root / VERSION_SOURCE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = _DECLARATION.findall(text)
    if len(found) != 1:
        return None
    version = found[0].strip()
    return version or None


def check(tag: str, root: Path) -> int:
    """Print the reading and return the exit code."""
    print(f"tree: {root}")
    print(f"tag: {tag}")
    declared = declared_version(root)
    if declared is None:
        print(
            f"not measurable: no single `__version__ = \"...\"` declaration could "
            f"be read from {VERSION_SOURCE} -- the tag cannot be compared with a "
            f"version this tree does not declare"
        )
        return 2
    print(f"declared: {declared} ({VERSION_SOURCE})")
    # The chain's own expression (`UpgradeManager.tick`), not a normalisation
    # invented here: the guard must measure the comparison a host will make.
    named = tag.lstrip("v")
    if named != declared:
        print(
            f"MISMATCH: tag {tag} names version {named!r}, the tree declares "
            f"{declared!r} -- the release would publish artifacts whose version.txt "
            f"says {declared} under a release named {tag}, and every host that "
            f"installs it re-triggers the upgrade session every tick (the chain "
            f"compares version.txt with the tag). Re-tag the commit that declares "
            f"{named}, or bump this tree to {named} before tagging."
        )
        return 1
    print(f"OK: tag {tag} names the version this tree declares ({declared})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A release tag must name the version the tagged tree declares.",
    )
    parser.add_argument("tag", help="the release tag, e.g. v0.3.3")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the tree to read (default: the checkout this script lives in)",
    )
    args = parser.parse_args(argv)
    return check(args.tag, args.root.resolve())


if __name__ == "__main__":
    sys.exit(main())
