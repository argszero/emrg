"""Tool executor base class — the interface every tool must implement.

Follows the Codex ToolExecutor pattern: a tool defines its spec
(via definition()) and executes via execute(arguments).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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


def _split_top_level_commas(text: str) -> list[str]:
    """`text` split on the commas that are not inside a nested `{...}`."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def expand_braces(pattern: str) -> list[str]:
    """Every plain glob pattern `pattern`'s `{a,b}` alternation stands for.

    pathlib's glob engine has no brace alternation — `Path.glob("*.{py,rs}")` looks
    for a file literally named ``something.{py,rs}`` — and that is a promise this
    repo's own schema makes: `grep`'s description offers **`'*.{py,rs}'`** as an
    example. Measured on master `256400f4`, 2026-10-02:

    * ``grep(glob='*.{py,rs}')`` -> ``No matches for 'MARK' ...`` and
      ``glob('*.{py,rs}')`` -> ``No files matched pattern '*.{py,rs}'``
    * while ``'*.py'`` found the Python file and ``'*.rs'`` found the Rust one

    So a caller following the documentation got an **empty answer shaped like a real
    one** — "there are none", never "that pattern cannot work". Expanding the
    alternation is what makes the documented example true; the alternative (deleting
    the example) makes the tool's own description smaller than the engine under it.

    The shell's own rules are followed where they are cheap and unambiguous:

    * an unmatched ``{`` is a literal, not an error (``foo{bar`` is one pattern);
    * ``{}`` with nothing between the braces is a literal too, rather than an
      empty alternative that would silently delete it;
    * nesting and more than one group both work (``{a,b}{c,d}`` is four patterns,
      ``{a,{b,c}}`` is three), and duplicates are dropped so a caller's union is
      computed once per pattern.
    """
    open_at = pattern.find("{")
    if open_at == -1:
        return [pattern]

    depth, close_at = 0, -1
    for i in range(open_at, len(pattern)):
        if pattern[i] == "{":
            depth += 1
        elif pattern[i] == "}":
            depth -= 1
            if depth == 0:
                close_at = i
                break
    if close_at == -1 or close_at == open_at + 1:
        return [pattern]

    head, tail = pattern[:open_at], pattern[close_at + 1:]
    out: list[str] = []
    for alternative in _split_top_level_commas(pattern[open_at + 1:close_at]):
        for expanded in expand_braces(head + alternative + tail):
            if expanded not in out:
                out.append(expanded)
    return out
