"""Command-position **contexts** the guard did not recognise (#1241, and its siblings).

The walk decides whether a token spelling an act is *invoked* or merely
*mentioned*. It does that by walking left to the nearest token that opens a
command context. Five contexts were missing, and every one of them is reachable
in a real shell — measured by running a sentinel variant of each shape under
`/bin/sh` and `/bin/bash` and observing that the command really executed; no
destructive command is ever executed here.

    if <act>; then :; fi                conditional keywords (if / while / until)
    while <act>; do :; done             — the act is the *condition*
    until <act>; do :; done
    case x in x) <act>;; esac           a case arm is a command position
    echo done >( <act> )                process substitution
    echo done >( <newline> <act>        process substitution, newline-fused

`if` / `while` / `until` are the conditional keywords: the shell's grammar puts a
command immediately after them, exactly as after `then` / `do`. They are listed by
name rather than by corpus because the set is a *language* fact — a corpus can
only show what someone thought of, and the first version of this file enumerated
four of the seven words while 103 tests passed with `if <act>` open.

P7 changed the act, not the walk. The writers this matrix was measured with were
git mutators, refused by the read-only tier's static verb scan — a scan that dies
with ``emrg/tools/bash_tool.py``, because the v2 boundary makes the tier an OS
fence rather than a word list (design ``bash-tool-v2-design.md`` §2.2/§8). The
walk survives in ``emrg/tools/command_scan.py`` and carries exactly one rule now:
a command that stops or restarts the daemon (``MANIFESTO.md`` 第四条附则二, issue
#1324). So the act below is that one, and the walk's own properties — the
language facts about where a command can stand — are what this file still
asserts.

The read direction must not regress, as in every guard test in this repo: a change
that refused *everything* must not pass. The control is deliberately the **same
program word with a read-only verb** (``emrg --help``) standing in the very same
position, because that is the distinction the rule implements — the position
decides whether the program is invoked, the verb decides whether it is the act.
"""
from __future__ import annotations

import pytest

from emrg.tools import command_scan
from emrg.tools.command_scan import _tokenize_command

#: The one live act the walk carries, in the spellings a command line can carry it.
ACTS = [
    "emrg server stop",
    "emrg server restart",
    "/usr/local/bin/emrg server restart",
    "python -m emrg server stop",
]
#: The same program, a verb that is not the act — the control for every row below.
READS = ["emrg --help", "emrg dev"]
KEYWORDS = ["then", "do", "else", "elif"]
CONDITIONALS = ["if", "while", "until"]


def _reads(cmd: str) -> bool:
    """True when the text clears both rules — i.e. it is ordinary work."""
    return command_scan.command_refusal(cmd) is None


# ── keyword contexts (then / do / else / elif) ────────────────────────────

@pytest.mark.parametrize("kw", KEYWORDS)
@pytest.mark.parametrize("act", ACTS)
def test_keyword_context_hides_the_act(kw, act):
    """A keyword puts the next word in command position — the shell's grammar."""
    assert _reads(f"if true; {kw} {act}; fi") is False


@pytest.mark.parametrize("act", ACTS)
def test_for_do_context_hides_the_act(act):
    assert _reads(f"for i in 1; do {act}; done") is False


@pytest.mark.parametrize("act", ACTS)
def test_while_do_context_hides_the_act(act):
    assert _reads(f"while true; do {act}; break; done") is False


# ── conditional keywords: the act IS the condition ────────────────────────

@pytest.mark.parametrize("act", ACTS)
def test_if_condition_hides_the_act(act):
    """`if <cmd>; then …` runs cmd — `if` opens a command position.

    Reachable in both shells (measured with a sentinel in the act's slot). The
    first version of this file only spelled the condition `if true; then <act>`,
    so this slot was never enumerated.
    """
    assert _reads(f"if {act}; then :; fi") is False


@pytest.mark.parametrize("act", ACTS)
def test_while_condition_hides_the_act(act):
    assert _reads(f"while {act}; do :; done") is False


