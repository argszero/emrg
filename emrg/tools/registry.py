"""Tool registry — holds all registered tools and serializes them
for the OpenAI function-calling API format.

Tools are registered at startup and read-only during execution,
so the registry is safe for concurrent access without locks.
"""

from __future__ import annotations

from typing import Optional

from emrg.tools.base import ToolExecutor


class ToolRegistry:
    """Registry of all available tools. Read-only after init."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolExecutor] = {}

    def register(self, executor: ToolExecutor) -> None:
        """Register a tool executor. Must be called before server starts."""
        name = executor.definition().name
        self._tools[name] = executor

    def get(self, name: str) -> Optional[ToolExecutor]:
        """Look up a tool by name. Returns None if not found."""
        return self._tools.get(name)

    def to_openai_tools(self) -> list[dict]:
        """Serialize all registered tools to OpenAI function-calling format.

        Returns a list of dicts suitable for the `tools` field
        of a chat completions API request:
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        """
        result: list[dict] = []
        for tool in self._tools.values():
            d = tool.definition()
            result.append({
                "type": "function",
                "function": {
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.parameters,
                },
            })
        return result

    @property
    def names(self) -> list[str]:
        """List all registered tool names."""
        return sorted(self._tools.keys())
