"""Command-position **contexts** the read-only guard did not recognise (#1241).

`_runs_as_a_command` decides whether a token spelling a mutator is *invoked* or
merely *mentioned*. It does that by walking left to the nearest token that opens
a command context. Five contexts were missing, and every one of them is reachable
in a real shell — measured by running a sentinel variant of each shape under
`/bin/sh` and `/bin/bash` and observing that the command really executed; no
destructive command is ever executed here.

    if <mutator>; then :; fi            conditional keywords (if / while / until)
    while <mutator>; do :; done         — the mutator is the *condition*
    until <mutator>; do :; done
    case x in x) <mutator>;; esac       a case arm is a command position
    echo done >( <mutator> )            process substitution
    echo done >( <newline> <mutator>    process substitution, newline-fused

`if` / `while` / `until` are the conditional keywords: the shell's grammar puts a
command immediately after them, exactly as after `then` / `do`. They are listed by
name rather than by corpus because the set is a *language* fact — a corpus can
only show what someone thought of, and the first version of this file enumerated
four of the seven words while 103 tests passed with `if <mutator>` open.

Both halves are asserted, as every guard test in this repo is: a change that
refused *everything* must not pass. So the same matrix is asserted in the read
direction (keyword and process-substitution payloads that only read), and the
mention forms that the shell itself treats as data stay allowed.

The `case` arm is no longer a recorded boundary: `)` is a command-position
operator now, and the cost of adding it was measured before it landed — every
continuation a shell accepts after a closing paren is an operator the guard
already knows, so the only newly-refused commands are ones the shell refuses too
(`rc=2`, `syntax error near unexpected token ')'` in both shells).
"""

import pytest

from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets, _tokenize_command

MUTATORS = [
    "git stash drop",
    "git checkout .",
    "git clean -fd",
    "git config user.name x",
    "git reset --hard",
]

KEYWORDS = ["then", "do", "else", "elif"]
CONDITIONALS = ["if", "while", "until"]


def _reads(cmd: str) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, "read-only")
    return allowed


# ── keyword contexts (then / do / else / elif) ────────────────────────────

@pytest.mark.parametrize("kw", KEYWORDS)
@pytest.mark.parametrize("writer", MUTATORS)
def test_keyword_context_hides_a_mutator(kw, writer):
    """A keyword puts the next word in command position — the shell's grammar."""
    assert _reads(f"if true; {kw} {writer}; fi") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_for_do_context_hides_a_mutator(writer):
    assert _reads(f"for i in 1; do {writer}; done") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_while_do_context_hides_a_mutator(writer):
    assert _reads(f"while true; do {writer}; break; done") is False


# ── conditional keywords: the mutator IS the condition ────────────────────

@pytest.mark.parametrize("writer", MUTATORS)
def test_if_condition_hides_a_mutator(writer):
    """`if <cmd>; then …` runs cmd — `if` opens a command position.

    Reachable in both shells (measured with a sentinel in the mutator's slot).
    The first version of this file only spelled the condition `if true; then
    <mutator>`, so this slot was never enumerated.
    """
    assert _reads(f"if {writer}; then :; fi") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_while_condition_hides_a_mutator(writer):
    assert _reads(f"while {writer}; do :; done") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_until_condition_hides_a_mutator(writer):
    assert _reads(f"until {writer}; do :; done") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_elif_condition_hides_a_mutator(writer):
    assert _reads(f"if false; then :; elif {writer}; then :; fi") is False


def test_keyword_context_still_allows_reads():
    """The clause changes *position*, not the verdict: a read payload stays a read."""
    assert _reads("if true; then git status; fi") is True
    assert _reads("if true; then git log --oneline -3; fi") is True
    assert _reads("if git status; then :; fi") is True
    assert _reads("while git status; do :; done") is True
    assert _reads("until git status; do :; done") is True
    assert _reads("for i in 1 2; do echo $i; done") is True
    assert _reads("while read -r l; do echo $l; done < f.txt") is True
    assert _reads("for f in $(git ls-files); do echo $f; done") is True
    assert _reads('if [ -n "$(git status --porcelain)" ]; then echo dirty; fi') is True


