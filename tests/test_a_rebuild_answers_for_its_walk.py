"""A rebuild answers about the files it walked — the whole answer, and only that.

`MemoryStore.rebuild_index` is the documented repair for an index that has gone out of
sync, and it works by **rewriting the file from a directory walk**. That makes two
directions load-bearing at once, and it was wrong in both, measured 2026-10-01 on this
repo's own memory directory (27 entries, every file named by hand or by an earlier rule):

**It named rows after something other than the file.** The entry's link target was
`MemoryFile.filename` — `<type>-<slug>.md`, derived from the *title* — which is the name
`create` *writes*, and not the name a file written by hand, by an agent's `write` tool, or
by an older rule has. On that directory the rebuild re-rendered every row with a target
that exists nowhere: **27 of 27 links dead after it ran**. The repair path for a broken
index turned a working index into links that resolve to nothing — and a dead link in this
index is not cosmetic: `scripts/check-memory-index.py` reports unresolved rows as a fault,
and the index is what every session's prompt embeds.

**It dropped the row of a file it could not read.** The catch is `(OSError, ValueError)`,
and `UnicodeDecodeError` is a `ValueError` subclass, so a file another writer is
mid-rewrite is skipped — the shape `_scan` documents by name (measured there: 37 of 121
reads raised while a 200-KB index was being rewritten, because `MemoryIndex.save`
truncates and then writes). A skip here is not a skip: the index is rewritten from this
walk, so the memory goes with it, and nothing re-runs a rebuild.

Both halves are pinned below, each with its opposite: a row the walk **can** read is
refreshed from its file (so the rebuild still rebuilds), and a row whose file is **gone**
is still dropped (so the pruning a rebuild exists for is not lost with the fix). The
mutation arms that back each test are the two defects and that pruning, one arm each.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from emrg.memory import MemoryStore

#: The shape `create` writes: a full ISO instant in the frontmatter.
MEMORY_MD = """---
id: {mem_id}
event_at: 2026-10-01T10:00:00+08:00
created_at: 2026-10-01T10:00:00+08:00
updated_at: 2026-10-01T10:00:00+08:00
type: {type}
scope: project
status: {status}
---

# {title}

