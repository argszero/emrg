"""The log lines that print foreign text apply the credential rule before they print it (#2173).

`emrg/server/redaction.py` exists because the daemon's log and the client's log are files a
host pastes into an issue. Every pattern in it has a **minimum length** (`sk-` 16
alphanumerics, `ghp_` 20, a base64 run 40) — which is why the *order* is the whole point of
`redact_prefix`: a value that is windowed first cannot match a credential that crosses the
cut, and the surviving prefix is written in the clear.

This file covers the sites an AST sweep of every ``emrg/**/*.py`` logger call found printing
foreign text without the rule, and it takes each one in both directions: the credential is
absent **and** the line is still written with its own subject, so an arm cannot pass on a log
line that simply vanished.

The sites, and the value each one prints:

* ``emrg/server/llm.py`` — the SSE parse skip. `json_str` is raw text off the provider's
  wire; the module's own convention (stated above `_redact_text`) is `_redact_text(text)[:N]`
  and this line was the one that stated none.
* ``emrg/server/scheduler.py`` — the cycle's server-error warning (unwindowed, and the only
  thing added there is the rule) and the abort report, which windows the same value at 200.
* ``emrg/server/scheduler.py`` — the vibe check's error line, windowed at 120.

The windows are 80 / 200 / 120, and every arm plants the credential at an offset that leaves
part of it inside the window: the shape a sink cannot rescue, because the record it receives
has already been cut.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.config import LlmConfig  # noqa: E402
from emrg.protocol import InstanceIdentity  # noqa: E402
from emrg.server import scheduler as sched_mod  # noqa: E402
from emrg.server import llm as llm_mod  # noqa: E402
from tests.task_handler_factory import make_handler  # noqa: E402

#: A live-shaped key: `sk-` plus 20 alphanumerics, above the rule's 16-character minimum.
KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0"

#: What the window leaves behind when the cut is taken **before** the rule — the defect.
PARTIAL = KEY[:10]


@contextlib.contextmanager
def _records(logger_name: str, level: int = logging.DEBUG):
    """Collect the records one logger emits, restoring its level on the way out.

    The level is set on the *named* logger and a handler is attached to it directly, so a
    record is captured whether or not the root logger would have filtered it — and no other
    test's logging state is disturbed.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Capture()
    saved_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)


def _args_of(records, needle: str) -> list[str]:
    """The positional args of every record whose message contains `needle`, flattened.

    Matched on the record's own message rather than on `caplog.text`: the value under test is
    a log *argument*, and a file-wide search would answer about a different line — the same
    session emits the server-error warning and the abort report for one error value.
    """
    out: list[str] = []
    for record in records:
        if needle in record.getMessage():
            out.extend(str(a) for a in record.args)
    return out


# ── the LLM layer: the SSE parse skip ─────────────────────────────────────


class _StreamResponse:
    status_code = 200
    headers = {}

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamClient:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        return self._response


def _drain(client) -> None:
    async def _run():
        async for _chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            pass

    asyncio.run(_run())


def _llm_client() -> llm_mod.LlmClient:
    return llm_mod.LlmClient(LlmConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-test-key",
        model="test-model",
    ))


def _sse_skip_records(payload: str):
    """One chat_stream over a stream whose first line is an unparseable `data:` payload.

    The second line is a well-formed terminal chunk: without one the stream "ends without
    finish_reason" and `chat_stream` retries the whole request, so the reading would be taken
    from a retry loop rather than from one turn.
    """
    client = _llm_client()
    client._client = _StreamClient(_StreamResponse([
        "data: " + payload,
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    ]))
    with _records("emrg.server.llm") as records:
        _drain(client)
    return records


def test_the_sse_parse_skip_masks_a_credential_at_the_cut():
    """The key starts at index 70 of the 80-character window: cut first, 10 characters survive."""
    payload = "x" * 70 + KEY
    records = _sse_skip_records(payload)
    values = _args_of(records, "SSE parse skip")

    assert values, "the parse skip was not logged at all — this arm would pass on a lost line"
    logged = values[-1]
    assert PARTIAL not in logged, (
        f"the credential crossed the cut and survived it: {logged!r}"
    )
    assert KEY not in logged, f"the credential reached the log: {logged!r}"


def test_the_sse_parse_skip_is_still_a_window():
    """The other direction: a benign payload is still bounded (and still logged)."""
    payload = "y" * 500
    records = _sse_skip_records(payload)
    values = _args_of(records, "SSE parse skip")

    assert values, "the parse skip was not logged at all"
    assert values[-1] == "y" * 80, (
        "the window is no longer a window: the line prints "
        f"{len(values[-1])} characters of the payload"
    )


