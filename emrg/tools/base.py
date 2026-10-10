"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).
"""

from __future__ import annotations

import math
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
    """True when a glob carries `{a,b}` alternation.

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

    `grep`'s description advertised exactly that pattern as an example, so a
    model following it got the false negative. Shared here rather than spelled in
    each tool because both are held to the same rule: a pattern this walk cannot
    expand is refused with the reason, never searched to an empty answer.
    """
    return "{" in pattern or "}" in pattern


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
