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


# ── Sandbox — file-level isolation for the bash tool (rant 2026-08-20T15:46:50) ──
#
# Three tiers (default danger-full-access = current, un-sandboxed behavior):
#   danger-full-access  — no checks at all (existing behavior)
#   read-only           — no writes allowed: destructive commands (rm -r /
#                         rmdir / mv / cp -r), git mutating commands (stash /
#                         checkout / restore / clean / reset / commit / push /
#                         pull / merge / rebase — community issue #979) and
#                         shell redirects (> / >>) to any non-/dev/null target
#                         are blocked
#   workspace-write     — writes inside the workspace root (and the OS temp
#                         area) are allowed; destructive writes to protected
#                         daemon state files and to absolute paths outside
#                         the workspace are blocked
#
# Enforcement is deliberately heuristic (host design-finalized): a static
# command scan, NOT an OS-level sandbox (no bwrap/Seatbelt/ACL). The checked
# modes report enforcement="partial" — honest reporting, never pretending
# full OS-level isolation. The core value is blocking a hallucinated LLM's
# obviously destructive commands (rm -rf with a wrong path, writing the
# daemon's own state files).

SANDBOX_MODES = ("danger-full-access", "read-only", "workspace-write")

# Daemon state files — writing to these from a sandboxed task is always
# blocked (they are the daemon's own data, not agent scratch space).
_PROTECTED_FILES = (
    "~/.emrg/config.toml",
    "~/.emrg/emrgd.token",
    "~/.emrg/tasks.yml",
    "~/.emrg/projects.yml",
    "~/.emrg/rants.jsonl",
)

# Git mutating commands — blocked under read-only (community issue #979,
# heinrichneb dev.to comment on the 2026-08-20 data-loss postmortem): the
# incident's actual killers (`git stash`, `git checkout .`, `git reset --hard`,
# `git clean`) were NOT caught by the rm/rmdir/mv/cp checks. Under read-only
# these must be structurally impossible, not merely discouraged by a prompt
# rule — "rules can regress; topology can't". Read-only git reads (status /
# fetch / log / diff / remote) stay allowed.
_GIT_MUTATOR_RE = re.compile(
    r"\bgit\s+(?:stash|checkout|restore|clean|reset|commit|push|pull|merge|"
    r"rebase|cherry-pick|cherry_pick|revert|rm|mv|switch)\b"
)
_GIT_DELETE_RE = re.compile(r"\bgit\s+(?:branch|tag)\s+-[dD]\b")


