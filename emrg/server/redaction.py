"""Credential redaction — one statement of the rule, for every log site that prints text.

Rant ``2026-08-06T10:21:26``: a tool call's arguments can carry a credential, and
``emrgd.log`` is a file hosts paste into issues, so a command must not reach it with a live
key in it. Two shapes are masked, and the second exists because the first is not enough:

* a **key-name/value pair** (``api_key=...``, ``token: ...``) — masked by pattern;
* a credential with no label at all (``sk-``, ``ghp_``, ``xox``, ``AKIA``, a JWT, a
  ``Bearer`` header) — the cross-project lesson was that a base64-wrapped ``access_token``
  body carries no plaintext label for a pattern to catch, so a long base64 run that *decodes*
  into one is masked whole.

**Why this lives here rather than in the daemon.** ``emrg.tools.bash_tool_v2`` and
``emrg.tools.pwsh_tool_v2`` log the argv they are about to spawn, and the argv of an
unconfined run is ``[<shell>, "-c", <the whole command>]`` — so the rule has to be reachable
from the tools. They cannot import ``emrg.server.daemon`` (it imports them), so the rule sits
in a leaf module both layers already import from (the tools already take
``emrg.server.tool_types`` and ``emrg.server.git_utils`` this way).

**What was cut when it moved.** The daemon also carried a dict/list-recursive form
(``_redact``) and a key-name rule (``SENSITIVE_KEY_SUBSTRINGS``). Both were written for "a
tool call's arguments reaching ``emrgd.log``", and that no longer happens: the tool-call log
line states the tool's **name and intent**, never its arguments, and no other site passes a
dict through. Measured 2026-10-12 on master — no caller anywhere in the tree (only five tests
in ``tests/test_daemon.py``, which pinned it) — so the two are gone and the four arms that
still describe behaviour every caller relies on moved with the rule, re-pointed at
:func:`redact_string`.
"""

from __future__ import annotations

import base64
import re

# 字符串值内联凭据模式（保守匹配，宁多勿漏）
INLINE_SECRET_PATTERNS = (
    # sk- 密钥：sk- 后必须 ≥16 位纯字母数字（OpenAI/DeepSeek），或 sk-proj-（OpenAI 项目）
    # 或 sk-ant-apiNN-（Anthropic）—— 排除 "task-evolution" 等路径片段误伤
    re.compile(r"(sk-(?:proj-)?[A-Za-z0-9]{16,})"),
    re.compile(r"(sk-ant-api[0-9]+-[A-Za-z0-9]{16,})"),           # Anthropic
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),                  # GitHub PAT / OAuth / gist token
    re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{10,})"),               # Slack token
    re.compile(r"(AKIA[0-9A-Z]{16})"),                            # AWS access key id
    re.compile(r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),  # Bearer 令牌
    re.compile(r"(Authorization\s*[:=]\s*[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
    re.compile(r"((api[_-]?key|apikey|password|passwd|secret|token)\s*[:=]\s*[^\s,;\"']+)", re.IGNORECASE),
    # JWT：三段 base64url（eyJ... 开头）—— 一段即泄露签名密钥
    re.compile(r"(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"),
)


def redact_string(s: str) -> str:
    """遮蔽字符串值内联的凭据模式；base64-JSON 含敏感键时整段遮蔽。"""
    out = s
    for pat in INLINE_SECRET_PATTERNS:
        out = pat.sub("***", out)
    # base64 编码的 JSON（跨项目教训：access_token 以编码形式进日志，明文正则匹配不到）
    for b64 in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", out):
        try:
            decoded = base64.b64decode(b64, validate=True)
            if any(k in decoded for k in (b"access_token", b"api_key", b"apikey", b"authorization", b"password", b"secret")):
                out = out.replace(b64, "***")
        except Exception:
            continue
    return out


def redact_argv(argv) -> list:
    """The spawn argv with every element passed through :func:`redact_string`.

    Element-wise rather than over ``repr(argv)``, and that is the point: a command is **one**
    element (``bash -c <command>`` / ``pwsh ... -Command <command>``), so a credential inside
    it is inside a single string and the per-element pass is both sufficient and narrower —
    quoting and separators of a repr never get a say in what the patterns see.

    Non-string elements are passed through untouched: an argv may carry a type a caller
    built (a ``Path``), and a redactor that stringified it would change what the caller
    spawns... except that this return value is only ever logged, never spawned. The list is
    still typed honestly so a reader cannot mistake it for the argv to run.
    """
    return [redact_string(el) if isinstance(el, str) else el for el in argv]