def test_keyword_as_a_plain_word_is_a_documented_cost():
    """A *word* spelled like a keyword followed by a mutator spelling is blocked.

    Measured, and it is the price of the clause: the walk left from `git` sees
    `then`/`if`/`while`/`until` and cannot tell an `echo`'s argument from a
    keyword. Every such shape is unreachable as a command, so the cost is a false
    block in the loud direction. Same trade as `_COMMAND_POSITION_OPERATORS`'
    existing `(` entry.
    """
    for kw in KEYWORDS + CONDITIONALS:
        assert _reads(f"echo {kw} git checkout .") is False
        # …and the same shape with a read payload must still pass.
        assert _reads(f"echo {kw} git status") is True


# ── process substitution ──────────────────────────────────────────────────

@pytest.mark.parametrize("writer", MUTATORS)
def test_process_substitution_hides_a_mutator(writer):
    assert _reads(f"echo done >({writer})") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_process_substitution_input_hides_a_mutator(writer):
    assert _reads(f"cat <({writer})") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_newline_fused_process_substitution_hides_a_mutator(writer):
    """`>(` and the newline fuse into one token — the same run family as #1241."""
    assert _reads(f"echo done >(\n{writer}\n)") is False
    assert _reads(f"echo done\n>({writer})") is False


def test_process_substitution_of_a_read_is_allowed():
    """`diff <(git show …)` is how a read is written; it must not be refused."""
    assert _reads("echo done >(git status)") is True
    assert _reads("echo done >(git stash list)") is True
    assert _reads("diff <(git show HEAD:a) <(git show HEAD:b)") is True
    assert _reads("wc -l <(git ls-files)") is True


def test_process_substitution_token_is_a_fused_run():
    """The mechanism, asserted directly rather than through a verdict."""
    tokens = _tokenize_command("echo done >(\ngit stash drop\n)")
    assert ">(" in tokens, tokens
    assert "\n" in tokens, tokens


# ── case arm: `)` is a command position ──────────────────────────────────

@pytest.mark.parametrize("writer", MUTATORS)
def test_case_arm_hides_a_mutator(writer):
    """`case x in x) <cmd>;; esac` runs cmd.

    This was a *recorded boundary* (a test asserting the hole was open) until the
    cost of adding `)` to `_COMMAND_POSITION_OPERATORS` was measured: the only
    continuations a shell accepts after a closing paren are operators the guard
    already knows, and every newly-refused shape is a syntax error the shell
    rejects (`rc=2`) or a keyword-shaped word.
    """
    assert _reads(f"case x in x) {writer};; esac") is False
    assert _reads(f"case x in x) {writer}\n;; esac") is False


def test_case_arm_of_a_read_is_allowed():
    assert _reads("case x in x) git status;; esac") is True
    assert _reads("case x in x) git log --oneline -3;; esac") is True


def test_closing_paren_as_a_word_is_blocked_and_unreachable():
    """The cost of `)`, stated exactly: only shapes whose payload spells a mutator.

    `echo ) <mutator>` is refused and the shell refuses it too (unquoted `)` is a
    syntax error in both shells, rc=2), so the refusal is a false block in the
    loud direction. A *read* payload after a word `)` is still allowed — the
    verdict comes from the resolved verb, not from the position — and the shell
    rejects that shape as well, so nothing is let through.
    """
    assert _reads("echo ) git checkout .") is False
    assert _reads("printf '%s' ) git stash drop") is False
    assert _reads("echo ) git status") is True


# ── backslash line continuation ──────────────────────────────────────────

def test_line_continuation_hides_a_mutator():
    """`\\<newline>` joins two lines into ONE command — and glues the newline to
    the next word, so the `git` token stops existing (measured: the token stream
    is `['x=1', '\\ngit', 'checkout', '.']`). The shell runs the mutator; the
    guard saw no `git` at all.
    """
    assert _reads("\\\ngit checkout .") is False
    assert _reads("x=1 \\\ngit checkout .") is False
    assert _reads("echo done; \\\ngit checkout .") is False
    assert _reads("x=1 \\\r\ngit checkout .") is False


