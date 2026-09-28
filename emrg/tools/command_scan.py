"""What a shell command *is*, read from its text — the guards the live executors call.

Two rules live here, and both answer a question about the **command text** rather
than about the file system, which is why they cannot be expressed by the kernel
fence the process-boundary tool runs under:

* :func:`check_containment_escape` — a destination-based scan for cloud
  metadata-credential fetches and egress tunnels (issue #1102). The write-target
  rules cannot see these: a metadata fetch is a **read**, it names no protected
  path, and the exfiltration happens over the network.
* :func:`stops_or_restarts_the_daemon` — the host's red line (`MANIFESTO.md`
  第四条附则二, host 2026-08-18T22:58): never stop or restart the emrg server. The
  act names no file and no git verb, so `emrg server stop` and `pkill -f
  emrg.server` reached every scan as ordinary commands.

**Why they were not where they were.** Both were written inside
``emrg/tools/bash_tool.py`` — the legacy tool P7 (issue #1675) deletes — and both
were consulted only by that tool's ``_check_sandbox``. Since P6 the executor a
session actually gets is the v2 one (``emrg/tools/bash_tool_v2.py``, or
``pwsh_tool_v2.py`` on Windows), so neither rule was reachable from a real
command: measured on ``aac28fb9``, ``stops_or_restarts_the_daemon`` had exactly
one call site, in the legacy executor, while the v2 fence is a **write** fence
(``(allow default)(deny file-write*)`` — signals and IPC are allowed) and
``emrg server stop`` reaches the daemon's ``shutdown`` frame with no check at all.

The fix is this module: the reading lives outside the doomed file, and every live
executor calls it at the boundary (``BashToolV2.execute`` / ``PwshToolV2.execute``),
so the rules are enforced on the path a command really takes rather than on the
one the switch retired.

**What is shared with the legacy file.** The tokenizer and its helpers
(``tokenize_command``, ``nested_command_texts``, ``basename``, the Windows
backslash protection) — the legacy scanner keeps private aliases to these for its
own ~30 call sites, and those aliases are the *same function objects*, so the two
readers cannot drift into two spellings of one command.
"""

from __future__ import annotations

import os
import re
import shlex

from emrg.tools import file_policy

def _windows_shell() -> bool:
    """``os.name == "nt"`` — the axis the backslash rules are gated on.

    Read from its **home** (`file_policy`, which is where the file tools read it
    too) on every call, rather than bound into a module constant here.  A bound
    copy is a second value with its own lifetime, and it cannot see the seam a
    test or an override moves — the failure the legacy scanner's alias has
    already produced once, when a forced axis greened locally and reddened the
    platform leg.  One reading, asked each time.

    :returns: whether the dialect being scanned is a Windows one.
    """
    return file_policy.WINDOWS_SHELL


_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})

_COMMAND_POSITION_OPERATORS = frozenset({"(", "{", "!", "`", ">(", "<(", ")"})
# Shell keywords after which the next word is a command, not an argument.
_SHELL_KEYWORD_POSITION = frozenset({"if", "then", "elif", "else", "while", "until", "do"})

