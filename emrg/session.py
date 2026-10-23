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
import ntpath
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Iterable

from emrg.agent import Agent, as_agent
from emrg.memory import NotARegularFile, SessionMemoryStore
from emrg.sandbox.grants import GRANT_REGISTRY, RootGrant
from emrg.sandbox.policy import SANDBOX_MODES
from emrg.sandbox.roots import canonical_path, canonical_target
from emrg.sessions_index import remove_session_index, upsert_session_index
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

# ── llm.jsonl rotation ────────────────────────────────────────
_LLM_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_LLM_LOG_BACKUP_COUNT = 2

#: How many prompts a session's input history keeps (issue #2349). Recall is a list walked
#: backwards from the newest, so what matters is that a long-lived session's cost stays
#: flat — 200 is far past what anyone scrolls to and small enough that the file is one
#: cheap read on every append.
_INPUT_HISTORY_MAX = 200


def generate_session_id(cwd: Path) -> str:
    """Generate a human-friendly session ID: s_YYMMDD_HHMM_xxxxxxxx.

    Suffix uses 4 random bytes (32 bits of entropy) — a 2-byte suffix
    (16 bits, 65536 possibilities) made the 10-sample uniqueness test
    flaky via the birthday paradox (~0.07% collision per run; observed
    on CI master push run 31145421676).
    """
    now = datetime.now()
    prefix = f"s_{now.strftime('%y%m%d_%H%M')}_"
    sessions_dir = as_agent(cwd).sessions_dir
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

    A task session is **one session for every run of that task**: the task's own name is
    reused cycle after cycle, so its history holds all of them. Anything reading
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

        elif r.get("type") == "context":
            # The turn's dynamic context — the clock, the write boundary, the
            # memory indexes — as `_inject_context_message` sent it. Persisting
            # it is what keeps a rebuilt request a prefix of the one the turn
            # before it sent: it rides immediately before the user prompt, and a
            # history that cannot reproduce it diverges exactly there, so the
            # provider re-processes the entire previous turn as uncached prompt
            # tokens (issue #2311; measured 2026-10-16 on this session, 87,440
            # of 107,920 tokens uncached at one turn boundary, against 134-261
            # for an ordinary round).
            #
            # It is deliberately not a `message` record: `append_message`
            # counts only `type == "message"` toward `message_count`, and the
            # client's history projection is an if/elif on `tool_result` /
            # `message`, so this record reaches the model and no client.
            messages.append({"role": "user", "content": r.get("content", "")})
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


def _refuse_a_non_regular_subject(path: Path) -> None:
    """Raise when ``path`` is not a regular file — an open on it would never return.

    Every file this module opens lives in the session's own directory
    (``<cwd>/.emrg/sessions/<id>/``), and that directory is **inside the agent the
    agent works in**: the workspace fence grants the agent root, so the subject of
    these opens can be any kind of thing. Measured 2026-10-11 (``cyc20261011-071202``) on
    master ``3386f239``, one call per child process under this parent's 8 s cap, with only
    the file that call reads made non-regular:

        call                        regular        directory          FIFO
        Session.load                returns        raises at once     no return, 8 s
        Session(...).title          returns title  returns the id     no return, 8 s
        Session(...)._read_history  returns        raises at once     no return, 8 s
        append_message              returns        raises at once     no return, 8 s

    An **open is not a read**: on a FIFO it waits at the open until a writer appears and
    raises nothing, so no ``except (OSError, …)`` around these call sites can bound the
    consequence — and the daemon runs every session read and write on its event loop, so
    the block is every session, the scheduler and the evolution loop, with a restart as
    the only recovery (which belongs to the host). The kind one over already answers:
    a *directory* at the same path raises at once, or degrades where the caller handles it
    (``title`` falls back to the session id). So this adds no new answer — it gives the
    kind that used to be answered by blocking the answer the rest of the family already
    has.

    The rule is the tool layer's, read from there rather than restated
    (:func:`emrg.tools.base.non_regular_kind` — the path-taking reader `#2062`, `#2076`
    and `#2083` share), and the refusal is this codebase's existing spelling of it
    (:class:`emrg.memory.NotARegularFile`, an ``OSError`` that names the kind, which is
    what the callers' handlers and the daemon's error frames already read).

    An absent path is not a refusal: every call site here is about to create or open
    one, and the ``stat`` this check itself makes is what answers it — one syscall, with
    no gap between a check and a read for the path to change in.

    :param path: the session file about to be opened.
    """
    try:
        kind = non_regular_kind(path)
    except FileNotFoundError:
        return
    if kind:
        raise NotARegularFile(
            f"{path} is {kind}, not a regular file. This session opens its own files "
            f"(history, meta, llm) directly, and opening this one blocks until a peer "
            f"appears — a FIFO waits for a writer, a socket for a connection, a device "
            f"never ends — so the call would never return, and neither would the "
            f"daemon, which runs every session read and write on its event loop."
        )


