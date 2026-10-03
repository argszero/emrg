"""`glob` names the directories it could not list — measured on this host.

`Path.glob` **swallows** the `PermissionError` a directory it cannot open raises: the
entries inside simply never appear, nothing is raised, and the tool answers confidently
over a tree it never finished reading. Measured 2026-10-04 (`cyc20261004-022954`) with
the real tool, over a tree holding two `.py` files with the second under `locked/`:

| tree | the tool answered |
|---|---|
| `locked/` listable | `Found 1 matches for '**/*.py' in <tree>` |
| `locked/` chmod 000 | `Found 1 matches for '**/*.py' in <tree>` — same answer, second file gone |
| `workdir = locked/` | `No files matched pattern '*.py' in <tree>/locked` |

The third row is the sharpest: a **confident zero** about a directory the tool never
managed to read. The first two are the quieter half — a partial answer that looks
complete, with nothing to say which part of the tree was unread.

So the answer gains the same kind of line it already gains for entries its own filter
dropped (`test_glob_names_what_it_skipped.py`): both branches end by naming the
directories that could not be opened, and nothing else changes.

The other half of this file is the **false hole**: a directory the search never means to
enter, or one the pattern merely names and that is not there, must not be reported as
coverage lost. That is why the probe asks `_skip_reason` and why it starts at the
pattern's literal base rather than at the search root.
"""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

from emrg.tools.glob_tool import GlobTool


def _run(**kwargs):
    args = {"intent": "measure what the search could not read"}
    args.update(kwargs)
    return asyncio.run(GlobTool().execute(args))


BLIND = re.compile(r"\[(\d+) director(?:y|ies) could not be listed: ([^\]]+)\]")


def _blind(content: str) -> tuple[int, list[str]] | None:
    """The unlistable-directories note as `(count, names)`, or `None` when absent."""
    match = BLIND.search(content)
    if not match:
        return None
    names = match.group(2).split(" — ")[0]
    return int(match.group(1)), [name.strip() for name in names.split(",")]


def _mentions(content: str) -> bool:
    return "could not be listed" in content


#: `chmod 000` does not stop root, so this whole section is vacuous there.
_not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so nothing here could be unlistable",
)
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX directory permissions; on Windows the shape does not exist to measure",
)


