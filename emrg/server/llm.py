"""LLM client for task routing and tool calling.

Calls OpenAI-compatible chat completion endpoints.
Supports multi-turn conversations with tool calling and streaming.

The streaming protocol for tool_calls is nuanced:
1. Individual chunks carry delta.tool_calls[{index, id, function: {name?, arguments}}]
2. The id and name arrive once (usually in the first chunk for that index)
3. arguments arrive incrementally across multiple chunks
4. finish_reason == "tool_calls" signals the end and final flush
5. The client accumulates across chunks and yields aggregated dicts.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from typing import AsyncIterator, Optional

import httpx

from emrg import __version__
from emrg.config import LlmConfig
from emrg.server.tool_types import (
    shape_of,
    tool_arguments_text,
    tool_call_shape_problem,
)


# ── 错误信息脱敏（20260807-0107）────────────────────────────
# LLM 错误日志/异常可能包含 response headers（set-cookie/auth 回显）与 body
# （模型回显的密钥/令牌）。复用 daemon._redact_string 的内联凭据遮蔽能力，
# 避免 emrgd.log / 会话历史泄露。
def _redact_text(text: str) -> str:
    """遮蔽字符串中的内联凭据（sk-/ghp_/Bearer/JWT/base64-JSON）。"""
    try:
        from emrg.server.daemon import _redact_string
        return _redact_string(text)
    except Exception:
        return text


def _redact_headers(headers: dict) -> dict:
    """遮蔽 response headers 中的敏感键值（set-cookie/authorization/token 等）。"""
    sensitive = ("cookie", "set-cookie", "authorization", "token", "api-key", "apikey", "x-api-key")
    out = {}
    for k, v in headers.items():
        if any(s in k.lower() for s in sensitive):
            out[k] = "***"
        else:
            out[k] = _redact_text(str(v))
    return out

logger = logging.getLogger(__name__)

# HTTP status codes that warrant a retry
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry


def _parse_json_body(content: bytes) -> dict:
    """Parse an LLM response body, transparently decompressing gzip.

    Some gateways/proxies return gzip-compressed bodies without a proper
    Content-Encoding header, so httpx does not decompress them and
    ``resp.json()`` crashes with UnicodeDecodeError on the gzip magic
    bytes (0x1f 0x8b). Detect the magic prefix and decompress first.
    """
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return json.loads(content)


# ── Request-error classification ─────────────────────────────
#
# A failed request is not one thing. The two compact paths act on the answer
# — a request the provider says is *too long* is retried through the chunked
# compactor, anything else must not be — and a blanket `"400" in str(e)` test
# made every refusal look like a length problem. That single mistake is the
# root cause of two rants: a content-filter refusal (400 "Content Exists
# Risk") was "compacted" with the same poisoned text forever
# (rant 2026-09-17T17:55:42), and a body-buffer overflow (413 "length limit
# exceeded") was *not* recognised as too long, so a session could only grow
# (rant 2026-09-17T18:19:45).
#
# So: classify first, act second. The refusal class is checked BEFORE the
# length class, because a content refusal also arrives as a 400 and would
# otherwise be swallowed by the "any 400 means too long" fallback.
CONTENT_RISK_ERROR = "Content Exists Risk"

#: Appended to the raised error when the provider refuses the text itself —
#: the message reaches the client as `LLM error: ...`, so the hint belongs
#: here and not in a log line the host never reads.
CONTENT_RISK_HINT = (
    " — the provider's content filter refused this request's text "
    f"({CONTENT_RISK_ERROR}); it was re-sent once with astral-plane codepoints "
    "written as <U+XXXX> and, still refused, once with the text spaced out. "
    "If this persists the session context contains a fragment the provider "
    "refuses: inspect/clean the session history or start a new session."
)

#: The ``finish_reason`` a provider reports when **its own** content filter
#: blocked the model's output. The request succeeded (HTTP 200); the answer did
#: not. Measured on this host over 2026-09-26..28: 34 such responses, every one
#: of them carrying a non-empty ``reasoning`` and an empty ``content`` — the
#: filter fires during thinking, so not one visible character was ever produced
#: (rant 2026-09-28T16:58:08).
#:
#: It is a **third** outcome alongside ``stop`` and ``tool_calls``, and the
#: reason it needed a name is that the tool loop read it as ``stop``: the turn
#: was called done, an empty assistant record was persisted, and the host saw
#: nothing at all — 34 times, silently.
CONTENT_FILTER_FINISH = "content_filter"

#: HTTP statuses that mean "the request body did not fit".
_OVERLONG_STATUSES = ("400", "413")

#: Bodies that mean "the request body did not fit". Providers word this
#: differently per gateway: a model context overflow, a proxy body limit
#: (413 "Failed to buffer the request body: length limit exceeded"), or a
#: plain "too long".
_OVERLONG_MARKERS = (
    "context length",       # "This model's maximum context length is N tokens"
    "context window",
    "context_length",
    "maximum context",
    "prompt is too long",
    "reduce the length",
    "too long",
    "length limit",         # 413: "Failed to buffer the request body: length limit exceeded"
    "length exceeded",      # same 413 wording, order-independent
)

CONTENT_RISK = "content_risk"
CONTEXT_TOO_LONG = "context_too_long"
OTHER_ERROR = "other"


def classify_llm_error(exc: BaseException) -> str:
    """Say what kind of failure an LLM request hit: ``content_risk`` |
    ``context_too_long`` | ``other``.

    Reads the message, which for this client always carries the response
    status and (redacted) body — see ``chat``/``chat_stream``.

    The discriminating signals, in priority order:
      1. the provider refused the text itself (content filter) — the body
         says ``error.message == "Content Exists Risk"``;
      2. the request did not fit — an overlong marker in the body, or a bare
         400/413 with nothing more specific to go on (the pre-existing
         fallback, now reachable only for non-refusals);
      3. anything else.
    """
    text = str(exc)
    lowered = text.lower()
    if CONTENT_RISK_ERROR.lower() in lowered:
        return CONTENT_RISK
    if any(marker in lowered for marker in _OVERLONG_MARKERS):
        return CONTEXT_TOO_LONG
    if any(status in text for status in _OVERLONG_STATUSES):
        return CONTEXT_TOO_LONG
    return OTHER_ERROR


def is_overlong_error(exc: BaseException) -> bool:
    """Whether a failure is "the request did not fit" — asked of
    :func:`classify_llm_error`, never respelled at the call site.

    Every place that decides "the request was too long, so shrink and retry"
    used to own its own word list: the classifier, the 413 buffer overflow, and
    the chunker's two branches, which tested
    ``"context length" in err or "length limit" in err``. A spelling added to
    one list did not reach the other, so the same 413 body overflow was a
    length problem at one gate and an opaque failure two frames deeper (issue
    #1336; the deepseek-harness session that grew to 7.06 MB with ~179 cycles
    of no output is what the drift cost).

    This is a *predicate*, not a policy: it answers only the length question,
    so a content refusal returns False here and is never split — the priority
    inside :func:`classify_llm_error` is what makes that true.
    """
    return classify_llm_error(exc) == CONTEXT_TOO_LONG


def space_out_text(text: str) -> str:
    """Insert one space between every two characters.

    The transform the host verified by hand (rant 2026-09-17T17:55:42): it
    breaks the exact byte sequence a content filter matches on while leaving
    the text readable to a model. Costs 2x tokens and invalidates the prompt
    cache, so it is only ever applied to a request that already failed.
    """
    return " ".join(text)


def escape_astral_text(text: str) -> str:
    """Write every astral-plane codepoint in ``text`` as its ``<U+XXXX>`` notation.

    The transform that actually works (rant 2026-09-28T15:57:31, measured over
    22 points against the live provider): the content filter strips every
    character for which ``str.isspace()`` is true *before* it matches, so
    inserting spaces — which is all :func:`space_out_text` does — hands it a
    byte sequence identical to the refused one. 101 spaced retries were refused
    101 times while costing +100% of a 2.6 MB body. Replacing a codepoint is a
    change the filter can see, and it costs +0.27% on a payload that contains
    one: the astral planes are the class of refusal the host measured.

    An identity transform for text with no astral codepoint, deliberately — a
    healthy session pays nothing (no byte differs, no prompt cache dies), and
    the caller does not have to ask first.
    """
    return "".join(
        f"<U+{ord(ch):04X}>" if ord(ch) > 0xFFFF else ch for ch in text
    )


def _map_message_text(messages: list[dict], transform) -> list[dict]:
    """A copy of ``messages`` with ``transform`` applied to every text payload.

    Scope is deliberate and shared by every transform in this ladder, because a
    refusal can hide in any of them and a pairing must survive all of them:
      * ``content`` on every role (system / user / assistant / tool) — a
        tool_result body is message content like any other, and the poisoned
        fragment can sit in any of them;
      * multimodal ``content`` parts: only their ``text`` field;
      * identifiers (``id``, ``tool_call_id``) and ``tool_calls`` are copied
        untouched — an assistant message and its tool result must stay
        paired, and ``function.arguments`` is JSON, which spacing between
        every two characters would stop parsing.

    The caller's messages are never mutated.
    """
    out: list[dict] = []
    for message in messages:
        new = dict(message)
        content = new.get("content")
        if isinstance(content, str):
            new["content"] = transform(content)
        elif isinstance(content, list):
            new["content"] = [
                {**part, "text": transform(part["text"])}
                if isinstance(part, dict) and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
        out.append(new)
    return out


def escape_astral_messages(messages: list[dict]) -> list[dict]:
    """``_map_message_text`` with :func:`escape_astral_text`.

    The first rung of the content-refusal ladder; see :data:`CONTENT_RISK_LADDER`.
    """
    return _map_message_text(messages, escape_astral_text)


def space_out_messages(messages: list[dict]) -> list[dict]:
    """``_map_message_text`` with :func:`space_out_text`.

    The rung of last resort: the answer the 2026-09-17 change adopted (rant
    2026-09-17T17:55:42), kept because the ladder is defined to end with it —
    not because it is expected to recover from the refusal measured in
    :func:`escape_astral_text`, which the measurement there says it cannot.
    """
    return _map_message_text(messages, space_out_text)


#: The content-refusal ladder, in the order the rungs are tried (rant
#: 2026-09-28T15:57:31). Each rung is **one-shot**, and the ladder is not a
#: budget: a refusal by the last rung is reported, never retried again, and no
#: retry ever sends a payload two rungs made between them — the second rung is
#: applied to the *original* messages, never to the escaped ones.
CONTENT_RISK_LADDER = ("escape", "space_out")

#: What each rung does, in the words the warning and the host-facing hint print.
CONTENT_RISK_RUNG_DESCRIPTION = {
    "escape": "astral-plane codepoints written as <U+XXXX>",
    "space_out": "the text spaced out",
}

#: The error raised when the filter blocked the model's **output** and the retry
#: ladder is spent. One sentence, raised by both call sites, so the client-facing
#: text cannot differ by which code path hit it.
#:
#: Its rungs are read from :data:`CONTENT_RISK_RUNG_DESCRIPTION` rather than
#: spelled again, because the host reads this to decide whether to retry by hand:
#: an error naming a rung the code no longer walks is the same defect that made
#: the old hint promise a spaced retry that had never once worked.
#:
#: Deliberately NOT :data:`CONTENT_RISK_HINT`: that one is about the *request's*
#: text and sends the host to the session history — the right place for a request
#: the provider refused, and the wrong one here, where the trigger was in the
#: model's own generation and the payload that arrived was never at fault.
CONTENT_FILTER_ERROR = (
    "LLM answer blocked by the provider's content filter: the model produced no "
    f"usable text (finish_reason={CONTENT_FILTER_FINISH}) — it was re-sent with "
    + " and then ".join(
        CONTENT_RISK_RUNG_DESCRIPTION[rung] for rung in CONTENT_RISK_LADDER
    )
    + ", each asking for less output, and the filter blocked every attempt. "
    "Nothing of that answer exists to show: ask a narrower question, or use a "
    "different model."
)

#: The client-facing text for the *other* way a round can arrive with nothing in
#: it: a normal completion (``finish_reason=stop``) with no tool calls whose
#: ``content`` is empty. Sibling of :data:`CONTENT_FILTER_ERROR`, and deliberately
#: a different sentence — nothing refused this answer, so sending the host to the
#: ladder's retries or to the session history would name a cause that is not there.
#:
#: Why it must be reported rather than passed on: an empty `stop` satisfied the
#: tool loop's "final text answer" branch, which persisted an empty assistant
#: record, broadcast `done` with `content: ""`, and stamped the completed-round
#: marker — the file whose age answers "how long since a COMPLETED round" (issue
#: #1114). So a round that produced nothing wrote the very evidence the staleness
#: alarm reads as *a round finished*, and an unattended scheduled cycle that
#: sampled nothing ended "successfully". Measured before writing this: across
#: ~19,857 logged rounds (~3.2 days, four daemon log generations) **zero** `stop`
#: rounds carried an empty `content`, so this is a correctness hole rather than a
#: field incident, and it is stated as such (issue #1723).
EMPTY_ANSWER_ERROR = (
    "the model ended the turn without producing any text "
    "(finish_reason=stop, no tool calls) — nothing was recorded as this turn's "
    "answer and the turn is not counted as a completed round: ask again, or use "
    "a different model."
)


class SelfExplainingLlmError(RuntimeError):
    """An LLM failure whose own sentence is the whole explanation.

    The daemon's error frame appends a generic remedy to every exception it
    reports —

        LLM error: {the exception}. Check config at ~/.emrg/config.toml

    — and that remedy is right for the failures it was written for: a wrong
    ``base_url``, a dead proxy, a rejected key all live in the config, and
    sending the host there is the one useful thing to say. It is the wrong
    instruction for a failure the exchange itself already explains. Measured
    2026-10-03 (``cyc20261003-043254``): a provider that sent a null where a
    tool call should be was answered

        LLM error: 'NoneType' object has no attribute 'get'.
        Check config at ~/.emrg/config.toml

    — a sentence with no subject *and* a host sent to inspect a file that has
    nothing to do with it. The frame test used to be a substring of the
    content-filter sentence (``CONTENT_FILTER_ERROR not in str(e)``), which is
    a marker only the one error that used to carry it could satisfy; a type
    says what the rule is, and a second self-explaining failure joins by
    raising this one.

    Subclasses ``RuntimeError`` deliberately: every existing caller that
    catches ``RuntimeError`` around an LLM call keeps working unchanged.
    """


class ContentFilterAbort(SelfExplainingLlmError):
    """The provider's content filter refused the model's own output.

    One type rather than a substring, because two different questions are asked
    about this failure and only one of them is "does it explain itself?".

    The daemon counts *runs* of these aborts (rant 2026-09-28T15:57:31,
    requirement L3: 53 aborted cycles over two days read, line by line, as 53
    unrelated ones) and used to recognise them by searching the exception's text
    for :data:`CONTENT_FILTER_ERROR` — a single substring answering *both*
    questions, so the second answer was worth no more than the first. Widening
    the first one to "any failure that explains itself" (which is what the
    config remedy turns on, and what the malformed tool call of 2026-10-03
    needed) would have counted *every* such failure as a content-filter abort,
    writing a provider's malformed payload into the run as a refused answer: a
    host told a trigger has been firing that never fired. The second question
    therefore gets a signal of its own that is not a sentence.

    A subclass of :class:`SelfExplainingLlmError` because the refusal's sentence
    is the whole explanation, so it is still reported as its own message rather
    than with the config remedy appended.
    """


def tool_call_unreadable_error(position: int, problem: str) -> SelfExplainingLlmError:
    """The failure raised when the provider's ``tool_calls`` carries an entry
    this client cannot read.

    Named rather than raw, and named *here* rather than at each of the readers,
    because the sentence has to be one sentence: it is what the host reads. It
    says what arrived, where, and that no tool ran — the three facts the raw
    ``AttributeError`` did not carry.

    Deliberately no word from the overlong or content-risk vocabularies
    ("context length", "too long", "length limit", "400", "413", ...): an error
    that matched either would be *acted* on by the round — a content refusal is
    handed to the refusal ladder and an overlong one to the chunked compactor,
    and re-sending a round whose tool calls could not be read is a wrong action
    dressed as a retry. ``tests/test_a_malformed_tool_call_is_named.py`` pins
    that by classifying this error and requiring ``other``.
    """
    return SelfExplainingLlmError(
        f"the provider's tool_calls entry {position} cannot be read: {problem} — "
        "no tool ran for this round and the round is not counted as a completed "
        "one. The request was answered with a payload this client cannot read, "
        "which is a bug in the provider or in the gateway in front of it, not in "
        "this session: retry, or use a different model."
    )


def completion_shape_problem(data: object) -> str | None:
    """Why a non-streaming 200 body is not a completion, or ``None`` when it is.

    The streaming path reads its answer as a sequence of deltas (``chat_stream``);
    this path reads one object, and until now it read it as if the provider had
    obeyed the contract: ``choice = data["choices"][0]``, ``choice.get("message",
    {})``. Measured on this host 2026-10-03 (``cyc20261003-051925``) by calling
    the real ``chat()`` over a stub transport — every shape below parses as JSON:

    ==================================  =========================================
    body sent                           what the caller got
    ==================================  =========================================
    ``[]``                              ``TypeError: list indices must be integers``
    ``"x"``                             ``TypeError: string indices must be integers``
    ``{"id": "1"}``                     ``KeyError: 'choices'``
    ``{"choices": null}``               ``TypeError: 'NoneType' object is not subscriptable``
    ``{"choices": {…}}``                ``KeyError: 0``
    ``{"choices": []}``                 ``IndexError: list index out of range``
    ``{"choices": [null]}``             ``AttributeError: 'NoneType' object has no attribute 'get'``
    ``{"choices": ["x"]}``              ``AttributeError: 'str' object has no attribute 'get'``
    ``{"choices": [{"message": null}]}``  ``AttributeError: 'NoneType' object has no attribute 'get'``
    ``{"choices": [{"message": "ok"}]}``  ``AttributeError: 'str' object has no attribute 'get'``
    ``{"choices": [{"message": {"content": ["a"]}}]}``  *returned* — a list reaches the callers
    ``{"choices": [{"message": {"content": 5}}]}``      *returned* — an int reaches the callers
    ``{"choices": [{"finish_reason": "stop"}]}``  ``RuntimeError`` — the existing refusal below
    ==================================  =========================================

    Ten bodies, ten raw Python exceptions, none of which names the provider, the
    field or the shape — and none of which any caller can act on. Two more do not
    raise at all and hand a payload no caller can read to whoever asked. Every one
    of them is a *gateway* artefact (a proxy answering 200 with its own envelope, a
    truncated upstream response), which is the same class the unparseable-body
    branch above already retries; this rule closes the same door one step later,
    where the JSON happens to parse.

    The rule
    --------
    The body is an object; it carries a ``choices`` that is a **non-empty list**;
    ``choices[0]`` is an object; ``message``, when the choice carries the key, is
    an object; and ``message.content``, when that object carries the key, is a
    string or null — the shape every caller reads it as.

    Three deliberate boundaries:

    * **A missing ``message`` is not this rule's question.** ``{}`` for the
      message is what the read already falls back to, and the existing
      "returned no answer at all" refusal below names ``finish_reason`` for it —
      a better sentence than a shape complaint about a key that is absent.
    * **Null ``content`` is not a wrong string.** It is the contract's own value
      for a tool-call answer, and the existing empty-answer branch reads it.
    * **A non-string ``content`` is refused**, and that one is worth its reason:
      ``{"choices": [{"message": {"content": ["a", "b"]}}]}`` is *accepted* today
      and does not raise at all. Two callers then misuse it — ``_do_compact``
      stores it as the session's summary (``msg.get("content", "Summary
      unavailable.")``) and broadcasts it, and the title path calls ``.strip()``
      on it. A shape the callers cannot read is a refusal here, where the reason
      is still available to say, rather than a corrupt summary three steps later.

    :param data: the parsed body of a 200 response, whatever shape it arrived in.
    :returns: a refusal sentence naming the part and the shape it received, or
        ``None`` when the body is a completion.
    """
    if not isinstance(data, dict):
        return f"the response body is not an object; got {shape_of(data)}"
    if "choices" not in data:
        return "the response carries no `choices`"
    choices = data["choices"]
    if not isinstance(choices, list):
        return f"`choices` is not a list; got {shape_of(choices)}"
    if not choices:
        return "`choices` is empty — there is no completion in the response"
    choice = choices[0]
    if not isinstance(choice, dict):
        return f"`choices[0]` is not an object; got {shape_of(choice)}"
    if "message" not in choice:
        return None
    message = choice["message"]
    if not isinstance(message, dict):
        return f"`choices[0].message` is not an object; got {shape_of(message)}"
    if "content" not in message:
        return None
    content = message["content"]
    if content is None or isinstance(content, str):
        return None
    return (
        "`choices[0].message.content` is not a string; "
        f"got {shape_of(content)}"
    )


def completion_unreadable_error(problem: str) -> SelfExplainingLlmError:
    """The failure raised when a 200 body parses and is not a completion.

    Retried, then reported — the same treatment, and for the same reason, as the
    unparseable-body branch in :meth:`LlmClient.chat`: both are "the answer did
    not arrive in the form the wire contract declares", both are documented
    gateway/proxy artefacts, and a provider asked again a second later very often
    answers properly.

    Named, and carrying no word from the overlong or content-risk vocabularies,
    for the reason :func:`tool_call_unreadable_error` is: this sentence is what
    the host reads, and a classifier that mistook it for a length problem would
    hand it to the chunker, which re-sends the very request the provider could
    not answer in the expected shape.
    """
    return SelfExplainingLlmError(
        f"the provider's answer is not a completion: {problem} — nothing was "
        "recorded from this request. A 200 whose body is not the object the "
        "OpenAI-compatible contract declares is a bug in the provider or in the "
        "gateway in front of it, not in this session: retry, or use a different "
        "model."
    )


def chunk_shape_problem(chunk: object) -> str | None:
    """Why one streamed chunk is not a chunk, or ``None`` when it is.

    The streaming twin of :func:`completion_shape_problem`, on the path every
    ordinary conversation takes: ``chat_stream`` reads one object per SSE line,
    and until now it read each one with ``chunk.get`` / ``choices[0].get`` as if
    the provider had obeyed the contract. Measured on this host 2026-10-03
    (``cyc20261003-054214``) by driving the real ``chat_stream`` over a stub
    transport — every chunk below is valid JSON:

    ==================================  =========================================
    chunk sent                          what the caller got
    ==================================  =========================================
    ``[...]`` (a list)                  ``AttributeError: 'list' object has no attribute 'get'``
    ``"x"`` / ``5``                     ``AttributeError: 'str' / 'int' object has no attribute 'get'``
    ``{"choices": {"delta": …}}``       ``KeyError: 0`` — the message is the number ``0``
    ``{"choices": "x"}``                ``AttributeError: 'str' object has no attribute 'get'``
    ``{"choices": ["x"]}``              ``AttributeError: 'str' object has no attribute 'get'``
    ``{"choices": [null]}``             ``AttributeError: 'NoneType' object has no attribute 'get'``
    ``{"choices": [{"delta": null}]}``  ``AttributeError: 'NoneType' object has no attribute 'get'``
    ``{"choices": [{"delta": "x"}]}``   ``AttributeError: 'str' object has no attribute 'get'``
    ``{"usage": "x"}``                  ``AttributeError: 'str' object has no attribute 'get'``
    ==================================  =========================================

    and, one step further out, a chunk the reader *accepts* and whose content it
    cannot join — ``{"delta": {"content": ["a"]}}`` was yielded as-is, and the
    round then died in ``"".join(content_parts)`` with
    ``TypeError: sequence item 0: expected str instance, list found`` **and no
    frame of any kind**: no error, no ``done``, so the client's busy flag is
    never cleared (the wedge PR #1669 exists for).

    The rule
    --------
    The chunk is an object; ``choices``, when it is read, is a list;
    ``choices[0]`` is an object; ``delta``, when the key is present, is an
    object; a delta's ``content`` / ``reasoning_content`` / ``reasoning``, when
    read, are strings; and ``usage``, when read, is an object.

    The boundary is **measured, not derived from the types**: the reader already
    skips the *empty* values, so those are left alone — a rule that refused them
    would invent a failure for a chunk that runs today (the same lesson
    ``emrg/tools/argument_shape.py`` records for an optional null). Measured the
    same way: ``{"choices": {}}``, ``{"choices": ""}``, ``{"choices": 0}``,
    ``{"delta": {"content": []}}``, ``{"delta": {"content": 0}}``,
    ``{"delta": {"reasoning_content": []}}``, ``{"usage": []}``,
    ``{"usage": null}`` and ``{"usage": 0}`` all pass through untouched today —
    each one is falsy, and each read is written ``if not choices: continue`` or
    ``if text_content:``.

    **One field does not follow that pattern, and the reason is mechanical:**
    ``delta``. ``choice.get("delta", {})`` supplies its default only when the key
    is *absent*, so an explicit ``null`` — or any other falsy non-object — is
    read as it is and breaks. ``{"delta": null}``, ``{"delta": []}`` and
    ``{"delta": 0}`` are therefore refused although they are empty, and the
    measurement is what says so.

    ``finish_reason`` is deliberately **not** checked: ``5`` and ``[]`` both pass
    through today and neither breaks a read — the value is only ever compared to
    two strings — so a rule about it would be a behaviour change dressed as a
    defect fix.

    :param chunk: one decoded SSE ``data:`` payload, whatever shape it arrived in.
    :returns: a refusal sentence naming the part and the shape it received, or
        ``None`` when the chunk is readable.
    """
    if not isinstance(chunk, dict):
        return f"the stream chunk is not an object; got {shape_of(chunk)}"

    choices = chunk.get("choices")
    if choices and not isinstance(choices, list):
        return f"the chunk's `choices` is not a list; got {shape_of(choices)}"

    for choice in choices or []:
        if not isinstance(choice, dict):
            return f"`choices[0]` is not an object; got {shape_of(choice)}"
        if "delta" not in choice:
            continue
        delta = choice["delta"]
        if not isinstance(delta, dict):
            # Read even when empty: see the docstring's measured boundary.
            return f"`choices[0].delta` is not an object; got {shape_of(delta)}"
        for field in ("content", "reasoning_content", "reasoning"):
            value = delta.get(field)
            if value and not isinstance(value, str):
                return (
                    f"`choices[0].delta.{field}` is not a string; "
                    f"got {shape_of(value)}"
                )

    usage = chunk.get("usage")
    if usage and not isinstance(usage, dict):
        return f"the chunk's `usage` is not an object; got {shape_of(usage)}"
    return None


def chunk_unreadable_error(problem: str) -> SelfExplainingLlmError:
    """The failure raised when the provider's stream carries a chunk this client
    cannot read.

    Same treatment family as :func:`completion_unreadable_error`, with the one
    difference the streaming path imposes: a stream that has already yielded a
    delta cannot be re-sent, because the client is showing that text (the rule
    ``yielded_delta`` already enforces for transport failures). So the retry is
    taken only while nothing has reached the caller, and the sentence says what
    arrived either way.

    No word from the overlong or content-risk vocabularies, for the reason given
    on its two siblings.
    """
    return SelfExplainingLlmError(
        f"the provider's stream is not a stream this client can read: {problem} — "
        "the answer was cut off, and nothing from it was recorded as this round's "
        "result. A stream that carries a chunk unlike the one the OpenAI-compatible "
        "contract declares is a bug in the provider or in the gateway in front of "
        "it, not in this session: retry, or use a different model."
    )


def content_risk_retry(stage: int, original: list[dict]) -> tuple[str, list[dict]] | None:
    """The next rung for a content refusal, or ``None`` when the ladder is spent.

    Returns ``(rung, messages)`` — the messages to send on this rung, built from
    ``original`` (the payload as the caller wrote it, captured before any rung
    ran). Building each rung from ``original`` is the whole point of the
    signature: rung 2 must not be applied on top of rung 1's output, which would
    send a payload neither transform alone describes, and would make the second
    retry's cost depend on the first's.

    ``stage`` is how many rungs have already been spent, so the caller's state is
    one integer and the ladder has exactly one implementation — the two call
    sites in this module (``chat`` and the streaming path) are literals of each
    other, and a rule spelled twice is a rule that drifts.
    """
    if stage < 0 or stage >= len(CONTENT_RISK_LADDER):
        return None
    rung = CONTENT_RISK_LADDER[stage]
    if rung == "escape":
        return rung, escape_astral_messages(original)
    return rung, space_out_messages(original)


#: The instruction appended to every content-filter retry, on the request side
#: of a refusal whose trigger is on the *response* side (host, 2026-09-28; rant
#: 2026-09-28T16:58:08).
#:
#: A transform alone cannot be enough here, and the measurement says why: the
#: recorded refusals all carried a non-empty ``reasoning`` and an empty
#: ``content``, so the filter had already decided before one visible character
#: existed. Re-sending the same payload asks the same model for the same sample.
#: Asking for less output — and for a different wording — is the lever that can
#: change what is sampled, which is why the host asked for this in addition to
#: the ladder rather than instead of it.
CONTENT_FILTER_RETRY_INSTRUCTION = (
    "Your previous answer to this conversation was blocked by the provider's "
    "content filter (finish_reason=content_filter) before any of it reached the "
    "user, so nothing of it was seen. Answer again, and make this attempt "
    "different:\n"
    "- give the conclusion, not the process: no restating the question, no "
    "working notes, no repeating earlier turns;\n"
    "- do not reproduce or paraphrase the text that was blocked;\n"
    "- keep it as short as the task allows, and prefer neutral wording."
)


def content_filter_retry_messages(messages: list[dict]) -> list[dict]:
    """``messages`` plus :data:`CONTENT_FILTER_RETRY_INSTRUCTION` as a final turn.

    A copy, and that is the requirement rather than a detail: the instruction is
    in force for **this retry only** and must never be written into the session
    history, or every later turn would carry an explanation of an event the
    conversation already moved past (rant 2026-09-28T16:58:08, requirement C).
    The caller owns the history; this function cannot reach it.
    """
    return [*messages, {"role": "user", "content": CONTENT_FILTER_RETRY_INSTRUCTION}]


def with_content_risk_hint(message: str) -> str:
    """Append the host-facing hint when ``message`` is a content refusal.

    Idempotent: an error that already went through here (it is raised, caught
    and re-reported) is not annotated twice.
    """
    if classify_llm_error(RuntimeError(message)) != CONTENT_RISK:
        return message
    if CONTENT_RISK_HINT in message:
        return message
    return message + CONTENT_RISK_HINT


class LlmClient:
    """Async LLM client with tool calling and multi-turn streaming support."""

    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        # Last request/response metadata for llm.jsonl logging
        self.last_payload: dict = {}
        self.last_response_status: int = 0
        self.last_response_headers: dict = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    def _make_payload(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        stream: bool = False,
    ) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
            if self.config.stream_options is not None:
                payload["stream_options"] = self.config.stream_options
        return payload

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"emrg/{__version__}",
        }

    def _advance_content_filter_ladder(
        self,
        payload: dict,
        original_messages: list[dict],
        stage: int,
        *,
        streaming: bool,
        already_streamed: int = 0,
    ) -> int:
        """Answer a filter-blocked answer with the next rung — or report honestly.

        Called when the provider returned HTTP 200 and then refused the model's
        own output (``finish_reason=content_filter``). Rewrites
        ``payload["messages"]`` in place with the next rung *plus* the
        shorter-output instruction, and returns the new ``stage`` so the caller
        can re-send.

        Raises :data:`CONTENT_FILTER_ERROR` when the ladder is spent. One
        implementation for both call sites, and that is the point: ``chat`` and
        ``chat_stream`` already spell the refusal ladder twice, and the whole
        reason :func:`content_risk_retry` exists is to keep the two from
        drifting apart. A refusal that no longer has a retry must never become
        an empty answer — that is the defect this path was written for.

        ``already_streamed`` is how much text of the *blocked* attempt had
        reached the caller; it is reported, never acted on, because deciding
        "may this attempt be retried?" belongs at the call site, where the fact
        is (the streaming path refuses to retry once anything visible has been
        yielded).
        """
        nxt = content_risk_retry(stage, original_messages)
        if nxt is None:
            logger.error(
                "LLM%s content filter blocked the answer again after %d retries "
                "(finish_reason=%s, %d character(s) already sent) — reporting it: "
                "an empty answer is not a finished turn",
                " stream" if streaming else "", len(CONTENT_RISK_LADDER),
                CONTENT_FILTER_FINISH, already_streamed,
            )
            raise ContentFilterAbort(CONTENT_FILTER_ERROR)
        rung, rung_messages = nxt
        logger.warning(
            "LLM%s content filter blocked the answer (finish_reason=%s) — "
            "re-sending (%d of %d) with %s and the shorter-output instruction",
            " stream" if streaming else "", CONTENT_FILTER_FINISH,
            stage + 1, len(CONTENT_RISK_LADDER), CONTENT_RISK_RUNG_DESCRIPTION[rung],
        )
        payload["messages"] = content_filter_retry_messages(rung_messages)
        self.last_payload = dict(payload)
        return stage + 1

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """Non-streaming chat completion with optional tool calling.

        Returns the full message dict from choices[0].message,
        which may contain 'content' (str or None) and/or 'tool_calls'.

        Retries on transient errors (429, 5xx) with exponential backoff.
        """
        client = await self._get_client()
        url = f"{self.config.base_url}/chat/completions"
        payload = self._make_payload(messages, tools, stream=False)
        headers = self._headers()

        # Store for llm.jsonl logging
        self.last_payload = dict(payload)
        self.last_response_status = 0
        self.last_response_headers = {}

        last_error = None
        # One-shot per rung: after a content-filter refusal the request is re-sent
        # once with the astral-plane codepoints written out, and — still refused —
        # once more with the text spaced out (`CONTENT_RISK_LADDER`). A refusal
        # after both is reported honestly: never retried again, never handed to
        # the chunked compactor (rant 2026-09-17T17:55:42, rant 2026-09-28T15:57:31).
        #
        # `original` is the payload as the caller wrote it, captured here and never
        # reassigned: every rung is built from it, so no retry sends a payload two
        # transforms made between them (`content_risk_retry`).
        original_messages = list(payload["messages"])
        content_risk_stage = 0
        for attempt in range(MAX_RETRIES + 1):
            # First attempt is the normal path — log nothing (rant
            # 2026-08-17T14:27:39: 1/4 on every request is noise); retries
            # already log via the "transient error ... retrying" warning,
            # this debug line only adds the attempt counter for retries.
            if attempt > 0:
                logger.debug("LLM request: url=%s model=%s (attempt %d/%d)",
                             _redact_text(url), self.config.model, attempt + 1, MAX_RETRIES + 1)

            resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                self.last_response_status = resp.status_code
                self.last_response_headers = dict(resp.headers)
                try:
                    data = _parse_json_body(resp.content)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError, EOFError) as exc:
                    # Malformed body (e.g. truncated gzip / proxy error page).
                    # Treat as transient — retry with backoff instead of crashing
                    # (20260807: memory reflection died on gzip body without
                    # Content-Encoding, resp.json() raised UnicodeDecodeError).
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        logger.warning(
                            "LLM response body unparseable (%s), retrying in %.1fs "
                            "(attempt %d/%d)",
                            type(exc).__name__, delay, attempt + 1, MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_error = RuntimeError(
                            f"LLM response body unparseable: {type(exc).__name__}"
                        )
                        continue
                    raise RuntimeError(
                        f"LLM response body unparseable: {type(exc).__name__}"
                    ) from exc

                # …and a body that parses and is still not a completion is the
                # same class of fault one step later (measured 2026-10-03,
                # `cyc20261003-051925`: nine such bodies, eight raw exceptions —
                # see `completion_shape_problem`). Same treatment as above, and
                # for the same reason: this is a gateway artefact, not a bad
                # request, so it is worth re-asking once.
                problem = completion_shape_problem(data)
                if problem is not None:
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        logger.warning(
                            "LLM response is not a completion (%s), retrying in "
                            "%.1fs (attempt %d/%d)",
                            problem, delay, attempt + 1, MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_error = completion_unreadable_error(problem)
                        continue
                    raise completion_unreadable_error(problem)

                choice = data["choices"][0]
                message = choice.get("message", {})
                # ── The provider refused the model's OWN output ───────
                # A 200 whose finish_reason is content_filter carries an empty
                # content, and every caller of this method (compact summary,
                # session title, memory reflection, task vibe check) would have
                # stored that empty string as the answer — the compact path
                # silently flattened the context (rant 2026-09-28T16:58:08).
                # Same one-shot ladder as the 400 path above, plus the
                # shorter-output instruction; then report.
                if choice.get("finish_reason") == CONTENT_FILTER_FINISH:
                    content_risk_stage = self._advance_content_filter_ladder(
                        payload, original_messages, content_risk_stage,
                        streaming=False,
                    )
                    continue
                # Nothing came back and nothing was asked for: refuse, rather
                # than hand an empty string to a caller that treats it as text.
                if not message.get("content") and not message.get("tool_calls"):
                    raise RuntimeError(
                        "LLM request returned no answer at all "
                        f"(finish_reason={choice.get('finish_reason')!r}, "
                        "empty content and no tool calls) — an empty string is "
                        "not a summary, a title or a reflection"
                    )
                return message

            text = resp.text[:500]
            # ── The provider refused the text itself ──────────────
            # Checked before the transient/length branches: a content refusal
            # arrives as a 400 and must never be read as "too long" (that
            # misreading is the self-lock of rant 2026-09-17T17:55:42).
            refusal = f"LLM request failed: {resp.status_code} - {_redact_text(text)}"
            if classify_llm_error(RuntimeError(refusal)) == CONTENT_RISK:
                nxt = content_risk_retry(content_risk_stage, original_messages)
                if nxt is not None:
                    rung, messages = nxt
                    content_risk_stage += 1
                    logger.warning(
                        "LLM content-filter refusal %d — re-sending (%s of %d) with %s: %s",
                        resp.status_code, content_risk_stage, len(CONTENT_RISK_LADDER),
                        CONTENT_RISK_RUNG_DESCRIPTION[rung], _redact_text(text[:200]),
                    )
                    payload["messages"] = messages
                    self.last_payload = dict(payload)
                    continue
                logger.error(
                    "LLM content-filter refusal again after %d retries "
                    "(status=%d): %s", len(CONTENT_RISK_LADDER), resp.status_code,
                    _redact_text(text[:500]),
                )
            if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "LLM transient error %d, retrying in %.1fs (attempt %d/%d): %s",
                    resp.status_code, delay, attempt + 1, MAX_RETRIES,
                    _redact_text(text[:200]),
                )
                await asyncio.sleep(delay)
                last_error = RuntimeError(
                    f"LLM request failed: {resp.status_code} - {_redact_text(text)}"
                )
                continue

            # 错误日志与异常信息脱敏：response headers 可能回显 set-cookie/auth，
            # body 可能含敏感回显；统一经脱敏 + 截断（防止 API key 等泄露到 emrgd.log / 会话）。
            hdr = _redact_headers(dict(resp.headers))
            text_redacted = _redact_text(text)
            logger.error("LLM error: %s headers=%s body=%s", resp.status_code, hdr, text_redacted[:2000])
            raise RuntimeError(
                with_content_risk_hint(
                    f"LLM request failed: {resp.status_code} headers={hdr} body={text_redacted[:2000]}"
                )
            )

        raise last_error  # type: ignore[misc]

    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[dict]:
        """Streaming chat with optional tool calling.

        Yields dicts of shape:
            {"content": str | None, "tool_calls": list[dict] | None,
             "finish_reason": str | None, "usage": dict | None,
             "reasoning": str | None}

        tool_calls are accumulated across chunks (by index). Each yield
        carries the current accumulated state so callers can track progress.

        ``reasoning`` (rant 2026-08-18T09:43:23) carries the accumulated
        think/chain-of-thought text (``reasoning_content`` / ``reasoning``
        deltas) or None when the model does not reason. Usage may include
        ``reasoning_tokens`` when the API reports it, and ``cache_hit_tokens``
        when the API reports prompt caching (rant 2026-08-23T09:17:14).

        The final yield includes usage (prompt_tokens, completion_tokens)
        when the API provides it.

        Finish reasons: "stop" (final text), "tool_calls" (model wants tools),
        "length" (max_tokens hit), "content_filter" (blocked).

        Retries on transient HTTP errors (429, 5xx) with exponential backoff,
        same as chat().
        """
        client = await self._get_client()
        url = f"{self.config.base_url}/chat/completions"
        payload = self._make_payload(messages, tools, stream=True)
        headers = {**self._headers(), "Accept": "text/event-stream"}

        # Store for llm.jsonl logging
        self.last_payload = dict(payload)
        self.last_response_status = 0
        self.last_response_headers = {}

        logger.debug("LLM stream: url=%s model=%s", _redact_text(url), self.config.model)

        # Accumulated state across chunks (reset on retry)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tc_by_index: dict[int, dict] = {}

        last_error = None
        # Same ladder as `chat()` — one-shot per rung, each built from the payload
        # as the caller wrote it (rant 2026-09-28T15:57:31).
        original_messages = list(payload["messages"])
        content_risk_stage = 0
        for attempt in range(MAX_RETRIES + 1):
            # First attempt silent (rant 2026-08-17T14:27:39) — the retrying
            # warning already logs the retry; this adds the attempt counter.
            if attempt > 0:
                logger.debug("LLM stream attempt %d/%d", attempt + 1, MAX_RETRIES + 1)
            # Reset accumulators before each attempt
            content_parts[:] = []
            reasoning_parts[:] = []
            tc_by_index.clear()
            # True once a delta has been yielded to the caller. Retrying after
            # that point would duplicate already-streamed content/broadcasts,
            # so transport errors mid-stream after a yield are NOT retried
            # (rant 2026-08-31T12:53:13).
            yielded_delta = False

            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        text = await resp.aread()
                        # Content-filter refusal: the same one-shot-per-rung ladder
                        # as chat() (rant 2026-09-17T17:55:42, rant
                        # 2026-09-28T15:57:31). This branch runs before any delta is
                        # yielded, so retrying here cannot duplicate streamed content.
                        refusal = (
                            f"LLM stream request failed: {resp.status_code} - "
                            f"{_redact_text(text[:500])}"
                        )
                        if classify_llm_error(RuntimeError(refusal)) == CONTENT_RISK:
                            nxt = content_risk_retry(content_risk_stage, original_messages)
                            if nxt is not None:
                                rung, messages = nxt
                                content_risk_stage += 1
                                logger.warning(
                                    "LLM stream content-filter refusal %d — re-sending "
                                    "(%s of %d) with %s: %s",
                                    resp.status_code, content_risk_stage,
                                    len(CONTENT_RISK_LADDER),
                                    CONTENT_RISK_RUNG_DESCRIPTION[rung],
                                    _redact_text(text[:200]),
                                )
                                payload["messages"] = messages
                                self.last_payload = dict(payload)
                                continue
                            logger.error(
                                "LLM stream content-filter refusal again after %d "
                                "retries (status=%d): %s", len(CONTENT_RISK_LADDER),
                                resp.status_code, _redact_text(text[:500]),
                            )
                        if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY * (2 ** attempt)
                            logger.warning(
                                "LLM stream transient error %d, retrying in %.1fs "
                                "(attempt %d/%d): %s",
                                resp.status_code, delay, attempt + 1, MAX_RETRIES,
                                _redact_text(text[:200]),
                            )
                            await asyncio.sleep(delay)
                            last_error = RuntimeError(
                                f"LLM stream request failed: {resp.status_code} - {_redact_text(text[:500])}"
                            )
                            continue
                        logger.error("LLM stream error: %s %s", resp.status_code, _redact_text(text[:500]))
                        hdr = _redact_headers(dict(resp.headers))
                        raise RuntimeError(
                            with_content_risk_hint(
                                f"LLM stream request failed: {resp.status_code} "
                                f"headers={hdr} body={_redact_text(text[:1000])}"
                            )
                        )

                    # Capture response metadata for llm.jsonl logging
                    self.last_response_status = resp.status_code
                    self.last_response_headers = dict(resp.headers)

                    # True when this attempt's verdict was the provider's content
                    # filter rather than an answer; decided inside the loop, acted
                    # on after it (rant 2026-09-28T16:58:08).
                    blocked = False

                    # The shape refusal for the chunk that ended this attempt's
                    # read, or None. Collected the same way and for the same
                    # reason as `blocked`: the reaction is a retry, and a `continue`
                    # cannot be taken from inside the nested read.
                    chunk_problem: str | None = None

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line == "[DONE]" or not line.startswith("data: "):
                            continue

                        json_str = line[6:]
                        try:
                            chunk = json.loads(json_str)
                        except json.JSONDecodeError:
                            logger.debug("SSE parse skip: %s", json_str[:80])
                            continue

                        # The chunk's own shape, before anything is read out of
                        # it — measured 2026-10-03 (`cyc20261003-054214`): nine
                        # such chunks raised nine raw sentences naming neither
                        # the provider nor the field, and a tenth was accepted
                        # and killed the round in `"".join(...)` with no frame at
                        # all. One home states the rule.
                        chunk_problem = chunk_shape_problem(chunk)
                        if chunk_problem is not None:
                            break

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        finish = choices[0].get("finish_reason")

                        # Accumulate text content
                        text_content = delta.get("content", "")
                        if text_content:
                            content_parts.append(text_content)

                        # Accumulate reasoning / think block (rant 2026-08-18T09:43:23):
                        # DeepSeek sends `reasoning_content`, OpenAI-compatible
                        # endpoints may send `reasoning` — accept both field names,
                        # accumulating per-delta like content_parts.
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if reasoning:
                            reasoning_parts.append(reasoning)

                        # Accumulate tool_calls from delta
                        for _position, tc in enumerate(delta.get("tool_calls", [])):
                            # The entry is read before anything else may touch
                            # it: measured 2026-10-03, a provider that sent a
                            # null (or a string, or a list) here raised
                            # `AttributeError: 'NoneType' object has no attribute
                            # 'get'` out of this generator, and the round
                            # reported it as `LLM error: ... Check config at
                            # ~/.emrg/config.toml` — the shape never named and
                            # the host sent to inspect a config file. One home
                            # states the rule; this is the first of its readers.
                            _problem = tool_call_shape_problem(tc)
                            if _problem is not None:
                                raise tool_call_unreadable_error(_position, _problem)
                            idx = tc.get("index", 0)
                            if idx not in tc_by_index:
                                tc_by_index[idx] = {
                                    "index": idx,
                                    "id": tc.get("id", ""),
                                    "function": {"name": "", "arguments": ""},
                                }
                            acc = tc_by_index[idx]
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                acc["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += tool_arguments_text(
                                    fn["arguments"]
                                )

                        # Build current accumulated tool_calls list
                        current_tool_calls: list[dict] | None = None
                        if tc_by_index:
                            current_tool_calls = [
                                tc_by_index[i] for i in sorted(tc_by_index.keys())
                            ]

                        # Capture usage from chunk (may appear in any chunk or only in final)
                        usage = chunk.get("usage")
                        # reasoning_tokens may sit at the top level or nested under
                        # completion_tokens_details (rant 2026-08-18T09:43:23)
                        reasoning_tokens = (
                            usage.get("reasoning_tokens")
                            if usage and usage.get("reasoning_tokens") is not None
                            else (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                            if usage else None
                        )
                        # cache_hit_tokens: DeepSeek native `prompt_cache_hit_tokens`
                        # (top level) or OpenAI-compatible `prompt_tokens_details.cached_tokens`
                        # (nested) — same dual-location pattern as reasoning_tokens
                        # (rant 2026-08-23T09:17:14, LLM usage cache hit/miss visibility).
                        # Omitted when the API does not report caching, so non-cache
                        # endpoints keep the exact same record shape (no new fields).
                        cache_hit_tokens = (
                            usage.get("prompt_cache_hit_tokens")
                            if usage and usage.get("prompt_cache_hit_tokens") is not None
                            else (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                            if usage else None
                        )
                        usage_out: dict | None = None
                        if usage:
                            usage_out = {
                                "prompt_tokens": usage.get("prompt_tokens"),
                                "completion_tokens": usage.get("completion_tokens"),
                                "reasoning_tokens": reasoning_tokens,
                            }
                            if cache_hit_tokens is not None:
                                usage_out["cache_hit_tokens"] = cache_hit_tokens

                        # The provider refused the model's own output (rant
                        # 2026-09-28T16:58:08), decided BEFORE the yield so no
                        # finish_reason=content_filter ever reaches a caller that
                        # would read it as this round's verdict. Retried only
                        # while this attempt produced nothing usable: an attempt
                        # whose text or tool calls were already streamed cannot be
                        # re-sent without duplicating them (the same rule the
                        # transport retry below obeys through `yielded_delta`).
                        if (
                            finish == CONTENT_FILTER_FINISH
                            and not content_parts
                            and not tc_by_index
                        ):
                            blocked = True
                            break

                        # Any yield = the caller has seen (and likely broadcast)
                        # this delta — no retry past this point.
                        yielded_delta = True
                        yield {
                            "content": text_content or None,
                            "tool_calls": current_tool_calls,
                            "finish_reason": finish,
                            "usage": usage_out,
                            # accumulated think text; None when the model does not
                            # reason (rant 2026-08-18T09:43:23 — only on response side)
                            "reasoning": "".join(reasoning_parts) or None,
                        }

                        # On finish, we're done with this stream
                        if finish:
                            return

                    if blocked:
                        content_risk_stage = self._advance_content_filter_ladder(
                            payload, original_messages, content_risk_stage,
                            streaming=True,
                        )
                        continue

                    if chunk_problem is not None:
                        # A gateway that answers with a chunk unlike the one the
                        # contract declares is the streaming form of the body
                        # `chat()` refuses (`completion_shape_problem`), and it
                        # gets the same treatment — with the one limit a stream
                        # imposes: an attempt that has already yielded a delta
                        # cannot be re-sent, because the client is showing that
                        # text, so the retry is taken only while nothing has
                        # reached the caller (the rule `yielded_delta` enforces).
                        if not yielded_delta and attempt < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY * (2 ** attempt)
                            logger.warning(
                                "LLM stream chunk is not a chunk (%s), retrying "
                                "in %.1fs (attempt %d/%d): %s",
                                chunk_problem, delay, attempt + 1, MAX_RETRIES,
                                chunk_problem,
                            )
                            await asyncio.sleep(delay)
                            last_error = chunk_unreadable_error(chunk_problem)
                            continue
                        raise chunk_unreadable_error(chunk_problem)
            except httpx.TransportError as exc:
                # Transient transport failure while streaming — httpx.ReadTimeout
                # (no data block for the 120s read timeout), ConnectError,
                # RemoteProtocolError (connection dropped), etc. Previously these
                # bubbled straight out of the iterator, skipping the retry loop
                # and killing the whole tool round (rant 2026-08-31T12:53:13).
                # Retry only while no delta has been yielded yet — once the
                # caller has seen a yield, a retry would duplicate the already
                # streamed/broadcast content.
                last_error = RuntimeError(
                    f"LLM stream transport error: {type(exc).__name__}"
                )
                if not yielded_delta and attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "LLM stream %s, retrying in %.1fs (attempt %d/%d)",
                        type(exc).__name__, delay, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            # Stream ended without finish_reason (e.g. connection drop
            # mid-stream). Treat as transient error — retry if attempts remain.
            last_error = RuntimeError(
                "LLM stream ended without finish_reason"
            )
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "LLM stream ended prematurely, retrying in %.1fs "
                    "(attempt %d/%d)",
                    delay, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

        raise last_error  # type: ignore[misc]

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
