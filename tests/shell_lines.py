r"""How a shell reads a fenced `bash` block: which lines are one command.

`DEVELOPMENT.md` and the task templates show commands a reader is meant to **run**, and
several guards in this suite read those blocks to ask a question about the command — does
it spell the flags it needs, does it name its head, does it invoke the tool it claims to.
Every one of them needs the same first step: split the block into *commands*, which is not
the same as splitting it into lines. This module is that step, in one place.

Why it is a module and not a line of each guard
-----------------------------------------------

The rule is one sentence and every hand-written version of it in this repository has been
wrong in the same direction:

    a line continues onto the next **iff its run of trailing backslashes is odd**

`line.rstrip().endswith("\\")` — the spelling two readers used — answers "is there a
backslash at the end", which is `True` for a run of **two**. In a shell that run is an
*escaped backslash*: the newline is not escaped and the command ends there. So the wrong
rule reads a block `a \\` / `b \\` / `c` as one command when the shell runs **three** —
and it reads it the same way as the correct `a \` / `b \` / `c`, which the shell joins.
Measured 2026-10-03 (`cyc20261003-194054`) against the two readers that carried it:

* `tests/test_run_mutation_arm.py::_documented_invocation` answered "3 lines, all five
  flags present" for **both** shapes, so its guard passed on a documented invocation that
  runs as three separate commands — the tool receiving no arguments at all. That is the
  defect `DEVELOPMENT.md` was fixed for one PR earlier (`cyc20261003-094115`), and the
  guard written beside the fix could not see it.
* `tests/test_prompt_templates.py::_create_calls` joined the same two shapes identically.

Two readers with one wrong rule is the shape this repository keeps re-learning: the rule
belongs in one home, and the home states the byte it is about.

The two shapes that are *not* continuations, and are the reason the count is not
`rstrip()`ed first
-------------------------------------------------------------------------------

* **An even run.** `x \\` — the backslash escapes its neighbour, the newline stands.
* **A backslash with anything after it.** `x \ ` — the backslash escapes the **space**,
  so the line is not continued either. `line.rstrip()` deletes that space and hands the
  rule a line that ends in a backslash, which is how a reader acquires a continuation the
  shell never performed. Counting from the raw line makes both cases fall out of the same
  loop: the last character is not `\\`, so the run is 0, which is even.
"""

from __future__ import annotations

BACKSLASH = "\\"


def trailing_backslashes(line: str) -> int:
    """The length of the run of backslashes that ends `line`, or 0 if it does not end in one.

    The line is read **as the file has it** — no `rstrip()`, no `strip()`. Whether a shell
    continues a line is decided by its last character, and whitespace after the backslash
    changes which character that is (`x \\ ` escapes a space and continues nothing).
    """
    count = 0
    for char in reversed(line):
        if char != BACKSLASH:
            break
        count += 1
    return count


def continues(line: str) -> bool:
    """Does the shell read `line` and its successor as one command?

    An **odd** run escapes the newline, so the next line is part of the same command; an
    even run is a whole number of escaped backslashes and the command ends here.
    """
    return trailing_backslashes(line) % 2 == 1


def commands(lines: list[str]) -> list[list[str]]:
    """Split a block into commands, each a list of the lines the shell reads as one.

    The mirror of `continues` for a whole block, so a caller that wants every command — not
    only the one that names some tool — does not re-derive the grouping and drift from this
    file. A trailing continuation with nothing after it (a block that ends mid-command) puts
    its last line in its own group rather than losing it: an unreadable input is reported by
    what it contains, not by becoming an absence.
    """
    out: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if not continues(line):
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out
