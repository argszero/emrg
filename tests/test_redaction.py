"""The credential-redaction rule, and every log site that must state it.

Rant ``2026-08-06T10:21:26``: a tool call's arguments can carry a credential, and ``emrgd.log``
is a file hosts paste into issues, so a live key must not reach it. The rule is one statement
of two masks — a labelled pair (``api_key=``, ``token:``) and an unlabelled credential shape
(``sk-``, ``ghp_``, a JWT, a ``Bearer`` header, or a base64 blob that *decodes* into one) —
and it lives in ``emrg/server/redaction.py``.

Why the rule needed a home of its own (measured 2026-10-12, cycle ``cyc20261012-220047``)
--------------------------------------------------------------------------------------------
``bash_tool_v2.run_command`` logs the argv it is about to spawn, and the argv of an unconfined
run is ``[<shell>, "-c", <the whole command>]`` — so **element 2 is the command**, printed
verbatim. The host's own ``~/.emrg/emrgd.log`` carried **909** such lines, each with the
command intact (read, not inferred: `grep -c 'bash v2: mode='`). A command is exactly where a
credential shows up — ``export OPENAI_API_KEY=sk-...``, a ``curl`` with a ``Bearer`` header —
which is the case the redactor was written for, and this was the one site that printed a whole
command without it. The rule could not be imported from the daemon (the daemon imports the
tools), so it moved to a leaf both layers already import from.

What is measured here
---------------------
* the **rule** — the arms that describe what every caller relies on, re-pointed at
  ``redact_string`` when the daemon's dict-recursive form was cut: it had no caller anywhere in
  the tree (its stated consumer, "a tool call's arguments reaching ``emrgd.log``", no longer
  exists — that log line states the tool's **name and intent** and never its arguments), while
  five tests pinned it. One of those five pinned the key-name rule, which nothing applies any
  more, and it is deleted with the function rather than re-pointed;
* the **sites**, each driven through its real caller with the credential-shaped string planted
  where that site reads from, and each asserting the *pair*: the credential absent **and** the
  line present. Without the second half an arm would pass on a log line that was never
  written.

Named limit
-----------
The pwsh arm plants its credential in the **executable path**, because that dialect's slice
(``argv[:3]``) stops before ``-Command`` — the command is not in the line at all today, so an
arm that planted it there would pass with and without the redaction and would be decoration.
Planting it in element 0 measures the property that actually holds the guarantee: every
element the line prints went through the rule, whatever the slice's width is.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.redaction import (
    INLINE_SECRET_PATTERNS,
    redact_argv,
    redact_prefix,
    redact_string,
)
from emrg.session import Session

#: A key-shaped string, assembled rather than written out: a literal `sk-` + 32 alphanumerics
#: is what GitHub's push protection recognises as a real DeepSeek key and refuses to accept
#: (the 20260806-2353 lesson recorded in `tests/test_daemon.py`).
SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


# ── the rule ──────────────────────────────────────────────────────────────


def test_redact_inline_secrets_in_strings():
    """字符串值内联凭据（sk-/ghp_/Bearer/JWT）被遮蔽。"""
    # bash command 内联 API key（拼接构造，避免 push protection 拦截）
    k = "sk-" + "a1b2c3d4" * 4
    assert k not in redact_string(f"export OPENAI_API_KEY={k}; curl x")
    # GitHub token 嵌在 URL
    assert "ghp_" not in redact_string("git push https://x-access-token:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890@github.com/r.git")
    # Bearer + JWT
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert "eyJ" not in redact_string(f"Authorization: Bearer {jwt}")


def test_redact_base64_encoded_token_blob():
    """base64 编码的 access_token JSON 被整段遮蔽（跨项目教训：明文正则匹配不到编码形式）。"""
    blob = base64.b64encode(json.dumps({"access_token": "super-secret"}).encode()).decode()
    out = redact_string(f"curl -d {blob} http://x")
    assert "***" in out
    assert "super-secret" not in out
    assert blob not in out


def test_redact_no_false_positive_on_normal_strings():
    """普通字符串/短 sk- 前缀/路径片段不被误伤。"""
    assert redact_string("echo hello world") == "echo hello world"
    # 短密钥（<16 位）不匹配 sk- 模式 → 保留
    assert "sk-abc" in redact_string("echo sk-abc")
    # 路径片段 "task-evolution-*" 含 "sk-evolution"（12 位）→ 不得误伤（#513 修复）
    assert "task-evolution-233-emrg-3193bc65.memory" in redact_string(
        "ls task-evolution-233-emrg-3193bc65.memory")
    assert "grep -r sk-evolution ~/scm" in redact_string("grep -r sk-evolution ~/scm")


def test_redact_real_key_formats_still_masked():
    """真实密钥格式（sk- 16+ 位 / sk-proj- / sk-ant-apiNN-）仍被遮蔽。

    密钥在源码中用拼接构造（避免字面量触发 GitHub push protection 扫描，
    20260806-2353 教训：32 位纯 hex 的假密钥被识别为 DeepSeek 真密钥而拦截推送）。
    """
    openai = "sk-" + "A1b2C3d4" * 6
    assert openai not in redact_string(f"export OPENAI_API_KEY={openai}; curl x")
    ds = "sk-" + "0a1b2c3d8" * 4
    assert ds not in redact_string(f"export DEEPSEEK={ds}; curl x")
    proj = "sk-proj-" + "AbCdEfGh" * 6
    assert "sk-proj-" not in redact_string(f"echo {proj}")
    ant = "sk-ant-api03-" + "Q2xjbHVkZ" * 4
    assert "sk-ant-" not in redact_string(f"echo {ant}")


def test_redact_string_applies_to_log_previews():
    """日志内容预览（rant 消息/任务 prompt/记忆工具结果）同样脱敏。"""
    assert "sk-" not in redact_string(
        "帮我看看 sk-A1b2C3d4A1b2C3d4A1b2C3d4A1b2C3d4 是不是我的 key")
    assert "ghp_" not in redact_string(
        "token 是 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 吗")
    # 普通内容保留
    assert "帮我看看这个文件" in redact_string("帮我看看这个文件")


def test_the_argv_form_masks_every_element():
    """`redact_argv` 逐元素脱敏，非字符串元素原样通过。"""
    assert redact_argv(["bash", "-c", f"echo {SECRET}"]) == ["bash", "-c", "echo ***"]
    assert redact_argv([Path("/tmp/x"), "echo ok"]) == [Path("/tmp/x"), "echo ok"]


def test_the_dict_form_and_the_key_name_rule_are_gone():
    """规则搬到叶子模块时删掉的两件：dict 递归形式与按键名规则。

    两者都是为「tool call 参数进 emrgd.log」写的，而工具调用日志只记录工具的**名字与
    intent**，全树再无字典经过脱敏。删掉的代码要有一条测试点名它不在，否则它随时可以被
    无声地加回来（本仓的规矩：一次删除由一条点名它的测试钉住）。
    """
    from emrg.server import redaction

    assert not hasattr(redaction, "redact")
    assert not hasattr(redaction, "SENSITIVE_KEY_SUBSTRINGS")
    assert not hasattr(daemon_mod, "_redact")
    # 相反的一侧：被搬过来的东西还在，所以上面三条不是因为模块空了。
    assert hasattr(redaction, "redact_string")
    assert INLINE_SECRET_PATTERNS


# ── the sites ─────────────────────────────────────────────────────────────


def _drive_bash_log(monkeypatch, command: str, tmp_path: Path) -> None:
    """The bash tool's log line for `command`, with the spawn intercepted.

    The spawn is stubbed because the subject **is** the log line: ``run_command`` writes it
    before it spawns anything, so the fake is never a stand-in for behaviour under test — it
    only keeps a real command from running. ``danger-full-access`` is the mode that reaches
    the spawn without a backend (the twin helper in ``tests/test_pwsh_tool_v2.py`` states the
    same reason), which is also the shape whose argv carries the command.
    """
    import emrg.tools.bash_tool_v2 as v2
    from emrg.sandbox.policy import SandboxPolicy

    async def fake_spawn(*argv, **kwargs):
        raise AssertionError("stop here: the log line is the subject")

    monkeypatch.setattr(v2.asyncio, "create_subprocess_exec", fake_spawn)
    policy = SandboxPolicy(mode="danger-full-access", workspace_root=str(tmp_path))
    with pytest.raises(AssertionError):
        asyncio.run(
            v2.run_command(
                command, policy=policy, workdir=str(tmp_path), timeout=5.0,
            )
        )


def test_the_bash_argv_log_masks_the_command(monkeypatch, tmp_path, caplog):
    """The line that prints the argv of an unconfined run must not print the credential in it."""
    caplog.set_level(logging.DEBUG, logger="emrg.tools.bash_tool_v2")
    _drive_bash_log(monkeypatch, f"export OPENAI_API_KEY={SECRET}; echo done", tmp_path)
    logged = caplog.text
    assert "bash v2: mode=" in logged, (
        "the argv line was not written at all, so 'the secret is absent' would prove nothing:\n"
        f"{logged[-800:]}"
    )
    assert SECRET not in logged
    # The rest of the command survives: the line still says which command ran.
    assert "echo done" in logged


def _drive_pwsh_log(monkeypatch, command: str, tmp_path: Path, pwsh_path: str) -> None:
    """The pwsh tool's log line, with the spawn intercepted (same reason as the bash twin)."""
    import emrg.tools.pwsh_tool_v2 as pwsh
    from emrg.sandbox.policy import SandboxPolicy

    async def fake_spawn(*argv, **kwargs):
        raise AssertionError("stop here: the log line is the subject")

    monkeypatch.setattr(pwsh.asyncio, "create_subprocess_exec", fake_spawn)
    policy = SandboxPolicy(mode="danger-full-access", workspace_root=str(tmp_path))
    with pytest.raises(AssertionError):
        asyncio.run(
            pwsh.run_command(
                command,
                policy=policy,
                workdir=str(tmp_path),
                timeout=5.0,
                platform_name="win32",
                pwsh_path=pwsh_path,
            )
        )


