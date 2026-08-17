"""Bash tool — execute shell commands and return stdout/stderr."""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import re
import signal
import tempfile

from emrg._win import win32_no_window_kwargs
from emrg.server.git_utils import no_prompt_env
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)


MAX_OUTPUT_CHARS = 200_000  # Truncate large outputs (framing supports up to 16MB)

# Heredoc start: `cmd <<'EOF'` / `cmd <<EOF` / `cmd <<-EOF` (quote optional,
# matched symmetrically via backreference). MULTILINE so ^/$ bound the first
# command line, not the whole command string. cmd.exe cannot parse this.
_HEREDOC_START_RE = re.compile(
    r"^(?P<head>.*?)(?P<op><<-?)(?P<quote>['\"]?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)\s*$",
    re.MULTILINE,
)


def _translate_windows_heredocs(cmd: str) -> tuple[str, str | None]:
    """Translate the first bash heredoc into a stdin redirect for cmd.exe.

    The bash tool's subprocess shell on Windows is cmd.exe (via COMSPEC),
    which cannot parse ``cmd <<'EOF' ... EOF`` heredocs — commands documented
    with heredoc syntax (e.g. ``browser-harness <<'PY' ... PY``) fail with
    ``<< is not recognized`` / ``此时不应有 <<``, forcing agents into temp-file
    workarounds. Rewriting the heredoc to ``cmd < tempfile`` feeds the same
    bytes via stdin redirect, so Windows agents run the identical commands as
    POSIX (host sessions 2026-08-14T21:26/21:36 observed the failure twice).

    Only the FIRST heredoc is translated (multiple heredocs in one command are
    rare); an unterminated heredoc is left untouched so cmd.exe reports the
    original error. ``<<-`` (tab-stripping) strips leading tabs from the body,
    mirroring bash. Content is written literally (quoted ``<<'EOF'``
    semantics; unquoted heredocs containing ``$`` expansion keep literal
    content — a documented approximation). Everything before the opener line
    (e.g. ``cd /tmp\n`` in a multi-line command) is preserved verbatim in the
    rewritten command, so the heredoc feeds the same stdin into the same
    command line as POSIX (review #797 ❌).

    Returns ``(rewritten_cmd, temp_path)`` — the caller must unlink temp_path
    after the subprocess finishes (normal or timeout path).
    """
    m = _HEREDOC_START_RE.search(cmd)
    if not m:
        return cmd, None
    head, op, name = m.group("head"), m.group("op"), m.group("name")
    strip_tabs = op.endswith("-")
    # Terminator: a line containing exactly the delimiter (optionally indented
    # with tabs when the opener used `<<-`, mirroring bash tab-stripping).
    term = re.compile(rf"(?m)^[ \t]*{re.escape(name)}\s*$" if strip_tabs
                      else rf"(?m)^{re.escape(name)}\s*$")
    tm = term.search(cmd, m.end())
    if not tm:
        return cmd, None  # unterminated — let cmd.exe report it
    body = cmd[m.end():tm.start()]
    if body.startswith("\r\n"):
        body = body[2:]  # opener-line newline is not part of the body
    elif body.startswith("\n"):
        body = body[1:]
    if strip_tabs:
        body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
    # bash keeps the newline that precedes the terminator line as part of the
    # body (a heredoc always ends with exactly one newline) — keep it verbatim.
    tail = cmd[tm.end():].strip()
    fd, path = tempfile.mkstemp(suffix=".heredoc", text=True)
    try:
        # newline="" keeps LF verbatim (no CRLF conversion of the body)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(body)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    # Keep everything before the opener line (multi-line commands such as
    # `cd /tmp\npython <<'PY'` must not lose their prefix — review #797 ❌).
    rewritten = f"{cmd[:m.start()]}{head}< \"{path}\""
    if tail:
        rewritten += f" {tail}"
    return rewritten.rstrip(), path


def _decode_output(data: bytes, os_name: str | None = None) -> str:
    """Decode subprocess output bytes without corrupting non-UTF-8 text.

    POSIX: subprocess output is UTF-8 — unchanged behavior.

    Windows: cmd.exe/dir/echo output uses the console locale code page
    (GBK/cp936 on zh-CN), while git/gh emit UTF-8. Both must decode
    correctly, so we try the locale encoding **strictly** first and fall
    back to UTF-8 (also strict), then to UTF-8 with replacement as a last
    resort. A non-strict first attempt would silently mojibake UTF-8
    output and never reach the fallback (rant 2026-08-08T09:35:30 —
    U+FFFD garbage from decoding GBK bytes as UTF-8).

    ``os_name`` is injectable for tests (defaults to ``os.name``).
    """
    if not data:
        return ""
    name = os_name or os.name
    if name == "nt":
        enc = locale.getpreferredencoding(False) or "utf-8"
        for candidate in (enc, "utf-8"):
            try:
                return data.decode(candidate)  # strict
            except (LookupError, UnicodeDecodeError):
                continue
    return data.decode("utf-8", errors="replace")