def test_line_continuation_before_a_writer_verb_is_seen():
    """The same defect in the *write-target* tokenizer, which is a second site."""
    assert _extract_write_targets("x=1 \\\nrm -f f.txt") == ["f.txt"]
    assert _extract_write_targets("x=1 \\\ncp a b") == ["b"]
    assert _extract_write_targets("x=1 \\\ntee out.txt") == ["out.txt"]
    assert _extract_write_targets("x=1 \\\nsed -i s/a/b/ f.txt") == ["f.txt"]
    assert _reads("x=1 \\\nrm -f f.txt") is False


def test_line_continuation_that_joins_into_a_mention_stays_allowed():
    """The shell joins the lines, so `git` here is an argument — not a command.

    Both directions by construction: a fix that treated `\\<newline>` as a
    separator would block this, and the shell does not.
    """
    assert _reads("echo done \\\ngit checkout .") is True
    assert _reads("echo a \\\nb") is True
    assert _tokenize_command("echo done \\\ngit checkout .") == [
        "echo", "done", "git", "checkout", ".",
    ]


# ── an ESCAPED backslash ends the continuation ────────────────────────────

@pytest.mark.parametrize("n", [2, 4, 6])
def test_an_escaped_backslash_does_not_join_the_lines(n):
    """`echo a\\\\<newline>git checkout .` is TWO commands, and the shell runs the
    second one (measured in both shells).

    The even number of backslashes is the whole point: the shell consumes them as
    literal pairs, so the newline left behind is a **real separator** and the
    command word is intact. A strip that pairs the *last* backslash with the
    newline instead deletes that separator and leaves one backslash to escape the
    first letter of the next word: `git` becomes the token `agit` and the guard
    stops seeing a command at all — the same failure the strip exists to prevent,
    one character deeper. This test pins the direction that costs data.
    """
    cmd = "echo a" + "\\" * n + "\ngit checkout ."
    assert _reads(cmd) is False
    assert "git" in _tokenize_command(cmd), _tokenize_command(cmd)


def test_an_escaped_backslash_before_a_writer_verb_is_seen():
    """The same spelling at the *write-target* tokenizer, which is a second site."""
    assert _extract_write_targets("echo a" + "\\" * 2 + "\nrm -f f.txt") == ["f.txt"]
    assert _reads("echo a" + "\\" * 2 + "\nrm -f f.txt") is False
    assert _reads("echo a" + "\\" * 2 + "\ngit stash drop") is False


def test_an_escaped_backslash_inside_an_assignment_or_after_a_separator():
    """The prefix does not change the arithmetic: only the parity does."""
    assert _reads("x=1 echo a" + "\\" * 2 + "\ngit checkout .") is False
    assert _reads("echo d; echo a" + "\\" * 2 + "\ngit checkout .") is False
    # Odd parity is a real continuation, so the lines join into one `echo` and
    # both the shell and the guard read `git` as an argument (control, and the
    # reason the strip is not simply "never join").
    assert _reads("echo a" + "\\" * 1 + "\ngit checkout .") is True
    assert _reads("echo a" + "\\" * 3 + "\ngit checkout .") is True


# ── a quoted apostrophe is data, not a quote state ────────────────────────

def test_a_quoted_apostrophe_does_not_stop_the_strip():
    """`echo "x'" ; a=1 \\<newline>git checkout .` — the `'` sits inside double
    quotes, so the shell reads it as data, the continuation is real, and the
    mutator runs. Tracking `'` alone flipped the quote state, the strip never
    ran, and the newline stayed fused to the next word — on master and on the
    fix alike. The token stream is the independent signal: the separator only
    appears once the strip has run.
    """
    assert _reads('echo "x\'" ; a=1 \\\ngit checkout .') is False
    assert _reads('echo "x\'" ; \\\ngit checkout .') is False
    joined = _tokenize_command('echo "x\'" ; a=1 \\\ngit checkout .')
    assert "\\ngit" not in joined, joined
    # Control: inside real single quotes a backslash is literal, nothing joins,
    # and the shell runs nothing (measured: no execution in either shell).
    assert _reads("echo 'a\\\ngit checkout .'") is True


