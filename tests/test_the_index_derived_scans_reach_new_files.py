"""A file that is not in the index is outside every index-derived guard.

The class
---------
Guards here decide what to read by asking git for the *tracked* files (`git ls-files`):
`test_script_decode_is_locale_independent.py` (this module's neighbour),
`test_cmd_crlf.py`, `test_conflict_markers.py`, `test_doc_counts.py`,
`test_command_position_contexts.py`, `test_no_duplicate_sources.py`,
`test_git_read_verbs_shape.py`, `scripts/check-doc-count.py` behind them, and others of the
same shape — the list is illustrative, because the property belongs to the *instrument*
rather than to any one file. That scope is deliberate — an index cannot go stale, a
hand-written directory list can — and it has one consequence that is easy to pay for: **a
new file that is not in the index is read by none of those rules, silently, and the run
still says pass.**

Measured 2026-09-18, cycle cyc20260918-105223 — this is the incident, not a worry:

* a new test file (`tests/test_relative_target_escape.py`) was written in a worktree;
* the full suite was run there: **3119 passed, 18 skipped**;
* the branch was pushed, and CI failed four minutes later on
  `test_every_text_mode_subprocess_pins_its_encoding`, naming a call *in that file*
  (`tests/test_relative_target_escape.py:121 subprocess.run(...) has no encoding=`);
* the fix was one keyword argument, and the local suite had been green both before and
  after it.

The local verdict was not wrong about the tree it read. It was **about a different tree**:
the file existed on disk but not in the index, so the rule that would have flagged it
never opened it. A green run is evidence about what the guards read, and nothing else.

Why this is a guard and not a habit
-----------------------------------
"remember to `git add` a new test file before trusting the suite" is prose, and it has
now been written down twice (session memory, and a previous cycle's lesson) without
preventing a second occurrence — which is the definition of a rule that should be
mechanised. What is mechanised here is the *question the green run answers*: while a
first-party `.py` file is untracked, this module fails, so the next local run cannot
report a verdict over source the class guards are unable to see. The remedy is one
command, and the message says which one.

Scope: derived, never listed
----------------------------
The report covers the top-level directories that hold **tracked** first-party Python —
`emrg/`, `packaging/`, `scripts/`, `tests/` as measured today — and that set is read from
the index at run time rather than written down here, for the same reason the guards it
protects derive their own scope from the index. A scratch file in the repository *root*
is deliberately not reported: its top-level component is the file itself, so it is outside
every directory the guards read, and it is where this repo's own throwaway scripts have
historically been left (`tmp_*.py`, see the session hygiene note).

What this does **not** catch, stated so the green is not over-read: the question is
membership in the index, not freshness of it. A file that is staged and then edited — the
`AM` state `git status --porcelain` shows — is listed, so it is scanned by the rules, and
those rules read the *worktree* content. What they read is then what you are about to
commit only after a second `git add`; that is a different question, and no guard here
answers it.

Two controls, because each half of the claim can be false separately
-------------------------------------------------------------------
* `test_a_scratch_file_in_the_root_is_not_reported...` — the *filter* discriminates:
  changing a path's directory changes the verdict, so "nothing is ever reported" cannot
  be what makes the guard green.
* `test_the_scan_reads_the_index_and_the_ignore_rules` — the *instrument* reads the index
  and honours `.gitignore`, measured in a real temporary repository: a staged file is
  invisible to it (that is the defect), an untracked non-ignored file is listed (that is
  the report), and an ignored file is silent (which is what keeps the deliberately
  gitignored scratch trees from firing).

Without the second control, the first test could be satisfied by a scan that lists
nothing at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendored trees: their `.py` files are third-party, are never staged by hand, and
# `node_modules` only exists after `npm install` — including them would make this
# report's reach depend on whether a developer had installed the GUI.
_VENDORED_DIRS = {"node_modules", "site-packages", ".venv", "venv", "dist-info"}


def _git(*args: str, cwd: Path) -> str:
    """Run git in `cwd` and return stdout, decoded as UTF-8 explicitly.

    The pin is not decoration: this module sits next to the one whose subject is the
    locale-codec class, and `git ls-files -z` emits raw UTF-8 path bytes.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {proc.stderr}"
    return proc.stdout


def _first_party(paths: list[str]) -> list[str]:
    """Drop paths inside a vendored tree."""
    return [
        rel
        for rel in paths
        if rel.endswith(".py") and not _VENDORED_DIRS & set(Path(rel).parts)
    ]


def _tracked_python(cwd: Path = REPO_ROOT) -> list[str]:
    return _first_party(_git("ls-files", "-z", "*.py", cwd=cwd).split("\0"))


