"""Guard: no committed file may duplicate the repo's own version sources.

Background (cycle cyc20260910-152337)
-------------------------------------
PR #1119's hardening commit (`c93d85c`) accidentally committed `.emrg-cmp2/` —
a scratch "next release" tree built by bumping a copy of the eight version
sources with `scripts/bump-version.py`. ~272 KB / 5518 lines of duplicated
`uv.lock`, `package-lock.json`, `pyproject.toml` and packaging scripts landed
on the branch, pinned at a version (`1.1.1`) that never ships.

Nothing caught it, and the reason is structural rather than accidental:

* **CI stayed green** — the duplication breaks no test.
* **The version guards use fixed paths** (`tests/test_version_sync.py`,
  `scripts/bump-version.py`'s ``VERSION_SOURCES``), so duplicate declarations
  under another directory never enter their scope. The pollution is *silent by
  construction*.
* **`.gitignore` line `.emrg` is an exact match**, so it does not cover a
  suffixed variant like `.emrg-cmp2` — the file stated the intent, the pattern
  did not enforce it. (Widened to `.emrg-*/` in the same cycle.)

An external contributor caught the leak by reading the diff. This test closes
the class so a reviewer is not the only line of defence.

Two orthogonal rules (either alone has a blind spot — #1119 review)
------------------------------------------------------------------
1. **Path** (`_duplicate_paths`): a tracked file that is not a canonical source
   but whose path *ends with* one — i.e. a copy of a version source at another
   location. Version-independent, so it catches a **bumped** copy.
2. **Content** (`_duplicate_declarations`): a tracked file outside the eight
   canonical paths that declares the repo's own version in a *declaration
   position*. Catches an **un-bumped** copy at a renamed path.

Version-matching alone was the original design and was ineffective against the
actual incident: `.emrg-cmp2/` was bumped to `1.1.1`, so no marker built from
the repo's current version could ever match it — a reconstructed leak shape
scored 0/8 detections. The path rule is what catches that shape (0 false
positives across 445 tracked files).

The content rule anchors on **declaration position**, not a bare substring
match: ``^__version__ = "…"$``, ``^version = "…"$``, ``^\\s*"version": "…",?$``,
and for the shell fallbacks a version terminated by shell syntax. A bare
substring test also matched *inline documentation* that quotes the forms —
which made the guard flag its own docstring whenever the repo version happened
to equal the quoted literal (the branch tree failed 1 test while CI, which
checks out the merge tree, was green).

Scope note: detection uses ``git ls-files``, so it runs against what would
actually be committed, and skips untracked scratch trees during local work.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
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


def _duplicate_paths(tracked: Iterable[str]) -> list[str]:
    """Rule 1 — tracked files that are a copy of a canonical source elsewhere.

    ``.emrg-cmp2/uv.lock`` ends with ``uv.lock``, so it is a copy of a version
    source at a non-canonical path. Content is irrelevant, which is the point:
    the real leak had been bumped to a different version.

    No legitimate tree nests a file whose path ends with one of the eight
    canonical relative paths (verified: 0 hits across the tracked tree).
    """
    return [
        rel
        for rel in tracked
        if rel not in CANONICAL_SOURCES
        and any(rel.endswith(canonical) for canonical in CANONICAL_SOURCES)
    ]


def _declaration_patterns(version: str) -> tuple[re.Pattern[str], ...]:
    """Rule 2 — the declaration forms, each anchored to a *declaration position*.

    Enumerated from the real sources so a duplicate is caught regardless of
    which kind of source it copies:

    * ``emrg/__init__.py``           ``__version__ = "X.Y.Z"``
    * ``pyproject.toml`` / ``uv.lock`` ``version = "X.Y.Z"``
    * ``emrg/gui/package*.json``     ``"version": "X.Y.Z",``
    * ``packaging/*.sh``             ``|| echo X.Y.Z`` / ``|| echo "X.Y.Z" > …``

    The anchors are what keep documentation quotes out: a prose line that
    *mentions* ``__version__ = "X.Y.Z"`` mid-sentence is not a declaration,
    while a real copy declares it on a line of its own. Without this, the
    guard flagged its own docstring whenever the repo version equalled the
    literal it quoted.

    Consequently documentation must show the **shape** (``X.Y.Z``), never a
    concrete repository version: a tracked file embedding the repo's current
    version in declaration form is indistinguishable from a duplicate — which
    is exactly what this guard exists to catch. (This is not hypothetical: the
    first version of this docstring quoted the concrete literals and made the
    guard fail on its own file.)
    """
    v = re.escape(version)
    return (
        re.compile(rf'^__version__ = "{v}"\s*$', re.MULTILINE),
        re.compile(rf'^version = "{v}"\s*$', re.MULTILINE),
        re.compile(rf'^\s*"version": "{v}",?\s*$', re.MULTILINE),
        # Shell fallbacks live mid-line, so anchor on the operator's position
        # and terminator instead: a real `||` operator is preceded by
        # whitespace and its version is followed by shell syntax (`)`, `>`, or
        # a space), whereas markdown inline code glues the operator to a
        # backtick. A backtick is deliberately not accepted.
        re.compile(rf'(?<=\s)\|\| echo "?{v}"?[)> ]'),
    )


def _duplicate_declarations(
    root: Path, tracked: Iterable[str], version: str
) -> list[str]:
    """Rule 2 — non-canonical files declaring the repo's own version."""
    patterns = _declaration_patterns(version)
    duplicates: list[str] = []
    for rel in tracked:
        if rel in CANONICAL_SOURCES:
            continue
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary, unreadable, or already removed
        if any(p.search(text) for p in patterns):
            duplicates.append(rel)
    return duplicates


def _find_leaks(root: Path, tracked: Iterable[str], version: str) -> list[str]:
    """Union of both rules, sorted and de-duplicated."""
    tracked = list(tracked)
    return sorted(
        set(_duplicate_paths(tracked))
        | set(_duplicate_declarations(root, tracked, version))
    )


def test_no_duplicate_version_sources_are_tracked():
    """No tracked file outside the 8 canonical paths may copy a version source.

    ``tests/test_no_duplicate_sources.py`` is checked too — it is a normal
    tracked file, and naming the declaration forms is not a licence to contain
    one. (Its own docstring stays clean because the patterns are anchored.)
    """
    leaks = _find_leaks(REPO_ROOT, _tracked_files(), _base_version())
    assert not leaks, (
        "these tracked files duplicate a version source outside the 8 canonical "
        "paths — almost certainly a scratch tree committed by accident (see "
        "cycle cyc20260910-152337, PR #1119 `.emrg-cmp2/`):\n"
        + "\n".join(leaks)
        + "\n\nRemove with `git rm -r <dir>`; if the directory is a local "
        "verification artifact, add its pattern to .gitignore so `git add -A` "
        "cannot sweep it in again."
    )


def test_path_rule_catches_a_bumped_copy_whatever_the_version():
    """The real incident: the copy had been bumped, so content matching missed it.

    Pure check — no files needed, because the path rule is version-independent
    by construction. This is the regression test for the original design flaw
    (a reconstructed `.emrg-cmp2` at 1.1.1 scored 0/8 detections).
    """
    tracked = [
        ".emrg-cmp2/uv.lock",
        ".emrg-cmp2/emrg/__init__.py",
        ".emrg-cmp2/packaging/make-installer.sh",
        ".emrg-cmp2/emrg/gui/package-lock.json",
    ]
    assert _duplicate_paths(tracked) == tracked

    # ...and it must not fire on the canonical paths themselves or on
    # plausible non-source neighbours.
    assert _duplicate_paths(list(CANONICAL_SOURCES)) == []
    assert _duplicate_paths(
        ["emrg/gui/renderer/package.json", "emrg/client/__init__.py", "README.md"]
    ) == []


def test_content_rule_catches_an_unbumped_copy_at_a_renamed_path(tmp_path):
    """Stated blind spot of the path rule: a copy at a path that does not end
    with a canonical relative path. Un-bumped, the content rule still catches it."""
    rel = "scratch/versions-snapshot.txt"
    (tmp_path / "scratch").mkdir()
    (tmp_path / rel).write_text('__version__ = "0.2.93"\n', encoding="utf-8")
    assert _find_leaks(tmp_path, [rel], "0.2.93") == [rel]


def test_content_rule_ignores_inline_documentation_of_the_forms(tmp_path):
    """Regression: the guard used to flag its own docstring.

    A bare substring test matched prose that merely *names* a declaration form,
    so whenever the repo version equalled the quoted literal the guard failed
    on its own file (the branch tree: 1 failed / 1267 passed, while CI — which
    checks out the merge tree — was green). Anchored patterns must ignore it.
    """
    rel = "docs/notes.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / rel).write_text(
        "The eight sources declare versions like:\n"
        '* ``emrg/__init__.py``          ``__version__ = "0.2.93"``\n'
        '* ``pyproject.toml``/``uv.lock`` ``version = "0.2.93"``\n'
        '* ``emrg/gui/package*.json``     ``"version": "0.2.93"``\n'
        '* ``packaging/*.sh``             ``|| echo 0.2.93`` / ``|| echo "0.2.93"``\n',
        encoding="utf-8",
    )
    assert _find_leaks(tmp_path, [rel], "0.2.93") == []


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