# ── a CR is not a newline ─────────────────────────────────────────────────

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_a_backslash_before_crlf_is_not_a_continuation(n):
    """`echo a\\<CR><LF>git checkout .` is TWO commands and the second one is real.

    The backslash escapes the **CR** — a literal character — and the LF is then a
    **real separator**, not a continuation. Measured with a sentinel in the
    mutator's slot: both `/bin/sh` and `/bin/bash` run the second command
    (`+ echo $'a\\r'` then `+ touch <sentinel>` in the `set -x` trace).

    A strip that pairs `\\` with a following `\\r\\n` deletes all three characters,
    joins the lines, and the command word stops being a token — so the guard
    answers ALLOW on a command the shell runs. This is the escaped-backslash case
    one character over, and it is why the rule is written as "only `\\<LF>` is a
    continuation" rather than "any newline spelling".
    """
    cmd = "echo a" + "\\" * n + "\r\ngit checkout ."
    assert _reads(cmd) is False
    assert "git" in _tokenize_command(cmd), _tokenize_command(cmd)


def test_crlf_without_a_backslash_was_never_a_continuation():
    """Control that keeps the distinction sharp: with no backslash the CR belongs
    to the previous word and the LF separates — which the guard already had right.
    """
    assert _reads("echo a\r\ngit checkout .") is False
    assert _reads("echo a" + "\\" * 0 + "\r\ngit checkout .") is False


def test_a_bare_cr_is_a_word_character_not_a_separator():
    """And the other direction: a CR with no LF does not separate at all, so the
    mutator text stays an argument and the command is a mention. The shell agrees
    (measured: `echo $'a\\rtouch …'` is one command, the sentinel is never made).
    """
    assert _reads("echo a\rgit checkout .") is True


# ── `eval` and `find -exec` are command prefixes ──────────────────────────

@pytest.mark.parametrize("writer", MUTATORS)
def test_eval_is_a_command_prefix(writer):
    """`eval` joins its arguments and runs the result, so the word after it is a
    command. The walk looked for a command word and found a prefix it did not
    know, and answered "argument"; the shell runs the mutator (sentinel-measured
    in both shells).
    """
    assert _reads(f"eval {writer}") is False


def test_eval_in_the_spellings_the_nested_walk_already_covered():
    """Quoted forms were already read by the nested-text walk; the unquoted
    spelling is the gap this closes, and both must agree."""
    assert _reads("eval git checkout .") is False
    assert _reads('eval "git checkout ."') is False
    assert _reads("eval 'git checkout .'") is False
    assert _reads("x=1 eval git clean -fd") is False
    assert _reads("echo done; eval git reset --hard") is False


@pytest.mark.parametrize("writer", MUTATORS)
def test_find_exec_is_a_command_prefix(writer):
    """`find … -exec <cmd> \\;` hands the next word to execve, so it is a command
    position — and `find` reports with its own text, not the shell's, which is why
    a reachability instrument keyed on `command not found` scores these wrong.
    """
    assert _reads(f"find . -maxdepth 0 -exec {writer} \\;") is False


def test_find_execdir_is_a_command_prefix():
    assert _reads("find . -maxdepth 0 -execdir git reset --hard \\;") is False


def test_find_exec_of_a_read_is_allowed():
    """`-exec`'s **value** is skipped, so a read that merely mentions the mutator
    after the flag stays allowed. Without this the clause would refuse every
    `find … -exec grep git` — measured in the corpus, and the reason `-exec` is
    spelled as a wrapper rather than as a new position rule.
    """
    assert _reads("find . -exec grep git {} \\;") is True
    assert _reads("find . -maxdepth 1 -name '*.py' -exec wc -l {} +") is True
    assert _reads("find . -type f -exec echo {} \\;") is True