_COMMAND_WRAPPERS = frozenset({
    # `eval` joins its arguments and executes the result; `-exec`/`-execdir` hand
    # the next word to execve. Both put the command where the walk looks for a
    # wrapper's argument. They are spells of the same prefix, and the flag shape
    # is deliberate: the value-skip below is what keeps `find . -exec grep git
    # {} \;` allowed, because `grep` is consumed as the flag's value.
    "eval", "-exec", "-execdir",
    # `builtin cd <dir>` is `cd <dir>` reached by its other spelling, and it was
    # the one prefix that left the candidate out of command position: measured on
    # master, `builtin cd <outside> && echo x > f.txt` created `<outside>/f.txt`
    # while the guard read the relative target as in-workspace and allowed it,
    # and the same held for `builtin cd -P <outside>`, `(builtin cd <outside> …)`
    # and `sh -c 'builtin cd <outside>; …'` (issue #1362). `builtin` prefixes
    # exactly one word, like `command`, so it belongs to the same list — and the
    # gap was here rather than in the walk: every consumer of
    # `_runs_as_a_command` read `builtin <cmd>` as an argument, not just this one.
    # The cost is the same over-approximation the others carry: `echo builtin cd
    # <dir>` reads `cd` as an invocation too (measured — a false block, in the
    # loud direction, of the class `echo command cd <dir>` already had).
    "builtin",
    # Four prefixes that exec the word after them, so the walk has to read that
    # word as a command. Measured on master `9a7bfe65` through `_check_sandbox`
    # at `read-only`, one `workdir`, nothing executed: ten rows answered ALLOW
    # with an empty target list — `unshare -r git checkout .`, `nsenter -t 1 git
    # checkout .`, `chroot / git checkout .`, `busybox git stash drop`, and each
    # prefix with `rm -rf /tmp/x` (`touch`, `patch` too) — because the word after
    # the prefix was read as an argument, so neither reader saw a mutator at all.
    # It is the `builtin` entry above one prefix further out (#1362).
    # These names also fence issue #1513: the named-wrapper branch of
    # `_nested_command_texts` takes `tokens[i + 1:]` with no position test, which
    # is the only reason `unshare -r sh -c …` is read today. A position test
    # landing there would stop reading every payload behind a prefix that is not
    # in this set — these four among them (`tests/test_exec_prefix_wrappers.py`
    # asserts that half, in both directions).
    # `busybox <applet>` names the program the same way (`busybox sh -c …`,
    # `busybox rm -rf …`). The cost is the over-approximation every entry here
    # carries — `busybox git checkout .` is refused although busybox has no git
    # applet — which is the loud direction this guard always errs in.
    "unshare", "nsenter", "chroot", "busybox",
    "env", "sudo", "doas", "xargs", "nohup", "time", "timeout", "nice",
    "setsid", "stdbuf", "command", "exec", "ionice", "chrt", "watch",
})

_RUNNER_WORDS = frozenset({"uv", "poetry", "pdm", "hatch", "pipenv", "rye"})
#: The sub-command word between a runner and the command it runs.
_RUNNER_SUBCOMMAND_WORDS = frozenset({"run"})

# `FOO=1 git checkout .` — the shell strips leading assignments and runs the
# rest, so an assignment is a prefix, not a command.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

_SHELL_EVALUATORS = frozenset({"eval"})

#: A shell parameter expansion — `$X`, `${X}`, `$1`, `$@` … — as one pattern, because
#: the rules below ask "does a value spelling name a variable?" rather than "which
#: variable?". `_UNRESOLVED_ROOT_RE` stays with the legacy scanner and imports this.
_PARAM_EXPANSION = r"\$(?:\{[^}]*\}|[A-Za-z_0-9@*#?$!-][A-Za-z0-9_]*)"
_UNRESOLVED_VAR_RE = re.compile(rf"(?:{_PARAM_EXPANSION})+")

# Cloud metadata endpoints — never legitimate in development commands.
# IMDSv1/v2 (AWS), ECS container creds (AWS), GCP metadata (IP + DNS),
# IMDSv2 IPv6 (AWS).
_METADATA_ENDPOINT_RE = re.compile(
    r"169\.254\.169\.254|169\.254\.170\.2|169\.254\.169\.123|"
    r"metadata\.google\.internal|fd00:ec2::254"
)

_SSH_TUNNEL_RE = re.compile(
    r"(?<!\S)ssh(?:\.exe)?\s+(?:[^|;&\n'\"]*?\s)?-[a-zA-Z]*[RD][a-zA-Z]*"
)

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

def check_containment_escape(cmd: str) -> str | None:
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

# ── The daemon's own life (host red line, issue #1324) ──────────────────────
#
# Never stopping or restarting the emrg server is the project's highest-priority
# rule (`MANIFESTO.md` 第四条附则二, host 2026-08-18T22:58), and until this rule it
# was enforced in one place (the test harness) and stated in two (the manifesto
# and a host-configured session prompt) — but classified **nowhere a shell
# command passes through**. Measured by the predicate alone on `1f2feefa`, the
# workspace-write tier: `emrg server stop`, `emrg server restart`,
# `pkill -f emrg.server`, `killall emrgd` and `kill $(pgrep -f "python -m emrg")`
# were all ordinary allowed commands. That is the shape the host actually hit —
# on 2026-08-21T09:41 a session in another project ran a "measurement" whose
# subprocess signalled the daemon, the TUI answered `server connection lost`, and
# the host asked *"你怎么验证的，怎么把 emrg server重启了？"*.
#
# The act, not the mention, is what is classified: the emrg program carrying a
# lifecycle verb, or a process signaller whose operand names emrg. A name that
# merely *appears* stays allowed — `git log --grep emrg`, `ls emrg`, `echo
# 'emrg server stop'` are all read commands, and a rule that refused them would
# teach the next reader to distrust it (the #1513 lesson: `echo sh "patch …"` was
# a bug, not a safe over-block).
_EMRG_PROGRAM_WORDS = frozenset({"emrg", "emrgd"})