def _extract_write_targets(cmd: str) -> list[str]:
    """Heuristic extraction of write targets from a command line.

    Returns path tokens the command appears to write to:
      - ``rm -r/-rf/-R <path>`` and ``rmdir <path>`` → the removed path
      - ``mv <src> <dst>`` / ``cp -r <src> <dst>`` → the destination
      - ``> / >> / 2> / &>`` redirects → the redirect target

    Deliberately non-exhaustive (the sandbox only catches obvious
    destructive writes — the boundary is honest: enforcement=partial).
    """
    targets: list[str] = []
    # rm -r / rm -rf / rm -R ... <path>  (recursive delete)
    for m in re.finditer(r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+([^\s|;&]+)", cmd):
        targets.append(m.group(1))
    # rmdir <path>
    for m in re.finditer(r"\brmdir\s+([^\s|;&]+)", cmd):
        targets.append(m.group(1))
    # mv <src> <dst> — the destination is the last bare token
    for m in re.finditer(r"\bmv\s+((?:-[a-zA-Z]*\s+)*[^\s|;&]+\s+[^\s|;&]+)", cmd):
        toks = m.group(1).split()
        if len(toks) >= 2:
            targets.append(toks[-1])
    # cp -r <src> <dst> — the destination is the last bare token
    for m in re.finditer(r"\bcp\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+([^\s|;&]+\s+[^\s|;&]+)", cmd):
        toks = m.group(1).split()
        if len(toks) >= 2:
            targets.append(toks[-1])
    # shell redirects: > file / >> file / 2> file / &> file
    for m in re.finditer(r"(?:\d*>>?|&>>?)\s*([^\s|;&]+)", cmd):
        targets.append(m.group(1))
    return targets


def _protected_paths() -> list[str]:
    """Canonicalized (realpath) protected daemon state files."""
    out: list[str] = []
    for p in _PROTECTED_FILES:
        try:
            out.append(os.path.realpath(os.path.expanduser(p)))
        except OSError:
            pass
    return out


def _is_absolute_path(p: str) -> bool:
    """True when ``p`` is absolute (or drive-less rooted, e.g. ``/etc/hosts``
    on Windows — ntpath.isabs returns False for those, but they still do not
    resolve under the cwd, so the sandbox must treat them as absolute)."""
    return os.path.isabs(p) or p.startswith("/") or p.startswith("\\")


def _is_within(path: str, root: str) -> bool:
    """True when ``path`` (absolute) is inside ``root`` (absolute) or equals it."""
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return rp == rr or rp.startswith(rr + os.sep)
    except OSError:
        return False


def _check_sandbox(cmd: str, mode: str, workdir: str | None = None) -> tuple[bool, str | None, str]:
    """Static sandbox check for a bash command (rant 2026-08-20T15:46:50).

    Returns ``(allowed, blocked_reason, enforcement)``:
      - danger-full-access → (True, None, "full") — no checks, current behavior.
      - read-only → blocks every destructive write (rm -r / rmdir / mv /
        cp -r and shell redirects to any non-/dev/null target).
      - workspace-write → blocks destructive writes to protected daemon
        files, to ``~/.emrg`` itself, and to absolute paths outside the
        workspace root (the OS temp dir is allowed — mirrors dsh's
        workspace + backend-promised temp area).

    Heuristic by design: static scan only, no OS-level boundary — checked
    modes honestly report enforcement="partial".
    """
    if mode not in SANDBOX_MODES:
        return False, f"invalid sandbox mode {mode!r}", "partial"
    if mode == "danger-full-access":
        return True, None, "full"

    targets = _extract_write_targets(cmd)
    if mode == "read-only":
        for t in targets:
            if t != "/dev/null":
                return False, (
                    f"read-only sandbox: blocked destructive write targeting {t!r}"
                ), "partial"
        # Git mutators are blocked too — the 2026-08-20 data-loss commands
        # (stash / checkout . / reset --hard / clean) write no file targets
        # and escaped the target scan (community issue #979).
        m = _GIT_MUTATOR_RE.search(cmd) or _GIT_DELETE_RE.search(cmd)
        if m:
            return False, (
                f"read-only sandbox: blocked git mutating command {m.group(0)!r} "
                "(dirty-tree guard, community issue #979)"
            ), "partial"
        return True, None, "partial"

    # workspace-write
    if not targets:
        return True, None, "partial"
    protected = _protected_paths()
    emrg_home = os.path.realpath(os.path.expanduser("~/.emrg"))
    workdir_real = os.path.realpath(workdir) if workdir else None
    for t in targets:
        if t == "/dev/null":
            continue
        expanded = os.path.expanduser(t)
        if not _is_absolute_path(expanded):
            # Relative target: assumed in-workspace (cwd = the workspace root).
            continue
        real = os.path.realpath(expanded)
        if real in protected:
            return False, (
                f"workspace-write sandbox: blocked write to protected daemon file {t!r}"
            ), "partial"
        if real == emrg_home:
            return False, (
                f"workspace-write sandbox: blocked destructive write to {t!r} "
                "(would erase the daemon's data directory)"
            ), "partial"
        if workdir_real and not _is_within(real, workdir_real) and not _is_within(real, tempfile.gettempdir()):
            return False, (
                f"workspace-write sandbox: blocked write outside workspace {t!r}"
            ), "partial"
    return True, None, "partial"


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
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'check how billing is implemented in billing.rs'.",
                    },
                },
                "required": ["command", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        cmd = arguments.get("command", "")
        timeout = arguments.get("timeout", 30)
        workdir = arguments.get("workdir", None)
        # Sandbox tier — daemon-injected per task config (the agent cannot
        # choose its own sandbox; rant 2026-08-20T15:46:50).
        sandbox = arguments.get("sandbox")

        if not cmd:
            return ToolResult(name="bash", content="Error: no command provided", error=True)

        # Static file-level isolation check (rant 2026-08-20T15:46:50).
        sandbox_tag: str | None = None
        if sandbox and sandbox != "danger-full-access":
            allowed, reason, enforcement = _check_sandbox(cmd, sandbox, workdir)
            if not allowed:
                logger.info("bash: BLOCKED by %s sandbox: %s", sandbox, reason)
                return ToolResult(
                    name="bash",
                    content=(
                        f"⛔ [sandbox:{sandbox} enforcement={enforcement}] "
                        f"{reason} — command not executed"
                    ),
                    error=True,
                )
            sandbox_tag = f"[sandbox:{sandbox} enforcement={enforcement}]"
            logger.debug("bash: sandbox %s check passed", sandbox)

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
            if sandbox_tag:
                result = f"{sandbox_tag} ok\n{result}"
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
