"""Shared types for the EMRG tool system.

ToolDefinition and ToolResult are the universal types used by tool
executors, the daemon tool loop, and the LLM client. ToolCall is
handled via OpenAI's API dict format rather than as a dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolDefinition:
    """OpenAI-compatible tool definition for the API.

    name: tool name exposed to the model
    description: what the tool does (used by the model for routing)
    parameters: JSON Schema dict for the tool's arguments
    """

    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result of executing a tool, sent back to the LLM as a tool message."""

    tool_call_id: str = ""
    name: str = ""
    content: str = ""
    error: bool = False
