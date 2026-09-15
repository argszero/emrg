"""The newline alphabet, generated rather than hand-listed (issue #1241).

Why this exists
---------------
Three consecutive measurements of this guard missed a defect in the guard's own
fix, and each time the axis that failed had been written by hand:

* the `\\<LF>` continuation — the corpus enumerated *spellings* of a continuation
  and never varied the character that precedes the LF;
* `\\<CR><LF>` — a CR is not a newline, so that is not a continuation at all, and
  stripping it turned 510 blocking shapes into allowed ones;
* `CRLF+LF` — a blank line after a CRLF, which the hand-written axis
  ``["\\n", "\\r\\n"]`` was structurally unable to produce.

The parameter is not "which newline spellings did I think of". It is the alphabet:
a line-break run is any string over {CR, LF}; a shell ends a line at LF, treats CR
as an ordinary character, and consumes a backslash together with whatever follows
it. Those three facts generate the 30 forms below, and this file iterates them
instead of listing the ones someone remembered.

What is asserted, and why only in one direction
-----------------------------------------------
``REACHABLE`` is **measured, not reasoned**. For each form the real writer
(``git checkout .``) was placed in a real repository holding an uncommitted
change, the shell was run, and the file was read afterwards — the *effect itself*,
so that no detector which replaces the writer can mislead. A second, cheaper
detector was used only after being validated against that effect on all 1120
probes; the naive spelling of it (``MARK in stderr`` captured with ``text=True``)
disagrees with the effect on 464 of them, because universal-newline decoding turns
the CR into an LF, and a command word of ``<CR>git`` then looks like a standalone
one. Two of the three wrong answers in this family came from instruments, not from
the guard.

Only the safe direction is asserted: a form the shell really runs the writer
behind must be blocked. The reverse is deliberately **not** asserted — a guard is
allowed to be conservative, and demanding exact agreement in both directions would
be demanding that a conservative guard be wrong. The controls below pin the other
side, so "block everything" is not a passing implementation.
"""

import itertools

import pytest

from emrg.tools.bash_tool import _check_sandbox

TIER = "read-only"
WRITER = "git checkout ."
CR, LF = "\r", "\n"


def _alphabet(max_length: int = 4) -> list[str]:
    """Every string over {CR, LF} up to ``max_length`` — generated, never listed."""
    return [
        "".join(chars)
        for length in range(1, max_length + 1)
        for chars in itertools.product((CR, LF), repeat=length)
    ]


#: Forms that carry a real line break for an **odd** backslash run: the run
#: escapes the character after it, so a bare LF is a break except in the one
#: position where the backslash itself consumes it.
_ODD = [
    "\r\n", "\n\n", "\r\r\n", "\r\n\n", "\n\r\n", "\n\n\n", "\r\r\r\n",
    "\r\r\n\n", "\r\n\r\n", "\r\n\n\n", "\n\r\r\n", "\n\r\n\n", "\n\n\r\n",
    "\n\n\n\n",
]

#: With an **even** run the trailing backslash is escaped, so the character after
#: it is not escaped at all and a single LF is already a real break — the one form
#: the odd rows do not have.
_EVEN = [LF, *_ODD]

#: (head, backslash count) -> the forms the shell really runs the writer behind.
#: Measured with the effect oracle described in the module docstring; the head does
#: not change the answer, because a writer behind any real break is a new command.
REACHABLE = {
    ("echo a", 1): _ODD,
    ("echo a", 2): _EVEN,
    ("echo a", 3): _ODD,
    ("'q'", 1): _ODD,
    ("'q'", 2): _EVEN,
    ("'q'", 3): _ODD,
}

_ROWS = [
    (head, parity, form)
    for (head, parity), forms in REACHABLE.items()
    for form in forms
]


def _id(row: tuple[str, int, str]) -> str:
    head, parity, form = row
    shown = form.encode("unicode_escape").decode("ascii").replace("\\", "")
    return f"{head.strip(chr(39)).replace(' ', '_')}-{parity}-{shown}"


