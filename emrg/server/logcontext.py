"""Per-task session label for the daemon log [session] column.

Rant 2026-08-24T10:48:50: emrgd.log lines from daemon session-core paths
(task received / tool loop round / LLM stream / round finish / tool call /
tool execution) had no session context, so concurrent sessions (interactive
client + evolution task + journal task) were indistinguishable. This
ContextVar carries the current session label (``session_id`` or
``session_id:name`` when the session has a title) for the duration of a
message's tool loop.

asyncio copies the context into each new task, so a label pushed before
``asyncio.create_task()`` is inherited by the child tool-loop task and never
bleeds into other tasks. Logging from worker threads (which have their own
default context) falls back to ``-`` in the formatter.
"""

from __future__ import annotations

import contextvars

session_label: contextvars.ContextVar[str] = contextvars.ContextVar(
    "emrg_session_label", default=""
)


def push_session_label(label: str) -> contextvars.Token[str]:
    """Set the session label for the current task context.

    Returns the token so the caller can ``reset()`` it once the tool-loop
    task has been spawned (the child task keeps its own copy of the context,
    so resetting in the parent does not affect it).
    """
    return session_label.set(label)


def session_label_for(session_id: str, title: str) -> str:
    """Build the display label: ``session_id:title`` when the session has a
    non-trivial title, otherwise just the session id (rant 10:48:50 — "若会话
    有 name（标题）则显示 session_id:name；无 name 时仅显示 id")."""
    if title and title != session_id:
        return f"{session_id}:{title}"
    return session_id