def test_the_pwsh_argv_line_masks_whatever_element_it_prints(monkeypatch, tmp_path, caplog):
    """Whatever the slice's width, every element it prints has been through the rule.

    The dialect's own slice stops before ``-Command`` today, so planting the credential in the
    command would make this arm green with **and** without the redaction — decoration. The
    credential goes in element 0 (the executable path), which the line does print, and that is
    the property the guarantee really rests on.
    """
    caplog.set_level(logging.DEBUG, logger="emrg.tools.pwsh_tool_v2")
    _drive_pwsh_log(
        monkeypatch, "Write-Output ok", tmp_path, pwsh_path=f"C:\\keys\\{SECRET}\\pwsh.exe",
    )
    logged = caplog.text
    assert "pwsh v2: mode=" in logged, (
        "the argv line was not written at all, so 'the secret is absent' would prove nothing:\n"
        f"{logged[-800:]}"
    )
    assert SECRET not in logged
    assert "pwsh.exe" in logged


def _make_server(tmp_path: Path) -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = tmp_path / "projects.yml"
    return server


def test_the_tool_call_log_masks_the_intent(tmp_path, monkeypatch, caplog):
    """The INFO line every turn writes — the tool's name and its intent — carries no credential.

    Driven through the real tool loop with a stubbed LLM (the shape
    ``tests/test_message_time_is_the_daemons.py`` uses): the intent is written by the model, so
    it is untrusted text, and a model that pastes the key it is asking about into the intent
    would otherwise put it in ``emrgd.log`` on the line that is always on.
    """
    # The loop stamps host state at round finalization; a suite run must not write ~/.emrg.
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire")
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round"
    )
    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    subject = tmp_path / "subject.txt"
    subject.write_text("body\n", encoding="utf-8")

    server = _make_server(tmp_path)
    session = Session.create_with_id("redaction-intent", tmp_path)
    rounds = {"n": 0}

    async def fake_stream(messages, tools=None):
        rounds["n"] += 1
        if rounds["n"] == 1:
            yield {
                "content": None, "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({
                            "path": str(subject),
                            "intent": f"check whether {SECRET} is still valid",
                        }),
                    },
                }],
            }
        else:
            yield {"content": "done", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_broadcast(session_id, payload):
        return None

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-redaction", session_id=session.session_id, prompt="read it")
    asyncio.run(server._run_tool_loop(req, None, session))

    logged = caplog.text
    assert "tool call: read" in logged, (
        "the tool-call line was never written, so 'the secret is absent' would prove nothing:\n"
        f"{logged[-800:]}"
    )
    assert SECRET not in logged
    # The intent itself is still readable — the line's job is to say why the call happened.
    assert "is still valid" in logged


# ── the window ────────────────────────────────────────────────────────────
#
# Thirteen sites printed a *window* of a value rather than the whole thing, and wrote the
# window **inside** the call: `redact_string(text[:60])` in the daemon (the `task received`
# prompt, the `rant recorded` message, the memory-reflection tool result) and
# `_redact_text(text[:N])` ten times in `emrg/server/llm.py`. Every pattern in
# `INLINE_SECRET_PATTERNS` has a minimum length — `sk-` needs 16 alphanumerics, `ghp_` 20, a
# base64 blob 40 — so a credential that **crosses the cut** stops matching and its surviving
# prefix is logged verbatim. Measured 2026-10-13 on master `af4dd670`: a key beginning at
# index 48 of a 60-character window leaves `sk-A1b2C3d4E`, twelve characters of a live key,
# in the line hosts paste into issues. The rule is not "print less", it is "the rule sees the
# whole value, then the line takes its window" — `redact_prefix`, plus an AST census that
# keeps the window from being written inside any string redactor's argument again.

#: The redactors that take **one string** and must therefore see all of it. A sequence
#: redactor (`redact_argv`) is deliberately absent: its callers slice a list of argv
#: elements, each element is redacted whole, and `test_the_argv_form_masks_every_element`
#: below is the arm that holds that contract up.
STRING_REDACTORS = ("redact_string", "_redact_text", "redact_prefix")

#: Per-module **floors** for the census below — minimums, never equalities (issue #2176).
#:
#: The census's own self-check exists to notice a module that stopped being read: "a module that
#: loses its callers is visible rather than silent". A module that **gains** a redactor call is
#: the opposite — a new site applying the rule, which is what the rule wants — and pinning that
#: as a red row blocks work the guard does not own. Measured 2026-10-13: #2169's landing tree
#: failed the equality with `assert 6 == 5` on `emrg/server/daemon.py`, on a PR whose only change
#: there was to redact the value **before** its window.
#:
#: The cost is not just the false red: the only way to land a legitimate addition without this
#: fix is to bump the number, which writes a *derived* count into a file a guard can measure and
#: makes this line a global write target for every future redacting site. The same census shape
#: in `tests/test_prompt_templates.py` already keeps a floor dict for this reason, and spells the
#: rule out: a removal is allowed, but it has to be deliberate and it has to be that line that is
#: edited.
#:
#: Numbers measured on master `7f59c2f7` (2026-10-13). Raising a floor when a module grows is
#: optional; lowering one is the deliberate act the floor exists to make visible.
REDACTOR_FLOORS = {
    "emrg/server/daemon.py": 5,
    "emrg/server/llm.py": 16,
    "emrg/server/redaction.py": 2,
}


def test_the_window_is_taken_after_the_rule_not_before():
    """The defect and its fix, in one arm — the negative half is what gives it power.

    A credential planted so that it crosses a 60-character cut. Truncating first loses it
    (the pattern no longer matches), redacting first cannot: the whole value goes through
    the rule, so the cut only ever lands on text a real key never reached.
    """
    text = "x" * 48 + SECRET + " and the rest of the request"

    # The defect shape, pinned: this is the leak the fix exists for. Without this half the
    # arm would pass on a redactor that had simply stopped redacting anything.
    leaked = redact_string(text[:60])
    assert SECRET[:6] in leaked, (
        "the truncate-first form no longer leaks, so this arm no longer measures the "
        f"defect it names: {leaked[-24:]!r}"
    )
    assert SECRET not in leaked

    masked = redact_prefix(text, 60)
    assert SECRET[:6] not in masked
    assert "sk-" not in masked
    # Same window, still a window: the cut is a cut, not the end of the line.
    assert len(redact_prefix(text, 60)) == 60
    assert masked.endswith("and the ")
    # A benign value is untouched and shortened by nothing but the window itself.
    assert redact_prefix("echo hello world", 60) == "echo hello world"
    assert redact_prefix("z" * 200, 60) == "z" * 60


#: The tree the census walks, and what it does not descend into. The walk skip reads the path
#: **relative to the root it walks** (the family's spelling, held by
#: `tests/test_walk_skips_read_their_root.py`); the skip set is the three that can appear inside
#: these roots, and is deliberately **smaller** than the guard family's, which pins only an exact
#: match (`tests/test_guard_scan_scope_pairing.py`'s membership reading) — the same reason the
#: dead-string guard's smaller set is left unpinned.
ROOT = Path(__file__).resolve().parent.parent
SWEPT = ("emrg",)
SKIPPED = {".venv", "__pycache__", "node_modules"}


def _takes_a_slice(call) -> bool:
    """Whether `call` is handed a `x[:n]` slice as one of its arguments."""
    import ast

    for arg in call.args:
        if not isinstance(arg, ast.Subscript):
            continue
        inner = arg.slice
        if isinstance(inner, ast.Slice):                                          # 3.9+
            return True
        if isinstance(inner, ast.Index) and isinstance(inner.value, ast.Slice):   # <= 3.8
            return True
    return False


def _string_redactor_calls():
    """`(path, line, source, node)` for every production call of a string redactor.

    Hand-written rather than driven by a `git ls-files` list, so the tree leg below can assert
    how many files it judged: a walk that silently stopped descending would otherwise come back
    as a clean census of nothing.
    """
    import ast

    found = []
    for name in SWEPT:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIPPED & set(path.relative_to(ROOT).parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                callee = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if callee in STRING_REDACTORS:
                    found.append((path, node.lineno, ast.unparse(node), node))
    return found


def test_no_string_redactor_is_handed_a_slice():
    """The window belongs *after* the call — a slice inside it is the defect, everywhere.

    An AST census rather than a reading of the thirteen sites, because the sites are thirteen
    today and the next log line is written by whoever adds it. **Named limits**: (1) the
    population is first-party code under `emrg/` — this file must be able to write the forbidden
    form, which is the negative half of the arm above; (2) it catches a *slice*, not a value
    truncated somewhere upstream and arriving as a plain name — the shape the client's
    `r.content[:30]` rows have, which no call-site census can see.
    """
    calls = _string_redactor_calls()

    # Fail closed on a shrunken population: a rename or a moved module must not read as a clean
    # census. 24 is what master `af4dd670` holds (measured 2026-10-13); a change that lowers it
    # has to say so here rather than pass.
    assert len(calls) >= 24, (
        f"only {len(calls)} string-redactor call(s) found under {ROOT} — the census has lost "
        "its subject, so 'no slice' would prove nothing"
    )

    offenders = [f"{p.relative_to(ROOT)}:{n} {src}" for p, n, src, node in calls if _takes_a_slice(node)]
    assert not offenders, (
        "a string redactor is handed a slice — the rule cannot see a credential crossing the "
        "cut, so its prefix reaches the log in the clear; redact the whole value and take the "
        "window outside the call (`redact_prefix(x, n)`):\n  " + "\n  ".join(offenders)
    )

    # The other direction: the census really is reading the sites it is about, counted by module,
    # so a module that loses its callers is visible rather than silent.
    per_file: dict[str, int] = {}
    for path, _n, _src, _node in calls:
        key = path.relative_to(ROOT).as_posix()
        per_file[key] = per_file.get(key, 0) + 1
    thin = [
        f"{name}: {per_file.get(name, 0)} < {needed}"
        for name, needed in REDACTOR_FLOORS.items()
        if per_file.get(name, 0) < needed
    ]
    assert not thin, (
        "these modules hold fewer string-redactor calls than they did when this floor was "
        "taken, so the census is no longer reading the population it is about: "
        + ", ".join(thin)
        + f"\n  the whole census: {per_file}"
    )


class _FakeWriter:
    """Minimal websocket stand-in: `_process_message` answers through `_send` → `ws.send`."""

    async def send(self, data) -> None:
        pass

    async def close(self) -> None:
        pass

    async def recv(self) -> str:
        raise ConnectionError("no frames to receive in test")


def test_the_rant_line_masks_a_credential_crossing_its_window(tmp_path, caplog):
    """A real windowed site, driven through its real caller: `rant recorded`.

    The rant is host text recorded into a file hosts paste into issues, and the line prints
    its first 100 characters. The credential sits at index 48, inside the window but
    crossing nothing — what matters is that the window is taken *after* the rule, so the same
    arm holds for any offset (the arm above sweeps that at the rule, where it is cheap).
    """
    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    server = _make_server(tmp_path)
    server._rants_log = tmp_path / "rants.jsonl"

    asyncio.run(server._process_message(
        {"type": "rant", "message": "x" * 48 + SECRET + " tail of the rant", "project": "emrg"},
        _FakeWriter(),
    ))

    logged = caplog.text
    assert "rant recorded" in logged, (
        "the rant line was not written at all, so 'the secret is absent' would prove "
        f"nothing:\n{logged[-800:]}"
    )
    assert SECRET[:6] not in logged
    assert "tail of the rant" in logged  # the window still shows the rant's own text