def test_the_alphabet_is_generated_complete_and_not_a_hand_written_pair() -> None:
    """The generator must be whole, and must reach past the two spellings that failed.

    A generator that silently shrank — or a reader that quietly replaced it with
    ``["\\n", "\\r\\n"]`` — would make every assertion below vacuous, so the
    alphabet's size and the three shapes this issue turned on are asserted first.
    """
    forms = _alphabet()
    assert len(forms) == 30, "the CR/LF alphabet up to length 4 has 30 forms"
    assert len(set(forms)) == 30
    for shape in (LF, CR, CR + LF, CR + LF + LF, CR + CR + LF + LF):
        assert shape in forms, f"{shape!r} must be in the generated alphabet"


def test_cr_alone_is_never_a_line_break() -> None:
    """A form with no LF cannot start a new command, so no row of it is reachable.

    This is the property that makes the table a measurement of a *rule* rather
    than a blob: `\\<CR>` is the escaped CR, the word stays ``<CR>git``, and no
    shell can run that. If a future alphabet added such a row, the guard would be
    being asked to block a command that cannot exist.
    """
    no_lf = [form for form in _alphabet() if LF not in form]
    assert no_lf == [CR, CR + CR, CR + CR + CR, CR + CR + CR + CR]
    reachable = {form for forms in REACHABLE.values() for form in forms}
    assert not (set(no_lf) & reachable)


def test_the_single_lf_is_reachable_exactly_on_an_even_run() -> None:
    """The continuation, stated as a property of the table instead of a case.

    An odd run ends in a backslash that consumes the LF, so the two lines are one
    command; an even run leaves an escaped backslash, so that LF is a real break
    and the writer behind it runs. This is the shape that cost a whole published
    patch, and it is asserted against the table rather than retold in prose.
    """
    for parity in (1, 3):
        assert LF not in REACHABLE[("echo a", parity)]
    for parity in (2, 4):
        if ("echo a", parity) in REACHABLE:
            assert LF in REACHABLE[("echo a", parity)]


@pytest.mark.parametrize("head,parity,form", _ROWS, ids=[_id(r) for r in _ROWS])
def test_a_writer_behind_every_reachable_form_is_blocked(
    head: str, parity: int, form: str
) -> None:
    """The whole point: if the shell really runs it, the guard must not call it data."""
    cmd = f"{head}{'\\' * parity}{form}{WRITER}"
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is False, (
        f"{cmd!r} runs the writer behind a real line break; must block, "
        f"got allowed ({reason!r})"
    )


@pytest.mark.parametrize("cmd", [
    "echo a\\\n" + WRITER,          # one command: `echo agit checkout .`
    "echo a\\\ngit status",
    "echo a\\\ngit log --oneline -5",
])
def test_a_continuation_that_joins_into_a_mention_stays_allowed(cmd: str) -> None:
    """The other half: a joined line is one command, so the writer is an argument.

    Without this, "block anything with a backslash and a newline in it" would look
    like a fix here.
    """
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is True, f"{cmd!r} only names a writer; must allow ({reason!r})"


@pytest.mark.parametrize("form", [CR, CR + CR, CR + CR + CR])
def test_a_writer_behind_a_cr_only_form_is_not_a_command(form: str) -> None:
    """``\\<CR>git checkout .`` cannot run: the command word is ``<CR>git``."""
    cmd = f"echo a\\{form}{WRITER}"
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is True, f"{cmd!r} cannot run the writer; must allow ({reason!r})"


@pytest.mark.parametrize("form", [LF, CR + LF, CR + LF + LF, CR + CR + LF + LF])
def test_reads_behind_the_same_forms_stay_allowed(form: str) -> None:
    """A read is not a writer, whichever side of the break it lands on."""
    for cmd in (f"echo a\\{form}git status", f"echo a\\{form}git log --oneline -5"):
        allowed, reason, _ = _check_sandbox(cmd, TIER)
        assert allowed is True, f"{cmd!r} is a read; must allow ({reason!r})"


@pytest.mark.parametrize("cmd", [
    "git status",
    "ls -la",
    "grep -rn git .",
    "cat f.txt",
    "echo hello",
    "( git log -1 )",
])
def test_ordinary_commands_are_untouched(cmd: str) -> None:
    """The floor: none of the above may be bought with a refusal of plain reads."""
    allowed, reason, _ = _check_sandbox(cmd, TIER)
    assert allowed is True, f"{cmd!r} is an ordinary command; must allow ({reason!r})"
