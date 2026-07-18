"""Session management for EMRG.

Session ID format: s_YYMMDD_HHMM_xxxx
  - s_260713_2130_a3f9 = 2026-07-13 21:30, a3f9 = random suffix

Directory structure:
  <cwd>/.emrg/sessions/<session_id>/
    meta.json              # Session metadata
    history.jsonl          # Current conversation (compacted)
    history_YYMMDD.jsonl   # Daily full history (never compacted)
    llm.jsonl              # Raw LLM request/response
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_session_id(cwd: Path) -> str:
    """Generate a human-friendly session ID: s_YYMMDD_HHMM_xxxx."""
    now = datetime.now()
    prefix = f"s_{now.strftime('%y%m%d_%H%M')}_"
    sessions_dir = cwd / ".emrg" / "sessions"
    for _ in range(100):
        suffix = secrets.token_hex(2)[:4]
        sid = prefix + suffix
        if not (sessions_dir / sid).exists():
            return sid
    # Fallback: use longer suffix
    suffix = secrets.token_hex(4)[:8]
    return prefix + suffix


class Session:
    """Manages a single conversation session on disk."""

    def __init__(self, session_id: str, cwd: Path) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self._dir = cwd / ".emrg" / "sessions" / session_id
        self._dir.mkdir(parents=True, exist_ok=True)

        # Initialize memory subdirectory
        self._memory_dir = self._dir / "memory"
        self._memory_dir.mkdir(exist_ok=True)

        self._meta_path = self._dir / "meta.json"
        self._history_path = self._dir / "history.jsonl"
        self._llm_path = self._dir / "llm.jsonl"

        self._message_count: int = 0
        self._compact_count: int = 0
        self._created_at: str = ""
        self._updated_at: str = ""
        self._last_compact_at: str | None = None

        # Lazy memory store (import here to avoid circular imports)
        self._memory_store = None

    # ── Factory methods ───────────────────────────────────────

    @classmethod
    def create(cls, cwd: Path) -> Session:
        """Create a new session with a fresh session ID."""
        sid = generate_session_id(cwd)
        session = cls(sid, cwd)
        now = datetime.now().isoformat()
        session._created_at = now
        session._updated_at = now
        session._save_meta()
        logger.info("session created: %s in %s", sid, cwd)
        return session

    @classmethod
    def create_with_id(cls, session_id: str, cwd: Path) -> Session:
        """Create a new session with a specific session ID (from client)."""
        session = cls(session_id, cwd)
        now = datetime.now().isoformat()
        session._created_at = now
        session._updated_at = now
        session._save_meta()
        logger.info("session created with given id: %s in %s", session_id, cwd)
        return session

    @classmethod
    def load(cls, session_id: str, cwd: Path) -> Session:
        """Load an existing session from disk."""
        session = cls(session_id, cwd)
        if session._meta_path.exists():
            meta = json.loads(session._meta_path.read_text())
            session._message_count = meta.get("message_count", 0)
            session._compact_count = meta.get("compact_count", 0)
            session._created_at = meta.get("created_at", "")
            session._updated_at = meta.get("updated_at", "")
            session._last_compact_at = meta.get("last_compact_at")
        logger.info("session loaded: %s (%d messages)", session_id, session._message_count)
        return session

    # ── Properties ────────────────────────────────────────────

    @property
    def dir_path(self) -> Path:
        return self._dir

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    @property
    def memory_store(self):
        """Lazy-initialized SessionMemoryStore for this session's memory."""
        if self._memory_store is None:
            from emrg.memory import SessionMemoryStore
            self._memory_store = SessionMemoryStore(self._dir)
        return self._memory_store

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def compact_count(self) -> int:
        return self._compact_count

    @property
    def title(self) -> str:
        """Return the session title (custom title or fallback to session ID)."""
        meta = {}
        if self._meta_path.exists():
            try:
                meta = json.loads(self._meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return meta.get("title", self.session_id)

    def rename(self, title: str) -> None:
        """Set a custom title for this session."""
        self._updated_at = datetime.now().isoformat()
        self._save_meta_with_title(title)
        logger.info("session renamed: %s -> %s", self.session_id, title)

    # ── Message persistence ───────────────────────────────────

    def _daily_history_path(self) -> Path:
        """Return the daily history file path for today."""
        date_str = datetime.now().strftime("%y%m%d")
        return self._dir / f"history_{date_str}.jsonl"

    def append_message(self, record: dict) -> None:
        """Append a message record to both history.jsonl and daily history.

        Timestamp is always the first field for readability.
        Automatically sets timestamp if not provided.
        """
        if "timestamp" not in record:
            record["timestamp"] = datetime.now().isoformat()

        # Ensure timestamp is first key
        entry = {"timestamp": record.pop("timestamp")}
        entry.update(record)

        line = json.dumps(entry, ensure_ascii=False) + "\n"

        # Write to main history
        with open(self._history_path, "a") as f:
            f.write(line)

        # Write to daily history
        with open(self._daily_history_path(), "a") as f:
            f.write(line)

        self._message_count += 1
        self._updated_at = datetime.now().isoformat()
        self._save_meta()

    def append_llm(self, record: dict) -> None:
        """Append an LLM request/response record to llm.jsonl."""
        if "timestamp" not in record:
            record["timestamp"] = datetime.now().isoformat()

        entry = {"timestamp": record.pop("timestamp")}
        entry.update(record)

        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self._llm_path, "a") as f:
            f.write(line)

    # ── History reading ───────────────────────────────────────

    def _read_history(self) -> list[dict]:
        """Read all records from history.jsonl."""
        if not self._history_path.exists():
            return []
        records = []
        with open(self._history_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("corrupt line in history.jsonl, skipping")
        return records

    def get_messages_for_llm(self) -> list[dict]:
        """Load history and convert to OpenAI-compatible messages format.

        Handles:
        - message entries → role/content messages
        - Embedded tool_calls in assistant message (current format)
        - Separate tool_call + tool_result records (legacy interleaved format)
        - summary entries → user message with context prefix
        """
        records = self._read_history()
        messages: list[dict] = []
        i = 0
        while i < len(records):
            r = records[i]

            if r.get("type") == "message":
                msg: dict = {"role": r["role"], "content": r.get("content")}

                # Check for embedded tool_calls (current format)
                embedded_tc = r.get("tool_calls")
                if embedded_tc and r["role"] == "assistant":
                    tool_calls = [
                        {
                            "id": tc["id"],
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in embedded_tc
                    ]
                    # Collect tool results from subsequent records.
                    # Skip any tool_call records (redundant with embedded
                    # tool_calls in current format, or interleaved legacy).
                    j = i + 1
                    tool_msgs: list[dict] = []
                    while j < len(records) and records[j].get("type") in (
                        "tool_call", "tool_result",
                    ):
                        tr = records[j]
                        if tr.get("type") == "tool_result":
                            tool_msgs.append({
                                "role": "tool",
                                "tool_call_id": tr["tool_call_id"],
                                "content": tr["content"],
                            })
                        j += 1

                    result_ids = {tm["tool_call_id"] for tm in tool_msgs}
                    valid_tc = [tc for tc in tool_calls if tc["id"] in result_ids]
                    if valid_tc:
                        msg["tool_calls"] = valid_tc
                        msg["content"] = msg.get("content") or None
                        messages.append(msg)
                        messages.extend(tool_msgs)
                    else:
                        messages.append(msg)
                    i = j
                else:
                    # Legacy format: look ahead for tool_call + tool_result records
                    # Handles both aggregated (all calls then all results) and
                    # interleaved (call, result, call, result) patterns.
                    j = i + 1
                    tool_calls: list[dict] = []
                    tool_msgs: list[dict] = []
                    while j < len(records) and records[j].get("type") in ("tool_call", "tool_result"):
                        tc_or_tr = records[j]
                        if tc_or_tr.get("type") == "tool_call":
                            tool_calls.append({
                                "id": tc_or_tr["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": tc_or_tr["tool_name"],
                                    "arguments": json.dumps(
                                        tc_or_tr.get("arguments", {}), ensure_ascii=False
                                    ),
                                },
                            })
                        else:
                            tool_msgs.append({
                                "role": "tool",
                                "tool_call_id": tc_or_tr["tool_call_id"],
                                "content": tc_or_tr["content"],
                            })
                        j += 1

                    if tool_calls:
                        result_ids = {tm["tool_call_id"] for tm in tool_msgs}
                        valid_tc = [tc for tc in tool_calls if tc["id"] in result_ids]
                        if valid_tc:
                            msg["tool_calls"] = valid_tc
                            msg["content"] = msg.get("content") or None
                            messages.append(msg)
                            messages.extend(tool_msgs)
                        else:
                            messages.append(msg)
                        i = j
                    else:
                        messages.append(msg)
                        i += 1

            elif r.get("type") == "summary":
                messages.append({
                    "role": "user",
                    "content": f"[Previous conversation summary]\n{r['content']}",
                })
                i += 1

            else:
                i += 1

        return _validate_tool_messages(messages)

    # ── Compact ───────────────────────────────────────────────

    def compact(self, summary: str, keep_recent: int = 5) -> int:
        """Replace old messages with a summary, keeping the most recent ones.

        Args:
            summary: The LLM-generated summary text.
            keep_recent: Number of most recent records to preserve.

        Returns:
            Number of messages that were compacted.
        """
        records = self._read_history()

        if len(records) <= keep_recent:
            return 0

        compacted = records[:-keep_recent]
        recent = records[-keep_recent:]

        summary_record = {
            "timestamp": datetime.now().isoformat(),
            "type": "summary",
            "content": summary,
            "compact_id": f"c_{self._compact_count + 1:03d}",
            "compacted_message_count": len(compacted),
        }

        new_history = [summary_record] + recent
        self._write_history(new_history)

        self._compact_count += 1
        self._last_compact_at = datetime.now().isoformat()
        self._updated_at = self._last_compact_at
        self._save_meta()

        logger.info("compact: %d messages → summary (kept %d)", len(compacted), len(recent))
        return len(compacted)

    def _write_history(self, records: list[dict]) -> None:
        """Overwrite history.jsonl with new records."""
        with open(self._history_path, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ── Meta persistence ──────────────────────────────────────

    def _save_meta(self) -> None:
        self._save_meta_with_title(None)

    def _save_meta_with_title(self, title: str | None) -> None:
        meta = {
            "session_id": self.session_id,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
            "cwd": str(self.cwd),
            "message_count": self._message_count,
            "compact_count": self._compact_count,
            "last_compact_at": self._last_compact_at,
        }
        if title is not None:
            meta["title"] = title
        else:
            # Preserve existing title if present
            if self._meta_path.exists():
                try:
                    old = json.loads(self._meta_path.read_text())
                    if "title" in old:
                        meta["title"] = old["title"]
                except (json.JSONDecodeError, OSError):
                    pass
        self._meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    # ── Clear ──────────────────────────────────────────────────

    def clear(self) -> None:
        """Clear the session's message history, keeping metadata intact.

        Writes an empty history.jsonl file, resets message_count to 0,
        and creates a system note about the reset.
        """
        now = datetime.now().isoformat()
        reset_record = {
            "timestamp": now,
            "type": "message",
            "role": "system",
            "content": "[Session cleared]",
        }
        self._write_history([reset_record])
        self._message_count = 0
        self._updated_at = now
        self._save_meta()
        logger.info("session cleared: %s", self.session_id)

    # ── Static: list sessions ─────────────────────────────────

    @staticmethod
    def list_sessions(cwd: Path) -> list[dict]:
        """List all sessions in cwd/.emrg/sessions/, sorted by created_at desc.

        Returns a list of metadata dicts with keys:
            session_id, created_at, updated_at, cwd, message_count,
            compact_count, last_compact_at, title (if set)
        """
        sessions_dir = cwd / ".emrg" / "sessions"
        if not sessions_dir.exists():
            return []

        results: list[dict] = []
        for entry in sorted(sessions_dir.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                results.append(meta)
            except (json.JSONDecodeError, OSError):
                logger.warning("corrupt meta.json in %s, skipping", entry.name)

        results.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return results


def _validate_tool_messages(messages: list[dict]) -> list[dict]:
    """Post-process messages to ensure OpenAI API validity.

    - Assistant messages with tool_calls must be followed by matching tool messages.
    - Strips tool_calls that don't have matching tool messages immediately after.
    - Strips orphaned tool messages (no preceding assistant with tool_calls).

    Safety net for corrupted history (e.g. daemon crash during tool execution).
    """
    result: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if "tool_calls" in m:
            tc_needed = {tc["id"] for tc in m["tool_calls"]}
            j = i + 1
            tool_msgs: list[dict] = []
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msgs.append(messages[j])
                j += 1
            found = {tm["tool_call_id"] for tm in tool_msgs}
            valid = tc_needed & found

            if valid:
                m["tool_calls"] = [tc for tc in m["tool_calls"] if tc["id"] in valid]
                m["content"] = m.get("content") or None
                result.append(m)
                result.extend(tm for tm in tool_msgs if tm["tool_call_id"] in valid)
            else:
                m.pop("tool_calls", None)
                if m.get("content") is None:
                    m["content"] = ""
                result.append(m)
            i = j
        elif m.get("role") == "tool":
            i += 1
        else:
            result.append(m)
            i += 1

    return result