# ── the scheduler: the vibe check's error line ────────────────────────────


class _ScriptedWS:
    """A websocket-shaped object that answers each `recv` with the next scripted frame."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        return json.dumps(self._frames.pop(0), ensure_ascii=False)

    async def close(self):
        pass


def _handler(tmp_path, monkeypatch, name="emrg-task"):
    monkeypatch.setattr(sched_mod, "config_dir", lambda: tmp_path)
    return make_handler(
        name=name,
        config={"project": "emrg"},
        interval=60,
        identity=InstanceIdentity(),
    )


def test_the_vibe_check_error_line_masks_a_credential_at_the_cut(tmp_path, monkeypatch):
    """The key starts at index 110 of the 120-character window."""
    handler = _handler(tmp_path, monkeypatch)
    error = "e" * 110 + KEY
    ws = _ScriptedWS([{"type": "vibe_check_result", "ok": False, "error": error}])

    with _records("emrg.server.scheduler") as records:
        result = asyncio.run(handler._request_vibe_check(ws, "p", "c"))

    assert result is None, "a failed vibe check must still return None"
    values = _args_of(records, "vibe check error")
    assert values, "the vibe check error was not logged at all"
    logged = values[-1]
    assert PARTIAL not in logged, (
        f"the credential crossed the cut and survived it: {logged!r}"
    )
    assert KEY not in logged, f"the credential reached the log: {logged!r}"
    assert logged.startswith("e" * 110), (
        "the line no longer carries its own subject — an arm cannot pass on a lost line"
    )


def test_the_vibe_check_error_line_is_still_a_window(tmp_path, monkeypatch):
    """The other direction: a benign error is still bounded at 120."""
    handler = _handler(tmp_path, monkeypatch)
    ws = _ScriptedWS([{"type": "vibe_check_result", "ok": False, "error": "z" * 400}])

    with _records("emrg.server.scheduler") as records:
        asyncio.run(handler._request_vibe_check(ws, "p", "c"))

    values = _args_of(records, "vibe check error")
    assert values, "the vibe check error was not logged at all"
    assert values[-1] == "z" * 120, (
        f"the window is no longer a window: {len(values[-1])} characters"
    )


# ── the scheduler: the aborted-cycle report ───────────────────────────────


def _aborted_cycle(tmp_path, monkeypatch, error_value: str):
    """Drive one real cycle whose daemon frame carries an `error`, and return records.

    The frame deliberately carries no `done`: a terminal frame is handled *before* the
    `error` field is read, so a `done` + `error` frame ends the cycle as a clean one and
    never reaches the abort report. This is the shape the daemon actually sends for a
    refused turn.
    """
    handler = _handler(tmp_path, monkeypatch)
    handler._build_evolution_prompt = lambda: "test prompt"
    ws = _ScriptedWS([{"error": error_value, "session_id": "s"}])

    async def _connect():
        return ws

    monkeypatch.setattr(sched_mod, "connect_to_server", _connect)
    with _records("emrg.server.scheduler") as records:
        reason = asyncio.run(handler._run_evolution_cycle())
    return reason, records


def test_the_abort_report_masks_a_credential_at_the_cut(tmp_path, monkeypatch):
    """The key starts at index 190 of the 200-character window the abort line takes."""
    error = "a" * 190 + KEY
    reason, records = _aborted_cycle(tmp_path, monkeypatch, error)

    assert reason == "server-error", f"the cycle did not end as scripted: {reason}"
    values = _args_of(records, "cycle aborted")
    assert values, "the abort report was not logged at all"
    logged = values[-1]
    assert PARTIAL not in logged, (
        f"the credential crossed the cut and survived it: {logged!r}"
    )
    assert KEY not in logged, f"the credential reached the log: {logged!r}"
    assert logged.startswith("a" * 190), (
        "the line no longer carries its own subject — an arm cannot pass on a lost line"
    )


def test_the_server_error_warning_masks_the_whole_value(tmp_path, monkeypatch):
    """That line takes no window, so the rule has to see the whole value — past 200 too."""
    error = "b" * 400 + KEY
    reason, records = _aborted_cycle(tmp_path, monkeypatch, error)

    assert reason == "server-error"
    values = _args_of(records, "server error")
    assert values, "the server-error warning was not logged at all"
    logged = values[-1]
    assert KEY not in logged, f"the credential reached the log: {logged!r}"
    assert logged.startswith("b" * 400), (
        "this line takes no window and must still print the whole error"
    )
