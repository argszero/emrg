"""Check that no git conflict markers are present in source files.

Squash merges can silently commit conflict markers to master, causing
SyntaxError in Python files and broken docs in Markdown files.
This test scans every tracked text file and fails if any markers exist.

Scope (measured 2026-09-10, cycle `cyc20260910-222254`): the scan used to walk
`emrg/` only, so leftover markers in the tracked files *outside* `emrg/` were
invisible - including `Agent.md`, this repo's most-conflicted file. Driven with
markers appended to `README.md`, the old scan passed while the repo docs were
plainly broken; a tool the host runs only ever reported "OK" on the same tree.
The walk is now rooted at the repository and derived from `git ls-files`, so it
cannot silently miss a directory the way a hardcoded root does.

The scan set is **every tracked file**, not the `.py`/`.md` pair an earlier
version named (measured 2026-09-10, cycle `cyc20260911-011300`): that version's
own docstring promised "whatever git tracks is what gets scanned" while the
command was `git ls-files -- '*.py' '*.md'` - a Python/Markdown subset of the
index, leaving the whole tracked JavaScript/TypeScript/shell/workflow surface
unseen. A marker left in a tracked `.js`/`.ts`/`.yml` was invisible, and driven
with markers appended to `emrg/gui/preload.js` the guard passed green. Same
defect shape as the one this module exists to fix (a claim wider than the
implementation), so the set is now the index minus binary assets, and the
extensions are *not* enumerated: an allow-list has to be maintained, the index
does not.

⚠️ **The proportions here are stated as a shape, not as literal counts.** An
earlier version of this docstring pinned "85 of the 143" - the figure measured
when the fix was written. #1126 then added two tracked files and the sentence
became false while every test stayed green: stale prose that still reads as a
measurement, in a module whose whole subject is states nobody is looking at.
What is *durable* is the shape the guard actually asserts: the scan reaches the
repository root and covers far more than the `emrg/` package
(`test_scan_reaches_outside_the_emrg_package` requires a non-empty outside set,
an inside set, and `total > 2 * inside` - the `.py`/`.md`-only scope was
*mostly* outside because the package is Python, while the tracked tree as a
whole is majority `emrg/gui`, so a raw `outside > inside` would invert it and
would have been a false constraint). **Need the current figure? Measure it -
do not trust this paragraph.**
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFLICT_START = "<<<<<<<"
CONFLICT_END = ">>>>>>>"

# Tracked files git reports but which cannot be text: nothing to scan.
BINARY_SUFFIXES = (".png", ".ico", ".icns", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")


def _tracked_source_files() -> list[str]:
    """Repo-relative paths of every tracked file, via `git ls-files`.

    Using git's own index instead of a directory walk is the point of the fix:
    a walk rooted at `emrg/` was exactly the bug, and a walk rooted at the repo
    would still drift from "what is actually published" as soon as a new
    top-level directory appears. `git ls-files` is authoritative by
    construction. It also skips the untracked/ignored trees (`.venv/`,
    `node_modules/`, `dist/`, `.pytest_cache/`) for free, which is why the test
    stays fast.

    **No extension filter** (added cycle `cyc20260911-011300`): the first version
    of this fix passed `-- '*.py' '*.md'` while its own docstring promised
    "whatever git tracks is what gets scanned" - 153 of 451 tracked files, and a
    marker in a tracked `.js`/`.ts`/`.yml` was invisible (measured: markers
    appended to `emrg/gui/preload.js` left the guard green). An allow-list of
    extensions has to be maintained and goes false silently; the index does not.
    Binary assets are skipped by suffix in `_collect_source_files`, since "does
    this file contain a conflict marker" is a text question.

    ⚠️ git emits the index path with the *platform* separator: POSIX gives
    `tests/x.py`, Windows gives a backslash. Measured 2026-09-10 on the
    `windows-2025` CI runner: the first version of this fix failed there
    because the returned names carried backslashes while the assertions
    compared them against forward-slash paths.

    There are therefore **two** places a backslash can enter, and fixing only
    the first left CI red for a second round (measured 2026-09-10):

    1. the string git emits - handled by `_split_ls_files`;
    2. `str(Path.relative_to(...))`, which uses the platform separator too, so
       every consumer below must use `.as_posix()`.

    Both are pinned by `test_scanned_names_use_forward_slashes`, which drives
    the string-level split with Windows-shaped input and asserts on the form of
    the names actually collected.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        # Explicit UTF-8: git emits index paths as UTF-8 bytes, and the default
        # text mode would decode them with the *locale* encoding (cp1252 on the
        # windows-2025 CI runner), which mangles non-ASCII paths.
        encoding="utf-8",
        check=True,
    ).stdout
    return _split_ls_files(listed)


