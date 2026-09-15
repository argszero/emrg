"""A *fused* separator run is still a separator (issue #1233's class, one character later).

`_tokenize_command` hands `\\n` to `shlex` as punctuation, but `punctuation_chars`
groups **adjacent** punctuation into a single token: `echo done;` followed by a
newline arrives as ``";\\n"``, a blank line as ``"\\n\\n"``, `git status &&`
followed by a newline as ``"&&\\n"``. None of those is `in _COMMAND_SEPARATORS`,
so `_runs_as_a_command` walked left past them, found the previous command's
operand — a word, not a separator — and answered "data, not an invocation". The
mutator was never seen.

Measured on master `e6eaaee4`, `read-only` tier, by calling `_check_sandbox`
directly (the commands are never executed): **40 shapes ALLOWED** that block when
the same writer is written inline — 5 writers × 8 fused forms.

    git stash drop                        -> blocked
    echo done<newline>git stash drop      -> blocked   (what #1233 fixed)
    echo done<blank line>git stash drop   -> ALLOWED   (this class)
    echo done;<newline>git stash drop     -> ALLOWED   (this class)
    echo done<newline>&&git checkout .    -> ALLOWED   (this class)

`git stash drop` discards a stash and `git checkout .` discards uncommitted work,
so this is the same data-loss class the read-only tier exists to make
structurally impossible, reachable by pressing Enter twice.

Both halves are asserted, exactly as #1233's own test does: a fix that split on
*every* newline would tear ``echo "git stash drop<newline>git clean -fd"`` in two
and read the mutator inside the string as a command, so the quoted cases must
keep being allowed.
"""

import pytest

from emrg.tools.bash_tool import _check_sandbox, _tokenize_command

TIER = "read-only"

WRITERS = (
    "git stash drop",
    "git checkout .",
    "git clean -fd",
    "git config user.name someone",
    "git reset --hard",
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
    """A writer behind any fused line break must be blocked, not read as data."""
    cmd = FUSED_FORMS[form].format(w=writer)
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is False, (
        f"{cmd!r} ({form}) writes; must block, got allowed ({reason!r})")


@pytest.mark.parametrize("writer", WRITERS)
def test_inline_control_for_every_writer(writer: str) -> None:
    """The control for the matrix above: each writer blocks when written inline.

    Without this, a change that merely refused everything would look like a fix.
    """
    for cmd in (writer, f"echo done; {writer}"):
        allowed, reason, _ = _check_sandbox(cmd, TIER)
        assert allowed is False, f"{cmd!r} must block ({reason!r})"


@pytest.mark.parametrize("cmd", [
    'echo "git stash drop\ngit clean -fd"',
    "echo 'git checkout .\ngit reset --hard'",
    'echo "git stash drop\n\ngit clean -fd"',
    'printf %s "x;\ngit stash drop"',
])
def test_the_same_forms_inside_quotes_stay_data(cmd: str) -> None:
    """A fused run *inside quotes* is one argument, not a command boundary.

    The shell never runs the mutator named there, so allowing it is correct —
    and it is the half a "split on every newline" fix would break.
    """
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is True, f"{cmd!r} only names a mutator; must allow ({reason!r})"


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

    `_extract_write_targets` matches a redirect *operator* by spelling and takes
    the token after it as the target; splitting ``">>\\n"`` into ``">"``, ``">"``
    would name the second `>` as the target instead of the file.
    """
    assert _tokenize_command("echo x >>\nfile") == ["echo", "x", ">>", "\n", "file"]
    # …and a newline that came from quotes is part of a word, never a token.
    assert _tokenize_command('echo "a\nb"') == ["echo", "a\nb"]