def test_eval_as_a_plain_word_is_a_documented_cost():
    """`echo eval git checkout …` is a mention the guard now refuses.

    The same over-block the keyword clause already accepts (`echo then git
    checkout .`), measured as the **only** new refusal in the read corpus: the
    walk cannot tell a prefix from a word without executing the shell.
    """
    assert _reads("echo eval git checkout is a phrase") is False


# ── the read direction must not regress ──────────────────────────────────

def test_inline_mutators_are_still_blocked():
    for writer in MUTATORS:
        assert _reads(writer) is False


def test_quoted_mutators_are_still_data():
    assert _reads('echo "git stash drop"') is True
    assert _reads('echo "git stash drop\ngit clean -fd"') is True


def test_ordinary_reads_are_unaffected():
    for cmd in (
        "git status",
        "git log --oneline -5",
        "git stash list",
        "grep -rn git .",
        "cat file.txt",
        "( git log -1 )",
        "f() { git log -1; }; f",
        "echo $(git rev-parse HEAD)",
        "echo `git status`",
        "time git status",
        "! git status",
        "awk 'BEGIN { print 1 }'",
        "git log --pretty='%h) %s'",
        "grep -n ')' x.txt",
    ):
        assert _reads(cmd) is True, cmd


# ── the same two directions at the *write-target* walk (#14xx) ────────────

WRITE_MENTIONS = [
    'grep -n "rm" f.txt',
    'grep -n "patch" f.txt',
    'grep -e mv f.txt',
    "echo rm -rf /tmp/x",
    "echo touch /tmp/x",
    "printf '%s\\n' sed -i f.txt",
    "cat mv.txt",
    "test -f patch",
]

WRITE_INVOCATIONS = [
    "rm -rf /tmp/x",
    "sudo rm -rf /tmp/x",
    "env FOO=1 rm -rf /tmp/x",
    "FOO=1 rm -rf /tmp/x",
    "xargs rm -rf /tmp/x",
    "find . -exec rm -rf /tmp/x {} +",
    "timeout 5 rm -rf /tmp/x",
    "nice -n 5 rm -rf /tmp/x",
    "if true; then rm -rf /tmp/x; fi",
    "for f in a; do rm -rf /tmp/x; done",
    "(rm -rf /tmp/x)",
    "! rm -rf /tmp/x",
    "cd /tmp\nrm -rf /tmp/x",
]


def test_a_writer_verb_in_argument_position_is_data_not_an_invocation():
    """`grep -n "rm" f.txt` searches for the word; it does not run `rm`.

    The walk visits every token and matched its word against the verb sets wherever it
    stood, which is what reaches `sudo rm` and `find -exec rm` — and also what read the
    word as an invocation when the shell passes it as an argument. `_runs_as_a_command`
    is the guard's own rule for the difference, and it is the question the git-mutator
    scan already asks.
    """
    for cmd in WRITE_MENTIONS:
        assert _reads(cmd) is True, f"{cmd!r} mentions a verb and writes nothing"
        assert _extract_write_targets(cmd) == [], cmd


def test_every_context_that_runs_a_writer_still_names_it():
    """The complement: a change that refused nothing would pass the test above."""
    for cmd in WRITE_INVOCATIONS:
        assert _reads(cmd) is False, f"{cmd!r} really runs the verb"
        assert _extract_write_targets(cmd), cmd


def test_a_backticked_writer_is_seen_by_the_write_target_walk():
    """The other tokenizer's backtick half, at the *second* site (#14xx).

    `_tokenize_command` was given `\\n` and the backtick in its punctuation set (issue
    #1156, #1233), and the write-target walk kept `_split_command_tokens`, whose `shlex`
    punctuation defaults to `();<>|&`. A backticked writer therefore stayed glued to its
    backtick — `` `rm `` is not `rm` — and named no target: measured, every one of 26
    writer verbs behind a backtick was ALLOWED at `read-only` while `$( ... )`, `( ... )`,
    `if`, `do`, a pipe and a bare newline all named the same path. The walk now reads the
    separator-preserving tokenizer, which is what the position question needs.
    """
    for cmd in (
        "`rm -rf /tmp/x`",
        "`touch /tmp/x`",
        "`tee /tmp/x`",
        "`sed -i s/a/b/ /tmp/x`",
    ):
        assert _reads(cmd) is False, f"{cmd!r} runs the verb"
    # The complement, at the same site: a backticked read stays allowed.
    assert _reads("`cat f.txt`") is True
    assert _reads("echo `date`") is True




