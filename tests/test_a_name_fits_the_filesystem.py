"""A memory's name has to fit the filesystem it is written to.

`MemoryFile.filename` derived `<type>-<slug>.md` from the title with no bound, so a
long title produced a name no filesystem accepts: measured 2026-10-01 on this host, a
250-character title raised `OSError: [Errno 63] File name too long` out of
`_resolve_filename`'s `.exists()` — a memory that could not be written **at all**, and
the failure named a temp path rather than the length. `os.pathconf(dir, "PC_NAME_MAX")`
answers 255, and that is exactly where the boundary is (255 characters accepted, 256
refused).

Two properties are load-bearing, and the second is why the fit is one function:

* the bound lives on the **derived** name, so the index row, the server's memory frame
  and the collision counter all see the same name the write used;
* the **collision suffix survives** the fit. `_resolve_filename` finds a free name by
  counting upwards, so a candidate that no longer changes with the counter is the same
  candidate forever — a loop that never ends. Truncating the whole name (rather than the
  stem, keeping the suffix) is exactly that bug, which is why it is tested here.

Deliberately **not** asserted here: what `list()` reports for several long titles that
truncate to the same stem. On this branch a memory read from disk re-derives its name
from its title, so those collapse onto one reported name — that is issue #1801's defect,
fixed by the `_filename` field rather than by this bound, and pinning the collapsed
reading in a test would make this file fail once that fix lands.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.memory import (  # noqa: E402
    FILENAME_MAX_BYTES,
    MemoryIndex,
    MemoryStore,
    ProjectMemoryStore,
    fit_filename,
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield MemoryStore(Path(tmp), scope="project")


def _bytes(name: str) -> int:
    return len(name.encode("utf-8"))


# ── the rule itself ────────────────────────────────────────────────


def test_a_name_at_the_bound_is_left_alone():
    """The fit is a bound, not a rewrite: a name that fits comes through byte-identical."""
    stem, suffix = "project-a-plain-title", ".md"

    assert fit_filename(stem, suffix) == f"{stem}{suffix}"


def test_a_name_over_the_bound_is_cut_to_fit():
    name = fit_filename("x" * 400, ".md")

    assert _bytes(name) <= FILENAME_MAX_BYTES


def test_the_suffix_survives_the_cut():
    """The counter is what makes `_resolve_filename` terminate — it may not be cut off."""
    name = fit_filename("x" * 400, "-12.md")

    assert name.endswith("-12.md"), f"the collision suffix was truncated away: ...{name[-12:]}"
    assert _bytes(name) <= FILENAME_MAX_BYTES


def test_a_cut_never_splits_a_character():
    """A filename is text: half a UTF-8 sequence is a name no reader can open."""
    for budget_stem in ("长" * 200, "a" * 300):
        name = fit_filename(budget_stem, ".md")
        assert _bytes(name) <= FILENAME_MAX_BYTES
        name.encode("utf-8").decode("utf-8")  # raises if the cut split a character
        assert not name.endswith("\ufffd")


def test_a_stem_with_no_room_left_still_yields_a_suffix():
    """Pathological input must answer, not raise: the suffix is the caller's name."""
    assert fit_filename("x" * 400, "-" + "9" * 300 + ".md").endswith(".md")


# ── the store's use of it ──────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "a" * 250,     # the measured failure
        "a" * 600,
        "长" * 200,    # 600 bytes of CJK, where bytes and characters disagree
        "a" * 240,     # inside the old working range, must not change behaviour
    ],
    ids=["ascii-250", "ascii-600", "cjk-200", "ascii-240"],
)
def test_create_writes_a_long_title_at_all(tmp_path, title):
    store = MemoryStore(tmp_path, scope="project")

    mem = store.create("project", title, "body")

    path = store.directory / mem.filename
    assert path.exists(), "the memory was not written"
    assert _bytes(mem.filename) <= FILENAME_MAX_BYTES


def test_a_long_title_still_gets_exactly_one_row(store):
    """The row is what makes the memory reachable; a bound must not cost it."""
    mem = store.create("project", "a" * 400, "body")

    rows = MemoryIndex.from_text(
        (store.directory / "MEMORY.md").read_text(encoding="utf-8")
    ).entries

    assert [e.filename for e in rows] == [mem.filename]
    assert (store.directory / rows[0].filename).exists()


def test_collisions_stay_distinct_and_the_counter_is_visible(tmp_path):
    """Several long titles truncating to one stem must still be several files.

    The property the suffix rule exists for: the counter has to change the name, or
    `_resolve_filename` would look at the same candidate forever.
    """
    store = MemoryStore(tmp_path, scope="project")
    title = "x" * 400

    memories = [store.create("project", title, f"body {i}") for i in range(3)]

    on_disk = sorted(p.name for p in store.directory.glob("*.md") if p.name != "MEMORY.md")
    assert len(on_disk) == 3, f"the collision loop wrote {len(on_disk)} files: {on_disk}"
    assert all(_bytes(name) <= FILENAME_MAX_BYTES for name in on_disk)
    # Distinct, and the difference is the counter the fit had to keep.
    assert len(set(on_disk)) == 3
    assert len({n[-5:] for n in on_disk}) == 3, f"the counters did not survive: {on_disk}"
    assert len({m.id for m in memories}) == 3


def test_a_long_title_works_through_promotion(tmp_path):
    """`promote_to_project` builds its own name and takes the same route."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    store = MemoryStore(session_dir, scope="session")
    project = ProjectMemoryStore(tmp_path / "project")
    title = "y" * 400

    mem = store.create("project", title, "body")
    promoted = store.promote_to_project(mem.id, project)

    assert promoted is not None
    assert (project.directory / promoted.filename).exists()
    assert _bytes(promoted.filename) <= FILENAME_MAX_BYTES
    # And a second copy of the same long title collides without overflowing. Asserted on
    # the directory rather than on the returned `.filename`: a memory read back from disk
    # re-derives its name from its title on this branch (issue #1801 — that is the
    # `_filename` field's job, not this bound's), so the two copies report one name while
    # being two files, which is what this test is about.
    second = store.create("project", title, "body 2")
    promoted2 = store.promote_to_project(second.id, project)
    assert promoted2 is not None
    assert _bytes(promoted2.filename) <= FILENAME_MAX_BYTES

    copies = sorted(p.name for p in project.directory.glob("*.md") if p.name != "MEMORY.md")
    assert len(copies) == 2, f"the second copy did not land separately: {copies}"
    assert all(_bytes(name) <= FILENAME_MAX_BYTES for name in copies)

