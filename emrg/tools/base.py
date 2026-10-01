"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from emrg.server.tool_types import ToolDefinition, ToolResult


def as_count(value, default, minimum: int = 1):
    """`value` as an int at least `minimum`, or `default` when it is not one.

    One home for the domain of every count a tool takes from the model. A tool
    argument arrives as parsed JSON from an LLM's function call, so it is untrusted
    input: it can be a negative int, a float, a string, or absent.

    `minimum` is the only thing a call site decides, because it is the one thing that
    genuinely differs between counts:

    * ``minimum=0`` — 0 is a real request (no context lines), so only a negative value
      falls back. Use for a *window*.
    * ``minimum=1`` (the default) — 0 is not a useful count (no matches, no lines), so
      0 falls back too. Use for a *budget*.

    Letting a bad value through is never the smaller failure, because what it produces
    is not a smaller request but a different one, on the success path where nothing
    reports it. Measured on master `256400f4`, 2026-10-02:

    * ``grep(context_before=-1)`` is not a smaller window: it becomes
      ``range(i + 1, i + 1)``, so a one-match file answered ``Found 1 matches`` with an
      **empty block body** — ``error=False``, a summary claiming a match it never shows.
    * ``read(line_limit=-5)`` is not a limit: it becomes the slice bound ``-5``, so
      ``all_lines[start:-5]`` drops the *last* five lines and the continuation note
      prints ``truncated at start_line=-4`` — a line number that cannot exist.
    * ``read(start_line=8, line_limit=-2)`` selects nothing and prints
      ``(empty range: lines 8-5 of 11)`` — a backwards range.
    * ``read(line_limit=-1)`` returns the whole file and still says ``truncated at
      start_line=0``, so the one hint the caller may copy-paste names line 0.

    Not raising is the house rule the read tool already followed for `start_line`
    (``max(1, int(raw))`` with ``except → 1``): a nonsense value takes the documented
    default and the caller still gets its answer.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    return count if count >= minimum else default


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
