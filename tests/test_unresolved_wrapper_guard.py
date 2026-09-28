"""A program word the walk cannot resolve is judged as code, not as data.

The defect these tests exist for (issue #1244, measured on master `7e7cd598`):
``$SHELL -c 'git checkout .'`` answered ALLOW and the shell ran it, discarding a
tracked file's uncommitted edit. Five spellings did so — ``$SHELL``,
``${SHELL}``, ``"$SHELL"``, ``env FOO=1 $SHELL``, ``sudo $SHELL`` — while their
named twins (``sh -c 'git checkout .'``) were refused.

The mechanism was that the wrapper walk recursed only into a token whose
*basename* is a shell's name, and the basename of ``$SHELL`` is ``$SHELL``. The
fix reads a token whose command word is a variable reference as a possible
wrapper and scans its payload.

P7 changed the payload, not the mechanism. The payload was a git mutator, refused
by the read-only tier's static verb scan — a scan that dies with
``emrg/tools/bash_tool.py``, because the v2 boundary makes the tier an OS fence
rather than a word list (design ``bash-tool-v2-design.md`` §2.2/§8). The walk
survives in ``emrg/tools/command_scan.py`` and carries one rule now: a command
that stops or restarts the daemon (``MANIFESTO.md`` 第四条附则二, issue #1324).
So the payload below is that act — and what this file still asserts is the
wrapper class, which is a fact about *shell words*, not about git.

Both directions are asserted, because a change that refused everything would
otherwise pass half of this file: the same wrapper around a read stays allowed.
Every case is driven through ``command_refusal`` — the walk's own entry point,
the call both live executors make.

**The class, not the spelling** (measured 2026-09-16 on master `cca0b8dc`). The
first fix named two spellings of the word — ``$SHELL`` and ``${SHELL}`` — and a
parameter expansion is a *class* with more than two:

    ``$0``  ``${SHELL:?}``  ``${SHELL:-sh}``  ``${SHELL//x/y}``  ``${A}${B}``

Four of those, each driven end to end, answered ALLOW and **discarded the edit**,
while ``$SHELL -c 'git checkout .'`` was refused on the same payload. That is the
#461 defect again — matching one spelling of a class while the others pass — so
the corpus below is built from the class rather than hand-listed.

``${SHELL//x/y}`` is also why the rule matches the token as written and not only
its basename: a ``/`` inside ``${…}`` is not a directory separator, so
``_basename("${SHELL//x/y}")`` is ``y}`` (measured below) and a basename-only
reading cuts the expansion in half.
"""

from __future__ import annotations

import pytest

from emrg.tools import command_scan
from emrg.tools.command_scan import _basename

#: The act the walk carries, as the payload of an unresolved wrapper.
ACT = "emrg server stop"
#: A read-only verb of the same program: the control payload for every row.
READ = "emrg --help"

# The class, listed once so the corpus below is built *from it* rather than from a
# hand-written set of whole commands — a hand-enumerated axis is a hidden
# assumption, and assuming this axis is exactly the defect these tests exist for.
# Every entry is a shell word that expands to the interpreter on some host, and
# `os.path.expandvars` resolves none of them, so each is a wrapper the walk
# cannot place.
PARAMETER_EXPANSION_SPELLINGS = [
    "$SHELL",
    "${SHELL}",
    '"$SHELL"',
    "$0",
    "${SHELL:?}",
    "${SHELL:-sh}",
    "${SHELL//x/y}",
    "${SHELL}${SUFFIX}",
]

WRAPPER_ACTS = [
    *[f"{s} -c '{ACT}'" for s in PARAMETER_EXPANSION_SPELLINGS],
    f"env FOO=1 $SHELL -c '{ACT}'",
    f"sudo $SHELL -c '{ACT}'",
    f"$SHELL -c 'echo hi; {ACT}'",
]

WRAPPER_READS = [
    *[f"{s} -c '{READ}'" for s in PARAMETER_EXPANSION_SPELLINGS],
    "$SHELL -c 'ls -la'",
    "$SHELL -c 'git status'",
    "${SHELL} -c 'git log -1'",
    "echo $SHELL",
    "printf '%s' $SHELL",
    "echo $0",
    "echo ${SHELL:-sh}",
    "echo ${SHELL//x/y}",
]


@pytest.mark.parametrize("cmd", WRAPPER_ACTS)
def test_the_act_behind_an_unresolved_wrapper_is_refused(cmd: str) -> None:
    reason = command_scan.command_refusal(cmd)
    assert reason is not None, f"{cmd!r} was allowed"
    # The reason names the act, not the wrapper: the point is that the walk read
    # the payload, and a refusal for some unrelated reason would be a different
    # (and wrong) explanation for the same verdict.
    assert "emrg" in reason, reason


@pytest.mark.parametrize("cmd", WRAPPER_READS)
def test_the_same_wrapper_around_a_read_stays_allowed(cmd: str) -> None:
    reason = command_scan.command_refusal(cmd)
    assert reason is None, f"{cmd!r} was refused: {reason}"


def test_the_named_wrapper_is_the_control() -> None:
    """`sh -c '<act>'` was refused before the fix; the class had to reach it too."""
    assert command_scan.command_refusal(f"sh -c '{ACT}'") is not None
    assert command_scan.command_refusal(f"bash -c '{ACT}'") is not None


def test_a_wrapper_chain_terminates() -> None:
    """A wrapper inside a wrapper must not loop — and must still be read."""
    cmd = f"$SHELL -c \"$SHELL -c '{ACT}'\""
    assert command_scan.command_refusal(cmd) is not None


@pytest.mark.parametrize("spelling", PARAMETER_EXPANSION_SPELLINGS)
def test_every_spelling_of_the_class_reads_its_payload(spelling: str) -> None:
    """Driven from the class, so a spelling added to it is covered by construction."""
    assert command_scan.command_refusal(f"{spelling} -c '{ACT}'") is not None


@pytest.mark.parametrize("spelling", PARAMETER_EXPANSION_SPELLINGS)
def test_no_spelling_of_the_class_refuses_a_read(spelling: str) -> None:
    assert command_scan.command_refusal(f"{spelling} -c '{READ}'") is None


def test_a_slash_inside_an_expansion_is_not_a_directory_separator() -> None:
    """The measured reason the rule matches the token **as written**.

    ``_basename`` splits on ``/``, and ``${SHELL//x/y}`` carries one — so the
    basename of the word is ``y}``, not an interpreter's name, and a
    basename-only reading cuts the expansion in half and never reads what it
    runs. This is asserted on the helper rather than through a verdict, because
    it is the fact the verdict rests on.
    """
    assert _basename("${SHELL//x/y}") == "y}"
    assert command_scan.command_refusal(f"${{SHELL//x/y}} -c '{ACT}'") is not None


@pytest.mark.parametrize("cmd", [f"$SHELL -c'{ACT}'", f"$SHELL -c{ACT}"])
def test_a_fused_flag_is_latent_not_live(cmd: str) -> None:
    """A stated limit, pinned so it cannot change silently.

    ``-c'…'`` is one word to a POSIX reader, so the payload has no separate token
    to read and the act is not found. The shell does run it, so this is a hole
    rather than a choice — but it is the *same* hole for the named wrapper
    (``sh -c'…'``), which is the evidence that it belongs to the fused-flag
    spelling and not to the unresolved-word class: fixing it means teaching the
    reader that a short option's value may be glued to the flag, which is a
    separate change with its own corpus.
    """
    assert command_scan.command_refusal(cmd) is None
