"""A byte this reader cannot decode must not cost the whole session's history.

`Session._read_history` is the one reader every session path goes through — the
LLM's own message list (`get_messages_for_llm`), `compact`, `drop_history_records`
(the rewind path) and the daemon's frames — over the one file the product itself
rewrites while a reader may be reading it: `_write_history` truncates and rewrites
the whole file, `append` writes one line at a time, and the daemon, a second
client and this session's own later reads can each observe it mid-write.

Measured 2026-10-04, before this file existed: a three-record `history.jsonl`
whose second record holds **one** invalid byte made `_read_history`,
`get_messages_for_llm`, `compact` and `drop_history_records` each raise
`UnicodeDecodeError` — so the session could not be read, answered, compacted or
rewound, and no product path could repair it, because every repair path reads
first. The reader was already tolerant at the *other* level (a line that is not
JSON is skipped and the rest are kept), so the asymmetry was the defect: a JSON
corruption cost one record, a byte corruption cost the session.

`errors="replace"` is not a new policy for this file — the codebase's other
readers of host-written files decode this way (`scripts/find-host-message.py`
reads this very file that way), and a torn multibyte character is what a write cut
in half produces, the condition `_index_for_prompt`'s docstring measures for a
MEMORY.md being rewritten ("37 of 121 reads raised").

Nothing here starts, stops or restarts a daemon: `Session` is a path-bound object
and the reads below are file reads.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

import emrg.sessions_index as sessions_index
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
FIND_HOST_MESSAGE = REPO_ROOT / "scripts" / "find-host-message.py"

#: One byte that is not valid UTF-8 — a latin-1 `é`, which is what a text editor,
#: a shell child or a locale-dependent writer leaves behind in a UTF-8 file.
_BAD_BYTE = b"\xe9"


def _load_find_host_message():
    """The R7 instrument, the other reader of `history.jsonl`, loaded by path.

    Registered in `sys.modules` before it executes, because the instrument defines
    dataclasses and `dataclasses` resolves their annotations through
    `sys.modules[cls.__module__]` — a module loaded by path alone has no entry
    there, and the failure is an `AttributeError` from inside the standard library
    rather than from the file under test.
    """
    spec = importlib.util.spec_from_file_location("find_host_message", FIND_HOST_MESSAGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["find_host_message"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("find_host_message", None)
        raise
    return module


def _line(text: str, *, role: str = "user", ts: str = "2026-10-04T00:00:00") -> bytes:
    """One history record as the writer writes it: `json.dumps` + newline, UTF-8."""
    record = {"timestamp": ts, "type": "message", "role": role, "content": text}
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def _damaged_line(text: str, *, ts: str = "2026-10-04T00:00:09") -> bytes:
    """The same record, with one undecodable byte inside its content string."""
    record = {"timestamp": ts, "type": "message", "role": "user", "content": text}
    head = json.dumps(record, ensure_ascii=False).replace(text, "", 1).encode("utf-8")
    # Splice the raw byte inside the (now empty) string literal, so the line is
    # still structurally a JSON object: the damage is in the content, not the syntax.
    return head.replace(b'"content": ""', b'"content": "' + text.encode() + b'"', 1).replace(
        text.encode(), text.encode()[:1] + _BAD_BYTE + text.encode()[1:]
    ) + b"\n"


def _session(tmp_path: Path, payload: bytes) -> Session:
    session = Session("s_probe", tmp_path / "proj")
    session.dir_path.mkdir(parents=True, exist_ok=True)
    session._history_path.write_bytes(payload)
    assert session._history_path.read_bytes() == payload, "the fixture wrote what it meant to"
    return session


# ── the property: a damaged record does not remove the intact ones ───

def test_one_undecodable_byte_does_not_take_the_whole_history(tmp_path: Path):
    session = _session(tmp_path, _line("first") + _damaged_line("cafe") + _line("last", role="assistant"))

    records = session._read_history()

    contents = [r.get("content") for r in records]
    assert "first" in contents and "last" in contents, (
        f"the intact records must survive the damaged one; got {contents!r}"
    )
    assert records[0] == json.loads(_line("first")), "an intact record comes back unchanged"


def test_the_damaged_record_is_returned_and_the_damage_is_visible(tmp_path: Path):
    """Not dropped and not passed off as intact: the replacement character says so."""
    session = _session(tmp_path, _line("first") + _damaged_line("cafe"))

    records = session._read_history()

    assert len(records) == 2, f"the damaged record is still a record; got {records!r}"
    assert "\ufffd" in records[1]["content"], (
        f"the byte the reader could not decode must show as a replacement; got {records[1]!r}"
    )


def test_a_healthy_history_comes_back_unchanged(tmp_path: Path):
    """Control leg: tolerance introduces nothing into a file that needs none of it."""
    lines = [_line("hello 世界", ts="2026-10-04T00:00:01"),
             _line("emoji 🎯 stays", role="assistant", ts="2026-10-04T00:00:02"),
             _line("plain", ts="2026-10-04T00:00:03")]
    session = _session(tmp_path, b"".join(lines))

    records = session._read_history()

    assert records == [json.loads(line) for line in lines]
    assert "\ufffd" not in json.dumps(records, ensure_ascii=False), (
        "a file with nothing to replace must come back byte-identical"
    )


def test_a_line_that_is_not_json_is_skipped_and_named(tmp_path: Path, caplog):
    """The other level of the same tolerance, and it says where (it used to say nothing)."""
    payload = _line("first") + b'{"broken": \n' + _line("third", ts="2026-10-04T00:00:03")
    session = _session(tmp_path, payload)

    with caplog.at_level(logging.WARNING, logger="emrg.session"):
        records = session._read_history()

    assert [r["content"] for r in records] == ["first", "third"]
    assert "line 2" in caplog.text, f"the skipped line must be named; log was {caplog.text!r}"


# ── the callers: the session is not locked out of its own repair ─────

def test_the_session_still_answers_compacts_and_rewinds(tmp_path: Path, monkeypatch):
    """Every repair path reads first — which is why a strict read locked the session.

    `get_messages_for_llm` is the turn's own message list, `compact` and
    `drop_history_records` are the two ways a host shrinks a history. All three
    read this file before they do anything, so today one byte makes each of them
    raise and leaves the file repairable only by hand.
    """
    monkeypatch.setattr(sessions_index, "config_dir", lambda: tmp_path)
    payload = b"".join(
        [_line(f"m{i}", ts=f"2026-10-04T00:00:{i:02d}") for i in range(6)]
        + [_damaged_line("cafe")]
    )
    session = _session(tmp_path, payload)

    messages = session.get_messages_for_llm()
    assert len(messages) == 7, f"all seven records reach the LLM's message list; got {len(messages)}"

    session.drop_history_records([0])
    assert len(session._read_history()) == 6

    assert session.compact("summary", keep_recent=1) > 0


# ── the same file's other reader ─────────────────────────────────────

def test_both_readers_of_this_file_report_the_same_host_messages(tmp_path: Path):
    """One file, two readers, one policy — the R7 instrument already decoded this way."""
    session = _session(
        tmp_path,
        _line("asked once", ts="2026-10-04T00:00:01")
        + _damaged_line("asked twice")
        + _line("the answer", role="assistant", ts="2026-10-04T00:00:03"),
    )
    instrument = _load_find_host_message()

    messages, scanned, _, _ = instrument.read_sessions([session.dir_path])

    host_rows = [r for r in session._read_history() if r.get("role") == "user"]
    assert scanned == [session.dir_path], "the instrument scanned the directory it was handed"
    assert len(messages) == len(host_rows) == 2, (
        f"the two readers disagree about one file: instrument {len(messages)}, session {len(host_rows)}"
    )
