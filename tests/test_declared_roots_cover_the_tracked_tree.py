"""The roots the guards declare are every Python package this repo tracks.

Why this file exists
--------------------
Two guards declare, by hand, the top-level roots they sweep -- `FIRST_PARTY_TREE` in
`tests/test_guard_scan_scope_pairing.py` (the scope tables' members) and `SWEPT` in
`tests/test_walk_skips_read_their_root.py` (the tree a walk-skip reading covers). Each justified
its four with the same sentence -- *every top-level directory this checkout holds Python in is
one of these four* -- and **neither held it**: a fifth package arrives swept by nobody, and both
sweeps stayed green while no reading covered it. The sentence is written here in the form it had;
both files now say **tracks** instead, which is the claim this reading can hold (see below), and
quoting the old wording is the only place it still appears.

The claim's subject is the **index**, not the disk. A disk walk would be red on a clean checkout: a
gitignored tree holds throwaway Python -- a session's `tmp/`, build output -- that the index does
not, and both *whether* such a tree exists and how much it holds are functions of what some cycle
happens to be doing. So this paragraph names the rule and leaves the enumeration to the control
below, which builds both cases and asserts them: `git ls-files` names exactly the four, and a fifth
package arrives *tracked* -- the same reasoning `tests/test_documented_scripts_exist.py` gives for
reading the index rather than the tree. (The counts that stood here -- a directory total and a
throwaway-script tally -- were the class this repository forbids: *a derived number is never written
where a guard can measure it*. Measured 2026-10-10, they were already false, and differently on each
host that read them, which is the rot such a sentence is guaranteed to suffer.)

The reading is driven in both directions below: the control builds a scratch repo where a
gitignored directory holds Python (which must not be counted) and a fifth tracked package holds
Python (which must be), so a predicate that returned a constant -- or that followed the disk --
is red here rather than silent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_conflict_markers import _split_ls_files
from tests.test_guard_scan_scope_pairing import FIRST_PARTY_TREE
from tests.test_walk_skips_read_their_root import SWEPT

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The guards that declare the roots they sweep, and the constant each declares them in.
#: **Imported rather than restated**, so this file holds no second copy of the list free to
#: disagree with the one a guard actually sweeps: a renamed constant is an ImportError here,
#: which is loud, where a copied literal would go stale in silence.
DECLARATIONS = {
    "tests/test_guard_scan_scope_pairing.py": FIRST_PARTY_TREE,
    "tests/test_walk_skips_read_their_root.py": SWEPT,
}


def _tracked_python_roots(root: Path = REPO_ROOT) -> set[str]:
    """Every top-level directory `root` tracks Python in.

    `git ls-files` rather than a walk, for the reason in the module docstring: the disk holds
    Python in gitignored directories, and the claim is about what this repo *has*. A root-level
    `.py` names no directory and is not counted -- the claim is about packages.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
        # git emits index paths as UTF-8; the default text mode would decode them with the
        # *locale* encoding (cp1252 on the windows-2025 runner) and mangle non-ASCII names.
        encoding="utf-8",
        check=True,
    ).stdout
    # `_split_ls_files` normalises the separator git emits: POSIX gives `/`, Windows gives `\`,
    # so splitting the raw line would read the whole path as one component on the Windows leg.
    return {name.split("/", 1)[0] for name in _split_ls_files(listed) if "/" in name}


def test_the_declared_roots_are_every_tracked_python_package():
    """Both declared root lists are, between them, the whole of this repo's tracked Python.

    Each guard is asked separately rather than compared to the other: the failure this pins is
    a list that has fallen behind the tree, and two lists that had fallen behind it *together*
    would agree with each other while still covering neither.
    """
    measured = _tracked_python_roots()
    assert measured, (
        "git ls-files listed no Python under this checkout, so this reading has no subject -- "
        "an empty measurement is not a covered tree."
    )
    for relative, declared in DECLARATIONS.items():
        assert set(declared) == measured, (
            f"{relative} declares that it sweeps {sorted(declared)}, but this repo tracks "
            f"Python in {sorted(measured)}. A top-level package outside the declared list is "
            "swept by neither guard and read by neither, in silence: admit it to both constants "
            "or the coverage claim in their docstrings is prose."
        )


def test_the_reading_names_the_tracked_tree_and_not_the_disk(tmp_path):
    """The control: on a repo this test builds, the index decides and the disk does not.

    Both wrong readings are here. A gitignored directory holding Python is the shape the disk
    shows and the index does not -- counting it invents a package this repo does not have. A
    fifth tracked package is the divergence the claim is about -- missing it is how a list falls
    behind the tree.
    """
    package = tmp_path / "emrg"
    package.mkdir()
    (package / "core.py").write_text("x = 1\n", encoding="utf-8")
    fifth = tmp_path / "tools"
    fifth.mkdir()
    (fifth / "helper.py").write_text("y = 2\n", encoding="utf-8")
    litter = tmp_path / ".emrg" / "tmp"
    litter.mkdir(parents=True)
    (litter / "throwaway.py").write_text("z = 3\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".emrg/\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)

    roots = _tracked_python_roots(tmp_path)
    assert "emrg" in roots, f"a tracked package was not read at all: {sorted(roots)}"
    assert "tools" in roots, (
        "a fifth tracked package holding Python is invisible to this reading, which is the "
        f"divergence it exists to catch: {sorted(roots)}"
    )
    assert ".emrg" not in roots, (
        "a gitignored directory was counted as a package this repo tracks -- the reading "
        f"followed the disk rather than the index: {sorted(roots)}"
    )
