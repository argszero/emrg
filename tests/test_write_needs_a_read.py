"""`write` replaces what it has been shown, and refuses what it has not.

The contract these tests pin (issue #2652): a call that would replace the whole
content of an existing file is answered only when this session has been shown
every line of the version that is on disk now. A partial reading, a reading of a
different version, and no reading at all are three states, and only a complete
one permits the replacement.

Measured cost of not asking (the issue's reproduction): a 5,000-line file read
through the read tool's default 1,000-line cap, written back with what that read
returned, came out at 1,002 lines — and the call reported `Updated`. Three
thousand nine hundred and ninety-eight lines went, silently, in a call that said
it had succeeded.
"""

import asyncio

import pytest

from emrg.tools import read_receipts
from emrg.tools.edit_tool import EditTool
from emrg.tools.read_tool import DEFAULT_MAX_LINES, ReadTool
from emrg.tools.write_tool import WriteTool


@pytest.fixture(autouse=True)
def _fresh_receipts():
    """No receipt outlives its test.

    The store is module state keyed by `(session, path)`, and a test that reused
    a temporary path would otherwise be answered by the previous test's reading —
    a green that measures the fixture rather than the guard.
    """
    read_receipts.clear()
    yield
    read_receipts.clear()


def _run(coro):
    return asyncio.run(coro)


def _read(path, **extra):
    return _run(ReadTool().execute({"file_path": str(path), "intent": "read", **extra}))


def _write(path, content, **extra):
    return _run(WriteTool().execute(
        {"file_path": str(path), "content": content, "intent": "write", **extra}
    ))


def test_blind_overwrite_is_refused_and_the_file_survives(tmp_path):
    """The control arm of the defect: nothing read, so nothing may be replaced."""
    target = tmp_path / "existing.txt"
    target.write_text("old content", encoding="utf-8")

    result = _write(target, "new content")

    assert result.error
    assert target.read_text(encoding="utf-8") == "old content"
    # "Read it first" is not actionable on its own: the caller needs to know what
    # is at stake, which is the size of what it would have destroyed.
    assert "none of them has been shown to this session" in result.content


def test_a_capped_read_does_not_license_a_whole_overwrite(tmp_path):
    """The reproduction itself: a truncated read, written back, loses the tail."""
    target = tmp_path / "long.md"
    original = "".join(f"line {i}: content\n" for i in range(1, 3001))
    target.write_text(original, encoding="utf-8")

    read = _read(target)
    assert not read.error
    assert "truncated at start_line=" in read.content

    result = _write(target, read.content)

    assert result.error, "a write built from a truncated read must not be answered"
    assert target.read_text(encoding="utf-8") == original
    # Both numbers, because the gap between them is the caller's next decision.
    assert f"{DEFAULT_MAX_LINES:,} of those 3,000 lines" in result.content
    assert "3,000 lines" in result.content


def test_a_complete_read_licenses_the_overwrite(tmp_path):
    """The other direction: the same call is answered once the file has been shown."""
    target = tmp_path / "small.md"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    assert not _read(target).error
    result = _write(target, "z\n")

    assert not result.error
    assert "Updated" in result.content
    assert target.read_text(encoding="utf-8") == "z\n"


def test_reads_from_where_the_last_one_stopped_accumulate(tmp_path):
    """A file longer than one read is covered by reading it in sequence.

    Reaching the end by repeated continuation is the remedy the refusal names, so
    it has to be the remedy that works: a guard whose advice does not open the
    door would leave every long file unwritable.
    """
    target = tmp_path / "long.md"
    total = DEFAULT_MAX_LINES + 500
    target.write_text("".join(f"line {i}\n" for i in range(1, total + 1)), encoding="utf-8")

    assert not _read(target).error                      # lines 1..1000
    assert not _read(target, start_line=DEFAULT_MAX_LINES + 1).error  # the rest

    result = _write(target, "replaced\n")

    assert not result.error, result.content
    assert target.read_text(encoding="utf-8") == "replaced\n"


def test_a_window_into_the_middle_is_not_a_reading_of_the_file(tmp_path):
    """Coverage is a prefix, never a count.

    Ten lines seen out of a hundred is not a tenth of the way to a licence: the
    lines that were skipped are exactly the ones a whole-file write destroys, and
    they are the ones nobody has been shown.
    """
    target = tmp_path / "hundred.md"
    original = "".join(f"line {i}\n" for i in range(1, 101))
    target.write_text(original, encoding="utf-8")

    assert not _read(target, start_line=50).error

    result = _write(target, "replaced\n")

    assert result.error
    assert target.read_text(encoding="utf-8") == original


def test_a_file_that_changed_after_the_read_is_read_again(tmp_path):
    """A receipt describes a version, and a different version is not covered.

    Without this the guard could be satisfied by reading a file and then having
    something else rewrite it — the caller's reading would license destroying
    content it never saw.
    """
    target = tmp_path / "moving.txt"
    target.write_text("first version\n", encoding="utf-8")
    assert not _read(target).error

    target.write_text("second version, longer than the first one\n", encoding="utf-8")

    result = _write(target, "replaced\n")

    assert result.error
    assert target.read_text(encoding="utf-8").startswith("second version")


