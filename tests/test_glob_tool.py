"""Tests for the glob tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.glob_tool import GlobTool


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("")
        (root / "src" / "utils.py").write_text("")
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text("")
        (root / "__init__.py").write_text("")
        (root / "README.md").write_text("")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "compiled.pyc").write_text("")
        (root / ".hidden.py").write_text("")
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "package.json").write_text("")
        (root / "node_modules" / "pkg" / "index.js").write_text("")
        yield root


def _run(coro):
    return asyncio.run(coro)


def test_glob_all_python(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    assert not result.error
    assert "main.py" in result.content
    assert "utils.py" in result.content
    assert "test_main.py" in result.content
    assert "__init__.py" in result.content
    assert ".hidden.py" not in result.content
    assert "compiled.pyc" not in result.content


def test_glob_markdown(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.md", "workdir": str(temp_cwd)}))
    assert "README.md" in result.content
    assert "main.py" not in result.content


def test_glob_test_files(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/test*", "workdir": str(temp_cwd)}))
    assert "test_main.py" in result.content


def test_glob_no_match(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.rs", "workdir": str(temp_cwd)}))
    assert "No files matched" in result.content


def test_glob_no_match_says_nothing_about_skipping_when_nothing_was_skipped(temp_cwd):
    """The control for the two legs below: the skip clause is a measurement, not boilerplate.

    `*.rs` matches no path at all, so `skipped` is 0 and there is nothing to declare. A
    clause printed unconditionally would pass both legs below while telling a reader
    about a skip that did not happen.
    """
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.rs", "workdir": str(temp_cwd)}))
    assert "skipped" not in result.content


def test_a_pattern_whose_every_match_was_skipped_does_not_deny_them(temp_cwd):
    """The defect: `No files matched` for a pattern that matched 352 paths in this checkout.

    Measured 2026-10-06 (`cyc20261006-192020`) in `emrg/gui`:
    `node_modules/**/package.json` matched **352** paths, the skip policy dropped every
    one, and the tool answered `No files matched pattern ...` — a false statement about
    the tree, and the reading an agent answers "is this dependency installed?" with.
    """
    tool = GlobTool()
    result = _run(
        tool.execute({"pattern": "node_modules/**/package.json", "workdir": str(temp_cwd)})
    )
    assert not result.error
    assert "No files matched" in result.content
    assert "1 path(s) matched it but were skipped" in result.content


def test_the_count_in_a_hit_line_names_what_the_skip_dropped(temp_cwd):
    """"Found N matches" is the number left after the skip, and has to say so.

    The sibling `grep` names its subject in the same line ("searched N files"); without
    the same qualifier a reader cannot tell this count from the number the tree holds.
    """
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    assert "Found 4 matches for '**/*.py'" in result.content
    assert "1 path(s) also matched but were skipped" in result.content


def test_pointing_workdir_at_a_skipped_directory_reads_it(temp_cwd):
    """The remedy the refusal names is the measured one, not a plausible sentence.

    The skip compares each path relative to the root the caller passed, so a `workdir`
    inside the skipped directory finds the files — verified here because a remedy that
    does not work is worse than none.
    """
    tool = GlobTool()
    result = _run(
        tool.execute(
            {"pattern": "**/package.json", "workdir": str(temp_cwd / "node_modules")}
        )
    )
    assert "Found 1 matches" in result.content
    assert "package.json" in result.content


def test_the_definition_declares_the_skip_the_way_grep_does(temp_cwd):
    """The tool description is the contract the agent reads, and it did not mention the skip.

    `grep` states its skipping in its description ("automatic binary/hidden file
    skipping"); `glob` skipped the same way and said nothing, so "Found 5 matches" read
    as a fact about the tree.
    """
    tool = GlobTool()
    description = tool.definition().description
    assert "Skips hidden entries" in description
    assert "node_modules" in description
    assert "relative to it" in tool.definition().parameters["properties"]["workdir"]["description"]


def test_the_definition_names_the_one_hidden_entry_that_is_not_skipped():
    """The skip declaration is a claim, and as first written it was a universal one.

    "Skips hidden entries and the noise directories …" is refuted by `.emrg`, which is a
    hidden entry `_is_hidden_or_ignored` deliberately keeps — measured 2026-10-07
    (`cyc20261007-000240`, and independently by review on 2026-10-06): from the repo root
    `.emrg/memory/MEMORY.md` is read while `.git/config` and `.github/workflows/test.yml`
    are skipped. A caller who believes the universal sentence does not expect
    `**/MEMORY.md` to answer from `.emrg`, which is the same overclaim the rest of that
    change is about.
    """
    description = GlobTool().definition().description
    assert ".emrg" in description, (
        "the skip declaration has to carry its exception, or it claims to drop a "
        f"directory that is read: {description}"
    )


def test_the_emrg_directory_it_declares_as_read_really_is_read(temp_cwd):
    """The exception in the description, measured rather than asserted in prose.

    `_is_hidden_or_ignored` keeps any path whose parts include `.emrg` (and skips every
    other dot-part), so a search from the root does return the agent's own state. Pinned
    because the description promises it: a promise in a tool description that no test holds
    is how the universal claim above survived review.
    """
    tool = GlobTool()
    memory = temp_cwd / ".emrg" / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# index\n")

    result = _run(tool.execute({"pattern": "**/MEMORY.md", "workdir": str(temp_cwd)}))

    assert not result.error
    assert "Found 1 matches" in result.content, result.content
    assert ".emrg/memory/MEMORY.md" in result.content
    assert "skipped" not in result.content, (
        "the .emrg path was dropped by the skip policy the description says reads it"
    )


def test_glob_no_pattern():
    tool = GlobTool()
    result = _run(tool.execute({"pattern": ""}))
    assert result.error
    assert "no pattern" in result.content.lower()


def test_definition():
    tool = GlobTool()
    d = tool.definition()
    assert d.name == "glob"
    assert "pattern" in d.parameters.get("properties", {})
    assert d.parameters.get("required") == ["pattern", "intent"]


def test_glob_invalid_workdir():
    """Non-existent workdir should return error."""
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": "/nonexistent"}))
    assert result.error
    assert "not found" in result.content.lower()
