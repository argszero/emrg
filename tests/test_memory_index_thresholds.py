"""The index threshold is one constant read in **two units** — measured, not assumed.

The class
---------
`memory.INDEX_SIZE_WARN` is compared against a file's **byte** size by the two
advisory readers (`SessionMemoryStore._warn_index_thresholds`, and the reflection
prompt's hygiene note, which prints the reading as "N bytes"), while
`EmrgServer._cap_memory_index` applies it as a **character** budget — because what
that cap bounds is a prompt, and prompts are counted in characters (the incident
the cap came from is quoted that way: 77% of a 452,972-char prompt).

Both halves were commented as though they were one reading of one knob. They are
not: measured 2026-09-24, a 30,024-char / 61,160-byte CJK index fires the advisory
while the cap truncates nothing. The band where they disagree is wide, because CJK
runs ~3 bytes per character — and every index this machine actually carries is CJK.

What has to hold, and why it does
---------------------------------
Not "the two agree": they don't, and this file pins that they don't rather than
pretending otherwise. What must hold is the direction the cap was written for —
**the cap may only truncate an index the advisory has already flagged, never one it
stayed silent about**. That holds structurally, not by luck: the advisory reads
bytes, the cap reads characters, and UTF-8 never encodes a string in fewer bytes
than characters, so `chars > T` implies `bytes > T`.

The arms this guards against are the plausible single edits, not hypothetical ones.
Change either reader alone and the pair stops being the pair: make the **cap** measure
bytes and it truncates the band above, where the advisory reports that nothing is
wrong — the exact silence the cap's comment promises cannot happen; make the
**advisory** measure characters and the band stops warning at all. Both arms were run
(2026-09-24) and both are caught by the band test below; a cap that simply stops
truncating is caught by the ordinary-case test. Byte-for-byte restore of the two
mutated modules was verified after each arm.

Why this file rather than an arm beside the existing cap test: the claim spans two
modules, and the thing being pinned is the *pair's* units, so it needs both readers
driven in one measurement. The fixtures are `tmp_path` throughout — the cap is a pure
function of the path it is handed (so it is called on an uninitialised instance, which
also keeps the test away from constructing a server), and the store is pointed at a
directory the test creates.
"""

import logging
from pathlib import Path

from emrg.memory import INDEX_SIZE_WARN, SessionMemoryStore
from emrg.server.daemon import EmrgServer

# A CJK row is ~3 bytes per character; an ASCII row is 1, which is the whole point
# of measuring both. Both are index-shaped (`^- [`) so a fixture reads like a real
# index rather than like filler.
CJK_ROW = "- [条目](x.md) — 中文索引行内容测试数据用于计量单位\n"
ASCII_ROW = "- [row](x.md) — ascii row for the byte and character comparison\n"


def _write_index(root: Path, chars: int, row: str) -> Path:
    """Write a store-shaped index whose *character* count first reaches ``chars``.

    Returns the index path; ``root`` is what the store is constructed from, since
    the store reads its own ``<root>/memory/MEMORY.md``.
    """
    directory = root / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "MEMORY.md"
    body = ""
    while len(body) < chars:
        body += row
    path.write_text(body, encoding="utf-8")
    return path


def _readings(path: Path) -> tuple[int, int]:
    """The same file in the two units the two readers use, measured not derived."""
    text = path.read_text(encoding="utf-8")
    return len(text), path.stat().st_size


def _advisory_fires(root: Path, caplog) -> bool:
    """Does the store's advisory fire for the index under ``root``?

    Matched on the size message specifically: the same method also warns when the
    entry *count* crosses its own threshold, and a fixture shaped like an index can
    cross that one too — counting that as "the size threshold fired" would make this
    reading answer a question it was not asked.
    """
    store = SessionMemoryStore(root)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="emrg.memory"):
        store._warn_index_thresholds()
    return any(
        "bytes >" in record.getMessage() for record in caplog.records
    )


def _cap_truncates(path: Path) -> bool:
    """Does the embed cap cut this index?

    ``_cap_memory_index`` reads nothing but the path it is given, so it is called on
    an instance that was never constructed — which is also what keeps this test out
    of `EmrgServer.__init__` and its host-path bookkeeping.
    """
    capped = EmrgServer.__new__(EmrgServer)._cap_memory_index(path)
    return "truncated" in capped


def test_the_advisory_reads_bytes_while_the_cap_reads_characters(tmp_path, caplog):
    """One constant, two units — the disagreement is the fact, pinned with numbers.

    The fixture is placed *in the band where the units differ* and asserted to be
    there, so this cannot go green by accident: if the constant or the row shape
    changes such that the index no longer straddles the threshold, the guard fails
    loudly instead of silently measuring nothing.

    Both assertions carry an arm, and each was run (2026-09-24, byte-for-byte
    restore verified): making the advisory measure characters kills the first
    (a silent advisory in the band), and making the cap measure bytes kills the
    second (it truncates the band, where the advisory said nothing was wrong).
    """
    path = _write_index(tmp_path, INDEX_SIZE_WARN // 2, CJK_ROW)
    chars, size = _readings(path)
    assert chars <= INDEX_SIZE_WARN < size, (
        "this fixture must sit where the two units disagree "
        f"(chars={chars}, bytes={size}, threshold={INDEX_SIZE_WARN})"
    )

    assert _advisory_fires(tmp_path, caplog), (
        "the advisory compares the file's byte size, so it must fire here"
    )
    assert not _cap_truncates(path), (
        "the cap compares characters, so it must stay silent here — if this now "
        "truncates, the cap has started reading bytes and the pair's order changed"
    )


def test_the_cap_never_truncates_an_index_the_advisory_left_alone(tmp_path, caplog):
    """The invariant the cap was written for: truncated ⟹ already flagged.

    Measured on both an ASCII and a CJK index, since the CJK one is where the two
    units diverge most. This pins the ordinary case — over the threshold in both
    readings at once — and it is the arm against a cap that stops truncating at all
    (run 2026-09-24: killing the truncation kills this test, and leaves the band test
    above green, which is why both are kept).

    The unit swap itself is pinned by the band test, not by this one: a fixture that
    is over the threshold in characters is over it in bytes too, so it cannot see the
    two readings disagree. Said here so the pair is not read as two copies.
    """
    for name, row in (("cjk", CJK_ROW), ("ascii", ASCII_ROW)):
        root = tmp_path / name
        root.mkdir()
        path = _write_index(root, INDEX_SIZE_WARN + 1, row)
        chars, size = _readings(path)
        assert chars > INDEX_SIZE_WARN, f"{name}: the fixture must be over the cap in characters"

        assert _cap_truncates(path), f"{name}: the fixture must actually be truncated"
        assert _advisory_fires(root, caplog), (
            f"{name}: the cap truncated an index the advisory never flagged "
            f"(chars={chars}, bytes={size}) — that is the silence this invariant forbids"
        )