# ── a runner's `run` sub-command ──────────────────────────────────────────
#
# `uv run <cmd>` is how this repo runs its own tools (`uv run pytest tests/ -v`
# is in Agent.md), and the `run` word stands exactly where a flag's *value*
# stands — so the value-skip above never looked past it, and the command after
# it read as an argument. Measured on master `6126273d`, one workdir, nothing
# executed: `uv run git checkout .`, `uv run --no-sync git checkout .`, `uv run
# -q git reset --hard` and `poetry run git checkout .` all answered ALLOW at
# read-only and named **no** mutator, while `git checkout .` beside them was
# refused. The whole git rule was one prefix away, and the prefix is the one the
# tree's own instructions use.
#
# Both halves are asserted, as everywhere in this file: a mention of the shape —
# `echo uv run git checkout .`, a runner name inside a grep pattern — is data and
# stays allowed.
RUNNERS = ["uv", "poetry", "pdm", "hatch", "pipenv", "rye"]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("mutator", MUTATORS)
def test_a_runner_prefix_hides_a_mutator(runner, mutator):
    assert _reads(f"{runner} run {mutator}") is False, f"{runner} run {mutator}"
    assert _reads(f"{runner} run --no-sync {mutator}") is False


def test_a_runners_own_flags_do_not_hide_the_mutator():
    """`uv --quiet run …` and `uv run -q …`: the flags are the runner's, not walls."""
    assert _reads("uv --quiet run git checkout .") is False
    assert _reads("uv run -q git reset --hard") is False
    assert _reads("timeout 60 uv run git checkout .") is False
    assert _reads("cd /tmp && uv run git checkout .") is False
    assert _reads("if uv run git stash drop; then :; fi") is False


def test_a_runner_name_used_as_data_stays_allowed():
    """Where the runner is not itself in command position, the shape is a string.

    The runner rule asks `_runs_as_a_command` about the *runner*, rather than
    treating the pair as always-an-invocation: `echo uv run git checkout .`
    prints its arguments, and `grep -rn 'uv run git checkout' .` searches for the
    text. Widening this to every mention would be the #1513 over-block one level
    out (`echo sh "patch /etc/hosts"` was a bug, not a safe refusal).
    """
    for cmd in (
        "echo uv run git checkout .",
        "printf %s uv run git checkout .",
        "grep -rn 'uv run git checkout' .",
        "git log --grep run uv",
    ):
        assert _reads(cmd) is True, cmd
        assert _extract_write_targets(cmd) == [], cmd


def test_a_script_runner_is_not_a_runner():
    """`npm run`/`yarn run`/`pnpm run` take a *script name*, not an argv.

    `yarn run git checkout .` asks yarn for a script called `git`, so reading it
    as an invocation of git would refuse ordinary JS tooling — the over-block this
    file keeps out. The measured cost of the opposite choice is stated here rather
    than left to be rediscovered.
    """
    for cmd in (
        "yarn run git checkout .",
        "npm run git checkout .",
        "pnpm run git checkout .",
    ):
        assert _reads(cmd) is True, cmd


def test_a_runner_does_not_hide_a_read():
    """The complement at the same site: a runner around a read stays allowed."""
    for cmd in (
        "uv run pytest tests/ -q",
        "uv run --no-sync python3 scripts/check-doc-count.py --measure",
        "uv run git status",
        "uv run git log --oneline -3",
        "uv run grep git .",
    ):
        assert _reads(cmd) is True, cmd