@pytest.mark.parametrize("act", ACTS)
def test_until_condition_hides_the_act(act):
    assert _reads(f"until {act}; do :; done") is False


@pytest.mark.parametrize("act", ACTS)
def test_elif_condition_hides_the_act(act):
    assert _reads(f"if false; then :; elif {act}; then :; fi") is False


@pytest.mark.parametrize("kw", KEYWORDS + CONDITIONALS)
@pytest.mark.parametrize("read", READS)
def test_the_same_position_with_a_read_only_verb_is_allowed(kw, read):
    """The clause changes *position*, not the verdict.

    Measured, not assumed: the same word in the same slot with a verb that is not
    the act clears both rules. Without this the file could pass by refusing every
    line that names the program.
    """
    assert _reads(f"if true; {kw} {read}; fi") is True
    assert _reads(f"if {read}; then :; fi") is True


def test_ordinary_reads_are_unaffected():
    for cmd in (
        "git status",
        "git log --oneline -5",
        "grep -rn git .",
        "cat file.txt",
        "( echo hi )",
        "f() { echo hi; }; f",
        "echo $(date)",
        "echo `date`",
        "time git status",
        "! git status",
        "awk 'BEGIN { print 1 }'",
        "grep -n ')' x.txt",
        "emrg --help | grep stop",
    ):
        assert _reads(cmd) is True, cmd


# ── process substitution ──────────────────────────────────────────────────

@pytest.mark.parametrize("act", ACTS)
def test_process_substitution_hides_the_act(act):
    assert _reads(f"echo done >({act})") is False


@pytest.mark.parametrize("act", ACTS)
def test_process_substitution_input_hides_the_act(act):
    assert _reads(f"cat <({act})") is False


@pytest.mark.parametrize("act", ACTS)
def test_newline_fused_process_substitution_hides_the_act(act):
    """`>(` and the newline fuse into one token — the same run family as #1241."""
    assert _reads(f"echo done >(\n{act}\n)") is False
    assert _reads(f"echo done\n>({act})") is False


def test_process_substitution_of_a_read_is_allowed():
    """`diff <(git show …)` is how a read is written; it must not be refused."""
    assert _reads("echo done >(git status)") is True
    assert _reads("echo done >(emrg --help)") is True
    assert _reads("diff <(git show HEAD:a) <(git show HEAD:b)") is True
    assert _reads("wc -l <(git ls-files)") is True


def test_process_substitution_token_is_a_fused_run():
    """The mechanism, asserted directly rather than through a verdict."""
    tokens = _tokenize_command("echo done >(\nemrg server stop\n)")
    assert ">(" in tokens, tokens
    assert "\n" in tokens, tokens


# ── case arm: `)` is a command position ──────────────────────────────────

@pytest.mark.parametrize("act", ACTS)
def test_case_arm_hides_the_act(act):
    """`case x in x) <cmd>;; esac` runs cmd.

    This was a *recorded boundary* (a test asserting the hole was open) until the
    cost of adding `)` to the command-position operators was measured: the only
    continuations a shell accepts after a closing paren are operators the walk
    already knows, and every newly-refused shape is a syntax error the shell
    rejects (`rc=2`) or a keyword-shaped word.
    """
    assert _reads(f"case x in x) {act};; esac") is False
    assert _reads(f"case x in x) {act}\n;; esac") is False


def test_case_arm_of_a_read_is_allowed():
    assert _reads("case x in x) emrg --help;; esac") is True
    assert _reads("case x in x) git status;; esac") is True


def test_closing_paren_as_a_word_is_refused_and_unreachable():
    """The cost of `)`, stated exactly: only shapes whose payload spells an act.

    `echo ) <act>` is refused and the shell refuses it too (unquoted `)` is a
    syntax error in both shells, rc=2), so the refusal is a false block in the
    loud direction. A *read* payload after a word `)` is still allowed — the
    verdict comes from the resolved verb, not from the position — and the shell
    rejects that shape as well, so nothing is let through.
    """
    assert _reads("echo ) emrg server stop") is False
    assert _reads("printf '%s' ) emrg server restart") is False
    assert _reads("echo ) emrg --help") is True