#: Characters that make a name more than one path component, on either platform named
#: here: `/` separates on POSIX and Windows, `\` separates on Windows and is an ordinary
#: character on POSIX — refusing both is the point, because the same id reaches a path on
#: whichever platform runs it.
_NAME_SEPARATORS = ("/", "\\")


def is_safe_session_id(session_id: object) -> bool:
    """True when `session_id` names exactly one entry inside the sessions directory.

    A session id arrives in a client message, so it is an input like any other:
    `Path(cwd) / ".emrg" / "sessions" / session_id` hands `..` and an absolute id to the
    kernel, which resolves them, and the caller then removes what it resolved to. Measured
    2026-10-21: `Session.delete("../..", cwd)` walked `cwd/.emrg/sessions/../..` — the
    agent tree — and left it empty, `victim/` and `.emrg/` alike, then raised from
    `shutil.rmtree`'s final `os.rmdir`.

    So the id is checked before a path is built: non-empty, no NUL, not `.` or `..`, no
    separator, and no drive. The drive is asked of **both** flavours — `os.path.splitdrive`
    answers `("", "C:evil")` on this host and `ntpath.splitdrive` answers `("C:", "evil")`,
    and a `C:`-relative id is a path, not a name, on the platform that can receive it.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    if "\0" in session_id or session_id in (".", ".."):
        return False
    if any(sep in session_id for sep in _NAME_SEPARATORS):
        return False
    if os.path.splitdrive(session_id)[0] or ntpath.splitdrive(session_id)[0]:
        return False
    return True


def session_dir(cwd: Path, session_id: str) -> Path:
    """The directory a session's state lives in, for a workspace and an id.

    This is the one producer of ``<cwd>/.emrg/sessions/<session_id>``. The rule has
    more than one reader: ``Session`` fills the directory it computes here, and the
    daemon derives a *parent's* directory from it to place a child's cwd inside —
    ``sub-agent/sub<N>`` for a delegation, ``sub-agent/sub-boss`` for an owner. A
    caller that has an id and a cwd but no handle asks this, so a layout change is an
    edit in one place rather than a search for every copy of the expression.

    The id is not validated here — ``is_safe_session_id`` decides that, and its caller
    decides whether an unsafe id is refused or merely not a directory (see its
    docstring for what an unchecked id did).

    The expression itself belongs to ``Agent.sessions_dir``: this function is the
    signature the callers already use, not a second convention beside the Agent's.
    """
    return as_agent(cwd).sessions_dir / session_id


class Session:
    """Manages a single conversation session on disk.

    A session runs *in* an Agent — one directory. Every path it keeps is derived from
    that Agent, so the session never spells a ``.emrg`` path itself: a second derivation
    is a second convention, waiting to disagree with the first.
    """

    def __init__(self, session_id: str, agent: Agent | Path | str) -> None:
        self.session_id = session_id
        # A caller may still hand over the path — every factory below does — and the
        # wrapping happens here rather than at each of them, so exactly one place knows
        # that a path means an Agent.
        self._agent = as_agent(agent)
        self._dir = session_dir(self._agent, session_id)
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
        # The grants the host named for this session on top of the tier, in the
        # order they were added (rant 2026-10-09T09:43:39), typed since issue
        # #2492 so a kind no dialect has to re-decide is stored beside the object
        # it names. Empty is the honest default: a session nobody has named a
        # grant for grants exactly what its tier and the default manifest derive.
        # Canonical targets only — the writer canonicalizes before storing, so a
        # reader never has to wonder which spelling was meant.
        self._sandbox_grants: list[RootGrant] = []
        # The default-manifest targets this session's host has withdrawn (issue
        # #2492). Per session, like the grants above and the tier: a default
        # applies without anyone typing anything, so "stop applying it" is state,
        # and the daemon is its only writer.
        self._sandbox_withheld: list[str] = []
        # How many delegations deep this session is (issue #2451): 0 for a session
        # a client or the scheduler started, parent + 1 for one a `subagent` call
        # started. It lives in `meta.json` rather than in a daemon table because
        # the *child* is the one that has to know its own depth — a child's
        # `subagent` call reads this, and a daemon restart must not reset the
        # count of a session that already exists on disk.
        self._depth: int = 0
        # When this session — acting as an owner — was last asked for a stance, in
        # seconds since the epoch (issue #2495). An owner is left alone for
        # `REASK_WINDOW_S` after a call, because direction does not change inside
        # four hours and a second call buys the same judgement again. It lives in
        # `meta.json` for the same reason `depth` does: the session is retained on
        # disk, so a daemon restart that reset this clock would let the very next
        # round re-buy a decision it already paid for.
        self._boss_asked_at: float | None = None

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

    # ── Identity ──────────────────────────────────────────────

    @property
    def agent(self) -> Agent:
        """The Agent — the directory — this session runs in."""
        return self._agent

    @property
    def cwd(self) -> Path:
        """The directory this session runs in: ``agent.base_dir``, read-only.

        A property rather than a field because the directory is decided once, when the
        session is built: a second assignment could put a session's files and its
        ``cwd`` in different places with nothing recording which one is right. The value
        is answered verbatim — a caller that passed a string reads that string back —
        so every existing reader of ``session.cwd`` is unchanged.
        """
        return self._agent.base_dir

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
            _refuse_a_non_regular_subject(session._meta_path)
            meta = json.loads(session._meta_path.read_text(encoding="utf-8"))
            session._message_count = meta.get("message_count", 0)
            session._compact_count = meta.get("compact_count", 0)
            session._created_at = meta.get("created_at", "")
            session._updated_at = meta.get("updated_at", "")
            session._last_compact_at = meta.get("last_compact_at")
            session._sandbox = meta.get("sandbox")
            stored_grants = meta.get("sandbox_grants")
            if isinstance(stored_grants, list):
                session._sandbox_grants = [
                    grant for grant in (RootGrant.from_wire(v) for v in stored_grants)
                    if grant is not None
                ]
            else:
                # A `meta.json` written before grants were typed (issue #2492):
                # the path list is the same fact in the older spelling, and a
                # session that predates the change must keep its boundary rather
                # than silently lose it on the next daemon restart.
                stored_roots = meta.get("sandbox_roots")
                if isinstance(stored_roots, list):
                    session._sandbox_grants = [
                        RootGrant.from_path(r) for r in stored_roots if isinstance(r, str)
                    ]
            stored_withheld = meta.get("sandbox_withheld")
            if isinstance(stored_withheld, list):
                session._sandbox_withheld = [r for r in stored_withheld if isinstance(r, str)]
            stored_depth = meta.get("depth")
            # `isinstance(..., int)` is not enough on its own: `True` is an int, and
            # a JSON `true` in this field would then read as depth 1.
            if isinstance(stored_depth, int) and not isinstance(stored_depth, bool):
                session._depth = max(0, stored_depth)
            stored_boss_asked_at = meta.get("boss_asked_at")
            # The same care as `depth`: `True` is an int, and a JSON `true` here
            # would read as the epoch's first second. Absent, malformed and
            # non-numeric all mean "never asked", which is the state that permits
            # a call — the safe direction for a value that only ever blocks one.
            if isinstance(stored_boss_asked_at, (int, float)) and not isinstance(
                stored_boss_asked_at, bool
            ):
                session._boss_asked_at = float(stored_boss_asked_at)
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
                # Inside the `try`, because an unreadable meta already falls back to
                # the session id here — a directory at this path does exactly that
                # today (`IsADirectoryError` is an `OSError`), and a named pipe must
                # not answer differently from the kind one over.
                _refuse_a_non_regular_subject(self._meta_path)
                meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return meta.get("title", self.session_id)

    def rename(self, title: str) -> None:
        """Set a custom title for this session."""
        self._updated_at = datetime.now().isoformat()
        self._save_meta_with_title(title)
        logger.info("session renamed: %s -> %s", self.session_id, title)

    # ── Delegation depth ──────────────────────────────────────

    @property
    def depth(self) -> int:
        """How many `subagent` delegations deep this session is; 0 at the top."""
        return self._depth

    def set_depth(self, depth: int) -> None:
        """Persist this session's delegation depth into ``meta.json``.

        The only writer of the key. Written only when non-zero, so a session that
        was never delegated to keeps the meta shape it has always had — the same
        reason the sandbox tier is written only when one was declared.
        """
        self._depth = int(depth)
        self._save_meta()
        logger.info("session delegation depth set: %s -> %d", self.session_id, self._depth)

    # ── The owner's clock ─────────────────────────────────────

    @property
    def boss_asked_at(self) -> float | None:
        """When this session was last asked for a stance, or ``None`` if never.

        Seconds since the epoch, so the quiet window's comparison is arithmetic
        rather than a parse: a malformed instant would otherwise have to be told
        apart from an absent one at the point where it decides something.
        """
        return self._boss_asked_at

    def set_boss_asked_at(self, when: float | None) -> None:
        """Persist the instant this session was last asked, into ``meta.json``.

        The only writer of the key, and it is called *before* the owner's turn
        runs rather than after it: a stamp written on completion leaves a turn that
        died looking like one that never happened, and the next call would then pay
        for the same judgement a second time.

        ``None`` clears it, and the key leaves ``meta.json`` with it — the state a
        session that was never asked is in. That is how a call which delivered
        nothing gives the window back (issue #2499): the clock exists to make a
        direction that *was* given stand, and a call that was cancelled gave none.
        """
        self._boss_asked_at = None if when is None else float(when)
        self._save_meta()
        logger.info(
            "session owner clock set: %s -> %s",
            self.session_id,
            "never" if self._boss_asked_at is None else f"{self._boss_asked_at:.0f}",
        )

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
    def sandbox_grants(self) -> list[RootGrant]:
        """The host-named writable grants this session carries, in added order.

        A copy, so a reader cannot mutate the session's list by accident — the
        daemon injects this value into every tool call, and an injected list a
        tool could edit in place would be a second writer of a fact the daemon
        owns. Typed since issue #2492: the **kind** travels with the object it
        names, so a dialect never re-decides it from the disk.
        """
        return list(self._sandbox_grants)

    @property
    def sandbox_roots(self) -> list[str]:
        """The **paths** among this session's grants — the projection clients read.

        Clients and `meta.json` consumers have always spoken in paths, and the two
        filesystem kinds still are paths; a registry key is not, so it contributes
        none. This is a derived view of
        :attr:`sandbox_grants` and never a second store: it is computed on every
        read, so it cannot disagree with the typed list.

        Kept because the GUI's resume path reads ``meta.sandbox_roots`` (the
        ``sandbox_roots`` frame is the live source, this key the reopened one) and
        because a removal is still named by a path.
        """
        return [
            grant.target for grant in self._sandbox_grants
            if grant.kind != GRANT_REGISTRY
        ]

    @property
    def sandbox_withheld(self) -> list[str]:
        """Default-manifest targets this session's host has withdrawn."""
        return list(self._sandbox_withheld)

    def set_sandbox_grants(self, grants: "list[RootGrant] | tuple[RootGrant, ...]") -> None:
        """Replace this session's host-named writable grants and persist them.

        The **only** writer of the key, and it does not validate the grants: the
        refusals (``/``, the home directory, a grant covering a protected daemon
        file, a kind no platform enforces) belong to the daemon's command, which
        is where a person is waiting for an answer and where a refusal can name
        the reason. This method is storage. Its one precondition is the shape the
        reader depends on — a canonical target — and it canonicalizes rather than
        trusting the caller, so a symlink handed in here is stored as the path the
        enforcement layer will actually compare against.
        """
        self._sandbox_grants = [
            grant._replace(target=canonical_target(grant)) for grant in grants
        ]
        self._save_meta()
        logger.info(
            "session sandbox grants set: %s -> %d grant(s)",
            self.session_id, len(self._sandbox_grants),
        )

    def set_sandbox_roots(self, roots: "list[str] | tuple[str, ...]") -> None:
        """Replace this session's grants from **paths** — the pre-#2492 spelling.

        Kept as a named convenience rather than a second store: it converts to
        typed grants and calls :meth:`set_sandbox_grants`, so the one writer stays
        one writer. Its cost is stated where callers meet it —
        :meth:`~emrg.sandbox.grants.RootGrant.from_path` reads each kind off the
        disk — which is why the daemon and the delegation paths use the typed
        method and only a path-only caller reaches for this one.
        """
        self.set_sandbox_grants([RootGrant.from_path(str(root)) for root in roots])

    def set_sandbox_withheld(self, targets: "list[str] | tuple[str, ...]") -> None:
        """Replace the default-manifest targets this session withdraws."""
        self._sandbox_withheld = [str(target) for target in targets]
        self._save_meta()
        logger.info(
            "session sandbox withheld set: %s -> %d target(s)",
            self.session_id, len(self._sandbox_withheld),
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
        _refuse_a_non_regular_subject(self._history_path)
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(line)

        # Write to daily history
        daily_path = self._daily_history_path()
        _refuse_a_non_regular_subject(daily_path)
        with open(daily_path, "a", encoding="utf-8") as f:
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

        _refuse_a_non_regular_subject(self._llm_path)
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
        _refuse_a_non_regular_subject(self._history_path)
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

    def _input_history_path(self) -> Path:
        """Where the prompts the host typed are kept, per session (issue #2349)."""
        return self._dir / "input_history.jsonl"

    def append_input_history(self, text: str) -> None:
        """Record one accepted prompt as recallable input history.

        A **separate file** from `history.jsonl`, deliberately, and the separation is the
        whole design: `history.jsonl` also holds the per-turn `context` record and one
        `summary` record per compaction, and both reach the model as user messages while
        nobody typed them — a filter over `role == "user"` would hand the host their own
        injected clock text as a recallable command. It also lets recall survive what
        conversation history does not: `rewind` truncates the conversation and `compact`
        rewrites it, and the text the host typed is exactly what they want back afterwards.

        Consecutive identical prompts collapse into one entry, because pressing up three
        times to reach the third distinct prompt is the point of recalling a list; the file
        is capped at `_INPUT_HISTORY_MAX` entries, dropping the oldest, so a long-lived
        session's recall cost stays flat.
        """
        prompt = (text or "").strip()
        if not prompt:
            return
        records = self.read_input_history()
        if records and str(records[-1].get("text", "")) == prompt:
            return
        records.append({"timestamp": datetime.now().isoformat(), "text": prompt})
        if len(records) > _INPUT_HISTORY_MAX:
            records = records[-_INPUT_HISTORY_MAX:]

        path = self._input_history_path()
        _refuse_a_non_regular_subject(path)
        # Rewritten whole rather than appended to: the cap is enforced on write, and a
        # half-written tail is what an append-then-truncate would produce if the process
        # died between the two. The temporary file is a sibling so the rename stays on one
        # filesystem, which is what makes it atomic.
        tmp = path.with_name(path.name + ".tmp")
        _refuse_a_non_regular_subject(tmp)
        with open(tmp, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def read_input_history(self) -> list[dict]:
        """The recorded prompts, oldest first — the order recall walks backwards through."""
        path = self._input_history_path()
        if not path.exists():
            return []
        _refuse_a_non_regular_subject(path)
        records: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("corrupt line in input_history.jsonl, skipping")
                    continue
                if isinstance(record, dict) and isinstance(record.get("text"), str):
                    records.append(record)
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
        _refuse_a_non_regular_subject(self._history_path)
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
        if self._sandbox_grants:
            meta["sandbox_grants"] = [grant.as_wire() for grant in self._sandbox_grants]
            # The path projection is written beside the typed list because the
            # GUI's resume path reads `meta.sandbox_roots` (its live source is the
            # `sandbox_roots` frame). One producer — `sandbox_roots` is computed
            # from the grants — so the two keys cannot disagree.
            meta["sandbox_roots"] = self.sandbox_roots
        if self._sandbox_withheld:
            meta["sandbox_withheld"] = list(self._sandbox_withheld)
        # Same rule as `sandbox`: a session nobody delegated to must not gain a
        # key saying it is zero deep.
        if self._depth:
            meta["depth"] = self._depth
        # Same rule again: a session that was never asked for a stance must not
        # gain a key claiming one was.
        if self._boss_asked_at is not None:
            meta["boss_asked_at"] = self._boss_asked_at
        if title is not None:
            meta["title"] = title
        else:
            # Preserve existing title if present
            if self._meta_path.exists():
                try:
                    _refuse_a_non_regular_subject(self._meta_path)
                    old = json.loads(self._meta_path.read_text(encoding="utf-8"))
                    if "title" in old:
                        meta["title"] = old["title"]
                except (json.JSONDecodeError, OSError):
                    pass
        _refuse_a_non_regular_subject(self._meta_path)
        self._meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        # Global cross-agent index (rant 2026-08-13T16:42:22): record this
        # session so other agents can locate it. Idempotent (no-op when the
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

        Returns True if the session was deleted, False if it didn't exist — and False,
        with a warning, for an id that is not a single path component, because the id
        comes from a client message and `shutil.rmtree` follows whatever it resolves to.
        """
        import shutil

        if not is_safe_session_id(session_id):
            logger.warning(
                "refusing to delete a session named %r: a session id is one path "
                "component, so nothing outside the sessions directory is reachable",
                session_id,
            )
            return False

        target = session_dir(cwd, session_id)
        if target.exists():
            shutil.rmtree(target)
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
        sessions_dir = as_agent(cwd).sessions_dir
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
                _refuse_a_non_regular_subject(meta_path)
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
