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

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Iterable

from emrg.memory import SessionMemoryStore
from emrg.sandbox.policy import SANDBOX_MODES
from emrg.sandbox.roots import canonical_path
from emrg.sessions_index import remove_session_index, upsert_session_index

logger = logging.getLogger(__name__)

# ── llm.jsonl rotation ────────────────────────────────────────
_LLM_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_LLM_LOG_BACKUP_COUNT = 2


def generate_session_id(cwd: Path) -> str:
    """Generate a human-friendly session ID: s_YYMMDD_HHMM_xxxxxxxx.

    Suffix uses 4 random bytes (32 bits of entropy) — a 2-byte suffix
    (16 bits, 65536 possibilities) made the 10-sample uniqueness test
    flaky via the birthday paradox (~0.07% collision per run; observed
    on CI master push run 31145421676).
    """
    now = datetime.now()
    prefix = f"s_{now.strftime('%y%m%d_%H%M')}_"
    sessions_dir = cwd / ".emrg" / "sessions"
    for _ in range(100):
        suffix = secrets.token_hex(4)
        sid = prefix + suffix
        if not (sessions_dir / sid).exists():
            return sid
    # Fallback: same 4-byte entropy (loop exhaustion is astronomically rare)
    suffix = secrets.token_hex(4)
    return prefix + suffix


