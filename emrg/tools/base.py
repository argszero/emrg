"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import PurePath

from emrg.server.tool_types import ToolDefinition, ToolResult


def relative_pattern_refusal(tool: str, pattern: str) -> ToolResult | None:
    """`tool`'s answer for a pattern pathlib will not glob, or None to proceed.

    pathlib's glob engine takes **relative** patterns: it parses the pattern
    against the directory it is searching and raises
    ``NotImplementedError("Non-relative patterns are unsupported")`` when the
    pattern carries its own root or drive. Measured on master `bc114ab9`,
    2026-10-02 — ``Path("/tmp/t").glob("/etc/*.conf")`` and
    ``Path("/tmp/t").glob(str(Path("/tmp/t") / "a.txt"))`` both raise it, while
    every spelling the two schemas document is relative and works.

    The refusal exists because that exception is not an answer, and neither tool
    currently gives one:

    * `glob` has a branch written for exactly this — ``Error: invalid pattern`` —
      but it catches ``(OSError, ValueError)``, and the shape those cover is
      ``'.'`` (``ValueError: Unacceptable pattern: PosixPath('.')``). An
      absolutely pathed pattern escapes it and reaches the daemon's generic
      crash handler: ``Tool execution error: Non-relative patterns are
      unsupported`` — an exception's own string, not the tool's words.
    * `grep`'s file collector has no guard at all, so the same input raises
      straight out of the tool to that same handler.

    Measured on both, same tree. The answer a caller needs is not "unsupported"
    but what to write instead, which is why this is a sentence and not a
    re-raise.

    Asked here rather than caught at the call sites because there are two of
    them and the rule is one fact. ``PurePath(pattern).drive or .root`` is the
    predicate pathlib itself applies (its ``Path._parse_path`` followed by
    ``if drv or root``), and ``tests/test_a_pattern_is_relative.py`` pins it
    against pathlib's real behaviour, so the two cannot drift apart unnoticed.
    """
    parsed = PurePath(pattern)
    if not (parsed.drive or parsed.root):
        return None
    return ToolResult(
        name=tool,
        content=(
            f"Error: '{pattern}' is not a relative pattern — {tool} searches the "
            f"directory it is given, so the pattern must be relative to it: write "
            f"'**/*.py' or 'emrg/tools/*.py', not a path from the filesystem root."
        ),
        error=True,
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
