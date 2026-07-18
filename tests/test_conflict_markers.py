"""Check that no git conflict markers are present in source files.

Squash merges can silently commit conflict markers to master, causing
SyntaxError in Python files and broken docs in Markdown files.
This test scans all tracked source files and fails if any markers exist.
"""

from pathlib import Path

import pytest

SOURCE_DIR = Path(__file__).resolve().parent.parent / "emrg"
CONFLICT_START = "<<<<<<<"
CONFLICT_END = ">>>>>>>"


def _collect_source_files() -> list[Path]:
    """Return all .py and .md files under emrg/, skipping __pycache__ etc."""
    files: list[Path] = []
    for path in SOURCE_DIR.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".md"):
            # Skip __pycache__ dirs and hidden files/dirs (relative to SOURCE_DIR)
            rel_parts = path.relative_to(SOURCE_DIR).parts
            if any(p.startswith("__pycache__") for p in rel_parts):
                continue
            if any(p.startswith(".") for p in rel_parts):
                continue
            files.append(path)
    return sorted(files)


def test_no_conflict_markers():
    """All .py and .md source files must be free of git conflict markers."""
    violations: list[tuple[str, int, str]] = []
    for file_path in _collect_source_files():
        try:
            content = file_path.read_text()
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(CONFLICT_START) or stripped.startswith(CONFLICT_END):
                rel = file_path.relative_to(SOURCE_DIR.parent)
                violations.append((str(rel), lineno, stripped[:80]))

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
