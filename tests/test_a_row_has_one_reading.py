"""One row, one reading: the parser and the index guard must agree.

`MEMORY.md` has two readers. `MemoryIndex.from_text` builds the index model a store
writes back; `scripts/check-memory-index.py` is the reading the host and the merge gates
run. They read the *same* lines, so a line they read differently is an index that means
one thing to the model and another to the guard — and a rule spelled twice is a rule free
to drift, which is exactly what had happened:

* **a code span in a label.** The guard reads a row's links outside code spans (a label
  that quotes `](target)` is the row showing the shape, not using it); the parser had no
  code-span rule, so for `- [how `](t.md)` is read](real.md)` it filed the memory under
  `` `](t.md)` is read](real.md `` — a name no file has, so the memory left the index and
  every later write appended a second row for it;
* **a fenced example.** The guard reads rows off `index_lines`' unfenced lines; the parser
  built entries from every line, so a fenced example of the row shape became an entry
  naming a file that does not exist.
* **a row the predicate counts and the parser's grammar cannot see.** `is_index_row` counts
  a row by shape — a `- ` list line, or a table body row — and `row_links` finds the file it
  names; the parser matched a *fuller* grammar (`- [label](target) [status] — rec: …`), so a
  hand-written pointer row (`- [Hand written](f.md)`, no `rec:` tail) and a table row parsed
  to **zero** entries. A row that names a file the model cannot see is a duplicate waiting to
  be written: `add_entry` de-duplicates by filename, so the next `create()`/`update()` of that
  same file appended a **second row** for it (measured 2026-10-01: lines 4 → 5, two rows naming
  one file — the line count a hygiene rule is stated in).

All three are fixed by giving the rule one home (`emrg.memory.row_links`) and having both
readers ask it — the same shape `is_index_row` and `index_lines` already had, and the third
divergence is the parser asking `is_index_row` and `row_links` instead of deciding for
itself. So the load bearing test here is the last one: not "the parser reads this shape", but
"the two readers answer the same file for every shape".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emrg.memory import MemoryIndex, row_links  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "check-memory-index.py"

HEAD = "# Memory Index\n\n## project\n"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_memory_index_rows", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load_guard()


def _write_index(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "MEMORY.md"
    path.write_text(HEAD + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def _touch(tmp_path: Path, *names: str) -> None:
    for name in names:
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")


# ── the parser's half ──────────────────────────────────────────────


def test_the_target_is_the_link_outside_the_code_span():
    """The label quotes the delimiter; the row still names the file it means."""
    text = HEAD + "- [how `](t.md)` is read](real.md) — rec: 26-10-01\n"

    entries = MemoryIndex.from_text(text).entries

    assert [e.filename for e in entries] == ["real.md"], (
        "the quoted delimiter was read as the row's own"
    )


def test_the_label_is_kept_as_written():
    """A code span is part of the label, not a gap in it."""
    text = HEAD + "- [how `](t.md)` is read](real.md) — rec: 26-10-01\n"

    entries = MemoryIndex.from_text(text).entries

    assert entries[0].title == "how `](t.md)` is read", (
        f"the label lost its code span: {entries[0].title!r}"
    )


def test_a_fenced_example_is_not_an_entry():
    """A fenced row is the index documenting its own format."""
    text = (
        HEAD
        + "```\n- [example](gone.md) — rec: 26-10-01\n```\n"
        + "- [real](real.md) — rec: 26-10-01\n"
    )

    entries = MemoryIndex.from_text(text).entries

    assert [e.filename for e in entries] == ["real.md"], (
        f"a fenced example became an entry: {[e.filename for e in entries]}"
    )


def test_a_fenced_example_survives_a_load_and_save():
    """Skipping it as an entry must not drop the line — it is still the file's text."""
    text = (
        HEAD
        + "```\n- [example](gone.md) — rec: 26-10-01\n```\n"
        + "- [real](real.md) — rec: 26-10-01\n"
    )

    assert MemoryIndex.from_text(text).to_markdown() == text, (
        "a load → save rewrote the document"
    )


def test_an_unclosed_fence_is_not_a_hiding_place():
    """The conservative direction `index_lines` documents: a fence never closed is text."""
    text = (
        HEAD
        + "```\n- [after an unclosed fence](real.md) — rec: 26-10-01\n"
    )

    entries = MemoryIndex.from_text(text).entries

    assert [e.filename for e in entries] == ["real.md"], (
        "an unclosed fence hid a row from the parser but not from the guard"
    )


def test_an_ordinary_row_is_parsed_as_before():
    """The control: nothing about the shape or its fields may move."""
    text = HEAD + "- [plain](real.md) [superseded] — rec: 26-10-01, evt: 26-09-30\n"

    entry = MemoryIndex.from_text(text).entries[0]

    assert entry.filename == "real.md"
    assert entry.title == "plain"
    assert entry.status == "superseded"
    assert entry.created_at.startswith("26-10-01")
    assert entry.event_at.startswith("26-09-30")