@pytest.fixture
def blocked_tree():
    """A tree whose `locked/` holds the only other `.py` file, unlistable for the run.

    The permission is restored before the temporary directory is removed — a `chmod
    000` directory makes the fixture's own cleanup fail, which would turn this into a
    test that errors on teardown rather than one that measures the tool.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "open").mkdir()
        (root / "open" / "a.py").write_text("x = 1\n")
        (root / "locked").mkdir()
        (root / "locked" / "secret.py").write_text("y = 2\n")
        os.chmod(root / "locked", 0o000)
        try:
            yield root
        finally:
            os.chmod(root / "locked", 0o755)


class TestAnUnreadableDirectoryIsNamed:
    """The hole is what the answer was missing, so both branches have to carry it."""

    @_posix_only
    @_not_root
    def test_a_match_under_an_unlistable_directory_is_not_silence(self, blocked_tree):
        """Measured 2026-10-04 (`cyc20261004-022954`) on master `a3d0a5ea`.

        The tree holds two `.py` files and the tool has always said `Found 1 matches`
        with `error=False` — a count a reader has no way to know is short. It still
        cannot read the directory, so what changes is only that the answer says so.
        """
        result = _run(pattern="**/*.py", workdir=str(blocked_tree))

        assert not result.error
        assert "Found 1 matches" in result.content
        assert _blind(result.content) == (1, ["locked"])

    @_posix_only
    def test_the_same_tree_listable_names_no_hole(self, blocked_tree):
        """The control, on the same tree: it is the permission that changes the answer.

        Without this leg a tool that named "could not be listed" unconditionally would
        pass the test above while claiming a hole over every readable tree.
        """
        os.chmod(blocked_tree / "locked", 0o755)
        result = _run(pattern="**/*.py", workdir=str(blocked_tree))

        assert "Found 2 matches" in result.content
        assert not _mentions(result.content)

    @_posix_only
    @_not_root
    def test_the_workdir_itself_unlistable_is_named(self, blocked_tree):
        """The sharpest row: a confident zero about a directory never read.

        `glob *.py` where the workdir is the unlistable directory answered `No files
        matched pattern '*.py' in <workdir>` — indistinguishable from an empty project.
        """
        result = _run(pattern="*.py", workdir=str(blocked_tree / "locked"))

        assert not result.error
        assert "No files matched" in result.content
        assert _blind(result.content) == (1, ["."]), (
            "the workdir is spelled the way a reader can go to; `.` is the same "
            "spelling the sibling fix in grep_tool.py uses"
        )

    @_posix_only
    @_not_root
    def test_a_non_recursive_pattern_names_its_own_base(self, blocked_tree):
        """`locked/*.py` reads exactly `locked`, so that directory is the whole hole."""
        result = _run(pattern="locked/*.py", workdir=str(blocked_tree))

        assert _blind(result.content) == (1, ["locked"])

    @_posix_only
    @_not_root
    def test_the_found_answer_carries_it_too(self, blocked_tree):
        """Finding something is not a reason to stop saying part of the tree was unread."""
        (blocked_tree / "open" / "b.py").write_text("z = 3\n")
        result = _run(pattern="**/*.py", workdir=str(blocked_tree))

        assert "Found 2 matches" in result.content
        assert _blind(result.content) == (1, ["locked"])


class TestAHoleIsNotClaimedWhereThereIsNone:
    """The other direction, and the reason the probe is not a bare `os.walk`."""

    def test_a_base_that_is_not_there_is_not_a_hole(self, tmp_path):
        """A pattern naming a directory that does not exist matches nothing.

        That is an answer, not coverage lost — and the two are the same `OSError` from
        the walk's point of view, which is why only `PermissionError` counts.
        """
        for pattern in ("nope/*.py", "nope/**/*.py", "**/*.py"):
            result = _run(pattern=pattern, workdir=str(tmp_path))
            assert not result.error
            assert not _mentions(result.content), pattern

    def test_a_base_that_is_a_file_is_not_a_hole(self, tmp_path):
        (tmp_path / "afile").write_text("x\n")
        result = _run(pattern="afile/*.py", workdir=str(tmp_path))

        assert not _mentions(result.content)

    def test_a_base_outside_the_patterns_base_is_out_of_scope(self, tmp_path):
        """`src/**/*.ts` never enters `vendor/`, so a locked `vendor/` is not its hole.

        The same tree asked a question that *does* reach `vendor/` reports it, so the
        leg above is about the probe's scope and not about the probe being disconnected.
        """
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("x\n")
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "v.ts").write_text("x\n")
        os.chmod(tmp_path / "vendor", 0o000)
        try:
            out_of_scope = _run(pattern="src/**/*.ts", workdir=str(tmp_path))
            in_scope = _run(pattern="**/*.ts", workdir=str(tmp_path))
        finally:
            os.chmod(tmp_path / "vendor", 0o755)

        assert not _mentions(out_of_scope.content)
        assert _blind(in_scope.content) == (1, ["vendor"])

    @_posix_only
    @_not_root
    def test_a_directory_skipped_by_design_is_not_a_hole(self, tmp_path):
        """`node_modules` is a directory this tool never means to enter.

        Reporting it would be a false hole, and it is the shape that makes the probe
        and the filter need **one** answer to "which directories are in scope".
        """
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.py").write_text("x\n")
        os.chmod(tmp_path / "node_modules", 0o000)
        try:
            result = _run(pattern="**/*.py", workdir=str(tmp_path))
        finally:
            os.chmod(tmp_path / "node_modules", 0o755)

        assert not _mentions(result.content)
        # ...and the same directory, were it in scope, WOULD be reported — checked by
        # asking the shared predicate both ways rather than by a second tree.
        assert GlobTool._skip_reason(tmp_path / "node_modules" / "pkg.py", tmp_path) == "node_modules"
        assert GlobTool._skip_reason(tmp_path / "src" / "a.py", tmp_path) is None

    @_posix_only
    @_not_root
    def test_a_hidden_directory_is_not_a_hole_either(self, tmp_path):
        """The other half of the same predicate: a dotted directory is skipped by design."""
        (tmp_path / ".cache").mkdir()
        (tmp_path / ".cache" / "c.py").write_text("x\n")
        os.chmod(tmp_path / ".cache", 0o000)
        try:
            result = _run(pattern="**/*.py", workdir=str(tmp_path))
        finally:
            os.chmod(tmp_path / ".cache", 0o755)

        assert not _mentions(result.content)
        assert GlobTool._skip_reason(tmp_path / ".cache" / "c.py", tmp_path) == ".cache"

    def test_a_literal_pattern_lists_nothing_to_miss(self, tmp_path):
        """A pattern with no wildcard in it lists no directory at all.

        `Path.glob("src")` stats the one path it names, so no contents can be missed
        and no hole can exist — asking here would invent one.
        """
        (tmp_path / "src").mkdir()
        os.chmod(tmp_path / "src", 0o000)
        try:
            result = _run(pattern="src", workdir=str(tmp_path))
        finally:
            os.chmod(tmp_path / "src", 0o755)

        assert "Found 1 matches" in result.content
        assert not _mentions(result.content)


class TestTheBaseIsTheLiteralPrefix:
    """`_literal_base` is what keeps the probe inside the pattern's own reach."""

    def test_the_literal_prefix_stops_at_the_first_wildcard(self):
        base = GlobTool._literal_base
        assert base("**/*.py") == "."
        assert base("*.md") == "."
        assert base("src/**/*.ts") == "src"
        assert base("emrg/tools/*.py") == os.path.join("emrg", "tools")

    @_posix_only
    def test_a_readable_tree_reports_no_directories(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x\n")
        assert GlobTool._unlistable_dirs(tmp_path, "**/*.py") == []

    @_posix_only
    @_not_root
    def test_a_locked_tree_reports_itself_once(self, tmp_path):
        """Deduplicated: one directory is one line, however many entries are under it."""
        (tmp_path / "locked").mkdir()
        (tmp_path / "locked" / "a").mkdir()
        (tmp_path / "locked" / "b").mkdir()
        os.chmod(tmp_path / "locked", 0o000)
        try:
            assert GlobTool._unlistable_dirs(tmp_path, "**/*.py") == ["locked"]
        finally:
            os.chmod(tmp_path / "locked", 0o755)