class BashTool(ToolExecutor):
    """Execute shell commands via asyncio subprocess."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            purpose="Execute a shell command and return its output (run tests, inspect files, git operations, etc.)",
            description=(
                "Execute a shell command and return stdout and stderr. "
                "Use for running tests, git commands, listing files, "
                "installing packages, and other shell operations. "
                "Commands run in the working directory by default; "
                "use the `workdir` parameter to override."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute. Bash heredocs "
                        "(cmd <<'EOF' ... EOF) are supported on all platforms "
                        "— on Windows they are auto-translated to stdin redirects.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30).",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the command (default: project root).",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        cmd = arguments.get("command", "")
        timeout = arguments.get("timeout", 30)
        workdir = arguments.get("workdir", None)

        if not cmd:
            return ToolResult(name="bash", content="Error: no command provided", error=True)

        logger.debug("bash: running %r (timeout=%ds)", cmd[:100], timeout)

        # Windows: cmd.exe cannot parse bash heredocs — translate the first
        # one to a stdin redirect (host sessions 2026-08-14T21:26/21:36 hit
        # "此时不应有 <<" with browser-harness <<'PY'). POSIX unchanged.
        temp_path = None
        if os.name == "nt":
            cmd, temp_path = _translate_windows_heredocs(cmd)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                # Non-interactive daemon: git/gh children must fail fast
                # silently, never spawn GCM/askpass popups (rant
                # 2026-08-07T10:17:27).
                env=no_prompt_env(),
                preexec_fn=os.setsid if os.name != "nt" else None,
                # Windows: background daemon children must never pop a
                # console window (rant 2026-08-09T13:16:36 — cmd-window
                # storm; bash tool was a top contributor).
                **win32_no_window_kwargs(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                # Kill entire process group to prevent orphaned children
                try:
                    if os.name != "nt":
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    proc.kill()
                await proc.wait()
                return ToolResult(
                    name="bash",
                    content=f"Command timed out after {timeout}s: {cmd[:100]}",
                    error=True,
                )
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

            out = _decode_output(stdout).rstrip()
            err = _decode_output(stderr).rstrip()

            # Smart truncation: keep stderr intact (errors are critical),
            # truncate stdout with head+tail when output exceeds limit.
            # This ensures build/test errors at the tail aren't lost.
            ERR_MAX = 30_000  # Always keep stderr up to this
            HEAD_TAIL_RATIO = 0.6  # 60% head, 40% tail
            _SEP = "\n[stderr]\n"  # 10 chars, separator prefix

            if len(err) > ERR_MAX:
                half = ERR_MAX // 2
                err = (
                    f"{err[:half]}\n\n"
                    f"... [stderr truncated: {len(err)} → {ERR_MAX} chars, head+tail kept]"
                    f"\n\n{err[-half:]}"
                )

            # Calculate remaining budget for stdout (accounting for separator)
            err_overhead = len(_SEP) if (out and err) else 0
            remaining = MAX_OUTPUT_CHARS - len(err) - err_overhead
            if remaining < 2000 and err:
                # stderr consumed most budget — truncate stderr further
                remaining = MAX_OUTPUT_CHARS // 2
                err = err[:remaining] + (
                    "\n\n... [stderr truncated to make room for stdout]"
                )

            if out and len(out) > remaining:
                head_chars = int(remaining * HEAD_TAIL_RATIO)
                tail_chars = remaining - head_chars - 200  # message overhead
                if tail_chars < 500:
                    out = out[:remaining - 50] + (
                        f"\n\n... [stdout truncated: {len(out)} → {remaining} chars]"
                    )
                else:
                    out = (
                        f"{out[:head_chars]}\n\n"
                        f"... [{len(out) - remaining} chars omitted] ..."
                        f"\n\n{out[-tail_chars:]}"
                    )

            parts: list[str] = []
            if out:
                parts.append(out)
            if err:
                parts.append(f"[stderr]\n{err}")
            if not parts:
                parts.append("(no output)")
            result = "\n".join(parts)
            return ToolResult(name="bash", content=result)
        except FileNotFoundError:
            return ToolResult(
                name="bash",
                content=f"Command not found: {cmd[:100]}",
                error=True,
            )
        except OSError as e:
            logger.warning("bash error: %s", e)
            return ToolResult(name="bash", content=f"Error: {e}", error=True)
