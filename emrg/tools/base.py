"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).

It also carries the three refusals every path-taking tool owes its caller, because
each of them was measured *not* being answered (2026-10-04, `cyc20261004-030427`,
`cyc20261004-032325`): a path the process cannot stat, a file it cannot read, and a
path that is not a regular file at all. The first two raised, so the caller saw the
daemon's generic ``Tool execution error: [Errno 13] Permission denied: '/…'``
instead of an answer; the third **never returned**. Five tools resolve a
caller-supplied path and three of them read or write one, so the rule lives here
rather than five times over.
"""

from __future__ import annotations

import stat
from abc import ABC, abstractmethod
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult


class ToolExecutor(ABC):
    """Interface for all tools in the EMRG micro-kernel.

    Each tool is a self-contained module: definition() describes the
    tool to the LLM (name + JSON Schema), execute() runs it locally.
    """

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the tool's name, description, and JSON Schema params."""
        ...

    @abstractmethod
    async def execute(self, arguments: dict) -> ToolResult:
        """Execute the tool with parsed arguments.

        Args:
            arguments: Dict parsed from the LLM's JSON function arguments.

        Returns:
            ToolResult with content string (tool output or error message).
        """
        ...


def _refusal_reason(exc: BaseException) -> str:
    """A short, human phrase for a filesystem's refusal, or the OS's own words.

    ``PermissionError`` is the one refusal worth naming rather than quoting: its
    message is ``[Errno 13] Permission denied: '/path'``, which repeats the path
    the caller already has and calls the cause "Permission denied" — but the
    *caller* here is a model reading a sentence, and "this process may not read
    that path" is the fact. Everything else is quoted from the OS, because a
    guess at its meaning is worth less than its words.
    """
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc)


def resolve_tool_path(value: str) -> tuple[Path | None, str | None]:
    """``Path(value).expanduser().resolve()``, or why the filesystem refused it.

    Returns ``(path, None)`` on success and ``(None, message)`` when the path
    cannot be resolved, where `message` is the complete sentence a tool puts in
    its ``ToolResult`` — one spelling for all five callers, because a message
    each tool composed itself is a message free to drift from the others.

    Two measured refusals (`cyc20261004-030427`, Python 3.13.9, this host), both
    of which used to leave ``execute()`` as an exception:

    =========================================  ==================================
    what the caller passed                     what ``read`` did
    =========================================  ==================================
    ``/tmp/locked/c.py`` (``chmod 000`` dir)   ``PermissionError: [Errno 13]``
    ``"a\\x00b"``                                ``ValueError: lstat: embedded
                                               null character in path``
    =========================================  ==================================

    :param value: the path as the caller spelled it.
    :returns: the resolved path, or the refusal sentence.
    """
    try:
        return Path(value).expanduser().resolve(), None
    except (OSError, ValueError) as exc:
        # A NUL byte is not an OS refusal but a path no filesystem accepts, and
        # `Path.resolve` surfaces it as a ValueError whose own text ("lstat:
        # embedded null character in path") names a syscall the caller never
        # made. The cause belongs in the sentence; the syscall does not.
        if "\x00" in value:
            return None, (
                f"Error: cannot use path {value!r}: it contains a NUL character, "
                f"which no filesystem accepts"
            )
        return None, f"Error: cannot use path {value!r}: {_refusal_reason(exc)}"


def not_a_regular_file(path: Path) -> str | None:
    """Why this path cannot be read or written as a file, or ``None`` when it can.

    A sentence, not a boolean, for the same reason the other two refusals are: the
    caller has to be told *what* it named, and "it is a FIFO (named pipe)" is the
    whole of the answer — a reader who asked for a path on purpose knows what a
    pipe is, and one who did not has just learned where the mistake is.

    Measured 2026-10-04 (`cyc20261004-032325`) on master `a3d0a5ea`, Python 3.13.9,
    this host, against a tree holding one named pipe made with ``os.mkfifo``:

    ========================  ==========================================
    the call                  what it did
    ========================  ==========================================
    ``read`` the FIFO         **blocked past 3.01 s**, killed by the probe's own
                              alarm — it never returned on its own
    ``edit`` the FIFO         same, 3.00 s
    ``write`` the FIFO        same, 3.01 s
    ``grep``/``glob`` the tree  answered at once (they collect with
                              ``Path.is_file()``, which is False for a FIFO)
    ========================  ==========================================

    That is why this is a *refusal* rather than a slowdown: a tool call has no
    timeout, so a call that never returns wedges the turn — the client's busy state
    never clears and no frame follows (the class PR #1669 records). ``stat`` says
    which kind it is; the kinds below are the ones a filesystem can report that are
    not a regular file. A path that does not exist is **not** this predicate's
    business — the callers' own existence checks answer that, and answering "not a
    regular file" for a missing file would be a claim about a stat that never ran.

    :param path: the target, already resolved.
    :returns: the refusal sentence, or ``None`` when it is a regular file.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    kinds = (
        (stat.S_ISFIFO, "a FIFO (named pipe)"),
        (stat.S_ISSOCK, "a socket"),
        (stat.S_ISCHR, "a character device"),
        (stat.S_ISBLK, "a block device"),
        (stat.S_ISDIR, "a directory"),
    )
    for test, kind in kinds:
        if test(mode):
            return (
                f"Error: {path} is {kind}, not a regular file — this tool reads and "
                f"writes regular files, and opening this one can block indefinitely"
            )
    return None


def read_text_or_refusal(
    path: Path, *, newline: str | None = None
) -> tuple[str | None, str | None]:
    """A file's text, or the reason this process could not read it.

    Returns ``(text, None)`` or ``(None, message)``, `message` being the whole
    sentence. Two tools read a file as text and both caught only
    ``UnicodeDecodeError``, so the *other* way the same call fails left
    ``execute()`` as an exception — measured (`cyc20261004-030427`) against a
    file whose own mode is ``000`` while its directory is readable, which is the
    realistic shape (another user's file, a secret, a key):

    =================================  ==========================================
    the file                           what ``read`` / ``edit`` did
    =================================  ==========================================
    ``..\\secret.txt`` (``chmod 000``)     ``PermissionError: [Errno 13] Permission
                                       denied: '/…/secret.txt'``
    =================================  ==========================================

    Both are "the bytes were not delivered", which is one fact about the call and
    belongs in one sentence. ``newline=""`` is how ``edit`` gets the file's own
    terminators back out (issue #1803), so it is a parameter rather than a second
    implementation.

    :param path: the file to read.
    :param newline: passed through to ``Path.open`` — ``""`` keeps terminator bytes.
    :returns: the text, or the refusal sentence.
    """
    refusal = not_a_regular_file(path)
    if refusal:
        return None, refusal
    try:
        return path.open("r", encoding="utf-8", newline=newline).read(), None
    except UnicodeDecodeError:
        return None, f"Error: cannot read {path} as text (not UTF-8; binary file?)"
    except OSError as exc:
        return None, f"Error: cannot read {path}: {_refusal_reason(exc)}"
