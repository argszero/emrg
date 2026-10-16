"""Tests for `scripts/check-markdown-table-pipes.py` -- the GFM table-pipe guard.

Issue #2324: the README's `/skills` row wrote a pipe inside a code span without
escaping it, so GFM parsed that row as four cells where its header has two and
the table broke there on github.com -- while the raw source still read as two
tidy columns. `README.cn.md` already carried the escaped form, so the English
README was the file that drifted.

These tests are in two halves, and the second is the one the issue asks for by
name: a **synthetic document where the guard must fire** and a **synthetic
document where it must stay silent**, so the reading is not a rule that reports
the healthy answer whatever it is given -- plus the verdict on the checkout this
suite lives in, which is the half that would have caught the drift.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-markdown-table-pipes.py"

#: An unescaped pipe, the same reading the guard makes: a backslash is the only
#: escape GFM offers inside a table row, and it works inside a code span too.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _load_guard():
    """Load the guard module so its pure readings can be driven directly."""
    spec = importlib.util.spec_from_file_location("_markdown_table_pipes_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(REPO_ROOT),
    )


def _doc(row: str) -> str:
    """A minimal two-column table whose one body row is `row`."""
    return f"| cmd | desc |\n|---|---|\n{row}\n"


def _cells(line: str) -> list[str]:
    """A table row's cells, split the way GFM splits it."""
    parts = _UNESCAPED_PIPE.split(line)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _is_delimiter_row(line: str) -> bool:
    """Whether `line` is a table's delimiter row -- the header is the line above."""
    if "|" not in line:
        return False
    cells = _UNESCAPED_PIPE.split(line)
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell.strip()) for cell in cells)


def _table_with(lines: list[str], needle: str) -> int:
    """The index of the header row of the table whose rows carry `needle`.

    Located by the needle rather than by the first header that matches, because
    the README has two tables headed `| Command | What it does |` and the defect
    lived in the second one.
    """
    for index, line in enumerate(lines):
        if not _is_delimiter_row(line) or index == 0:
            continue
        header = index - 1
        body = index + 1
        rows = [lines[header], lines[index]]
        while body < len(lines) and lines[body].strip() and "|" in lines[body]:
            rows.append(lines[body])
            body += 1
        if any(needle in row for row in rows):
            return header
    raise AssertionError(f"no table in README.md carries {needle!r}")


class TestItFires:
    def test_an_unescaped_pipe_in_a_code_span_is_reported(self) -> None:
        """The exact shape that shipped, reduced to one row."""
        guard = _load_guard()
        found = guard.findings_in_text(_doc("| `/skills` | `/skills available|install|update` |"))
        assert [line for line, _, _ in found] == [3], found
        assert found[0][1] == "/skills available|install|update", found

    def test_it_fires_through_the_script_with_a_finding_line_and_rc_one(self, tmp_path) -> None:
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "README.md").write_text(
            _doc("| `/skills` | `/skills available|install|update` |"), encoding="utf-8"
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "README.md:3" in proc.stdout, proc.stdout
        assert "unescaped `|`" in proc.stdout, proc.stdout

    def test_a_header_row_is_a_row_too(self) -> None:
        """A code span in the header is rendered as a cell like any other."""
        guard = _load_guard()
        text = "| cmd | `/a b|c` |\n|---|---|\n| x | y |\n"
        found = guard.findings_in_text(text)
        assert [line for line, _, _ in found] == [1], found


class TestItStaysSilent:
    def test_the_escaped_form_passes(self) -> None:
        """The control the fix produced, and the form README.cn.md already had."""
        guard = _load_guard()
        found = guard.findings_in_text(
            _doc(r"| `/skills` | `/skills available\|install\|update` |")
        )
        assert found == [], found

    def test_the_same_pipe_in_prose_outside_a_table_passes(self) -> None:
        """Measured to be common here (8 lines, 4 files) and harmless: prose is
        not split into cells, so the pipe is exactly what a reader sees."""
        guard = _load_guard()
        assert guard.findings_in_text("the key is `enabled = true|false` (default true)\n") == []

    def test_a_pipe_inside_a_fenced_block_is_not_a_table_row(self) -> None:
        """A documented example of a broken table must not red the rule."""
        guard = _load_guard()
        assert guard.findings_in_text("```\n| a | b|c |\n|---|---|\n```\n") == []

    def test_a_pipe_outside_a_code_span_is_not_this_rules_subject(self) -> None:
        """Stated limit, pinned so it is a decision rather than an omission."""
        guard = _load_guard()
        assert guard.findings_in_text(_doc("| `/x` | a|b |")) == []


class TestExitCodes:
    def test_a_tree_with_no_markdown_refuses_to_pass(self, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        proc = _run(empty)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr

    def test_a_path_that_is_not_there_refuses_to_pass(self, tmp_path) -> None:
        proc = _run(tmp_path / "absent")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr

    def test_an_undecodable_file_refuses_to_pass(self, tmp_path) -> None:
        """A file it cannot read is not a file it cleared."""
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "binary.md").write_bytes(b"\xff\xfe\x00\x00 not utf-8 \xff")
        proc = _run(tree)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNMEASURABLE" in proc.stderr, proc.stderr

    def test_a_clean_tree_reports_what_it_read(self, tmp_path) -> None:
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "ok.md").write_text("# fine\n\nno tables here\n", encoding="utf-8")
        proc = _run(tree)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 file(s) read" in proc.stdout, proc.stdout


