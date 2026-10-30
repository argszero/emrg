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
A fact about a **file version**, recorded by every file read: the version (size,
``mtime_ns`` and — once the whole file has been shown — a digest of its bytes),
how many lines from the start of the file that session has been shown, and how
many the file holds. ``write`` asks for one before it overwrites an existing
file, and refuses while ``shown`` is short of ``total`` — naming both numbers, so
the caller learns what it was about to remove.

Four properties are deliberate, and each closes a way the guard could lie:

* **Version-keyed, and the version is the bytes.** A file that changed after the
  read is not the file that was shown, so the receipt expires by itself instead
  of authorising a write against content nobody has seen. Size and ``mtime_ns``
  are the cheap half of that key and the digest is the half that cannot be
  spoofed by an unchanged pair of numbers: a whole-file replacement that preserves
  the length *and* the modification time is ordinary, not exotic — ``cp -p``,
  ``rsync -a``, ``tar -x`` and ``git checkout`` all restore the old timestamp onto
  new bytes — and a receipt keyed on the two numbers alone answers that state as
  though the file had not moved (measured 2026-10-30 on the branch this module
  was reviewed on: ``'AAAAAAAAAA'`` read, ``'BBBBBBBBBB'`` written back at the same
  length and the same ``mtime_ns``, whole-file write answered, and the unread
  content destroyed).
* **A contiguous prefix from line 1.** Reading a file in chunks extends the
  receipt when the next chunk starts where the last one ended, so the ordinary
  way of reading a long file does reach ``total``. A window that starts past the
  beginning leaves the count alone: it understates what was seen rather than
  overstating it, which is the only safe direction for a guard whose wrong answer
  destroys text.
* **A digest only when the whole file was shown.** A digest of a partial reading
  describes bytes the file no longer has as a whole, so recording one would
  vouch for a version nobody saw. Partial coverage therefore carries no digest,
  and :func:`content_matches` — which is asked only about a version the receipt
  already claims to have covered completely — answers ``False`` rather than
  falling back on the numbers.
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

``edit`` is not a subject here, but it is a **writer**, and the two tools share
one receipt for a reason. Its ``old_string`` must already be in the file before it
changes anything, so it can only remove text the caller has shown it. What it
must not do is leave a stale receipt behind: before this rule, a session that
wrote a file and then edited it could no longer rewrite the file whole, because
the edit had moved the version out from under the receipt the write had filed
(measured 2026-10-30, same review). :func:`emrg.tools.edit_tool.EditTool` now
re-records the receipt for the version it produced, and only when the version it
replaced had been completely shown — so a file the session authored stays
writable, and a file nobody read stays refused.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from emrg.tools.base import special_file_kind

#: How many receipts are kept. Each is four integers, a digest and a path, so this
#: is a few hundred kilobytes at worst; past it the oldest is dropped first, which
#: costs a caller one extra read and never a wrong answer (a missing receipt is a
#: refusal).
MAX_RECEIPTS = 4096

_lock = threading.Lock()

#: ``(session, resolved path) -> (size, mtime_ns, shown_lines, total_lines, digest)``.
#: Insertion-ordered, so the eviction below takes the oldest without sorting.
_receipts: dict[tuple[str, str], tuple[int, int, int, int, str | None]] = {}


def _key(session: str | None, path: Path) -> tuple[str, str]:
    """The bucket a receipt lives in: who was shown it, and which file."""
    return (session or "", str(path))


def digest_of(path: Path) -> str | None:
    """A digest of the bytes on disk, or ``None`` when they cannot be had.

    ``None`` is a refusal everywhere it is used, never "unchanged": a file this
    guard cannot read is one it cannot vouch for, and the two states must not
    collapse into each other.

    The subject's kind is asked first, for the reason every opener in this tree
    asks it: `open` on a FIFO blocks until a peer appears, and this runs on the
    daemon's event loop.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    if special_file_kind(info.st_mode) is not None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def record(
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    start_line: int,
    end_line: int,
    total_lines: int,
    session: str | None = None,
    digest: str | None = None,
) -> None:
    """Note the lines of `path` at this version that a read has just shown.

    The stored count is a **contiguous prefix from line 1**: the read that starts
    at line 1 sets it, a later read that begins exactly where the last one ended
    extends it, and a window that begins anywhere else leaves it where it was. A
    window in the middle of a file therefore never counts as showing the file,
    which is the failure this module exists to prevent.

    `digest` is stored **only when coverage is complete**: a digest of a partial
    reading does not describe the file as a whole, so keeping it would let a
    later write be judged against bytes nobody was shown.
    """
    key = _key(session, path)
    with _lock:
        # Re-inserted rather than overwritten so insertion order keeps meaning
        # "least recently recorded", which is what the eviction rule reads.
        previous = _receipts.pop(key, None)
        shown = end_line if start_line <= 1 else 0
        if previous is not None:
            size_then, mtime_then, shown_then, _total_then, _digest_then = previous
            if size_then == size and mtime_then == mtime_ns:
                if start_line <= shown_then + 1:
                    shown = max(shown, end_line)
                else:
                    shown = max(shown, shown_then)
        keeps_digest = digest if shown >= total_lines else None
        _receipts[key] = (size, mtime_ns, shown, total_lines, keeps_digest)
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
    size_then, mtime_then, shown_then, total_then, _digest_then = receipt
    if size_then != size or mtime_then != mtime_ns:
        return None
    return shown_then, total_then


def content_matches(
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    session: str | None = None,
) -> bool:
    """Whether the bytes on disk are the bytes this version's receipt was filed on.

    Asked only about a version whose coverage is already complete, and it answers
    **no** in every case it cannot decide: no receipt, a different version, a
    receipt that carries no digest (so the bytes were never recorded), an
    unreadable subject, or a digest that differs. The one answer that permits a
    whole-file replacement is a digest that matches.
    """
    with _lock:
        receipt = _receipts.get(_key(session, path))
    if receipt is None:
        return False
    size_then, mtime_then, _shown_then, _total_then, digest_then = receipt
    if size_then != size or mtime_then != mtime_ns or digest_then is None:
        return False
    return digest_of(path) == digest_then


def forget(path: Path, *, session: str | None = None) -> None:
    """Drop a receipt. Used when a write makes the version it described obsolete."""
    with _lock:
        _receipts.pop(_key(session, path), None)


def clear() -> None:
    """Forget every receipt. For tests, which must not inherit another's reads."""
    with _lock:
        _receipts.clear()
