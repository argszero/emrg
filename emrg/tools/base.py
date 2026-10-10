"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).
"""

from __future__ import annotations

import math
import stat
from abc import ABC, abstractmethod

from emrg.server.tool_types import ToolDefinition, ToolResult


def count_argument(
    arguments: dict,
    *names: str,
    minimum: int,
    default: int | None = None,
    hint: str = "",
) -> tuple[int | None, str | None]:
    """Read an integer count out of a tool call, or hand back the refusal it earns.

    A JSON Schema says ``{"type": "integer"}`` and nothing more, so a parameter's
    lower bound exists only in the code that consumes the number — and every
    consumer here used to read an out-of-domain value as a **different number**
    rather than as a caller error. Measured 2026-10-08 (`cyc20261008-175325`):

    * ``read`` with ``line_limit=-3``: a 10-line file came back as lines 1-7, because
      ``all_lines[start:start-3]`` slices from the *end* — and the continuation hint
      printed ``truncated at start_line=-2``, a line number that cannot be asked for;
    * ``read`` with ``line_limit=0``: falsy, so an ``or`` chain read it as *absent*
      and returned the default 1000 lines;
    * ``grep`` with ``context_before=-2``: ``max(0, i - context_before)`` starts
      *after* the match, so the block printed its ``file:3:`` header and **no line at
      all** — the matching line itself was dropped;
    * the same ``grep`` call collapsed the stop budget ``max_results * (2 + cb + ca)``
      to zero, so the search stopped at the first match and the summary attributed it
      to ``max_results=200`` — a cause that was not the cause, with a remedy ("raise
      max_results") aimed at it.

    So the rule is the one this family uses for every other caller error (an invalid
    regex, a missing path): refuse, and name the parameter, the value and the domain.
    Answering from a range nobody asked for is the thing that must not happen
    silently.

    :param arguments: the tool call's parsed arguments.
    :param names: the parameter and its aliases, in precedence order. The refusal
        names the spelling the caller used. Presence is ``is not None``, never
        truthiness — ``0`` is a value, not an absence.
    :param minimum: the smallest value the parameter's domain admits.
    :param default: what an absent parameter means (``None`` when the tool has its
        own notion of "unset").
    :param hint: a clause saying what a correct call looks like, appended when given.
    :returns: ``(value, None)``, or ``(None, refusal)`` for the caller to report as
        ``ToolResult(..., error=True)``.
    """
    for name in names:
        if arguments.get(name) is not None:
            spelled = name
            raw = arguments[name]
            break
    else:
        return default, None

    def refuse(domain: str) -> tuple[None, str]:
        tail = f" {hint}." if hint else ""
        return None, (
            f"{spelled} {domain} (got {raw!r}); this call is refused rather than "
            f"answered with a different reading.{tail}"
        )

    if isinstance(raw, bool):
        # `True` is an `int` in Python but not a count in any schema.
        return refuse(f"must be an integer >= {minimum}; a boolean is not a count")
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return refuse(f"must be a finite integer >= {minimum}")
        if not raw.is_integer():
            return refuse(f"must be an integer >= {minimum}; a fractional count is not")
        value = int(raw)
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return refuse(f"must be an integer >= {minimum}")
    if value < minimum:
        return refuse(f"must be an integer >= {minimum}")
    return value, None


def boolean_argument(
    arguments: dict,
    *names: str,
    default: bool = False,
) -> tuple[bool | None, str | None]:
    """Read a boolean flag out of a tool call, or hand back the refusal it earns.

    The same rule as `count_argument`, one type over, and this is the carrier where
    getting it wrong **writes**: measured 2026-10-08 (`cyc20261008-175325`), `edit`
    with ``replace_all="false"`` — a string, truthy — replaced **both** occurrences of
    a two-occurrence ``old_string`` and answered ``Made 2 replacements``, i.e. it
    edited a second place the caller had explicitly excluded. Truthiness is the
    defect: ``"false"``, ``"no"``, ``0`` and ``""`` are all ``True`` to ``if``.

    The two string spellings of the value are read as the value they name, because a
    model that quotes a boolean means the boolean (the same leniency the shell tool
    documents for a numeric string). Anything else is refused rather than coerced.

    :param arguments: the tool call's parsed arguments.
    :param names: the parameter and its aliases, in precedence order.
    :param default: what an absent parameter means.
    :returns: ``(value, None)``, or ``(None, refusal)`` for the caller to report as
        ``ToolResult(..., error=True)``.
    """
    for name in names:
        if arguments.get(name) is not None:
            spelled = name
            raw = arguments[name]
            break
    else:
        return default, None

    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true", None
    return None, (
        f"{spelled} must be true or false (got {raw!r}); this call is refused rather "
        f"than run with a value read as its opposite."
    )


# The one non-regular kind that fails *immediately* instead of blocking: `open()` on a
# directory raises (`IsADirectoryError` / `PermissionError`) on every platform rather than
# waiting for a peer, so the FIFO/socket/device sentence does not describe it. Named once
# and read by both producers — `special_file_kind` returns it and `special_file_refusal`
# recognises it — so the two cannot drift into disagreeing about which kind this is.
_DIRECTORY_KIND = "a directory"


def special_file_kind(mode: int) -> str | None:
    """Name a file subject's kind from its ``st_mode`` — ``None`` when it is a regular file.

    Every file tool here opens its subject directly (``write_text`` / ``open``), and an
    **open is not a read**: on a FIFO it blocks until the other end appears — the one kind
    measured here, so the only one this docstring speaks for. Nothing in these tools asked
    what it was opening, so the subject's *kind* decided whether the call returned. Measured
    2026-10-10 (``cyc20261010-215146``) on a ``mkfifo`` named pipe, each call in its own
    process under a 10 s cap: ``read``, ``write`` and ``edit`` all **failed to return**,
    while the same three calls against a regular file returned normally.

    That is not a slow tool. ``daemon._run_tool_loop`` awaits ``tool.execute(args)`` **on
    the event loop** (``daemon.py:4504``, ``6931``) with no timeout, so the block is the
    whole daemon — every session, the scheduler and the evolution loop with it — and the
    only recovery is a restart, which belongs to the host alone.

    ``S_ISREG`` is the only kind those tools can act on, so the predicate is a whitelist:
    everything else is named and refused. Naming it is the point — a caller who pointed at
    a socket is told their path *is* a socket, instead of watching a call that never ends.

    :param mode: ``stat_result.st_mode`` of the resolved subject.
    :returns: a noun phrase for a non-regular subject (``"a directory"``, ``"a FIFO (named
        pipe)"``, …), or ``None`` when the subject is a regular file.
    """
    if stat.S_ISREG(mode):
        return None
    if stat.S_ISDIR(mode):
        return _DIRECTORY_KIND
    if stat.S_ISFIFO(mode):
        return "a FIFO (named pipe)"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    return "not a regular file"


def special_file_refusal(path: object, kind: str) -> str:
    """The one refusal the file tools give a subject that is not a regular file.

    Shared rather than written three times: the three tools refuse for the same reason,
    and three copies of the reason drift into three different explanations of one
    boundary. ``kind`` comes from :func:`special_file_kind`, so the caller is told what
    its path is before it is told what to do instead.

    Two failures live behind this one boundary, and the reason must match the one the
    subject actually produces. A **directory** fails at once — ``open`` raises rather
    than waits — while a FIFO, a socket and a device node *block*, and a block is the
    whole daemon (measured 2026-10-10, ``cyc20261010-215146``: ``read``/``write``/``edit``
    on a ``mkfifo`` did not return in 10 s). Telling a caller its directory "would never
    return" is false about the subject just named: the sentence was written for the kind
    that blocks and shown for both (the regression #2062 introduced, fixed here).
    """
    if kind == _DIRECTORY_KIND:
        return (
            f"Error: {path} is a directory, not a regular file. This tool opens its "
            f"subject directly, and a directory cannot be opened as a file — the call "
            f"fails at once rather than returning any bytes. Use the bash tool if you "
            f"need to touch it."
        )
    return (
        f"Error: {path} is {kind}, not a regular file. This tool opens its subject "
        f"directly, and opening this one blocks until a peer appears — a FIFO waits for "
        f"a writer, a socket for a connection, a device never ends — so the call would "
        f"never return, and neither would the daemon, which awaits tools on its event "
        f"loop. Use the bash tool if you need to touch it."
    )


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
