"""A heredoc the guard could not read was scanned as commands (issue #1320).

`_mask_data_heredoc_bodies` masks a heredoc body only when the simple command
owning the `<<` **reads its stdin as data**. That test used to be a property of
the *tool* (`_DATA_READER_CONSUMERS`), so an invocation that is nothing but a
tool, a subcommand and `-` — `git commit -F -`, measured to put the body into the
commit message — kept its body in the text, and the write-target scan read a
*mention* of a path inside a message as a target:

    git commit -q -F - <<'EOF'
    echo x > ~/.emrg/config.toml
    EOF
    -> BLOCKED, "blocked write to protected daemon file '~/.emrg/config.toml'"

Nothing in that command writes anything; the only file it touches is inside
`.git`. The reader is now pinned on the *invocation*: the subcommand must be
argv[0] after the tool, and an operand must name stdin.

Both halves are asserted, because "allow more" is also what a broken guard does.
The refusals in the second half are load-bearing rather than caution — measured
on git 2.50.1, `git -c core.editor=sh commit -F - -e` and the
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` spelling of the same
override each ran the heredoc body as a script.

The operand spellings were measured too, in a scratch repo: `-F -`, `--file -`,
`--file=-` and `-F-` all put stdin into the tag message, while `-F=-` opens the
file `=-` and fails — so `-F=-` is not a stdin spelling and is not treated as
one.
"""

import pytest

from emrg.tools.bash_tool import _check_sandbox, _heredoc_delimiters_read_as_data

TIER = "workspace-write"
BODY = "echo x > ~/.emrg/config.toml"


def _heredoc(first_line: str, body: str = BODY) -> str:
    return f"{first_line}\n{body}\nEOF\n"


def _allowed(cmd: str) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, TIER)
    return allowed is True


# ── May mask: the message reader owns its stdin ─────────────────────────────

@pytest.mark.parametrize("first_line", [
    "git commit -q -F - <<'EOF'",
    "git commit --file - <<'EOF'",
    "git commit --file=- <<'EOF'",
    "git commit -F- <<'EOF'",
    "git commit -F - <<'EOF'",
    "git tag -F - <<'EOF'",
    "git tag -q --file - <<'EOF'",
])
def test_a_message_readers_body_is_not_scanned(first_line: str) -> None:
    """The body is a commit message; the path in it is prose, not a target."""
    assert _allowed(_heredoc(first_line)), f"{first_line!r} reads its stdin as a message"


@pytest.mark.parametrize("body", [
    "echo x > ~/.emrg/config.toml",     # a protected daemon file
    "echo x > /etc/hosts",              # an absolute path outside the workspace
])
def test_a_message_body_is_prose_whatever_path_it_names(body: str) -> None:
    """The point of the mask: a path in a message is a character, not a target."""
    assert _allowed(_heredoc("git commit -F - <<'EOF'", body))


@pytest.mark.parametrize("first_line", [
    "cat <<'EOF'",
    "grep x <<'EOF'",
    "python3 <<'EOF'",
    "wc -l <<'EOF'",
])
def test_the_named_data_readers_are_unchanged(first_line: str) -> None:
    """The pre-existing allowlist must keep working — the new axis is additive."""
    assert _allowed(_heredoc(first_line)), f"{first_line!r} was already masked"


# ── Must stay scanned ──────────────────────────────────────────────────────

@pytest.mark.parametrize("first_line", [
    # A config value set before the subcommand can make it run a program
    # (measured: core.editor runs the message file as a script under `-e`).
    "git -c core.editor=sh commit -F - -e <<'EOF'",
    "git -c alias.commit='!sh' commit -F - <<'EOF'",
    "git --config-env=core.editor=EDITOR commit -F - <<'EOF'",
    # The env spelling of the same override — invisible in the command word.
    # The value is the one the exploit needs and the guard must not lean on
    # something else for: it is deliberately *not* a shell name. `sh` would be
    # caught by boundary 4 (`_basename` strips the assignment, so the value
    # reads as a wrapper token outside the bodies), which would hide whether
    # the env prefix refuses the mask at all.
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.editor GIT_CONFIG_VALUE_0=python3 git commit -F - -e <<'EOF'",
    "FOO=1 git commit -F - <<'EOF'",
    # A tool that reads stdin as a program, not as data.
    "sh <<'EOF'",
    "bash <<'EOF'",
    "env sh <<'EOF'",
    # Readers that are also executors stay on the allowlist's guarded side.
    "sed <<'EOF'",
    "patch <<'EOF'",
    "ssh host <<'EOF'",
    "awk <<'EOF'",
])
def test_the_invocation_is_not_a_message_reader(first_line: str) -> None:
    """Each of these keeps the body in the text, so the path in it stays a target."""
    allowed, reason, _ = _check_sandbox(_heredoc(first_line), TIER)
    assert allowed is False, f"{first_line!r} must stay scanned"
    assert "protected" in reason


@pytest.mark.parametrize("first_line", [
    # No operand names stdin: the heredoc is not the message.
    "git commit -m x <<'EOF'",
    # An operand, but not stdin.
    "git commit -F message.txt <<'EOF'",
    "git commit --file=message.txt <<'EOF'",
    # Measured: `-F=-` reads the file `=-`, so it is not a spelling of stdin.
    "git commit -F=- <<'EOF'",
    # A subcommand that does not read a message.
    "git checkout -F - <<'EOF'",
    "git log -F - <<'EOF'",
    # A different tool, however similar the spelling.
    "hg commit -F - <<'EOF'",
    "gitk -F - <<'EOF'",
])
def test_the_operand_or_subcommand_is_not_a_message_reader(first_line: str) -> None:
    assert _allowed(_heredoc(first_line)) is False


def test_a_pipe_still_forfeits_the_mask() -> None:
    """`cat <<EOF | sh` is the existing boundary; a message reader does not lift it."""
    allowed, reason, _ = _check_sandbox(_heredoc("cat <<'EOF' | sh"), TIER)
    assert allowed is False
    assert "protected" in reason
    allowed, reason, _ = _check_sandbox(_heredoc("git commit -F - <<'EOF' | sh"), TIER)
    assert allowed is False
    assert "protected" in reason


# ── The delimiter extractor itself ─────────────────────────────────────────

@pytest.mark.parametrize("line, expected", [
    ("git commit -F - <<EOF", ["EOF"]),
    ("git commit --file=- <<'EOF'", ["EOF"]),
    ("git commit -F- <<'EOF'", ["EOF"]),
    ("git tag -F - <<-EOF", ["EOF"]),
    ("cat <<EOF", ["EOF"]),
    ("git commit -m x <<EOF", []),
    ("git commit -F message.txt <<EOF", []),
    ("git -c core.editor=sh commit -F - -e <<EOF", []),
    ("FOO=1 git commit -F - <<EOF", []),
    ("git checkout -F - <<EOF", []),
])
def test_the_delimiters_read_as_data(line: str, expected: list) -> None:
    """Unit level: exactly the invocations of the message matrix return a delimiter."""
    assert _heredoc_delimiters_read_as_data(line) == expected
