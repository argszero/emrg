"""What a read has shown, so a write cannot replace a file with less of it.

The class this exists for
-------------------------
``read`` answers a **window**, not a file: it stops at ``DEFAULT_MAX_LINES``
unless the caller asks for more, and it says so in its own report. ``write``
replaces a file's whole content. Nothing connected the two, so a capped read
followed by a write of what that read returned destroyed every line past the
cap — and the write reported success (issue #2652).

Measured by calling the two tools directly on a 5,000-line file, in a temporary
directory, with no daemon and no session state:

    BEFORE: 5000 lines, 193893 chars
    READ:   1002 lines returned, 44967 chars, error=False
    WRITE:  Updated ... (44967 characters), error=False
    AFTER:  1002 lines, 44967 chars

The read tool's truncation marker — its own sentence saying the reading is
partial — was written into the file as content, so what survived did not even
end at a clean prefix of the original.

What a receipt is
-----------------
A fact about a **file version**, recorded by every file read: the version (size
and ``mtime_ns``), how many lines from the start of the file that session has
been shown, and how many the file holds. ``write`` asks for one before it
overwrites an existing file, and refuses while ``shown`` is short of ``total`` —
naming both numbers, so the caller learns what it was about to remove.

Three properties are deliberate, and each closes a way the guard could lie:

* **Version-keyed.** A file that changed after the read is not the file that was
  shown, so the receipt expires by itself instead of authorising a write against
  content nobody has seen. This is the "identity, not spelling" rule
  :func:`emrg.tools.file_policy.same_file` states for paths, applied to a version.
* **A contiguous prefix from line 1.** Reading a file in chunks extends the
  receipt when the next chunk starts where the last one ended, so the ordinary
  way of reading a long file does reach ``total``. A window that starts past the
  fingerprint leaves the count alone: it understates what was seen rather than
  overstating it, which is the only safe direction for a guard whose wrong answer
  destroys text.
* **Missing is a refusal, never a pass.** No receipt and a short receipt are
  different states with the same consequence, so the one function that answers
  cannot return a value that reads like "fine".

Scope, stated rather than implied
---------------------------------
A receipt is keyed by ``(session, resolved path)``. The session id is the one the
daemon injects (``_session_id`` in
:meth:`emrg.server.daemon.EmgServer._inject_tool_arguments`), and it **replaces**
whatever the model put in the call: the model is the party this guard is about,
so an identity it chose is the one fact the guard cannot take from it.

A call carrying no identity at all — an in-process caller, a test — shares one
bucket. That is the honest limit of a guard that cannot name its caller, and it
is that way round on purpose: the alternative is a guard whose protection depends
on which caller happens to be asking.

``edit`` is not a subject here. Its ``old_string`` must already be in the file
before it changes anything, so it can only remove text the caller has shown it —
which is exactly what it is for, and narrowing it would delete the safe way to
change a large file.
"""

from __future__ import annotations

import threading
from pathlib import Path

#: How many receipts are kept. Each is four integers and a path, so this is a few
#: hundred kilobytes at worst; past it the oldest is dropped first, which costs a
#: caller one extra read and never a wrong answer (a missing receipt is a refusal).
MAX_RECEIPTS = 4096

_lock = threading.Lock()

#: ``(session, resolved path) -> (size, mtime_ns, shown_lines, total_lines)``.
#: Insertion-ordered, so the eviction below takes the oldest without sorting.
_receipts: dict[tuple[str, str], tuple[int, int, int, int]] = {}


def _key(session: str | None, path: Path) -> tuple[str, str]:
    """The bucket a receipt lives in: who was shown it, and which file."""
    return (session or "", str(path))


def record(
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    start_line: int,
    end_line: int,
    total_lines: int,
    session: str | None = None,
) -> None:
    """Note the lines of `path` at this version that a read has just shown.

    The stored count is a **contiguous prefix from line 1**: the read that starts
    at line 1 sets it, a later read that begins exactly where the last one ended
    extends it, and a window that begins anywhere else leaves it where it was. A
    window in the middle of a file therefore never counts as showing the file,
    which is the failure this module exists to prevent.
    """
    key = _key(session, path)
    with _lock:
        # Re-inserted rather than overwritten so insertion order keeps meaning
        # "least recently recorded", which is what the eviction rule reads.
        previous = _receipts.pop(key, None)
        shown = end_line if start_line <= 1 else 0
        if previous is not None:
            size_then, mtime_then, shown_then, _total_then = previous
            if size_then == size and mtime_then == mtime_ns:
                if start_line <= shown_then + 1:
                    shown = max(shown, end_line)
                else:
                    shown = max(shown, shown_then)
        _receipts[key] = (size, mtime_ns, shown, total_lines)
        while len(_receipts) > MAX_RECEIPTS:
            _receipts.pop(next(iter(_receipts)))


def coverage(
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    session: str | None = None,
) -> tuple[int, int] | None:
    """``(lines shown, lines held)`` for this exact version, or ``None``.

    ``None`` means "nothing has been read of this version" — never "the file is
    fine". The two are the whole distinction this module exists to keep: a caller
    that has been shown the file and one that has not are different states, and
    only the first may replace it. A returned pair whose first number is below its
    second is the third state, and it is the one the reproduction above lives in.
    """
    with _lock:
        receipt = _receipts.get(_key(session, path))
    if receipt is None:
        return None
    size_then, mtime_then, shown_then, total_then = receipt
    if size_then != size or mtime_then != mtime_ns:
        return None
    return shown_then, total_then


def forget(path: Path, *, session: str | None = None) -> None:
    """Drop a receipt. Used when a write makes the version it described obsolete."""
    with _lock:
        _receipts.pop(_key(session, path), None)


def clear() -> None:
    """Forget every receipt. For tests, which must not inherit another's reads."""
    with _lock:
        _receipts.clear()
