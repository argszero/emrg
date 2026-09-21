"""A wrapper *word* is believed only where the shell would run it.

Issue #1513. `_nested_command_texts` decides which *texts* the two payload readers see,
and it had three branches: the named wrapper (`sh`, `bash`, `zsh`, `dash`, … and any of
them by `_basename`, so `/bin/sh` too) and `eval` took everything after the token with
**no position test**, while the third branch (`_unresolved_wrapper_payloads`) was already
gated by `_runs_as_a_command`. A wrapper word used as *data* therefore re-read the rest of
the line as a command, and the payload was judged twice: once as the string it is, and
once as the command it is not.

Measured on master `9a7bfe65` through `_check_sandbox` alone (both tiers, nothing
executed):

    BLOCK BLOCK  echo sh "patch /etc/hosts"        targets ['/etc/hosts']
    BLOCK BLOCK  printf %s sh "patch /etc/hosts"   targets ['/etc/hosts']
    BLOCK BLOCK  echo eval "patch /etc/hosts"      targets ['/etc/hosts']
    BLOCK ALLOW  echo bash "git checkout ."        targets []
    ALLOW ALLOW  echo foo "patch /etc/hosts"       targets []      <- the control

`echo sh "patch /etc/hosts"` **prints a string**: it is refused at both tiers, at
`read-only` — the tier whose whole purpose is to let a `sh` be read — and the three
controls in that slot (a non-wrapper word, no word at all) are allowed, so the
discriminator is the wrapper word and not the payload. The gate that removes it is the one
#1469 gave the verb walk inside a text ("may this verb, standing here, be believed?"),
asked one site over: which texts is the walk handed at all.

Both directions are asserted, because a change that simply *removed* the wrapper branch
would pass half of this file. The corpus below is the same one the gate was measured
against, and the invocations are the reason the gate is a **position** test rather than
"the wrapper must be first": `env FOO=1 sh -c …`, `sudo -u root sh -c …`,
`xargs -I{} sh -c …`, `find . -exec sh -c …`, `timeout 5 sh -c …` and
`bash --login -c …` are all reachable today, and a first-token-only gate would under-block
every one of them — the direction this guard must never move in.

**The gate was rebuilt once, and the reason is the third corpus below** (veto by
`pm25coder`, cycle `cyc20260921-190928`, on this PR's first head `4bce5f44`; reproduced and
fixed by `cyc20260921-193118`). The first version skipped the payload whenever the verb
walk's `_runs_as_a_command` said the wrapper word was not in command position — and for a
word after a command it does not *recognise*, that function's answer is "data", because its
job one site over is to decide whether a **verb** is invoked. At this site the same answer
means "the payload is not read", so a true block became a silent allow for every prefix that
execs the next word without being listed:

    fakeroot sh -c "patch /etc/hosts"   master BLOCK/BLOCK   first version ALLOW/ALLOW
    taskset  sh -c "patch /etc/hosts"   master BLOCK/BLOCK   first version ALLOW/ALLOW
    ltrace   sh -c "git checkout ."     master BLOCK/ALLOW   first version ALLOW/ALLOW

13 of the 13 unlisted exec prefixes the veto measured flipped that way. The fix inverts the
default at this one site: the payload is skipped only on a **positive** proof that its
command word does not run its arguments (`_DATA_ONLY_COMMANDS`), so an unrecognised prefix
keeps master's over-approximation — a loud false block, never a silent allow. The two sites
genuinely need opposite defaults, and `UNRECOGNISED_PREFIX_SHAPES` is the fence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emrg.tools.bash_tool import (
    _SHELL_SEPARATORS,
    _check_sandbox,
    _extract_write_targets,
    _nested_command_texts,
    _split_command_tokens,
    _tokenize_command,
)

READ_ONLY = "read-only"
WW = "workspace-write"

# A wrapper word standing where the shell reads it as data. Each line only *prints*
# the quoted payload; the payload is spelled with an absolute path (or a git mutator)
# so that believing it is what makes the difference between ALLOW and BLOCK.
#
# A double-quoted **command substitution** was in this list and is not data: the shell
# runs `$( … )` there, so `echo "$(sh -c 'git checkout .')"` really performs the checkout.
# It read as data only while the substitution reached the walk as one opaque token, which
# is issue #1516 — the row moved to `tests/test_quoted_substitution_guard.py`, where the
# refusal is asserted through the reading that can see into the body. Removing it here
# rather than leaving it is the point: pinned as data it asserted an ALLOW for a line that
# mutates, and the same tree asserted the *unquoted* twin as an invocation two lines below
# (`echo $(sh -c "git checkout .")`), so the file contradicted itself about one shell
# behaviour. Measured with both readings on one tree (PR #1518 merged into this branch):
# the row is refused at `read-only` — `blocked git mutating command 'git checkout'`.
DATA_SHAPES = [
    'echo sh "patch /etc/hosts"',
    'echo bash "git checkout ."',
    'echo /bin/sh "patch /etc/hosts"',
    'echo zsh "rm -rf /tmp/x"',
    'echo eval "patch /etc/hosts"',
    'printf %s sh "patch /etc/hosts"',
    'echo "sh"',
]

# The same wrapper word where the shell *runs* it. Every row is either a destructive
# write to a path outside any workspace or a git mutator, so `read-only` is the tier
# where the answer is unambiguous.
INVOCATION_SHAPES = [
    'sh -c "patch /etc/hosts"',
    'bash -c "git checkout ."',
    '/bin/sh -c "patch /etc/hosts"',
    'eval "patch /etc/hosts"',
    'bash -lc "git checkout ."',
    'sh -c \'sh -c "git checkout ."\'',
    'echo $(sh -c "git checkout .")',
    # ...and the same thing behind every prefix the tree already knows reaches a
    # command: a gate that only accepted position 0 would allow all six.
    'true && sh -c "patch /etc/hosts"',
    'echo x; sh -c "patch /etc/hosts"',
    '(sh -c "git checkout .")',
    'FOO=1 sh -c "git checkout ."',
    'env FOO=1 sh -c "patch /etc/hosts"',
    'sudo -u root sh -c "patch /etc/hosts"',
    'xargs -I{} sh -c "patch /etc/hosts"',
    'timeout 5 sh -c "git checkout ."',
    'find . -exec sh -c "patch /etc/hosts" \\;',
    'bash --login -c "git checkout ."',
    'if true; then sh -c "git checkout ."; fi',
]


# The same wrapper word behind a prefix the guard does **not** know execs the next word.
# Master over-approximates here (it reads the payload after any wrapper word, whatever
# stands in front of it), and that over-approximation is the whole value of the branch:
# the prefix list cannot be completed (#1420's shape), so the safe answer for an
# unrecognised one is the loud false block. Every row below must stay refused at
# `read-only` — this is the corpus the veto on the first head measured as flipping.
UNRECOGNISED_PREFIX_SHAPES = [
    'fakeroot sh -c "patch /etc/hosts"',
    'taskset sh -c "patch /etc/hosts"',
    'ltrace sh -c "git checkout ."',
    'strace sh -c "patch /etc/hosts"',
    'ssh host sh -c "patch /etc/hosts"',
    'flock /tmp/l sh -c "patch /etc/hosts"',
    'setpriv --reuid 1 sh -c "git checkout ."',
    'bwrap --dev-bind / / sh -c "patch /etc/hosts"',
    # ...and the same shape one word further out, where the *unknown* word is not the
    # head: `fakeroot env sh -c …` still runs the payload, so it is still refused.
    'fakeroot env FOO=1 sh -c "patch /etc/hosts"',
]


@pytest.mark.parametrize("cmd", DATA_SHAPES)
def test_a_wrapper_word_in_data_is_not_a_wrapper(cmd: str, tmp_path: Path) -> None:
    """The line is data, so neither reader may recurse into it."""
    assert _nested_command_texts(_tokenize_command(cmd)) == [], (
        f"the wrapper word in {cmd!r} was believed, so the rest of the line is read "
        "as a command"
    )
    assert _extract_write_targets(cmd) == [], f"{cmd!r} names a write it does not make"
    for tier in (READ_ONLY, WW):
        allowed, reason, _ = _check_sandbox(cmd, tier, str(tmp_path))
        assert allowed, f"{cmd!r} is data, and {tier} refuses it: {reason}"


@pytest.mark.parametrize("cmd", INVOCATION_SHAPES)
def test_a_wrapper_in_command_position_is_still_believed(cmd: str, tmp_path: Path) -> None:
    """The payload is a command the shell will run, and read-only must refuse it."""
    nested = _nested_command_texts(_tokenize_command(cmd))
    assert nested, f"{cmd!r} runs a command through a wrapper, and no text was read"
    allowed, reason, _ = _check_sandbox(cmd, READ_ONLY, str(tmp_path))
    assert not allowed, f"{cmd!r} was allowed at {READ_ONLY} (nested={nested!r})"
    assert reason


@pytest.mark.parametrize("cmd", UNRECOGNISED_PREFIX_SHAPES)
def test_an_unrecognised_prefix_keeps_the_over_approximation(cmd: str, tmp_path: Path) -> None:
    """The prefix is not in any list, so the payload is read and `read-only` refuses it.

    This is the veto's fence: the first head of this gate skipped the payload when the
    verb walk's position test said the wrapper word was "data", which is exactly what
    that test answers for a word after a command it does not recognise — so a true block
    became a silent allow for 13 measured prefixes. The payload is now skipped only on a
    positive proof (`_DATA_ONLY_COMMANDS`), so these rows come back.
    """
    nested = _nested_command_texts(_tokenize_command(cmd))
    assert nested, f"{cmd!r} runs a payload behind an unrecognised prefix, and none was read"
    allowed, reason, _ = _check_sandbox(cmd, READ_ONLY, str(tmp_path))
    assert not allowed, f"{cmd!r} was allowed at {READ_ONLY} (nested={nested!r})"
    assert reason


@pytest.mark.parametrize(
    "cmd",
    [
        'echo foo "patch /etc/hosts"',
        'echo "patch /etc/hosts"',
        'grep -rn sh /etc/hosts',
        "cat sh.txt",
    ],
)
def test_the_controls_stay_allowed(cmd: str, tmp_path: Path) -> None:
    """The controls the discriminator was read against: same slot, no wrapper word."""
    for tier in (READ_ONLY, WW):
        allowed, reason, _ = _check_sandbox(cmd, tier, str(tmp_path))
        assert allowed, f"{cmd!r} names no write and no invocation: {reason}"


def test_the_docstring_names_the_gate_the_code_calls() -> None:
    """`_nested_command_texts` must name `_is_data_argument`, not the vetoed predicate.

    The gate's paragraph is the first carrier a reader meets, and it named the verb walk's
    `_runs_as_a_command` — the reading the veto removed. Taken literally it restores exactly
    that: at the same index the two predicates disagree on 7 of the 11 shapes measured in
    `_is_data_argument`, and `fakeroot sh -c "patch /etc/hosts"` is one of them, so a reader
    following the prose re-opens the silent allow. Nothing else could catch it — the fences
    above assert behaviour, and behaviour was already right; prose is what drifted.

    The split is at the historical sentence on purpose: the paragraph *may* name the
    superseded predicate where it says what the first version asked, and must not name it as
    the gate.
    """
    doc = " ".join((_nested_command_texts.__doc__ or "").split())
    paragraph = doc.split("**But a wrapper *word* is not a wrapper invocation**")[1]
    paragraph = paragraph.split("An **un-resolvable** wrapper")[0]
    gate, _, history = paragraph.partition("The first version of this gate")
    assert history, "the paragraph must still record what the first version asked"
    assert "_is_data_argument" in gate, (
        "the gate paragraph must name the predicate this function actually calls"
    )
    assert "_runs_as_a_command" not in gate, (
        "the gate paragraph names the verb walk's position test, which is the vetoed reading"
    )


# The operators a command can follow *directly*, one row each. The whole proof this gate rests on
# is a positive one — "this word heads a shell whose only job is to print" (`_DATA_ONLY_COMMANDS`) —
# so an operator the walk does not recognise as a border is a hole in that proof, not a nuisance:
# the walk steps through it, reaches the data-only head, and answers "data".
#
# `|&` was the missing one (PR #1515 review). It is bash's pipe-stdout-and-stderr, one character
# wider than `|`, and the tokenizer emits it as a **single** token — measured, as every row here is:
#
#   echo x |& sh -c "patch /etc/hosts"   master BLOCK/BLOCK -> first head ALLOW/ALLOW
#   echo x |& sh -c "git checkout ."     master BLOCK/ALLOW -> first head ALLOW/ALLOW
#   echo x |& eval "patch /etc/hosts"    master BLOCK/BLOCK -> first head ALLOW/ALLOW
#
# Whether the payload *can* run is host-dependent, and the corpus is written to the host where it
# can: bash ≥ 4 runs `|&` (the review measured it with Git-for-Windows bash 5, where a marker file
# was really written), while this host's `/bin/bash` is 3.2.57 and rejects the row as
# `syntax error near unexpected token '&'` — so a corpus calibrated to the shell in front of it
# would have called this row inert. The deny is the safe direction either way: the cost is a false
# block on bash 3.2 alone, and the alternative is a silent allow one host over.
BORDER_OPERATOR_SHAPES = [
    'echo x && sh -c "patch /etc/hosts"',
    'echo x || sh -c "patch /etc/hosts"',
    'echo x ; sh -c "patch /etc/hosts"',
    'echo x | sh -c "patch /etc/hosts"',
    'echo x |& sh -c "patch /etc/hosts"',
    'echo x & sh -c "patch /etc/hosts"',
    # A newline, spelled with `chr(10)` so the row cannot be read as two lines of this file.
    # It is the row that forces the corpus to tokenize the way the product does: the walk's own
    # tokenizer emits `\n` as a token, while `_split_command_tokens` drops it, so a corpus that
    # read with the latter saw an empty `nested` for this row and would have called a live hole
    # into the 2026-08-20 data-loss class a pass.
    'echo done' + chr(10) + 'sh -c "patch /etc/hosts"',
]

# The operators the tokenizer *also* emits as single tokens, which are deliberately **not** borders.
# Listed with the shape that shows why, so "put every operator token in the set" is not the rule
# either — that would be an over-approximation bought with no measurement:
#   `;;` / `;&` / `;;&` — legal only inside a `case` body, where what follows is the next *pattern*.
#                        The command position after them is the pattern's `)`, already in
#                        `_COMMAND_POSITION_OPERATORS`; measured, the row below is BLOCK/BLOCK.
#   `<<<` / `>>` / `<<`  — the word after them is the here-string fed to stdin, or a filename to
#                        write: data, not a command. Measured ALLOW/ALLOW and BLOCK for the
#                        redirect target that really is one.
NOT_A_BORDER_SHAPES = [
    'case a in a) echo ;; b) sh -c "patch /etc/hosts" ;; esac',
    'echo x <<< sh -c "patch /etc/hosts"',
    # ...and an operator row whose right-hand command writes nothing: the tier is not the question
    # here, the reading is — `grep -f` names a pattern file to read.
    'echo x |& grep -f /etc/passwd',
]


@pytest.mark.parametrize("cmd", BORDER_OPERATOR_SHAPES)
def test_every_command_border_is_a_border(cmd: str, tmp_path: Path) -> None:
    """A wrapper word after any command border is believed, whatever the operator's spelling."""
    nested = _nested_command_texts(_tokenize_command(cmd))
    assert nested, f"{cmd!r} runs a payload behind a border, and no text was read"
    allowed, reason, _ = _check_sandbox(cmd, READ_ONLY, str(tmp_path))
    assert not allowed, f"{cmd!r} was allowed at {READ_ONLY} (nested={nested!r})"
    assert reason


