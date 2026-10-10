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


def brace_alternation(pattern: str) -> bool:
    """True when a glob carries `{a,b}` alternation — a comma inside braces.

    The file-finding tools walk with `Path.glob`/`Path.rglob`, whose pattern
    language has no brace expansion: `*.{py,rs}` is not two patterns, it is one
    literal string that matches no file whose name contains a brace. The failure
    is silent and it is a **false negative** - `No matches` / `No files matched`,
    the same sentence a real absence produces - so the tool answers "nothing
    here" about a question it never asked.

    Measured 2026-10-10 (`cyc20261010-220909`) in this checkout, `grep` over
    `emrg/tools/`:

        glob='*.py'       -> Found 9 matches ... (searched 15 files)
        glob='*.{py,rs}'  -> No matches ... (searched 0 files)

    **The comma is the whole predicate, and a bare brace is not one.** This
    function first answered `"{" in pattern or "}" in pattern`, and that refused
    patterns the walk reads correctly: `Path.glob` gives `{` no special meaning,
    so `a{b}.py` is the exact name of a real file. Measured 2026-10-10
    (`cyc20261011-001130`) on a tree holding a file literally named `a{b}.py`:
    `glob` and `grep` both answered `Found 1 matches … a{b}.py` on master and both
    **refused** it on the branch, with a message asserting `{a,b}` alternation the
    input does not carry. That is the over-block this repository named once
    already (`emrg/tools/command_scan.py`, the #1513 lesson: `echo sh "patch …"`
    was a bug, not a safe over-block). Alternation needs two alternatives, so the
    subject is a brace group holding a comma - `{b}` has nothing to alternate
    between.

    What a caller does with this is the caller's half: both tools **also** require
    that the pattern selected nothing before they refuse, so a comma-bearing
    pattern that selects files (`a{,b}.py`, a literal name) is searched normally.
    :func:`emrg.tools.glob_tool.GlobTool.execute` and
    :meth:`emrg.tools.grep_tool.GrepTool.execute` each say so where they check.

    :param pattern: the glob pattern as the caller passed it.
    :returns: True when the pattern is alternation-shaped.
    """
    depth = 0
    for char in pattern:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "," and depth:
            return True
    return False


def special_file_kind(mode: int) -> str | None:
    """Name a file subject's kind from its ``st_mode`` — ``None`` when it is a regular file.

    Every file tool here opens its subject directly (``write_text`` / ``open``), and an
    **open is not a read**: on a FIFO it blocks until the other end appears, on a socket
    until a connection does, and a character device has no end of file at all. Nothing in
    these tools asked what it was opening, so the subject's *kind* decided whether the call
    returned. Measured 2026-10-10 (``cyc20261010-215146``) on a ``mkfifo`` named pipe, each
    call in its own process under a 10 s cap: ``read``, ``write`` and ``edit`` all **failed
    to return**, while the same three calls against a regular file returned normally.

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
        return "a directory"
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
    """
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