class TestWhichFilesItReads:
    def test_a_checkout_is_read_as_tracked_files_only(self, tmp_path) -> None:
        """An untracked scratch file must not be able to red the rule."""
        tree = tmp_path / "checkout"
        (tree / "scripts").mkdir(parents=True)
        (tree / "README.md").write_text("# clean\n", encoding="utf-8")
        (tree / "scratch.md").write_text(
            _doc("| `/x` | `/x a|b` |"), encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
        subprocess.run(["git", "add", "README.md"], cwd=tree, check=True)
        proc = _run(tree)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 file(s) read" in proc.stdout, proc.stdout

    def test_a_tree_with_no_git_is_walked(self, tmp_path) -> None:
        """The export path, which holds only tracked content by construction."""
        tree = tmp_path / "export"
        tree.mkdir()
        (tree / "README.md").write_text(
            _doc("| `/x` | `/x a|b` |"), encoding="utf-8"
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "README.md:3" in proc.stdout, proc.stdout


class TestTheNamingConvention:
    def test_the_first_line_names_the_tree_it_was_given(self, tmp_path) -> None:
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "ok.md").write_text("# fine\n", encoding="utf-8")
        proc = _run(tree)
        assert proc.stdout.splitlines()[0] == f"tree: {tree.resolve()}", proc.stdout


class TestTheRealTree:
    def test_no_tracked_markdown_carries_the_defect(self) -> None:
        """The half the issue asks for: the checkout itself, not a built tree."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.splitlines()[0] == f"tree: {REPO_ROOT}", proc.stdout
        assert "OK:" in proc.stdout, proc.stdout

    def test_the_subject_really_is_in_the_read_set(self) -> None:
        """A clean reading of a set that excludes the file the defect lived in
        would be a clean reading of nothing — the coverage is asserted."""
        guard = _load_guard()
        files = guard.markdown_files(REPO_ROOT)
        assert "README.md" in files, files
        assert "README.cn.md" in files, files
        assert len(files) >= 17, files

    def test_the_readme_command_table_rows_match_their_header(self) -> None:
        """Issue #2324's acceptance item 2, pinned on the file it names.

        The `/skills` row is the one that was four cells; the assertion is over
        the whole table so a later row cannot drift the same way unnoticed.
        """
        lines = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        header_at = _table_with(lines, "/skills")
        header = _cells(lines[header_at])
        assert header == ["Command", "What it does"], header
        expected = len(header)
        seen = 0
        for line in lines[header_at + 2 :]:
            if not line.strip() or "|" not in line:
                break
            seen += 1
            assert len(_cells(line)) == expected, f"row is not {expected} cells: {line!r}"
        assert seen >= 10, f"the table was not walked ({seen} rows)"