def _split_ls_files(listed: str) -> list[str]:
    """`git ls-files -z` output -> sorted repo-relative names, `/`-separated.

    Split out from `_tracked_source_files` so the separator normalisation can be
    driven directly with Windows-shaped input on any platform (see
    `test_scanned_names_use_forward_slashes`). Takes a string rather than
    running git, so the test needs no Windows checkout.
    """
    return sorted(name.replace("\\", "/") for name in listed.split("\0") if name)


def _collect_source_files() -> list[Path]:
    """Every tracked text file as an absolute path, skipping binary suffixes."""
    return [
        REPO_ROOT / name
        for name in _tracked_source_files()
        if not name.endswith(BINARY_SUFFIXES)
    ]


def test_scan_reaches_outside_the_emrg_package():
    """The scan must cover the whole tracked repo, not just `emrg/`.

    This is the regression guard for the defect itself. The old scan was rooted
    at `emrg/`, so it reported "pass" on a repo whose `README.md`/`Agent.md`
    plainly contained markers. Asserting on the *set of files scanned* (rather
    than only on the "no markers present" outcome) is what makes that failure
    mode detectable: a scan that inspects nothing is trivially marker-free, and
    a green marker-free scan is indistinguishable from a correct one unless the
    scope is pinned.

    The counts below are **computed, not pinned** — deliberately. The module
    docstring once stated "85 of the 143" as a literal measured figure and
    silently went stale when #1126 added two tracked files. The durable claim is
    a *shape*, so that is what is asserted; the failure message prints the
    current numbers, keeping them available without making a test depend on a
    value that grows every few commits.

    Note the shape is **not** `outside > inside`. That held while the scan set
    was the `.py`/`.md` pair (87 of 153 outside, because the `emrg/` package is
    Python) but inverts for the whole index (115 outside vs 336 inside, since
    `emrg/gui` is majority of the tree) - asserting it would have been a false
    constraint that the *correct* widening fails. What is durable is that both
    halves are non-empty and that the scan is the whole tree rather than one
    package's worth of it.
    """
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _collect_source_files()}
    for required in (
        "Agent.md",
        "README.md",
        "README.cn.md",
        "MANIFESTO.md",
        "tests/test_conflict_markers.py",
        "scripts/check-doc-count.py",
        # A tracked file from each non-Python, non-Markdown family the index
        # carries. These are the files the `.py`/`.md`-only scope could not see,
        # so requiring them is what makes the widening itself the assertion.
        "emrg/gui/preload.js",
        "emrg/gui/renderer/src/lib/markdown.ts",
        ".github/workflows/test.yml",
        "packaging/make-installer.sh",
    ):
        assert required in scanned, (
            f"{required} is not scanned for conflict markers, so a leftover "
            f"conflict there would go unnoticed. Scanned {len(scanned)} files."
        )
    inside = [name for name in scanned if name.startswith("emrg/")]
    outside = [name for name in scanned if not name.startswith("emrg/")]
    assert outside, "nothing outside emrg/ is scanned - the scan is rooted too deep"
    assert inside, "nothing inside emrg/ is scanned - the scan is not the whole tree"

    # The contract of the widening, asserted without re-deriving the set (which
    # would only re-run the code under test): the scan must reach *file families*,
    # not a hand-maintained list. Re-introducing the defect (`-- '*.py' '*.md'`)
    # removes whole families and fails here.
    families = {Path(name).suffix for name in scanned}
    for family in (".py", ".md", ".js", ".ts", ".tsx", ".sh", ".yml", ".json"):
        assert family in families, (
            f"no tracked `{family}` file is scanned - the scan set is a list of "
            f"extensions rather than the git index, which is the defect this "
            f"guard was widened to fix. Scanned suffixes: {sorted(families)}"
        )


