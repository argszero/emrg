"""A *fused* separator run is still a separator (issue #1233's class, one character later).

`_tokenize_command` hands `\\n` to `shlex` as punctuation, but `punctuation_chars`
groups **adjacent** punctuation into a single token: `echo done;` followed by a
newline arrives as ``";\\n"``, a blank line as ``"\\n\\n"``, `git status &&`
followed by a newline as ``"&&\\n"``. None of those is a separator token, so the
walk left past them, found the previous command's operand — a word, not a
separator — and answered "data, not an invocation". The act named on the next
line was never seen.

Measured on master `e6eaaee4` (via the tier scanner this rule then lived in, by
calling it directly — the commands are never executed): **40 shapes ALLOWED**
that block when the same writer is written inline — 5 writers × 8 fused forms.
Five of the 8 forms are shapes a shell really runs the writer in (probe: the
writer word replaced by `mkdir <fresh dir>`, asked of both `/bin/sh` and
`/bin/bash`); the other three — ``"\\n;"``, ``"\\n&&"``, ``"\\n(\\n"`` — are parse
errors in both, so 25 of the 40 are shapes whose writer a shell executes and the
rest are closed conservatively.

    emrg server stop                        -> refused
    echo done<newline>emrg server stop      -> refused   (what #1233 fixed)
    echo done<blank line>emrg server stop   -> ALLOWED   (this class)
    echo done;<newline>emrg server stop     -> ALLOWED   (this class)
    echo done<newline>&&emrg server stop    -> ALLOWED   (this class)

P7 changed what the writer has to be, not the defect. The writers this matrix was
measured with were git mutators, refused by the read-only tier's static verb scan
— a scan that dies with `emrg/tools/bash_tool.py` because the v2 boundary makes
the tier an OS fence instead of a word list (design
`bash-tool-v2-design.md` §2.2/§8). The walk it defeated survives, in
`emrg/tools/command_scan.py`, and now carries exactly one rule: a command that
stops or restarts the daemon (`MANIFESTO.md` 第四条附则二, issue #1324). So the
writer here is that act — same matrix, same direction, asked of the rule that
still walks it. Nothing about the fused-run defect is git-specific: it is a
property of the token stream.

Both halves are asserted, exactly as #1233's own test does: a fix that split on
*every* newline would tear ``echo "emrg server stop<newline>emrg server restart"``
in two and read the act inside the string as a command, so the quoted cases must
keep being allowed.
"""

import pytest

from emrg.tools import command_scan
from emrg.tools.command_scan import _tokenize_command

# The one live rule the walk carries. Every form below must deliver it.
WRITERS = (
    "emrg server stop",
    "emrg server restart",
    "emrg stop",
    "/usr/local/bin/emrg server restart",
    "sh -c 'emrg server stop'",
)

# Every spelling of "the next line starts a new command" that shlex can produce
# by fusing the newline with the punctuation beside it.
FUSED_FORMS = {
    "blank-line": "echo done\n\n{w}",
    "two-blank-lines": "echo done\n\n\n{w}",
    "semicolon-then-newline": "echo done;\n{w}",
    "newline-then-semicolon": "echo done\n;{w}",
    "andand-then-newline": "echo done &&\n{w}",
    "newline-then-andand": "echo done\n&&{w}",
    "pipe-then-newline": "echo done |\n{w}",
    "newline-paren-newline": "echo done\n(\n{w}",
}


@pytest.mark.parametrize("form", sorted(FUSED_FORMS))
@pytest.mark.parametrize("writer", WRITERS)
def test_fused_separator_run_still_separates(form: str, writer: str) -> None:
    """The act behind any fused line break must be read, not taken for data."""
    cmd = FUSED_FORMS[form].format(w=writer)
    reason = command_scan.command_refusal(cmd)
    assert reason is not None, (
        f"{cmd!r} ({form}) names the act; must refuse, got allowed")


@pytest.mark.parametrize("writer", WRITERS)
def test_inline_control_for_every_writer(writer: str) -> None:
    """The control for the matrix above: each writer is refused when inline.

    Without this, a change that merely refused everything would look like a fix.
    """
    for cmd in (writer, f"echo done; {writer}"):
        assert command_scan.command_refusal(cmd) is not None, f"{cmd!r} must refuse"


@pytest.mark.parametrize("cmd", [
    'echo "emrg server stop\nemrg server restart"',
    "echo 'emrg stop\nemrg server restart'",
    'echo "emrg server stop\n\nemrg server restart"',
    'printf %s "x;\nemrg server stop"',
])
def test_the_same_forms_inside_quotes_stay_data(cmd: str) -> None:
    """A fused run *inside quotes* is one argument, not a command boundary.

    The shell never runs the act named there, so allowing it is correct — and it
    is the half a "split on every newline" fix would break.

    One stated limit, measured: the guard refuses a *mention* of the act in some
    payload positions by design (`test_the_act_is_refused`'s `env -S` row does
    exactly that, and the legacy corpus pinned the same over-refusal). These rows
    are quoted arguments to a program that prints them, so the walk leaves them
    as data; that is the property under test.
    """
    assert command_scan.command_refusal(cmd) is None, f"{cmd!r} only names the act"


def test_tokenizer_splits_the_fused_newline_apart() -> None:
    """The mechanism, asserted directly — the walks can only see what is a token."""
    assert _tokenize_command("echo done\n\ngit stash drop") == [
        "echo", "done", "\n", "\n", "git", "stash", "drop"]
    assert _tokenize_command("echo done;\ngit stash drop") == [
        "echo", "done", ";", "\n", "git", "stash", "drop"]
    assert _tokenize_command("echo done &&\ngit stash drop") == [
        "echo", "done", "&&", "\n", "git", "stash", "drop"]


def test_other_punctuation_runs_are_not_split() -> None:
    """`>>` must survive as one token.

    The unfusing is scoped to `\\n` on purpose: a redirect operator is matched by
    spelling, and splitting ``">>\\n"`` into ``">"``, ``">"`` would corrupt every
    rule that reads what follows an operator.
    """
    assert _tokenize_command("echo x >>\nfile") == ["echo", "x", ">>", "\n", "file"]
    # …and a newline that came from quotes is part of a word, never a token.
    assert _tokenize_command('echo "a\nb"') == ["echo", "a\nb"]
