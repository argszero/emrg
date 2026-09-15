"""`git config` decides by flag or subcommand — never by counting positionals.

The defect these tests exist for (issue #1253, measured on master with git
2.50.1): six spellings of `git config` wrote while the read-only guard said
ALLOW — `--unset`, `--unset-all`, `--edit`, `-e`, `--remove-section`, and the
subcommand spelling `edit`. They leaked for the same reason: the config branch
asked "how many positional tokens are there?" instead of "what does this flag
do?". `git config --add k v` blocked only because it leaves two positionals and
`--unset k` leaves one, which is an accident of the rule, not a decision about
the flag — so the next write flag someone adds would leak the same way.

The mirror image was live at the same time: `git config get k`, a pure read in
git's 2.46+ subcommand spelling, was refused as a write, because two positionals
also look like an assignment.

Every case below is driven through `_check_sandbox(cmd, "read-only")` — the
guard's own entry point, in the tier where the question is asked — and the two
lists are asserted in BOTH directions, so a change that blocks everything or
allows everything fails rather than passing half the file.
"""

from __future__ import annotations

import pytest

from emrg.tools.bash_tool import (
    _GIT_CONFIG_READ_SUBCOMMANDS,
    _GIT_CONFIG_WRITE_FLAGS,
    _GIT_CONFIG_WRITE_SUBCOMMANDS,
    _check_sandbox,
)

# ── the corpus ─────────────────────────────────────────────────────────────

WRITES = [
    # flag spelling
    "git config --unset foo.bar",
    "git config --unset-all foo.bar",
    "git config --edit",
    "git config -e",
    "git config --remove-section foo",
    "git config --rename-section foo bar",
    "git config --add foo.bar baz",
    "git config --replace-all foo.bar baz",
    "git config foo.bar baz",
    "git config user.name x",
    # subcommand spelling (git 2.46+)
    "git config set user.name x",
    "git config unset foo.bar",
    "git config unset-all foo.bar",
    "git config edit",
    "git config remove-section foo",
    "git config rename-section foo bar",
]

READS = [
    "git config foo.bar",
    "git config -l",
    "git config --list",
    "git config --get foo.bar",
    "git config --get-all foo.bar",
    "git config --get-regexp foo",
    "git config get foo.bar",
    "git config get-all foo.bar",
    "git config list",
]


@pytest.mark.parametrize("cmd", WRITES)
def test_a_writing_git_config_shape_is_blocked(cmd: str) -> None:
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is False, f"{cmd!r} writes but read-only allowed it"
    # The block must come from the git rule, not from something incidental
    # (a redirect scan, a path rule) that would leave the shape undecided.
    assert "git" in (reason or "").lower(), f"{cmd!r} blocked for the wrong reason: {reason!r}"


@pytest.mark.parametrize("cmd", READS)
def test_a_reading_git_config_shape_is_allowed(cmd: str) -> None:
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is True, f"{cmd!r} only reads but was refused: {reason!r}"


def test_both_spellings_of_one_operation_agree() -> None:
    """`--unset k` and `unset k` are the same operation, so they share a verdict.

    This is the property that makes the flag list and the subcommand list one
    decision rather than two, and it fails if only one of them is maintained.
    """
    pairs = [
        ("git config --unset foo.bar", "git config unset foo.bar"),
        ("git config --unset-all foo.bar", "git config unset-all foo.bar"),
        ("git config --edit", "git config edit"),
        ("git config --remove-section foo", "git config remove-section foo"),
        ("git config --rename-section foo bar", "git config rename-section foo bar"),
        ("git config --get foo.bar", "git config get foo.bar"),
        ("git config --list", "git config list"),
    ]
    for flagged, spelled in pairs:
        a = _check_sandbox(flagged, "read-only")[0]
        b = _check_sandbox(spelled, "read-only")[0]
        assert a == b, f"{flagged!r} -> {a} but {spelled!r} -> {b}"


def test_a_key_named_like_a_subcommand_still_reads() -> None:
    """The subcommand test must look at position, not at the word.

    `git config --get unset` reads a key that *happens* to be spelled like a
    write subcommand; it must stay allowed. A bare `unset` in command position
    is the subcommand (and would be a write), which is the fail-closed reading
    and is asserted in the corpus above — the two are told apart by where the
    word sits, which is the whole point.
    """
    for cmd in ("git config get", "git config --get unset", "git config --get get",
                "git config list"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} only reads but was refused: {reason!r}"


def test_a_wrapped_write_is_still_blocked() -> None:
    """The recursion through `sh -c` must reach the config rule too."""
    allowed, reason, _ = _check_sandbox(
        "sh -c 'git config --unset foo.bar'", "read-only")
    assert allowed is False, f"a nested write was allowed: {reason!r}"


def test_the_lists_are_not_vacuous() -> None:
    """The two-directional corpus above is only meaningful if the tables are real."""
    assert _GIT_CONFIG_WRITE_FLAGS
    assert _GIT_CONFIG_WRITE_SUBCOMMANDS
    assert _GIT_CONFIG_READ_SUBCOMMANDS
    # The spellings this work was reported for must be in the tables, so a later
    # "cleanup" that drops one is red here rather than silently permissive.
    for flag in ("--unset", "--unset-all", "--edit", "-e", "--remove-section"):
        assert flag in _GIT_CONFIG_WRITE_FLAGS, flag
    for sub in ("unset", "edit", "set"):
        assert sub in _GIT_CONFIG_WRITE_SUBCOMMANDS, sub
    assert "get" in _GIT_CONFIG_READ_SUBCOMMANDS