_DAEMON_LIFECYCLE_VERBS = frozenset({"stop", "restart"})
#: Programs whose whole purpose is to signal another process by name.
_PROCESS_SIGNALLER_WORDS = frozenset({"pkill", "killall", "kill"})
#: The sub-command word between the program and the verb (`emrg server stop`).
_DAEMON_SUBCOMMAND_WORDS = frozenset({"server"})

_DAEMON_GROUPING_TOKENS = frozenset({"(", ")", "{", "}", "$", "`"})

def _is_a_command_border(tok: str) -> bool:
    """Does this token separate one command from the next?

    The tokenizer fuses adjacent punctuation, so `;;`, `&&` and `|&` arrive as
    single tokens: the test is "every character is a separator character", which
    covers the fused spellings without enumerating them. Grouping operators are
    deliberately **not** borders — `kill $(pgrep -f emrg)` names its target
    inside a substitution, and stopping at `$` or `(` would lose it.
    """
    return bool(tok) and all(c in ";&|\n" for c in tok)

def _daemon_lifecycle_verb_after(tokens: list[str], start: int) -> tuple[str, int] | None:
    """The lifecycle verb this statement carries after the emrg program, and where.

    Only the words a real invocation may put between the program and its verb
    are stepped over — flags (`emrg --verbose server stop`) and the `server`
    sub-command — and the walk stops at the first other word. Stopping there is
    what keeps `git -C <a path ending in emrg> log --grep restart` allowed: the
    `emrg` there is a directory *name*, and `log` is not a verb of the daemon.
    """
    j = start
    while j < len(tokens):
        tok = tokens[j]
        if _is_a_command_border(tok):
            return None
        word = _basename(tok).lower()
        if word in _DAEMON_LIFECYCLE_VERBS:
            return word, j
        if (
            tok.startswith("-")
            or word in _DAEMON_SUBCOMMAND_WORDS
            or tok in _DAEMON_GROUPING_TOKENS
        ):
            j += 1
            continue
        return None
    return None

def _operand_naming_emrg(tokens: list[str], start: int) -> str | None:
    """The operand a signaller's target is spelled in, if it names emrg.

    Read from the text of the operands rather than from a process table: the
    guard is a static scan, so what it can see is the name the command spells —
    `pkill -f emrg.server`, `killall emrgd`, `kill $(pgrep -f "python -m emrg")`
    (the substitution's tokens include the pattern as one word). A signaller
    whose target is spelled some other way is a stated limit, not a hole this
    rule claims to cover; see `stops_or_restarts_the_daemon`'s docstring.
    """
    for tok in tokens[start:]:
        if _is_a_command_border(tok):
            return None
        if "emrg" in tok.lower():
            return tok
    return None

#: `env`'s string handover — the flag whose value `env` splits into an argv and
#: execs. BSD/macOS `env` takes the short form only (measured on this host:
#: `env --split-string <string>` answers `illegal option -- s`, while
#: `env -S 'printf RAN'` prints `RAN`), GNU coreutils takes both spellings, so
#: both are read: this is a static reading and the tree is developed on both.
_ENV_SPLIT_STRING_FLAGS = frozenset({"-S", "--split-string"})

