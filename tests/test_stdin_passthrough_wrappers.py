"""A wrapper in front of the reader lost the heredoc mask (issue #1466).

`_owns_stdin_as_data` reads the consumer at `argv[0]`, so a command that reads its
stdin as data was recognised only when it stood first. Every wrapper in front of
it lost the mask and the body was scanned as shell code — including this
repository's own documented spelling, which `Agent.md` names three times:

    uv run --no-sync python3 - <<'PY'
    x = i > ai
    PY
    -> BLOCKED, "blocked destructive write targeting 'ai'"

`cat` lost the mask under a wrapper too, so the carrier was never the
interpreter — it is *where the consumer is read*. The reading is now the one a
bare reader gets; what is new is the **location**, and that half is fail-closed:
the wrapped command word is used only when it is unambiguous (exactly one
non-flag token after the wrapper's subcommand), because an option's value and the
command word are otherwise indistinguishable without a per-tool flag table — the
enumeration this file refuses everywhere else.

Measured on uv 0.9.x while writing this, so the rule has ground truth rather than
a guess:

* `printf 'print(1)' | uv run --no-sync python3 -` printed `1` — the wrapper does
  hand its stdin to the command it runs;
* `uv run --no-sync sh - name` reached `sh` with the body on its stdin — a shell
  *is* reachable through the wrapper, which is why a rule that guessed the wrong
  word would mask a body that really runs;
* `uv run --no-sync --python python3 <<EOF`, with no command word, executed
  nothing ("Provide a command or script to invoke with `uv run <command>`").

Both halves are asserted, because "mask more" is also what a broken guard does.
"""

import pytest

from emrg.tools.bash_tool import _check_sandbox, _heredoc_delimiters_read_as_data

TIER = "workspace-write"
# A comparison, not a redirect: the tokenizer reads `>` and names `ai` as the
# target, which is what the issue measured against this repository's spelling.
COMPARISON = "x = i > ai"
# Prose a body may legitimately hold, and a path no command here writes.
MUTATOR = "echo x > ~/.emrg/config.toml"


def _cmd(first_line: str, body: str = COMPARISON) -> str:
    return f"{first_line}\n{body}\nPY\n"


def _masked(cmd: str) -> bool:
    """Whether the heredoc body was recognised as data and blanked."""
    return _heredoc_delimiters_read_as_data(cmd) == ["PY"]


def _allowed(cmd: str) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, TIER)
    return allowed is True


# ── May mask: the wrapper hands the body to the reader it runs ──────────────

@pytest.mark.parametrize("first_line", [
    # The spelling the issue was filed on: `uv run --no-sync python3 …`, used by
    # every merge gate in `Agent.md`.
    "uv run --no-sync python3 - <<'PY'",
    # A further flag changes nothing: the region between the subcommand and the
    # command word is never enumerated, so a flag the guard has never heard of
    # does not decide the answer.
    "uv run --no-sync --frozen python3 - <<'PY'",
    "uv run --no-sync --offline python3 - <<'PY'",
    # `--` is a flag token, and the word after it is still the only non-flag one.
    "uv run --no-sync -- python3 - <<'PY'",
    # No flags at all.
    "uv run python3 - <<'PY'",
    # Wrapped named readers keep the mask they have bare (`cat <<EOF` is masked).
    "uv run --no-sync cat <<'PY'",
    "uv run --no-sync wc -l <<'PY'",
])
def test_a_wrapped_reader_keeps_the_mask_it_has_bare(first_line: str) -> None:
    """The body is data for the command the wrapper runs, whatever the wrapper."""
    assert _masked(_cmd(first_line)), f"{first_line!r} reads its stdin as data"
    assert _allowed(_cmd(first_line))


@pytest.mark.parametrize("body", [
    COMPARISON,
    "echo x > ~/.emrg/config.toml",     # a protected daemon file
    "echo x > /etc/hosts",              # an absolute path outside the workspace
])
def test_a_wrapped_body_is_prose_whatever_path_it_names(body: str) -> None:
    """The point of the mask, one wrapper out: a path in a body is not a target."""
    assert _allowed(_cmd("uv run --no-sync python3 - <<'PY'", body))