Body of {title}.
"""

#: A row in the short form this repo's own index uses ("rec: 26-10-01"), which is the form
#: whose re-rendering is wrong: `_normalize_date` gives it a time, and `_short_date` then
#: slices it at ten characters — `26-10-01T0`, a date that is not a date.
SHORT_ROW = "- [{title}]({filename}) — rec: {rec}, evt: {rec}"


def _write_memory(dirpath: Path, filename: str, **fields) -> Path:
    """One memory file, under the name given rather than a derived one."""
    fields.setdefault("mem_id", "id-" + filename)
    fields.setdefault("type", "project")
    fields.setdefault("status", "active")
    fields.setdefault("title", Path(filename).stem)
    path = dirpath / filename
    path.write_text(MEMORY_MD.format(**fields), encoding="utf-8")
    return path


def _rows(index_path: Path) -> list[str]:
    return [
        line
        for line in index_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]


def _targets(rows: list[str]) -> list[str]:
    return [re.search(r"\]\((.+?)\)", row).group(1) for row in rows]


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path, "project")


def test_every_row_names_a_file_that_exists(store: MemoryStore, tmp_path: Path) -> None:
    """The whole point of a link: it resolves.

    Three names the store did not choose — a hand-written slug, a name with a type prefix
    the title does not imply, and the cycle-record shape this repo's own memory uses —
    because the derived name happens to be right for exactly one shape: a file the store
    itself wrote. A rebuild that files entries under the derived name passes on that one
    and destroys the other two.
    """
    for filename in (
        "identity-github-role.md",
        "decision-use-httpx-over-requests.md",
        "cycle-20261001-220742.md",
    ):
        _write_memory(tmp_path, filename)

    store.rebuild_index()

    rows = _rows(store.index_path)
    assert len(rows) == 3, rows
    missing = [t for t in _targets(rows) if not (tmp_path / t).exists()]
    assert missing == [], (
        f"the rebuilt index names files that do not exist: {missing} — a row's link is a "
        f"claim about a file, and the name to make that claim with is the one the file has"
    )


def test_the_row_of_a_file_that_could_not_be_read_survives(
    store: MemoryStore, tmp_path: Path
) -> None:
    """The one direction where a skip costs the memory rather than the row.

    The file is here; it is the *read* that fails. Its row is the only thing on disk that
    names the memory, so a rebuild that rewrites the index without it deletes the memory
    from the agent's view while leaving the file behind — invisible, not gone.
    """
    _write_memory(tmp_path, "readable.md")
    # A cut inside a multi-byte character: what a concurrent writer's truncate-then-write
    # leaves observable (the shape `_scan`'s docstring records raising 37 of 121 reads).
    (tmp_path / "torn.md").write_bytes(b"---\nid: t1\n---\n\n# Torn\n\n\xe4\xb8\n")
    store.index_path.write_text(
        "# Memory Index\n\n"
        + SHORT_ROW.format(title="Readable", filename="readable.md", rec="26-10-01")
        + "\n"
        + SHORT_ROW.format(title="Torn", filename="torn.md", rec="26-10-01")
        + "\n",
        encoding="utf-8",
    )

    store.rebuild_index()

    rows = _rows(store.index_path)
    assert "torn.md" in _targets(rows), (
        f"the unreadable file lost its row: {rows} — the file is still on disk, so the row "
        f"is still true of it"
    )
    # And it is kept as written, not re-rendered: a short `rec:` comes back mangled
    # (`26-10-01T0`) when it goes through the renderer instead.
    torn_row = next(r for r in rows if "torn.md" in r)
    assert "rec: 26-10-01," in torn_row, torn_row


def test_a_rebuild_says_which_files_it_could_not_read(
    store: MemoryStore, tmp_path: Path, caplog
) -> None:
    """A partial walk is reported, never passed off as a complete rebuild."""
    import logging

    _write_memory(tmp_path, "readable.md")
    (tmp_path / "torn.md").write_bytes(b"\xe4\xb8\n")

    with caplog.at_level(logging.WARNING, logger="emrg.memory"):
        store.rebuild_index()

    named = [r.getMessage() for r in caplog.records if "partial read" in r.getMessage()]
    assert named and "torn.md" in named[0] and "UnicodeDecodeError" in named[0], named


def test_a_row_whose_file_is_gone_is_still_dropped(
    store: MemoryStore, tmp_path: Path
) -> None:
    """The other direction: pruning is what a rebuild is for.

    A fix that keeps every row it cannot verify would keep this one too, and the index
    would then list a file that is not there — the same dead link as the first defect,
    arrived at from the other side.
    """
    _write_memory(tmp_path, "kept.md")
    store.index_path.write_text(
        "# Memory Index\n\n"
        + SHORT_ROW.format(title="Gone", filename="deleted.md", rec="26-10-01")
        + "\n"
        + SHORT_ROW.format(title="Kept", filename="kept.md", rec="26-10-01")
        + "\n",
        encoding="utf-8",
    )

    store.rebuild_index()

    assert _targets(_rows(store.index_path)) == ["kept.md"]


def test_a_readable_file_still_refreshes_its_row(
    store: MemoryStore, tmp_path: Path
) -> None:
    """The rebuild is still a rebuild: the row follows the file it names.

    A status set on disk after the index was written is what the index has to carry — if
    the row survived verbatim for every file, a rebuild would be a no-op wearing the name
    of a repair.
    """
    _write_memory(tmp_path, "changed.md", title="Changed", status="active")
    store.index_path.write_text(
        "# Memory Index\n\n"
        + SHORT_ROW.format(title="Stale title", filename="changed.md", rec="26-10-01")
        + "\n",
        encoding="utf-8",
    )
    _write_memory(tmp_path, "changed.md", title="Changed", status="superseded")

    store.rebuild_index()

    row = _rows(store.index_path)[0]
    assert "[superseded]" in row, row
    assert "Stale title" not in row, row


def test_a_complete_read_reports_no_gap(store: MemoryStore, tmp_path: Path) -> None:
    """The half that keeps the warning from being noise: everything read, nothing said."""
    _write_memory(tmp_path, "a.md")
    _write_memory(tmp_path, "b.md")

    index, unreadable = store._rebuild_index()

    assert unreadable == []
    assert sorted(entry.filename for entry in index.entries) == ["a.md", "b.md"]