def _env_split_string_texts(tokens: list[str]) -> list[str]:
    """The command texts an `env` invocation hands over as one argument.

    `env -S 'emrg server stop'` splits its value into an argv and execs it, so
    the act is *in the string*, whatever the spelling — measured on this host
    with harmless payloads: `env -S 'printf RAN'` prints `RAN`, and
    `env -S 'printf %s' MARK` prints `MARK`, i.e. the tokens written after the
    string join the argv `env` execs.

    The text is **argv-shaped, not shell text**: there is no shell to read a
    redirect, so `env -S 'printf %s RAN > <marker>'` prints `RAN>` and writes no
    file (measured). That is why this reader belongs to the act classifier and
    is deliberately **not** a source inside `_nested_command_texts`: reading the
    string as shell text there would refuse writes that cannot happen — the
    #1513 over-block, one level down.

    Joining the string with the tokens that follow it and re-tokenising can only
    widen the reading (`env` itself splits on whitespace and honours quotes), and
    widening is the side this guard takes.
    """
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if _basename(tok) != "env":
            continue
        j = i + 1
        while j < len(tokens) and not _is_a_command_border(tokens[j]):
            tok_j = tokens[j]
            if tok_j in _ENV_SPLIT_STRING_FLAGS:
                value = tokens[j + 1 : j + 2]
                if value:
                    rest: list[str] = []
                    k = j + 2
                    while k < len(tokens) and not _is_a_command_border(tokens[k]):
                        rest.append(tokens[k])
                        k += 1
                    out.append(" ".join(value + rest))
            elif tok_j.startswith("--split-string="):
                out.append(tok_j.split("=", 1)[1])
            j += 1
    return out

def stops_or_restarts_the_daemon(cmd: str, _depth: int = 0) -> str | None:
    """The spelling of the act, if ``cmd`` stops or restarts the emrg daemon.

    Returns a short name of the spelling (for the refusal message) or ``None``.
    Consulted by `_check_sandbox` on both checked tiers, because the act is not
    a write and the write-target scans cannot see it: `emrg server stop` names no
    file and no git verb, and `pkill -f emrg.server` writes nothing at all.

    What is read is the *act*: an emrg program word (`emrg`, `emrgd`, a path to
    one, or `python -m emrg`) followed by `stop`/`restart`, optionally through
    the `server` sub-command and flags; or a signaller (`pkill`, `killall`,
    `kill`) one of whose operands names emrg. `_nested_command_texts` is asked
    for the texts a shell re-parses, so `sh -c 'emrg server stop'` and
    `eval 'emrg server restart'` are classified as the same act rather than as a
    string literal — the same walk the write-target rule already recurses
    through. `env -S "<string>"` is read by `_env_split_string_texts`: execing
    the argv that string splits into is `env`'s documented job, so that spelling
    is the act too, and it is read *here* rather than in the write walk because
    the text is argv-shaped — no shell reads the redirects in it.

    **Stated limits** (refused-direction bias does not apply here: each of these
    is *allowed*, and none is an ordinary route to the daemon):

    * a signaller whose operand does not name emrg — `killall python`,
      `pkill -f 'python -m emrg'` without the name in the text, `kill 12345`.
      A process table is not a static reading, and widening the signaller to
      every interpreter name would refuse ordinary `pkill node` work;
    * a quoted argument of a program that cannot run it — `echo "emrg server
      stop"` is one argument and prints it. Reading every quoted argument as a
      command is the over-block #1513 removed from this file (`echo sh "patch
      /etc/hosts"` was a false refusal), so the payload read is the shell's
      (`sh -c`, `eval`) and `env -S`'s, whose whole purpose is to split and exec
      it;
    * a name built at runtime (`$EMRG server stop`, `emrg${X} server stop`).
    """
    if _depth >= 3:
        return None
    tokens = _tokenize_command(cmd)
    for i, tok in enumerate(tokens):
        base = _basename(tok).lower()
        if base in _EMRG_PROGRAM_WORDS:
            entry = i
        elif tok == "-m" and tokens[i + 1 : i + 2] in (["emrg"], ["emrg.server"]):
            # The interpreter spelling — `python -m emrg server stop` — is how
            # this repo runs its own CLI, so it is a route to the live daemon
            # rather than a curiosity.
            entry = i + 1
        elif base in _PROCESS_SIGNALLER_WORDS:
            named = _operand_naming_emrg(tokens, i + 1)
            if named is not None:
                return f"{tok} {named}"
            continue
        else:
            continue
        verb = _daemon_lifecycle_verb_after(tokens, entry + 1)
        if verb is not None:
            return " ".join(
                t
                for t in tokens[entry : verb[1] + 1]
                if t not in _DAEMON_GROUPING_TOKENS
            )
    for nested in _nested_command_texts(tokens) + _env_split_string_texts(tokens):
        hit = stops_or_restarts_the_daemon(nested, _depth + 1)
        if hit is not None:
            return hit
    return None