def test_the_verdict_is_the_reading_and_not_the_spelling() -> None:
    """The mask's own rationale (issue #1320): one body, two spellings, one answer.

    This is the row the issue is about — the same script judged differently for
    the language spelling that runs it. Read as a pair so a guard that changed
    the bare half along with the wrapped one cannot pass.
    """
    bare = _cmd("python3 - <<'PY'")
    wrapped = _cmd("uv run --no-sync python3 - <<'PY'")
    assert _masked(bare) and _masked(wrapped)
    assert _allowed(bare) and _allowed(wrapped)


# ── Must stay scanned: the command word is not locatable, or not a reader ───

@pytest.mark.parametrize("first_line", [
    # A shell reached through the wrapper: measured, `uv run --no-sync sh` and
    # `uv run --no-sync sh -s cat` each printed the heredoc's `echo BODY-RAN`, so
    # the body runs here.
    "uv run --no-sync sh <<'PY'",
    "uv run --no-sync sh -s cat <<'PY'",
    "uv run --no-sync bash - <<'PY'",
    # …and the row that pins *which* word may not be guessed: in `sh -s cat` the
    # only reader-named word is the shell's `$0`, so a rule that located the last
    # non-flag token would mask a body a shell is about to execute.
    "uv run --no-sync sh -s python3 <<'PY'",
    # An option's value that *is* a reader name, in front of the real command.
    # Without a flag table the first non-flag token is the value (`cat`) and the
    # command word is the later one (`sh`), so no single answer is right.
    "uv run --no-sync --directory cat sh - <<'PY'",
    "uv run --no-sync --directory cat python3 - <<'PY'",
    "uv run --no-sync --python cat python3 - <<'PY'",
    # Measured: `sh - name` reads the *file* `name` rather than the body, so this
    # row is refused for the other reason — the command word is a shell, not a
    # reader, whatever the operand turns out to mean.
    "uv run --no-sync sh - name <<'PY'",
    # The reader's own argument makes two non-flag tokens: refused in the loud
    # direction (the body is scanned and a data body is refused) rather than
    # guessed. This is the price of the rule, not a hole in it.
    "uv run --no-sync cat body.txt <<'PY'",
    "uv run --no-sync python3 script.py <<'PY'",
    # No command word at all: measured, uv runs nothing, so there is no consumer
    # to hand the body to.
    "uv run --no-sync <<'PY'",
    # A global option before the subcommand is the git case again (`-c` can make
    # the subcommand run a program), refused by position rather than enumerated.
    "uv --directory x run python3 - <<'PY'",
    "uv --python cat run python3 - <<'PY'",
    # An env prefix can carry `UV_*` to the wrapper's own resolver, exactly as
    # `GIT_CONFIG_*` carries an editor to git's.
    "UV_PYTHON=sh uv run --no-sync python3 - <<'PY'",
    "FOO=1 uv run --no-sync python3 - <<'PY'",
    # Wrappers that are not enumerated keep today's behaviour: the body is
    # scanned. `env -S` takes a *string* to re-split, which is the shape the
    # issue names as unprovable.
    "env -S 'python3 -' <<'PY'",
    "sudo python3 - <<'PY'",
    "nohup python3 - <<'PY'",
    "time python3 - <<'PY'",
])
def test_an_unresolvable_wrapper_keeps_the_body_scanned(first_line: str) -> None:
    """Each of these keeps the body in the text, so its path stays a target."""
    cmd = _cmd(first_line, MUTATOR)
    assert not _masked(cmd), f"{first_line!r} must stay scanned"
    allowed, reason, _enforcement = _check_sandbox(cmd, TIER)
    assert allowed is False
    assert "protected" in reason


def test_the_bare_readers_are_unchanged() -> None:
    """The new axis is additive: the allowlist that was there still decides alone."""
    for first_line in ("cat <<'PY'", "grep x <<'PY'", "python3 - <<'PY'", "wc -l <<'PY'"):
        assert _masked(_cmd(first_line)), f"{first_line!r} was already masked"


def test_a_pipe_still_forfeits_the_mask() -> None:
    """`cat <<EOF | sh` is the existing boundary; a wrapper does not lift it."""
    cmd = _cmd("uv run --no-sync cat <<'PY' | sh", MUTATOR)
    assert not _masked(cmd)
    assert _allowed(cmd) is False