def test_scanned_names_use_forward_slashes():
    """Scanned paths must be normalised, not platform-dependent.

    Regression guard for a real CI failure: `git ls-files` emits the index path
    with the platform separator, so on `windows-2025` these names came back as
    `tests\\test_conflict_markers.py` and every forward-slash assertion in
    `test_scan_reaches_outside_the_emrg_package` failed - a guard that passes on
    the developer's machine and fails only in CI. Asserting on the *form* of
    the names, not just their presence, is what pins this: the names are paths
    into a repo whose canonical form is forward slashes.
    """
    # Windows-shaped output, verbatim as the failing CI job produced it.
    windows_out = "tests\\test_conflict_markers.py\0.github\\workflows\\README.md\0Agent.md\0"
    assert _split_ls_files(windows_out) == [
        ".github/workflows/README.md",
        "Agent.md",
        "tests/test_conflict_markers.py",
    ]

    # POSIX-shaped output must be unchanged by the same call.
    assert _split_ls_files("tests/x.py\0Agent.md\0") == ["Agent.md", "tests/x.py"]
    assert _split_ls_files("") == []

    # The second entry point for backslashes: str(Path.relative_to(...)).
    # On Windows this yields `tests\\x.py`, so every consumer has to use
    # .as_posix(). Skipping this check left CI red for a second round.
    assert (REPO_ROOT / "tests" / "test_conflict_markers.py").relative_to(
        REPO_ROOT
    ).as_posix() == "tests/test_conflict_markers.py"

    # And the real tree, on whatever platform this runs.
    names = _tracked_source_files()
    assert names, "no tracked source files returned"
    backslashed = [name for name in names if "\\" in name]
    assert not backslashed, (
        "scanned paths contain backslashes, so they are not normalised and will "
        f"not compare equal to repo-relative paths on every platform: {backslashed[:5]}"
    )
    assert all(not name.startswith("/") for name in names), "paths must be repo-relative"


def test_no_conflict_markers():
    """Every tracked `.py` and `.md` file must be free of git conflict markers."""
    violations: list[tuple[str, int, str]] = []
    files = _collect_source_files()
    assert files, "git ls-files returned no .py/.md files; the scan cannot be trusted"
    assert REPO_ROOT / "Agent.md" in files, (
        "Agent.md is missing from the scanned set - the scan is not reaching the "
        "repository root, which is the exact defect this test fixes"
    )
    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(CONFLICT_START) or stripped.startswith(CONFLICT_END):
                violations.append(
                    (file_path.relative_to(REPO_ROOT).as_posix(), lineno, stripped[:80])
                )

    if violations:
        msg_lines = [
            f"Found {len(violations)} git conflict marker(s) in source files:",
            "",
        ]
        for path, lineno, snippet in violations:
            msg_lines.append(f"  {path}:{lineno}  {snippet}")
        msg_lines.append("")
        msg_lines.append(
            "Resolve these conflict markers before merging. "
            "Hint: merge the conflicting branches properly, don't squash-merge "
            "conflicts blindly."
        )
        pytest.fail("\n".join(msg_lines))


def test_module_docstring_states_no_literal_tracked_count():
    """The docstring must not pin a literal count that grows every few commits.

    Measured history: this module's docstring said "85 of the 143 tracked
    `.py`/`.md` files live outside `emrg/`". #1126 added two tracked files
    (`scripts/check-node-test-count.py`, `tests/test_check_node_test_count.py`)
    and the sentence became false while every test stayed green - stale prose
    that still reads as a measurement, in a module whose entire subject is
    detecting states nobody is looking at.

    The rule is deliberately narrow: an un-dated `N [of M] tracked` claim is what
    goes stale. The durable replacement is the *shape* the guard already asserts,
    and a figure explicitly framed as historical is allowed - so this cannot
    creep into policing every number in the file.
    """
    docstring = (
        REPO_ROOT / "tests" / "test_conflict_markers.py"
    ).read_text(encoding="utf-8").split('"""')[1]
    pattern = r"\b\d+\s+(?:of\s+(?:the\s+)?\d+\s+)?tracked\b"

    stale = re.findall(pattern, docstring)
    assert not stale, (
        f"the module docstring pins a literal tracked-file count ({stale}), "
        "which goes false whenever a tracked file is added - while every test "
        "stays green. State the shape instead (most tracked sources live "
        "outside emrg/), or mark the figure as a historical measurement."
    )

    # Positive half: the sentence that actually went stale must be caught,
    # in both forms it appeared in.
    for sentence in (
        "so leftover markers in the *other* 85 tracked `.py`/`.md` files were invisible",
        "Measured 2026-09-10: 85 of the 143 tracked `.py`/`.md` files live outside `emrg/`.",
    ):
        assert re.findall(pattern, sentence), (
            f"the stale-prose matcher misses the form that motivated it: {sentence!r}"
        )

    # Negative half: the durable, cycle-framed form must stay allowed, so the
    # rule does not become a blanket ban on numbers.
    allowed = "When the fix was written that was 87 of 153 (66 inside); the count grows"
    assert not re.findall(pattern, allowed), (
        "the matcher rejects the durable form - the rule is too wide"
    )