def command_refusal(cmd: str) -> str | None:
    """Every command-text refusal a *checked* tier makes, in the order it makes them.

    The single answer for the two rules above, so an executor does not have to
    know which of them exist or in what order: the containment-escape scan runs
    first (a metadata-credential fetch is a read and must be seen before any
    write-shaped early return), then the daemon-lifecycle rule. ``None`` means
    the text cleared both.

    The tier is deliberately **not** a parameter. These are readings of the text;
    whether a call is checked at all is the policy's question, and the two calls
    that ask it (``BashToolV2.execute`` / ``PwshToolV2.execute``) hold the policy
    they just resolved. ``danger-full-access`` therefore never reaches this
    function — the same exemption the legacy ``_check_sandbox`` made in the same
    place, one line above its own call to these rules.

    :param cmd: the command text as the model wrote it.
    :returns: the refusal, ready to show, or ``None``.
    """
    escape = check_containment_escape(cmd)
    if escape:
        return escape
    stops = stops_or_restarts_the_daemon(cmd)
    if stops:
        return (
            f"blocked {stops!r} — it stops or restarts the emrg daemon, which is "
            "EMRG's life core (host red line, issue #1324). Restart it from the "
            "host's own terminal; `emrg pause` / `emrg resume` pause the evolution "
            "instead of the server."
        )
    return None

# A character no shell command can contain, so the restore cannot corrupt one.
_WINDOWS_BACKSLASH = "\x00"

def _protect_windows_backslashes(cmd: str) -> str:
    """Make backslashes survive the POSIX split — Windows shells only (#1261)."""
    if not _windows_shell() or "\\" not in cmd:
        return cmd
    return cmd.replace("\\", _WINDOWS_BACKSLASH)

def _restore_windows_backslashes(tokens: list[str]) -> list[str]:
    """Undo `_protect_windows_backslashes`, so the guard reads the real spelling."""
    if not _windows_shell():
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
        if _basename(tok) in _RUNNER_SUBCOMMAND_WORDS:
            # `<runner> run <candidate …>`: the runner execs a command word after
            # its `run` sub-command, so the candidate is that word. The runner
            # itself has to be in command position for this to be an invocation
            # — `echo uv run git checkout .` prints a string, and asking the same
            # walk about the `uv` keeps that allowed rather than widening the
            # rule to every mention of a runner name.
            k = j - 1
            while k >= 0 and tokens[k].startswith("-"):
                k -= 1
            if (
                k >= 0
                and _basename(tokens[k]) in _RUNNER_WORDS
                and _runs_as_a_command(tokens, k)
            ):
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

def _unresolved_wrapper_payloads(tokens: list[str]) -> list[str]:
    """Payloads of tokens the guard cannot resolve to a program.

    A token is a possible *wrapper* when its command word is a variable
    reference (`$SHELL`, `${SHELL}`, `"$SHELL"` — the tokenizer dequotes — and
    their assignment-prefixed spellings) rather than the name of a shell.
    Nothing in the text says whether that variable holds a shell, so what
    follows it is read as a command that may run: the same over-approximation a
    named wrapper already gets, and one that can only add blocking, never
    remove it (issue #1244).

    **Where** that word stands decides whether the words behind it are a payload
    at all, and `_runs_as_a_command` is the file's one answer to that question. A
    variable reference in *operand* position names no program the shell will run
    — `wc -c "$F"` hands `$F` to `wc` — and reading the rest of the line as its
    payload turned data into a command: measured on master `c1a70c94`,
    `wc -c "$F" && echo "patch rc=$?"` answered **BLOCK** at `read-only` naming
    `rc=$?` as a write target, because the payload token `patch rc=$?` was
    re-tokenized into the words `patch` and `rc=$?` and `patch` is a write verb
    (issue #1467). Nothing in that command writes anything, and since a refusal
    aborts the whole compound command the reads sharing the call are lost with
    it. The wrapper class itself is unchanged: every spelling in the corpus stands
    where a command can begin.
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
        ) and _runs_as_a_command(tokens, i):
            out.extend(tokens[i + 1:])
    return out