def test_a_complete_read_from_elsewhere_does_not_license_this_session(tmp_path):
    """The receipt belongs to the session that was shown the file.

    `_session_id` is injected by the daemon rather than supplied by the caller for
    this reason: the model is the party the guard is about, so a name it chose
    would let it read under one identity and write under another.
    """
    target = tmp_path / "shared.txt"
    target.write_text("content only one session has seen\n", encoding="utf-8")

    assert not _read(target, _session_id="reader").error

    result = _write(target, "replaced\n", _session_id="writer")

    assert result.error
    assert target.read_text(encoding="utf-8") == "content only one session has seen\n"


def test_what_this_session_wrote_it_may_write_again(tmp_path):
    """A file the caller authored needs no reading: it has seen every byte.

    Without this, an agent could not correct its own output within a turn — and
    writing a new file, which is the commonest call this tool makes, would be
    followed by a refusal against itself.
    """
    target = tmp_path / "mine.txt"

    assert not _write(target, "first draft\n").error
    result = _write(target, "second draft\n")

    assert not result.error, result.content
    assert target.read_text(encoding="utf-8") == "second draft\n"


def test_an_empty_existing_file_needs_no_reading(tmp_path):
    """Nothing to be shown is nothing to lose.

    A zero-line file has no content for a write to destroy, so refusing here
    would be a refusal with no harm behind it — the guard is about the loss, not
    about the act of opening an existing path.
    """
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = _write(target, "content\n")

    assert not result.error, result.content
    assert target.read_text(encoding="utf-8") == "content\n"


# ── The receipt is the bytes, not just the numbers ────────────────────────
#
# Review of this branch found two states the first version could not tell apart,
# and both are the harm direction: one let a write through, the other refused a
# write that had no unseen content behind it.


def test_a_same_length_same_mtime_change_is_not_the_version_that_was_read(tmp_path):
    """Size and `mtime_ns` agreeing is not the file agreeing.

    A replacement that keeps the length *and* the modification time is ordinary
    rather than exotic — `cp -p`, `rsync -a`, `tar -x` and `git checkout` all put
    the old timestamp back onto new bytes — and a receipt keyed on those two
    numbers alone answers it as though nothing moved. Measured on the first
    version of this guard: `'AAAAAAAAAA'` read, `'BBBBBBBBBB'` put in its place at
    the same length and the same `mtime_ns`, whole-file write answered, and
    content the session never saw destroyed.
    """
    import os

    target = tmp_path / "same.txt"
    target.write_text("AAAAAAAAAA\n", encoding="utf-8")
    filed = target.stat()

    assert not _read(target).error

    target.write_text("BBBBBBBBBB\n", encoding="utf-8")
    os.utime(target, ns=(filed.st_atime_ns, filed.st_mtime_ns))
    now = target.stat()
    assert (now.st_size, now.st_mtime_ns) == (filed.st_size, filed.st_mtime_ns), (
        "the fixture must reach the state under test: identical size and mtime_ns"
    )

    result = _write(target, "CCCCCCCCCC\n")

    assert result.error, "a write over bytes this session never saw must be refused"
    assert target.read_text(encoding="utf-8") == "BBBBBBBBBB\n"


def test_a_file_this_session_wrote_and_then_edited_stays_writable(tmp_path):
    """`edit` is a writer too, and it must not leave a stale receipt behind.

    Every byte of the new version is known to a session that authored the text and
    then replaced a string it named, so the loop write → edit → write has to stay
    open; before this rule it was refused with "none of them has been shown to this
    session" about text the session had just written.
    """
    target = tmp_path / "edited.txt"

    assert not _write(target, "alpha\nbeta\n", _session_id="s1").error
    edit = _run(EditTool().execute({
        "file_path": str(target), "old_string": "beta", "new_string": "gamma",
        "intent": "edit", "_session_id": "s1",
    }))
    assert not edit.error, edit.content
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"

    result = _write(target, "alpha\ngamma\ndelta\n", _session_id="s1")

    assert not result.error, result.content
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\ndelta\n"


def test_an_edit_does_not_license_a_rewrite_of_a_file_read_only_in_part(tmp_path):
    """The other direction: the receipt `edit` files is only for a complete reading.

    A session shown the first 1,000 lines of a longer file has not seen the rest,
    and an edit to a line it did see does not change that — so the whole-file
    rewrite stays refused, which is the direction that cannot destroy anything.
    """
    target = tmp_path / "long.txt"
    total = DEFAULT_MAX_LINES + 50
    original = "".join(f"line {i}\n" for i in range(1, total + 1))
    target.write_text(original, encoding="utf-8")

    assert not _read(target).error                       # lines 1..1000 only
    edit = _run(EditTool().execute({
        "file_path": str(target), "old_string": "line 5\n", "new_string": "line five\n",
        "intent": "edit", "_session_id": "s1",
    }))
    assert not edit.error, edit.content

    result = _write(target, "replaced\n", _session_id="s1")

    assert result.error
    assert target.read_text(encoding="utf-8").startswith("line 1\nline 2\n")
