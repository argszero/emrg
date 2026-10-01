"""A row the index writes has to be a row its readers can read.

`MEMORY.md` is a line-based document whose rows carry their target between the
first `](` after the label's opening `[`. A memory's title is whatever its first
heading says, so it can hold a line break or that delimiter — and the row used
to be rendered from the title verbatim, which made the row unreadable to the two
readers the index exists for (`MemoryIndex.from_text`, and the link reading in
`scripts/check-memory-index.py`). The label is the writer's to render, so these
tests hold the writer to the two properties that make a row a row.

Measured before the fix, on a store built through this module's own API:

* a title holding a newline wrote the row as two lines — `MemoryIndex` parsed
  **zero** entries for an index that had one, `is_index_row` still counted the
  first line as a row, and `scripts/check-memory-index.py` reported
  `row links 0` and exited **0**; a later `update()` appended a second row naming
  `project-line1.md`, which does not exist;
* a title holding `](` parsed its row with the filename
  `' in it](project-has-in-it.md'`, so the guard exited 1 and every later write
  appended another copy of the row — one memory, two rows.

Both directions are tested: the shapes that must be repaired, and the ordinary
titles that must come through **byte-identical** (a sanitiser that quietly
rewrites every label would be a second defect, not a fix).
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.memory import (  # noqa: E402
    INDEX_TITLE_MAX_CHARS,
    MemoryFile,
    MemoryIndex,
    MemoryStore,
    is_index_row,
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield MemoryStore(Path(tmp), scope="project")


def _index(store) -> str:
    return (store.directory / "MEMORY.md").read_text(encoding="utf-8")


def _row_lines(store) -> list[str]:
    """The rows as the *line* predicate sees them — not as the parser does."""
    return [line for line in _index(store).splitlines() if is_index_row(line)]


def _parsed(store) -> list[str]:
    return [e.filename for e in MemoryIndex.from_text(_index(store)).entries]


def test_a_title_with_a_newline_still_writes_one_row(store):
    """A row is a line. Two lines are not a row, and the parser said so."""
    mem = store.create("project", "line1\nline2", "body")

    rows = _row_lines(store)
    assert len(rows) == 1, f"a title broke the row across lines: {rows}"

    parsed = _parsed(store)
    assert parsed == [mem.filename], (
        f"the index does not point at the memory that wrote it: {parsed}"
    )


def test_a_title_with_a_newline_does_not_add_lines_to_the_index(store):
    """The line count is the number the hygiene rule is stated in."""
    store.create("project", "line1\nline2", "body")

    lines = _index(store).splitlines()
    assert len(lines) == 4, f"a title added a line to the index: {lines}"


def test_a_title_with_the_row_delimiter_still_names_its_file(store):
    """`](` inside the label used to become the row's delimiter."""
    mem = store.create("project", "Has ]( in it", "body")

    assert _parsed(store) == [mem.filename], (
        f"the row names something other than its file: {_parsed(store)}"
    )
    assert (store.directory / mem.filename).exists()


def test_a_write_after_a_row_shaped_title_does_not_duplicate_the_row(store):
    """The de-duplication is by filename, so a mis-parsed row cannot be found."""
    mem = store.create("project", "Has ]( in it", "body")
    store.update(mem.id, body="edited")

    mem = store.create("project", "line1\nline2", "second")
    store.update(mem.id, body="edited too")

    parsed = _parsed(store)
    assert len(parsed) == len(set(parsed)), f"the index carries a row twice: {parsed}"
    assert len(_row_lines(store)) == len(parsed), (
        "the row lines and the parsed entries disagree — one of the two readers "
        f"is being fed a row it cannot read: {_row_lines(store)} vs {parsed}"
    )


def test_every_row_the_store_writes_opens_the_file_it_names(store):
    """The property the two halves above exist for, over a corpus of titles."""
    titles = [
        "a plain title",
        "line1\nline2",
        "Has ]( in it",
        "trailing newline\n",
        "  leading and trailing  ",
        "parens (fine)",
        "brackets [fine]",
        "Windows\r\nline ending",
        "tab\tseparated",
    ]
    memories = [store.create("project", t, "body") for t in titles]

    for mem in memories:
        assert (store.directory / mem.filename).exists()

    parsed = _parsed(store)
    assert sorted(parsed) == sorted(m.filename for m in memories), (
        f"a row does not open the file it names: {parsed}"
    )
    for line in _row_lines(store):
        target = line.split("](", 1)[1].split(")", 1)[0]
        assert (store.directory / target).exists(), f"row names a missing file: {line}"


def test_an_ordinary_title_is_rendered_exactly_as_before(store):
    """The narrow half: a label with nothing to repair must not be rewritten."""
    store.create("project", "parens (fine) and [brackets]", "body")

    row = _row_lines(store)[0]
    assert row.startswith("- [parens (fine) and [brackets]](project-parens-fine-and-brackets.md)"), row


def test_a_row_label_is_never_empty(store):
    """`[](...)` is not a row `MemoryIndex.from_text` parses either."""
    mem = store.create("project", "   ", "body")

    assert _parsed(store) == [mem.filename], (
        f"a whitespace-only title produced a row with no label: {_row_lines(store)}"
    )


def test_a_long_row_shaped_title_is_truncated_and_still_names_its_file():
    """The truncating path renders the label too, not just the fast path.

    Rendered through the index rather than through `create()` on purpose: a title
    long enough to overflow the row also overflows the *filename* it derives
    (measured: `create()` with a 600-character title raises
    `OSError: [Errno 63] File name too long` from `_resolve_filename`), so a
    long-title row can only reach the renderer as index data — which is exactly
    the "legacy dirty index data" the fallback below was written for.
    """
    mem = MemoryFile(type="project", title="long\n" + "x" * 600 + " ]( tail", body="b")
    idx = MemoryIndex()
    idx.add_entry(mem, filename="project-long.md")
    text = idx.to_markdown()

    rows = [line for line in text.splitlines() if is_index_row(line)]
    assert len(rows) == 1, f"the truncating path wrote more than one line: {rows}"
    assert len(rows[0]) <= INDEX_TITLE_MAX_CHARS, "the row exceeds the cap"
    assert "(project-long.md)" in rows[0], (
        f"the truncated row lost the link to its detail file: {rows[0]}"
    )
    parsed = MemoryIndex.from_text(text).entries
    assert [e.filename for e in parsed] == ["project-long.md"], (
        f"the truncated row does not name its file: {[e.filename for e in parsed]}"
    )
    assert "]( tail" not in rows[0], "the label still carries the row delimiter"