def _parse_instant(value: object) -> datetime | None:
    """An ISO-8601 instant from a stored timestamp, or `None` if unreadable.

    `None` is "could not measure", never "epoch": the caller keeps the record
    when this returns `None`, because excluding evidence is the direction that
    cannot be noticed.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def records_since(records: list[dict], since: str) -> list[dict]:
    """The records from `since` (an ISO-8601 instant) onward — the run's own.

    A task session is **one session for every run of that task**: `emrg-evolution-<name>`
    is reused cycle after cycle, so its history holds all of them. Anything reading
    "the recent history" as evidence about *this* run therefore reads the previous
    run's work as if it were this one's, unless it knows where the run began. This
    function is that knowledge, applied: `since` is the instant the run was dispatched,
    and it is carried in the `task` frame the scheduler already sends, so no clock is
    consulted and no state is kept.

    Fail-open in both directions, deliberately:

    * an empty or unparseable `since` returns the list **unchanged** — a window whose
      start is unknown is the window the session always had, and guessing a boundary
      would be worse than not having one;
    * a record whose own timestamp is missing or unreadable is **kept** — dropping it
      would delete evidence silently, and a summary that read one record too many is
      the smaller error than one that cannot see a tool call at all.

    Comparing a naive timestamp with an aware one raises, and a stored timestamp is
    naive local time (`datetime.now().isoformat()`), so exactly one side being aware
    is treated as unmeasurable rather than as an order.
    """
    if not since:
        return list(records)
    cutoff = _parse_instant(since)
    if cutoff is None:
        return list(records)
    kept: list[dict] = []
    for record in records:
        at = _parse_instant(record.get("timestamp"))
        if at is None or (at.tzinfo is None) != (cutoff.tzinfo is None):
            kept.append(record)
            continue
        if at >= cutoff:
            kept.append(record)
    return kept


def records_to_messages(records: list[dict]) -> list[dict]:
    """Convert stored history records to OpenAI-compatible messages.

    Handles:
    - message entries -> role/content messages
    - Embedded tool_calls in assistant message (current format)
    - Separate tool_call + tool_result records (legacy interleaved format)
    - summary entries -> user message with context prefix

    A pure function of `records` on purpose: `Session.get_messages_for_llm`
    is its history, and the content-risk probe asks the provider about a
    candidate record list that was never written anywhere — the question has
    to be the one the refusal answered, so both go through here.
    """
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

        # Initialize images subdirectory
        self.images_dir = self._dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)

        self._meta_path = self._dir / "meta.json"
        self._history_path = self._dir / "history.jsonl"
        self._llm_path = self._dir / "llm.jsonl"

        self._message_count: int = 0
        self._compact_count: int = 0
        self._created_at: str = ""
        self._updated_at: str = ""
        self._last_compact_at: str | None = None
        # The session's sandbox tier, or None when the session has never been
        # given one (rant 2026-09-30T09:30:16). Stored, not defaulted: the
        # default belongs to the path that *starts* a turn — a client turn runs
        # at `workspace-write` when nothing was declared, while the upgrade
        # session's silence keeps meaning "unconfined" (`policy.DEFAULT_MODE`),
        # and a property that answered the default would erase that difference.
        self._sandbox: str | None = None
        # The roots the host named for this session on top of the tier, in the
        # order they were added (rant 2026-10-09T09:43:39). Empty is the honest
        # default: a session nobody has named a root for grants exactly what its
        # tier derives, which is what every session did before this existed.
        # Canonical spellings only — the writer canonicalizes before storing, so
        # a reader never has to wonder which spelling was meant.
        self._sandbox_roots: list[str] = []

        # Lazy-initialized memory store
        self._memory_store = None

    # ── Image support ─────────────────────────────────────────

    def save_image(self, data: bytes, label: str = "") -> str:
        """Save an image to the session images directory with dedup.

        Uses BLAKE2b hash for content-based dedup — same image pasted
        multiple times reuses the same file.

        Returns the filename (relative to images_dir).
        """
        h = hashlib.blake2b(data, digest_size=4).hexdigest()
        # Check for existing file with same hash
        for existing in self.images_dir.iterdir():
            if existing.is_file() and h in existing.name:
                return existing.name

        # Determine counter from existing images
        counter = 1
        for existing in self.images_dir.iterdir():
            if existing.is_file() and existing.name.startswith("img_"):
                try:
                    n = int(existing.name.split("_")[1])
                    if n >= counter:
                        counter = n + 1
                except (ValueError, IndexError):
                    pass

        # Use label as filename stem if provided (sanitized), else default
        if label:
            safe_label = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)
            safe_label = safe_label[:40].rstrip("._") or "image"
        else:
            safe_label = f"Image{counter}"

        filename = f"{safe_label}_{h}.png"
        filepath = self.images_dir / filename

        # If filename collision (unlikely but possible), append counter
        while filepath.exists():
            filename = f"{safe_label}_{counter}_{h}.png"
            filepath = self.images_dir / filename
            counter += 1

        filepath.write_bytes(data)
        logger.info("image saved: %s (%d bytes)", filename, len(data))
        return filename

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
            meta = json.loads(session._meta_path.read_text(encoding="utf-8"))
            session._message_count = meta.get("message_count", 0)
            session._compact_count = meta.get("compact_count", 0)
            session._created_at = meta.get("created_at", "")
            session._updated_at = meta.get("updated_at", "")
            session._last_compact_at = meta.get("last_compact_at")
            session._sandbox = meta.get("sandbox")
            stored_roots = meta.get("sandbox_roots")
            if isinstance(stored_roots, list):
                session._sandbox_roots = [r for r in stored_roots if isinstance(r, str)]
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
                meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return meta.get("title", self.session_id)

    def rename(self, title: str) -> None:
        """Set a custom title for this session."""
        self._updated_at = datetime.now().isoformat()
        self._save_meta_with_title(title)
        logger.info("session renamed: %s -> %s", self.session_id, title)

    # ── Sandbox tier ──────────────────────────────────────────

    @property
    def sandbox(self) -> str | None:
        """This session's sandbox tier, or ``None`` when it has never had one.

        Read by the daemon as the *session* half of a turn's tier (rant
        2026-09-30T09:30:16). Deliberately not defaulted — see the attribute's
        comment in ``__init__``.
        """
        return self._sandbox

    def set_sandbox(self, mode: str) -> None:
        """Persist this session's sandbox tier into ``meta.json``.

        The only writer of the key, and it validates before writing: an unknown
        mode is refused and leaves the file untouched, so a typo cannot become a
        stored tier that every later turn inherits.
        """
        if mode not in SANDBOX_MODES:
            raise ValueError(
                f"unknown sandbox mode {mode!r} (expected one of {', '.join(SANDBOX_MODES)})"
            )
        self._sandbox = mode
        self._save_meta()
        logger.info("session sandbox tier set: %s -> %s", self.session_id, mode)

    @property
    def sandbox_roots(self) -> list[str]:
        """The host-named writable roots this session carries, in added order.

        A copy, so a reader cannot mutate the session's list by accident — the
        daemon injects this value into every tool call, and an injected list a
        tool could edit in place would be a second writer of a fact the daemon
        owns.
        """
        return list(self._sandbox_roots)

    def set_sandbox_roots(self, roots: "list[str] | tuple[str, ...]") -> None:
        """Replace this session's host-named writable roots and persist them.

        The **only** writer of the key, and it does not validate the paths: the
        refusals (``/``, the home directory, a root covering a protected daemon
        file) belong to the daemon's command, which is where a person is waiting
        for an answer and where a refusal can name the reason.  This method is
        storage.  Its one precondition is the shape the reader depends on —
        absolute, canonical spellings — and it canonicalizes rather than
        trusting the caller, so a symlink handed in here is stored as the path
        the enforcement layer will actually compare against.
        """
        self._sandbox_roots = [canonical_path(str(r)) for r in roots]
        self._save_meta()
        logger.info(
            "session sandbox roots set: %s -> %d root(s)", self.session_id, len(self._sandbox_roots)
        )

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
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(line)

        # Write to daily history
        with open(self._daily_history_path(), "a", encoding="utf-8") as f:
            f.write(line)

        # message_count counts message-type records only — tool_result / summary
        # records are persisted but do not inflate the user-facing message count
        # (rant 2026-08-31T14:18:14: count previously grew for every record,
        # including tool_results, and compact never decremented it).
        if record.get("type", "message") == "message":
            self._message_count += 1
        self._updated_at = datetime.now().isoformat()
        self._save_meta()

    def append_llm(self, record: dict) -> None:
        """Append an LLM request/response record to llm.jsonl.

        Automatically rotates the file when it exceeds 50 MB, keeping
        up to 2 backup files (llm.jsonl.1, llm.jsonl.2).
        """
        if "timestamp" not in record:
            record["timestamp"] = datetime.now().isoformat()

        entry = {"timestamp": record.pop("timestamp")}
        entry.update(record)

        line = json.dumps(entry, ensure_ascii=False) + "\n"

        # Rotate if file exceeds max size
        self._rotate_llm_log()

        with open(self._llm_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _rotate_llm_log(self) -> None:
        """Rotate llm.jsonl if it exceeds _LLM_LOG_MAX_BYTES."""
        if not self._llm_path.exists():
            return
        try:
            size = os.path.getsize(self._llm_path)
        except OSError:
            return
        if size < _LLM_LOG_MAX_BYTES:
            return

        # Shift existing backups: .2 → .3 (delete), .1 → .2, main → .1
        for i in range(_LLM_LOG_BACKUP_COUNT, 0, -1):
            old = Path(str(self._llm_path) + f".{i}")
            new = Path(str(self._llm_path) + f".{i + 1}")
            if i == _LLM_LOG_BACKUP_COUNT and new.exists():
                new.unlink()
            if old.exists():
                old.rename(new)

        # Rename current to .1
        backup_path = Path(str(self._llm_path) + ".1")
        self._llm_path.rename(backup_path)

    # ── History reading ───────────────────────────────────────

    def _read_history(self) -> list[dict]:
        """Read all records from history.jsonl."""
        if not self._history_path.exists():
            return []
        records = []
        with open(self._history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("corrupt line in history.jsonl, skipping")
        return records

    def get_messages_for_llm(self, since: str = "") -> list[dict]:
        """This session's history, converted to OpenAI-compatible messages.

        The conversion itself is :func:`records_to_messages`, which is a function
        of the record list and nothing else — the content-risk probe (rant
        2026-09-29T15:55:44.643795+08:00) has to ask the provider about a
        *candidate* record list it has not written anywhere, and asking with a
        different conversion than the live request uses would be asking a
        different question than the refusal answered.

        `since` narrows the records first, through :func:`records_since`: a task
        session is reused by every run of its task, so a reader that wants *this*
        run's work has to say where it began. The default is the whole history,
        which is what this method always returned.
        """
        return records_to_messages(records_since(self._read_history(), since))


    # ── Compact ───────────────────────────────────────────────

    #: Appended to a retained record that the tail budget had to cut. Issue
    #: #1992: `keep_recent` was an unconditional slice, so one oversized tool
    #: result — a `grep` returned 2,676,553 chars in session s_260727_0946_dfa1
    #: on 2026-10-09 — stayed in the retained tail through every compaction.
    #: The surface then never fell below the auto-compact gate's trigger: the
    #: gate re-fired round after round (102 compacts in a day), each retried
    #: request was refused as overlong, and the session stopped answering —
    #: a compaction reporting success while nothing shrank.
    TAIL_TRUNCATION_NOTICE = (
        "\n\n[truncated on compaction: this record held {total} chars, and one "
        "record of the retained tail may hold at most {budget}; {omitted} chars "
        "were dropped. The untrimmed record is still in history_YYMMDD.jsonl.]"
    )

    @staticmethod
    def _record_chars(record: dict) -> int:
        """The size `compact` budgets: a record's content, as stored."""
        content = record.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return len(content)

    def _fit_tail(self, recent: list[dict], max_tail_chars: int) -> list[dict]:
        """Return ``recent`` with every record trimmed to its share of the budget.

        The budget is **shared equally** among the retained records, and only a
        record that is over its share is touched. Spreading it newest-first was
        the first attempt: the oversized record took the whole budget and the
        older — much smaller — ones were then dropped, which is the wrong trade
        here. The summarizer that produced this compact may have been the
        chunker, and that one summarizes only the records *before* `keep_recent`
        (`_chunked_compact`: ``to_compact = records[:-keep_recent]``), so a
        dropped tail record is gone from the conversation, not absorbed into the
        summary. Sharing keeps every live turn's end and trims only what is over
        its share; the total is then at most the budget by construction.

        Text records only: a record whose content is not a string (a multimodal
        list) is left as it is — slicing it would destroy parts — and the
        estimator bounds those with `_TOKENS_PER_IMAGE`.
        """
        share = max(1, max_tail_chars // max(1, len(recent)))
        fitted: list[dict] = []
        for record in recent:
            if self._record_chars(record) > share:
                record = self._trim_record(record, share)
            fitted.append(record)
        return fitted

    def _trim_record(self, record: dict, max_chars: int) -> dict:
        """Copy ``record`` with its text content cut to ``max_chars`` + the notice."""
        record = dict(record)
        content = record.get("content", "")
        if not isinstance(content, str):
            return record
        notice = self.TAIL_TRUNCATION_NOTICE.format(
            total=len(content),
            budget=max_chars,
            omitted=len(content) - max_chars,
        )
        record["content"] = content[: max(0, max_chars - len(notice))] + notice
        record["truncated"] = True
        return record

    def compact(
        self,
        summary: str,
        keep_recent: int = 5,
        max_tail_chars: int | None = None,
    ) -> int:
        """Replace old messages with a summary, keeping the most recent ones.

        Args:
            summary: The LLM-generated summary text.
            keep_recent: Number of most recent records to preserve.
            max_tail_chars: Budget for the retained tail. ``None`` keeps the
                historical behaviour — ``keep_recent`` records whatever their
                size; a number gives the retained records an equal share of it
                and trims those over their share (issue #1992).

        Returns:
            Number of messages that were compacted. A record trimmed to the tail
            budget is still in the history, so it does not count.
        """
        records = self._read_history()

        if len(records) <= keep_recent:
            if max_tail_chars is None:
                return 0
            # Nothing left to summarize away, but the retained tail can still be
            # over budget — and returning 0 here without trimming is exactly
            # issue #1992's trap: the gate re-fires on every round forever.
            recent = self._fit_tail(records, max_tail_chars)
            if recent == list(records):
                return 0
            compacted = []
        else:
            compacted = records[:-keep_recent]
            recent = records[-keep_recent:]
            if max_tail_chars is not None:
                recent = self._fit_tail(recent, max_tail_chars)

        summary_record = {
            "timestamp": datetime.now().isoformat(),
            "type": "summary",
            "content": summary,
            "compact_id": f"c_{self._compact_count + 1:03d}",
            "compacted_message_count": len(compacted),
        }

        new_history = [summary_record] + recent
        self._write_history(new_history)

        # Recompute message_count from the surviving records — compact replaces
        # the compacted messages with one summary, so the count must shrink with
        # them (rant 2026-08-31T14:18:14: previously the count was never
        # decremented, inflating the TUI/GUI "msgs" display). Same semantics as
        # the rewind handler (daemon.py): count message-type records only.
        self._message_count = sum(
            1 for r in new_history if r.get("type", "message") == "message"
        )

        self._compact_count += 1
        self._last_compact_at = datetime.now().isoformat()
        self._updated_at = self._last_compact_at
        self._save_meta()

        # `len(compacted)` is still the honest count: a record trimmed to fit the
        # tail budget is *retained* (it keeps its place in the history), so it is
        # not one of the records the summary replaced.
        trimmed = sum(1 for r in recent if r.get("truncated"))
        logger.info(
            "compact: %d messages → summary (kept %d%s)",
            len(compacted), len(recent),
            f", {trimmed} trimmed to the tail budget" if trimmed else "",
        )
        return len(compacted)

    def drop_history_records(self, positions: Iterable[int]) -> int:
        """Remove records at the given positions from history.jsonl.

        Rant 2026-09-29T15:55:44.643795+08:00 (content-risk L2): the only cure for
        a trigger that is a word rather than a replaceable codepoint is taking the
        offending record out of the history, and this is that write. Positions are
        absolute record indexes, the same handle `list_history include_records`
        hands out, so a caller that located a record by reading can name it here.

        The message count is recomputed the way `compact` recomputes it
        (message-type records only) rather than decremented: the caller may hand
        in a tool round's worth of records, and a count derived from the surviving
        records is the one that cannot drift from disk. Out-of-range positions are
        ignored, so a stale index removes nothing instead of removing a neighbour.

        Returns the number of records actually removed.
        """
        records = self._read_history()
        doomed = {p for p in positions if isinstance(p, int) and 0 <= p < len(records)}
        if not doomed:
            return 0
        kept = [r for i, r in enumerate(records) if i not in doomed]
        self._write_history(kept)
        removed = len(records) - len(kept)
        self._message_count = sum(
            1 for r in kept if r.get("type", "message") == "message"
        )
        self._updated_at = datetime.now().isoformat()
        self._save_meta()
        logger.warning(
            "history: dropped %d record(s) at %s (%d left, %d message(s))",
            removed, sorted(doomed), len(kept), self._message_count,
        )
        return removed

    def _write_history(self, records: list[dict]) -> None:
        """Overwrite history.jsonl with new records."""
        with open(self._history_path, "w", encoding="utf-8") as f:
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
        # Only when one was set: a session that never declared a tier must not
        # gain a key saying it did — the silence is what tells the upgrade
        # session's path from a client turn's (rant 2026-09-30T09:30:16).
        if self._sandbox:
            meta["sandbox"] = self._sandbox
        if self._sandbox_roots:
            meta["sandbox_roots"] = list(self._sandbox_roots)
        if title is not None:
            meta["title"] = title
        else:
            # Preserve existing title if present
            if self._meta_path.exists():
                try:
                    old = json.loads(self._meta_path.read_text(encoding="utf-8"))
                    if "title" in old:
                        meta["title"] = old["title"]
                except (json.JSONDecodeError, OSError):
                    pass
        self._meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        # Global cross-project index (rant 2026-08-13T16:42:22): record this
        # session so other projects can locate it. Idempotent (no-op when the
        # path is unchanged) and never raises — a failed index write must not
        # break session creation or message persistence.
        upsert_session_index(self.session_id, self._dir)

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

    # ── Static: delete session ────────────────────────────────

    @staticmethod
    def delete(session_id: str, cwd: Path) -> bool:
        """Delete a session directory and all its contents.

        Returns True if the session was deleted, False if it didn't exist.
        """
        import shutil

        session_dir = cwd / ".emrg" / "sessions" / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir)
            logger.info("session deleted: %s", session_id)
            # Global index (rant 2026-08-13T16:42:22): drop the deleted session.
            remove_session_index(session_id)
            return True
        logger.warning("session not found for deletion: %s", session_id)
        return False

    # ── Static: list sessions ─────────────────────────────────

    @staticmethod
    def list_sessions(cwd: Path) -> list[dict]:
        """List all sessions in cwd/.emrg/sessions/, sorted by last activity desc.

        Last activity is ``updated_at`` — refreshed by append_message, rename,
        compact and rewind (rant 2026-09-30T10:27:20) — with ``created_at`` as
        the fallback for a meta written before that key existed. Sorting is the
        single point both clients read, so TUI and GUI order agree.

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
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                results.append(meta)
            except (json.JSONDecodeError, OSError):
                logger.warning("corrupt meta.json in %s, skipping", entry.name)

        # Rant 2026-09-30T10:27:20：按「最后活动」排序。仅看 created_at 时，一个天天在用的
        # 会话会随着新会话不断创建而沉到列表底部——顺序必须跟着它变的是哪一种「新」。
        # `updated_at` 缺失（老 meta）时回退 `created_at`，不丢行也不抛错。
        results.sort(
            key=lambda m: m.get("updated_at") or m.get("created_at", ""),
            reverse=True,
        )
        return results


def last_n_messages(messages: list[dict], n: int) -> list[dict]:
    """Take the last ``n`` messages of a validated LLM message list.

    The list must already be OpenAI-valid (e.g. produced by
    ``_validate_tool_messages``). Slicing a valid list can still orphan a
    leading ``role: "tool"`` message: its matching assistant message with
    ``tool_calls`` lies just before the window boundary, so the API rejects
    it with "Messages with role 'tool' must be a response to a preceding
    message with 'tool_calls'" (observed on task_vibe_check, 2026-08-19).
    Leading orphaned tool messages are dropped; the remaining window keeps
    its original order.
    """
    window = messages[-n:]
    while window and window[0].get("role") == "tool":
        window.pop(0)
    return window


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