def test_the_border_set_matches_the_operators_the_tokenizer_splits_out() -> None:
    """Every operator that can be followed directly by a command is in `_SHELL_SEPARATORS`.

    Written as a rule over the set rather than as six more rows because the failure is a *set*
    drifting away from the tokenizer, and the two must be read together: `|&` was emitted as one
    token and sat in none of the three sets the walk consults, so a single operator defeated a proof
    whose strength is a positive one. The control half is what keeps this from being satisfied by
    putting every punctuation token in the set.
    """
    for op in ("&&", "||", ";", "|", "|&", "&", "\n"):
        tokens = _tokenize_command(f'echo x {op} sh -c "patch"')
        assert op in tokens, f"the tokenizer does not emit {op!r} as a token: {tokens!r}"
        assert op in _SHELL_SEPARATORS, (
            f"{op!r} separates one command from the next, and the walk in `_runs_as_a_command` "
            "reads only the sets — a border outside them is stepped through, and the head it "
            "reaches can be a data-only command whose whole job is printing"
        )

    for op in (";;", ";&", ";;&"):
        tokens = _tokenize_command(f"echo x {op} sh")
        assert op in tokens, f"precondition: the tokenizer emits {op!r} as one token: {tokens!r}"
        assert op not in _SHELL_SEPARATORS, (
            f"{op!r} is not a command border — it is legal only inside a `case` body, where the "
            "next word is a pattern and the `)` after it is the command position"
        )


@pytest.mark.parametrize("cmd", NOT_A_BORDER_SHAPES)
def test_the_operators_that_are_not_borders_are_covered_by_their_own_reader(
    cmd: str, tmp_path: Path
) -> None:
    """The control side: each non-border operator's shape is handled, by the reader that can see it.

    A `case` body's command position is its pattern's `)`, a here-string operand is data, and a
    pattern file is read rather than written — so none of these needs the operator itself to be a
    border, and each answer is measured rather than assumed.
    """
    if cmd.startswith("case "):
        allowed, reason, _ = _check_sandbox(cmd, READ_ONLY, str(tmp_path))
        assert not allowed, "a `case` body reaches its command through the pattern's `)`"
        assert reason
    else:
        for tier in (READ_ONLY, WW):
            allowed, reason, _ = _check_sandbox(cmd, tier, str(tmp_path))
            assert allowed, f"{cmd!r} names no write and no invocation at {tier}: {reason}"
            assert _extract_write_targets(cmd) == [], f"{cmd!r} names a write it does not make"