def _untracked_python(cwd: Path = REPO_ROOT) -> list[str]:
    """Non-ignored, unstaged `.py` files — the ones no index-derived guard can see."""
    raw = _git("ls-files", "-z", "--others", "--exclude-standard", cwd=cwd)
    return _first_party(raw.split("\0"))


def _scanned_roots(cwd: Path = REPO_ROOT) -> set[str]:
    """The top-level directories that hold tracked first-party Python."""
    return {rel.split("/")[0] for rel in _tracked_python(cwd) if "/" in rel}


def _outside_every_guard(paths: list[str], roots: set[str]) -> list[str]:
    """The paths that live where the index-derived guards read. Pure, so it is testable."""
    return [
        rel
        for rel in paths
        if "/" in rel and rel.split("/")[0] in roots and not _VENDORED_DIRS & set(Path(rel).parts)
    ]


def test_no_first_party_python_file_is_left_out_of_the_index() -> None:
    """The guard: nothing the index-derived rules read may be sitting unstaged.

    Failing here does not mean the new file is wrong — it means **no local run has yet
    read it**. `git add` it (the fix the message asks for), or move it out of the
    directories the guards cover.
    """
    roots = _scanned_roots()
    unreached = sorted(_outside_every_guard(_untracked_python(), roots))
    assert not unreached, (
        "these files exist on disk but not in the index, so every index-derived scan "
        f"(encoding, CRLF, conflict markers, doc counts, duplicate sources) skips them: "
        f"{unreached}. A green local suite says nothing about them - measured 2026-09-18: "
        "a new test file was run, pushed, and failed CI on a rule that never opened it. "
        "Run `git add <file>` (staging is enough - `git ls-files` lists staged files) and "
        "re-run, or move the file outside the directories those scans read."
    )


def test_the_scope_is_derived_from_the_index_and_is_not_empty() -> None:
    """Vacuity control for the guard above: if the roots came back empty, nothing fires."""
    roots = _scanned_roots()
    assert len(roots) >= 2, (
        f"the index reported tracked Python in {sorted(roots) or 'no directory at all'} - "
        "the reach of this report is derived from that set, so an empty or singleton set "
        "makes the guard above green by construction rather than by fact"
    )
    assert "tests" in roots, (
        f"this module's own directory is missing from the derived roots {sorted(roots)} - "
        "the derivation is not reading what it claims to read"
    )


@pytest.mark.parametrize(
    "rel",
    [
        "tests/test_brand_new.py",
        "tests/nested/deeper/test_brand_new.py",
        "emrg/tools/scratch.py",
    ],
)
def test_a_file_in_a_scanned_directory_is_reported(rel: str) -> None:
    assert _outside_every_guard([rel], {"tests", "emrg", "scripts", "packaging"}) == [rel]


@pytest.mark.parametrize(
    "rel",
    [
        "tmp_scratch.py",  # repository root: outside every directory the guards read
        "docs/example.py",  # a directory with no tracked Python
        "emrg/gui/node_modules/left_pad.py",  # vendored
        "notes.txt",  # not Python at all
    ],
)
def test_a_file_outside_the_scanned_directories_is_not_reported(rel: str) -> None:
    assert _outside_every_guard([rel], {"tests", "emrg", "scripts", "packaging"}) == []


def test_the_scan_reads_the_index_and_the_ignore_rules(tmp_path: Path) -> None:
    """The instrument, measured in a real repository: staged, unstaged, ignored.

    Three properties, each of which decides whether the guard above means anything:

    * a **staged** file is not listed — this is the defect being reported (the file is on
      disk, the guards cannot see it), and it is the reason the remedy is `git add`;
    * an **unstaged, non-ignored** file is listed — otherwise the report could never fire;
    * an **ignored** file is silent — otherwise the gitignored scratch trees this repo's
      own tests create would fail every run.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "staged.py").write_text("STAGED = True\n", encoding="utf-8")
    (sub / "listed.py").write_text("LISTED = True\n", encoding="utf-8")
    (sub / "ignored.py").write_text("IGNORED = True\n", encoding="utf-8")
    (tmp_path / "root_scratch.py").write_text("ROOT = True\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("sub/ignored.py\n", encoding="utf-8")

    _git("init", "-q", cwd=tmp_path)
    _git("add", "sub/staged.py", cwd=tmp_path)

    assert _untracked_python(tmp_path) == ["root_scratch.py", "sub/listed.py"]

    roots = _scanned_roots(tmp_path)
    assert roots == {"sub"}, f"the derived roots came from somewhere else: {roots}"
    assert _outside_every_guard(_untracked_python(tmp_path), roots) == ["sub/listed.py"]

    # And the remedy the failure message asks for, measured rather than asserted: staging
    # that same file silences the report for it, without disturbing the root scratch file.
    _git("add", "sub/listed.py", cwd=tmp_path)
    assert _outside_every_guard(_untracked_python(tmp_path), roots) == []
