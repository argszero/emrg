"""Guard: no committed file may duplicate the repo's own version sources.

Background (cycle cyc20260910-150729)
-------------------------------------
PR #1119's hardening commit accidentally committed `.emrg-cmp2/` — a scratch
"next release" tree built by bumping a copy of the eight version sources with
`scripts/bump-version.py`. ~272 KB / 5518 lines of duplicated `uv.lock`,
`package-lock.json`, `pyproject.toml` and packaging scripts landed on the
branch, pinned at a version (`1.1.1`) that never ships.

Nothing caught it, and the reason is structural rather than accidental:

* **CI stayed green** — the duplication breaks no test.
* **The version guards use fixed paths** (`tests/test_version_sync.py`,
  `scripts/bump-version.py`'s ``VERSION_SOURCES``), so duplicate declarations
  under another directory never enter their scope. The pollution is *silent by
  construction*.
* **`.gitignore` line `.emrg` is an exact match**, so it does not cover a
  suffixed variant like `.emrg-cmp2` — the file stated the intent, the pattern
  did not enforce it. (Widened to `.emrg-*/` in the same cycle; this test is the
  backstop for any *other* directory name someone invents.)

An external contributor caught the leak by reading the diff. This test closes
the class so a reviewer is not the only line of defence: it asks the *content*
question — does any tracked file declare the repo's own version at a path that
is not one of the eight real sources?

Scope note: detection uses ``git ls-files``, so it runs against what would
actually be committed, and skips untracked scratch trees during local work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The eight real version declarations (8 declarations across 8 files; the GUI
# lockfile carries two of them). Mirrors scripts/bump-version.py VERSION_SOURCES
# and tests/test_version_sync.py — kept literal here so this test does not
# import a module that itself has to be located first.
CANONICAL_SOURCES = {
    "emrg/__init__.py",
    "pyproject.toml",
    "emrg/gui/package.json",
    "emrg/gui/package-lock.json",
    "uv.lock",
    "packaging/build-runtime.sh",
    "packaging/make-installer.sh",
    "packaging/make-run-installer.sh",
}


def _tracked_files() -> list[str]:
    """Every path git would commit, or skip if git is unavailable.

    ``conftest.py`` puts a real git on PATH for hosts that lack one; if the
    call still fails (shallow or packed-away index in an exotic checkout),
    skip rather than report a false failure — the guard's job is to catch a
    leaked duplicate, not to police the environment.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=str(REPO_ROOT), text=True, stderr=subprocess.PIPE
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git ls-files unavailable: {exc}")
    return out.splitlines()


def _base_version() -> str:
    content = (REPO_ROOT / "emrg" / "__init__.py").read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise AssertionError("no __version__ in emrg/__init__.py")


def _declaration_markers(version: str) -> tuple[str, ...]:
    """Every textual form the eight sources use to declare ``version``.

    Enumerated from the real files so a duplicate is caught regardless of which
    kind of source it copies — the original leak was a copy of *all* of them:

    * ``emrg/__init__.py``          ``__version__ = "0.2.93"``
    * ``pyproject.toml``/``uv.lock`` ``version = "0.2.93"``
    * ``emrg/gui/package*.json``     ``"version": "0.2.93"``
    * ``packaging/*.sh``             ``|| echo 0.2.93`` / ``|| echo "0.2.93"``

    Anchoring on the exact version string keeps this precise: a false positive
    would require an unrelated file to restate the repo's own version verbatim.
    """
    return (
        f'__version__ = "{version}"',
        f'version = "{version}"',
        f'"version": "{version}"',
        f"|| echo {version}",
        f'|| echo "{version}"',
    )


def test_no_duplicate_version_sources_are_tracked():
    """No tracked file outside the 8 canonical paths may declare the version.

    Anchored on content, not on directory names: a leak is 'this file restates
    the repo's own version', whatever the path happens to be called. That is
    what makes it robust to the next scratch-tree naming scheme.
    """
    version = _base_version()
    markers = _declaration_markers(version)
    duplicates: list[str] = []

    for rel in _tracked_files():
        if rel in CANONICAL_SOURCES:
            continue
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — cannot hold a text declaration
        if any(marker in text for marker in markers):
            duplicates.append(rel)

    assert not duplicates, (
        f"these tracked files duplicate a version declaration (={version}) "
        "outside the 8 canonical sources — almost certainly a scratch tree that "
        "was committed by accident (see cycle cyc20260910-150729, PR #1119 "
        "`.emrg-cmp2/`):\n"
        + "\n".join(sorted(duplicates))
        + "\n\nRemove with `git rm -r <dir>`; if the directory is a local "
        "verification artifact, add its pattern to .gitignore so `git add -A` "
        "cannot sweep it in again."
    )


def test_canonical_sources_are_all_tracked():
    """Counterpart check: the allowlist must describe reality.

    Prevents the guard above from silently weakening if a real version source is
    renamed or moved — a missing entry would make every file look like a
    duplicate, but a *stale* entry (a path that no longer exists) would let a
    genuine source escape the allowlist and be reported as pollution.
    """
    tracked = set(_tracked_files())
    missing = sorted(CANONICAL_SOURCES - tracked)
    assert not missing, (
        "canonical version sources are no longer tracked — update "
        f"CANONICAL_SOURCES (and scripts/bump-version.py VERSION_SOURCES): {missing}"
    )