# ── the rule has one home ──────────────────────────────────────────


def test_the_guard_reads_links_through_the_shared_rule(guard):
    """The guard's link reading *is* the library's function, not a second copy of it."""
    assert guard.row_links is row_links, (
        "the guard spells the link rule itself again — the pair that drifted"
    )


def test_the_shared_rule_ignores_a_quotation():
    """Both directions: a real link is read, a quoted one is not."""
    assert row_links("- [how `](t.md)` is read](real.md) — rec: 26-10-01") == ["real.md"]
    # The row that *documents* the shape carries no link of its own, so a reading that
    # takes the quotation names a file the row never mentions — and both readers then
    # agree that this row has no link at all (`entries == []`).
    quoted = "- [the shape is `[x](y.md)`] — rec: 26-10-01"
    assert row_links(quoted) == []
    assert MemoryIndex.from_text(HEAD + quoted + "\n").entries == []


# ── the two readers, on the same lines ─────────────────────────────


ROWS = [
    "- [plain](a.md) — rec: 26-10-01, evt: 26-10-01",
    "- [how `](t.md)` is read](b.md) — rec: 26-10-01",
    "- [a label with [brackets] in it](c.md) — rec: 26-10-01",
    "- [status tag](d.md) [superseded] — rec: 26-10-01",
    "- [parens (and) parens](e.md) — rec: 26-10-01",
    # The shapes the store's own grammar does not produce and the predicate counts
    # anyway: what an agent's compaction writes by hand, and a table index.
    "- [Hand written](f.md)",
    "| [Table row](g.md) | note |",
]


def test_both_readers_name_the_same_file_for_every_shape(tmp_path, guard):
    """The invariant the two halves above exist for.

    Not "the parser reads this shape" but "the parser and the guard answer the same file"
    — a case can be added here without knowing which reader a future change moves.
    """
    _touch(tmp_path, "a.md", "b.md", "c.md", "d.md", "e.md", "f.md", "g.md")
    _write_index(tmp_path, ROWS)

    reading = guard.measure(tmp_path / "MEMORY.md")
    guard_targets = [target for _, target in reading.row_links]
    parser_targets = [e.filename for e in MemoryIndex.from_text(HEAD + "\n".join(ROWS) + "\n").entries]

    assert parser_targets == guard_targets, (
        f"the two readers disagree: parser {parser_targets} vs guard {guard_targets}"
    )


def test_both_readers_skip_the_same_fenced_line(tmp_path, guard):
    """The same invariant for the second divergence: what is not a row at all."""
    _touch(tmp_path, "real.md")
    lines = [
        "```",
        "- [example](gone.md) — rec: 26-10-01",
        "```",
        "- [real](real.md) — rec: 26-10-01",
    ]
    _write_index(tmp_path, lines)

    reading = guard.measure(tmp_path / "MEMORY.md")
    guard_targets = [target for _, target in reading.row_links]
    parser_targets = [
        e.filename for e in MemoryIndex.from_text(HEAD + "\n".join(lines) + "\n").entries
    ]

    assert parser_targets == guard_targets == ["real.md"], (
        f"the two readers disagree about the fenced line: {parser_targets} vs {guard_targets}"
    )


# ── what the disagreement costs ────────────────────────────────────


@pytest.mark.parametrize(
    "template",
    ["- [Hand written]({f})", "| [Table row]({f}) | note |"],
    ids=["list-line", "table-row"],
)
def test_a_row_the_parser_cannot_read_is_duplicated_by_the_next_write(tmp_path, template):
    """The invariant above, measured at the store: a row read twice is a row written once.

    `add_entry` de-duplicates by filename, so a row the parser cannot see is a file the
    store cannot find — and the next write of that file appends a **second row** for it.
    That is why the two readers agreeing is not tidiness: the model's reading is what
    the writer consults before it appends.
    """
    from emrg.memory import MemoryStore

    store = MemoryStore(tmp_path, scope="project")
    mem = store.create("project", "The memory", "body")

    index = store.directory / "MEMORY.md"
    index.write_text(HEAD + template.format(f=mem.filename) + "\n", encoding="utf-8")
    before = index.read_text(encoding="utf-8").splitlines()

    store.update(mem.id, body="edited")

    after = index.read_text(encoding="utf-8").splitlines()
    naming = [line for line in after if mem.filename in line]
    assert len(naming) == 1, (
        f"the write added a second row naming {mem.filename}: {after}"
    )
    assert len(after) == len(before), (
        f"the write grew the index by {len(after) - len(before)} line(s): "
        f"{before} -> {after}"
    )
