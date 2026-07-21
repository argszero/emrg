"""Protocol types for EMRG client-server communication.

Mirrors the Rust emrg-protocol crate. All messages are JSON over IPC
(newline-delimited, one JSON object per line).

The actual IPC transport is abstracted by emrg.connect (platform-adaptive:
Unix domain socket on macOS/Linux, Named Pipe on Windows).

Message types:
  Client → Server: ping, task
  Server → Client: pong, delta, done, tool_start, tool_end, error
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class TaskRequest:
    """Sent from client to server to execute a task."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    cwd: str = ""
    prompt: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )
    stream: bool = False

    def to_dict(self) -> dict:
        return {
            "type": "task",
            "id": self.id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "prompt": self.prompt,
            "timestamp": self.timestamp,
            "stream": self.stream,
        }


@dataclass
class TaskResponse:
    """Streaming delta or completion from server to client."""
    request_id: str = ""
    content: str = ""
    done: bool = False
    delta: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> TaskResponse:
        return cls(
            request_id=d.get("request_id", ""),
            content=d.get("content", ""),
            done=d.get("done", False),
            delta=d.get("delta", False),
        )


@dataclass
class ToolStart:
    """Emitted when the LLM invokes a tool."""
    request_id: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> ToolStart:
        return cls(
            request_id=d.get("request_id", ""),
            tool_name=d.get("tool_name", ""),
            tool_call_id=d.get("tool_call_id", ""),
            arguments=d.get("arguments", {}),
        )


@dataclass
class ToolEnd:
    """Emitted when a tool has finished executing."""
    request_id: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    content: str = ""
    error: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> ToolEnd:
        return cls(
            request_id=d.get("request_id", ""),
            tool_name=d.get("tool_name", ""),
            tool_call_id=d.get("tool_call_id", ""),
            content=d.get("content", ""),
            error=d.get("error", False),
        )


@dataclass
class ServerPong:
    """Server status for client polling."""
    identity: Optional[dict] = None
    uptime_seconds: int = 0
    evolution_count: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> ServerPong:
        return cls(
            identity=d.get("identity"),
            uptime_seconds=d.get("uptime_seconds", 0),
            evolution_count=d.get("evolution_count", 0),
        )


@dataclass
class EvolutionLog:
    """Evolution log entry written by the task scheduler's evolution handler."""
    timestamp: str = ""
    trigger: str = ""
    impact: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    upstream_contribution: Optional[dict] = None


@dataclass
class InstanceIdentity:
    """Identity of this EMRG instance in the upstream ecosystem."""
    instance_id: str = ""
    host_name: str = ""
    fork_source: Optional[str] = None
    branch_id: str = "master"