# ── backslash line continuation ──────────────────────────────────────────

def test_line_continuation_hides_the_act():
    """`\\<newline>` joins two lines into ONE command — and glues the newline to
    the next word, so the program token stops existing (measured: the token stream
    is `['x=1', '\\nemrg', 'server', 'stop']`). The shell runs the act; a reader
    that never split the fused run saw no program word at all.
    """
    assert _reads("\\\nemrg server stop") is False
    assert _reads("x=1 \\\nemrg server stop") is False
    assert _reads("echo done; \\\nemrg server stop") is False
    assert _reads("x=1 \\\r\nemrg server stop") is False
    assert "\\nemrg" not in _tokenize_command("x=1 \\\nemrg server stop")


def test_the_fused_run_is_the_mechanism_not_the_verdict():
    """Where the two directions meet, stated rather than hidden.

    Once the lines are joined the act's words are arguments of `echo`, and the
    live rule refuses a *mention* of the act in that position by design (its own
    corpus pins `echo emrg server stop`, measured since the rule landed —
    over-blocking a mention is the side it chooses). So this row asserts the
    reader's half, which is the half the strip exists for: the fused token is
    gone and the program word is a token again.
    """
    assert _tokenize_command("echo done \\\nemrg server stop") == [
        "echo", "done", "emrg", "server", "stop",
    ]
    assert _reads("echo done \\\nemrg --help") is True


# ── an ESCAPED backslash ends the continuation ────────────────────────────

@pytest.mark.parametrize("n", [2, 4, 6])
def test_an_escaped_backslash_does_not_join_the_lines(n):
    """`echo a\\\\<newline>emrg server stop` is TWO commands, and the shell runs the
    second one (measured in both shells).

    The even number of backslashes is the whole point: the shell consumes them as
    literal pairs, so the newline left behind is a **real separator** and the
    command word is intact. A strip that pairs the *last* backslash with the
    newline instead deletes that separator and leaves one backslash to escape the
    first letter of the next word: `emrg` becomes the token `aemrg` and the walk
    stops seeing a command at all — the same failure the strip exists to prevent,
    one character deeper. This test pins the direction that costs data.
    """
    cmd = "echo a" + "\\" * n + "\nemrg server stop"
    assert _reads(cmd) is False
    assert "emrg" in _tokenize_command(cmd), _tokenize_command(cmd)


def test_an_escaped_backslash_inside_an_assignment_or_after_a_separator():
    """The prefix does not change the arithmetic: only the parity does."""
    assert _reads("x=1 echo a" + "\\" * 2 + "\nemrg server stop") is False
    assert _reads("echo d; echo a" + "\\" * 2 + "\nemrg server stop") is False
    # Odd parity is a real continuation, so the lines join into one `echo` — and
    # the control is a read-only verb in the same joined position, which clears.
    assert _reads("echo a" + "\\" * 1 + "\nemrg --help") is True
    assert _reads("echo a" + "\\" * 3 + "\nemrg --help") is True


# ── a quoted apostrophe is data, not a quote state ────────────────────────

def test_a_quoted_apostrophe_does_not_stop_the_strip():
    """`echo "x'" ; a=1 \\<newline>emrg server stop` — the `'` sits inside double
    quotes, so the shell reads it as data, the continuation is real, and the act
    runs. Tracking `'` alone flipped the quote state, the strip never ran, and the
    newline stayed fused to the next word. The token stream is the independent
    signal: the separator only appears once the strip has run.
    """
    assert _reads('echo "x\'" ; a=1 \\\nemrg server stop') is False
    joined = _tokenize_command('echo "x\'" ; a=1 \\\nemrg server stop')
    assert "\\nemrg" not in joined, joined
    # Control: inside real single quotes a backslash is literal, nothing joins,
    # and the act is a quoted argument — the shape the rule leaves as data
    # (measured: no execution in either shell).
    assert _reads("echo 'a\\\nemrg server stop'") is True


