"""Two descriptions of the tree-health gate, written outside its own file, claimed a family.

`scripts/check-merge-tree-health.py` judges every merge by exactly one guard
(`scripts/check-doc-count.py`, its `GUARD`), and its own carriers now say so - the
docstring, the exit-code contract, the summary and `--help`. Two descriptions of that
gate live in *other* files, and both still said "the guards":

* `Agent.md`'s merge-gates line - the map an agent reads before draining a queue.
* `scripts/check-merge-landing-diff.py`'s header - the map a reviewer reads to learn
  which sibling asks which question.

Measured 2026-09-25 (`cyc20260925-234325`): both named a family, while the gate runs
one guard. Fixing a tool's own prose does not reach a *second* file that describes it,
which is why these two outlived the change that corrected the gate itself - and a map
is read at exactly the moment the claim matters, when the reader is deciding what to
run and how much to trust the answer.

Why each test asserts both directions
-------------------------------------
A negative assertion ("no family claim here") passes **vacuously** if the entry it is
about is missing or renamed: an absent block contains none of the words it forbids.
So every test first requires the entry to be there and to name the gate, and only then
holds it to the singular - the shape the prompt-rule pin uses when it requires a
block's opening words before asserting what is gone from it.

The wording is not pinned
-------------------------
Only the claim is: the singular must be present and the family absent. Pinning the
sentence would forbid rewording it, and the fix here is the claim, not the phrasing.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = "check-merge-tree-health.py"
AGENT_MD = REPO_ROOT / "Agent.md"
LANDING_DIFF = REPO_ROOT / "scripts" / "check-merge-landing-diff.py"

#: The sentence that opens the gate list in the landing-diff header, and the one that
#: ends it. Taken as boundaries so the assertion is about that list and not about the
#: rest of the docstring, which uses "guards" in the ordinary sense ("the header guards
#: against") - an unscoped word-level assertion would fail on a correct file.
LIST_OPENS = "The gates in this family ask"
LIST_ENDS = "None of them answers"


def _parenthetical_after(text: str, needle: str) -> str:
    """`(what the map says this tool answers)` - the entry's claim, not its name.

    Requires the phrase before it: a map that dropped the entry would otherwise make
    the caller's negative assertion pass for the wrong reason.
    """
    after = text.split(needle, 1)
    assert len(after) == 2, f"the map does not name {needle!r} at all"
    tail = after[1].lstrip()
    assert tail.startswith("("), f"{needle!r} is named but with no claim after it: {tail[:40]!r}"
    return tail[: tail.index(")") + 1]


def _agent_md_merge_gates_line() -> str:
    lines = [
        line
        for line in AGENT_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith("- Merge gates")
    ]
    assert len(lines) == 1, f"expected exactly one merge-gates line in Agent.md, found {len(lines)}"
    return lines[0]


def test_agent_md_says_the_gate_passes_one_guard() -> None:
    """`Agent.md` is the map an agent reads first, and it claimed a family.

    The gate runs one guard, so a reader who took this line as the gate's scope would
    credit it with a breadth of checking it never does.
    """
    entry = _parenthetical_after(_agent_md_merge_gates_line(), f"`scripts/{GATE}`")

    assert "guards" not in entry, entry
    assert "one guard" in entry, entry


def test_the_landing_diff_header_says_the_gate_passes_one_guard() -> None:
    """The sibling's header is the map a reviewer reads to pick a gate; it said "the guards".

    Scoped to the gate list: this docstring also uses "guards" as a verb, and a
    word-level assertion over the whole file would forbid that correct usage.
    """
    doc = LANDING_DIFF.read_text(encoding="utf-8")
    assert LIST_OPENS in doc, "the gate list this test is about is gone from the header"
    listed = doc.split(LIST_OPENS, 1)[1].split(LIST_ENDS, 1)[0]

    assert GATE in listed, f"the list no longer names {GATE}"
    assert "guards" not in listed, listed
    assert "one guard" in listed, listed


def test_the_agent_md_entry_this_test_reads_is_the_real_one() -> None:
    """The control: the reading above must fail when the claim it forbids is present.

    Asserted on a synthetic line rather than on the file, because the file is the
    subject being held correct - a test that mutated it to prove its own reach would
    leave the tree it runs in unusable.
    """
    family_line = (
        f"- Merge gates, run before merging: `scripts/{GATE}` "
        "(the merged tree passes the guards) - all under `uv run --no-sync python3`"
    )
    entry = _parenthetical_after(family_line, f"`scripts/{GATE}`")
    assert "guards" in entry, "the control line must carry the claim the rule forbids"

    singular_line = family_line.replace("passes the guards", "passes one guard")
    assert "guards" not in _parenthetical_after(singular_line, f"`scripts/{GATE}`")

    # And a map with no entry for the gate is not read as clean: the assertion is on
    # the entry, so a missing entry has to fail rather than pass.
    import pytest

    with pytest.raises(AssertionError):
        _parenthetical_after("- Merge gates, run before merging: nothing here", f"`scripts/{GATE}`")