# ── a CR is not a newline ─────────────────────────────────────────────────

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_a_backslash_before_crlf_is_not_a_continuation(n):
    """`echo a\\<CR><LF>emrg server stop` is TWO commands and the second one is real.

    The backslash escapes the **CR** — a literal character — and the LF is then a
    **real separator**, not a continuation. Measured with a sentinel in the act's
    slot: both `/bin/sh` and `/bin/bash` run the second command (`+ echo
    $'a\\r'` then `+ touch <sentinel>` in the `set -x` trace).

    A strip that pairs `\\` with a following `\\r\\n` deletes all three characters,
    joins the lines, and the command word stops being a token — so the walk answers
    ALLOW on a command the shell runs. This is the escaped-backslash case one
    character over, and it is why the rule is written as "only `\\<LF>` is a
    continuation" rather than "any newline spelling".
    """
    cmd = "echo a" + "\\" * n + "\r\nemrg server stop"
    assert _reads(cmd) is False
    assert "emrg" in _tokenize_command(cmd), _tokenize_command(cmd)


def test_crlf_without_a_backslash_was_never_a_continuation():
    """Control that keeps the distinction sharp: with no backslash the CR belongs
    to the previous word and the LF separates — which the walk already had right.
    """
    assert _reads("echo a\r\nemrg server stop") is False
    assert _reads("echo a" + "\\" * 0 + "\r\nemrg server stop") is False


def test_a_bare_cr_is_a_word_character_not_a_separator():
    """A CR with no LF does not separate at all, so the act's text stays inside one
    word and the shell runs nothing (measured: `echo $'a\\rtouch …'` is one command,
    the sentinel is never made). Asserted on the token stream, which is the half
    that survives here: the rule refuses a mention in that position by design (the
    same over-approximation as `echo emrg server stop`), so a verdict assertion
    would measure the rule's choice rather than the separator's absence.
    """
    tokens = _tokenize_command("echo a\remrg server stop")
    assert tokens[0] == "echo"
    assert all("emrg server" not in t for t in tokens), tokens
    assert _reads("echo a\remrg --help") is True


# ── `eval` and `find -exec` are command prefixes ──────────────────────────

@pytest.mark.parametrize("act", ACTS)
def test_eval_is_a_command_prefix(act):
    """`eval` joins its arguments and runs the result, so the word after it is a
    command. The walk looked for a command word and found a prefix it did not
    know, and answered "argument"; the shell runs the act (sentinel-measured in
    both shells).
    """
    assert _reads(f"eval {act}") is False


def test_eval_in_the_spellings_the_nested_walk_already_covered():
    """Quoted forms were already read by the nested-text walk; the unquoted
    spelling is the gap this closes, and both must agree."""
    assert _reads("eval emrg server stop") is False
    assert _reads('eval "emrg server stop"') is False
    assert _reads("eval 'emrg server stop'") is False
    assert _reads("x=1 eval emrg server restart") is False
    assert _reads("echo done; eval emrg stop") is False
    assert _reads("eval emrg --help") is True


@pytest.mark.parametrize("act", ACTS)
def test_find_exec_is_a_command_prefix(act):
    """`find … -exec <cmd> \\;` hands the next word to execve, so it is a command
    position — and `find` reports with its own text, not the shell's, which is why
    a reachability instrument keyed on `command not found` scores these wrong.
    """
    assert _reads(f"find . -maxdepth 0 -exec {act} \\;") is False


def test_find_execdir_is_a_command_prefix():
    assert _reads("find . -maxdepth 0 -execdir emrg server restart \\;") is False


def test_find_exec_of_a_read_is_allowed():
    """`-exec`'s **value** is skipped, so a read that merely mentions something
    after the flag stays allowed. Without this the clause would refuse every
    `find … -exec grep git` — measured in the corpus, and the reason `-exec` is
    spelled as a wrapper rather than as a new position rule.
    """
    assert _reads("find . -exec grep git {} \\;") is True
    assert _reads("find . -maxdepth 1 -name '*.py' -exec wc -l {} +") is True
    assert _reads("find . -type f -exec echo {} \\;") is True


def test_eval_as_a_plain_word_is_a_documented_cost():
    """`echo eval emrg server stop …` is a mention the walk refuses.

    The same over-block the keyword clause already accepts. It is the only new
    refusal in the read corpus: the walk cannot tell a prefix from a word without
    executing the shell.
    """
    assert _reads("echo eval emrg server stop is a phrase") is False


# ── the read direction must not regress ──────────────────────────────────

def test_inline_acts_are_still_refused():
    for act in ACTS:
        assert _reads(act) is False


def test_quoted_acts_are_still_data():
    assert _reads('echo "emrg server stop"') is True
    assert _reads('echo "emrg server stop\nemrg server restart"') is True
    assert _reads("printf %s 'emrg server stop'") is True


# ── a runner's `run` sub-command ──────────────────────────────────────────
#
# `uv run <cmd>` is how this repo runs its own tools (`uv run pytest tests/ -v`
# is in Agent.md), and the `run` word stands exactly where a flag's *value*
# stands — so the value-skip above never looked past it, and the command after
# it read as an argument. Measured on master `6126273d`, one workdir, nothing
# executed: `uv run git checkout .`, `uv run --no-sync git checkout .`, `uv run
# -q git reset --hard` and `poetry run git checkout .` all answered ALLOW and
# named **no** act, while the same command beside them was refused. The whole
# rule was one prefix away, and the prefix is the one the tree's own
# instructions use.
RUNNERS = ["uv", "poetry", "pdm", "hatch", "pipenv", "rye"]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("act", ACTS)
def test_a_runner_prefix_hides_the_act(runner, act):
    assert _reads(f"{runner} run {act}") is False, f"{runner} run {act}"
    assert _reads(f"{runner} run --no-sync {act}") is False


def test_a_runners_own_flags_do_not_hide_the_act():
    """`uv --quiet run …` and `uv run -q …`: the flags are the runner's, not walls."""
    assert _reads("uv --quiet run emrg server stop") is False
    assert _reads("uv run -q emrg server restart") is False
    assert _reads("timeout 60 uv run emrg server stop") is False
    assert _reads("cd /tmp && uv run emrg server stop") is False
    assert _reads("if uv run emrg stop; then :; fi") is False


def test_a_runner_does_not_hide_a_read():
    """The complement at the same site: a runner around ordinary work stays allowed."""
    for cmd in (
        "uv run pytest tests/ -q",
        "uv run --no-sync python3 scripts/check-doc-count.py --measure",
        "uv run git status",
        "uv run git log --oneline -3",
        "uv run grep git .",
        "uv run emrg --help",
    ):
        assert _reads(cmd) is True, cmd


def test_a_script_runner_is_not_a_runner():
    """`npm run`/`yarn run`/`pnpm run` take a *script name*, not an argv.

    `yarn run emrg server stop` asks yarn for a script of that name, so reading it
    as an invocation would refuse ordinary JS tooling — the over-block this file
    keeps out. The measured cost of the opposite choice is stated here rather than
    left to be rediscovered.
    """
    for cmd in (
        "yarn run emrg --help",
        "npm run emrg --help",
        "pnpm run emrg --help",
    ):
        assert _reads(cmd) is True, cmd


# ── a backtick is a command position ──────────────────────────────────────

def test_a_backticked_act_is_seen():
    """The tokenizer was given `\\n` and the backtick in its punctuation set
    (issues #1156, #1233) so a backticked program word is a token rather than a
    glued `` `emrg ``; the walk then reads it as a command.
    """
    for cmd in (
        "`emrg server stop`",
        "`emrg server restart`",
        "x=`emrg stop`",
    ):
        assert _reads(cmd) is False, f"{cmd!r} runs the act"
    # The complement, at the same site: a backticked read stays allowed.
    assert _reads("`emrg --help`") is True
    assert _reads("`date`") is True
    assert _reads("echo `date`") is True
