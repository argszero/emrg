"""Every count a prompt template states about its own list is measured against that list.

Why this is one guard and not four
----------------------------------
The same defect has now been fixed three times, each time as a hand-written pin for one section:
the promote template's reflection heading (`#2576`), the competition template's gate lines, and the
competition closing summary's two counts (`#2587`). Each pin was correct and each covered one
sentence, so the fourth section written in the same shape would have had nothing watching it until
somebody happened to read the template and notice.

This module walks **every** prompt template instead, and compares each count against the list that
follows it. A new section is covered the moment it is written; nothing here knows any section's
name, its subject, or its numbers.

The shape it measures
---------------------
A paragraph that states a count of list-like things **and ends with a colon** introduces the block
after it, and that block's item count must equal the stated count. Item forms it understands: a
marker list (`1.` or `-`) and a table, whose header row is not an item.

What it deliberately does not measure, and why
----------------------------------------------
The cheaper rules were measured before this one was chosen, and both read red on a tree that is
correct:

* *any list later in the same section* — 9 claims, of which 2 are false reds. `ios_app` says "Two
  things happen in this phase, in this order: the second …, then the submission" and enumerates
  inline; `journal` says "the six Heilmeier questions", a set defined in another document. Neither
  introduces the list that happens to follow it.
* *any prose paragraph directly followed by a list* — still 2 false reds, for the same two reasons.

A false red is how a guard gets weakened or deleted, so the introducer — a paragraph ending in a
colon — is the decidable subset: it is what "the list this sentence introduces" actually means in
prose. The cost is stated rather than hidden: a count whose paragraph is not colon-terminated is not
measured here, and the promote template's "these 9 questions" is one of those, which is why it keeps
its own pin in `tests/test_prompt_templates.py`.

A colon-terminated count with **no list after it** is also not a failure, and that was measured
rather than assumed: exactly one claim in today's templates has that shape — `ios_app`'s "the loop is
six phases …", which introduces its six phases as a fenced code block. A code block is free-form, so
counting the "items" inside it is not decidable, and calling that claim a defect would be a false red
on a template that is correct. The guard measures the lists it can read and says so.

Count items, never lines
------------------------
`paper_prompt.md` wraps one of its seven questions onto an indented continuation line. Counting the
block's lines reported eight items against a claim of seven — a red on a template that is correct.
Items are counted by their marker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "emrg" / "server"

#: Spelled-out counts, plus digits, which one template already uses ("these 9 questions").
_WORD_COUNTS = {
    word: number
    for number, word in enumerate(
        "two three four five six seven eight nine ten eleven twelve".split(), start=2
    )
}

#: The nouns a count is stated about. `more` is separate: "Three more the host asked for by name"
#: states a count without repeating the noun its previous sentence supplied.
_NOUNS = (
    r"(?:questions?|rules?|steps?|lines?|checks?|phases?|items?|stages?|parts?|things?"
    r"|columns?|kinds?|reasons?|ways?|gates?)"
)

_CLAIM = re.compile(
    rf"\b({'|'.join(_WORD_COUNTS)}|\d{{1,2}})\s+(?:(?:\w+)\s+)?(?:{_NOUNS}|more)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{2,4} ", re.MULTILINE)
_MARKER_ITEM = re.compile(r"^(?:\d+\.|-) ")
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")

#: How many claims the tree must yield before a clean walk means anything. Measured today: 8 across
#: five templates. A floor rather than an equality, because a template may legitimately gain one; the
#: point is that a renamed file or a rewritten heading cannot turn this guard into a walk over nothing.
_MIN_CLAIMS_MEASURED = 5


def _as_count(token: str) -> int:
    """A stated count as an int, refusing a token this guard cannot read."""
    if token.isdigit():
        return int(token)
    try:
        return _WORD_COUNTS[token.lower()]
    except KeyError:  # pragma: no cover - the regex only offers readable tokens
        raise AssertionError(f"unreadable count {token!r}") from None


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for every heading-delimited section, so a count stays in its own section.

    The text before the first heading is a section of its own. Dropping it would leave anything a
    template says above its first `##` unmeasured, and a guard that silently skips the top of every
    file is the same defect as one that skips the bottom.
    """
    parts = _HEADING.split(text)
    bodies = [parts[0]] + parts[1:]
    headings = ["(preamble)"] + [h.strip() for h in _HEADING.findall(text)]
    return list(zip(headings, bodies))


def _introduces(line: str) -> bool:
    """Whether a paragraph's last line is an introducer: it ends with a colon.

    The emphasis markers are stripped first. `**Three rules the round owes:**` ends with `*`, not
    `:`, so a plain `endswith(":")` reads the most common introducer shape in these templates as
    "not an introducer" and measures nothing at all — a guard that skips its own subject.
    """
    return re.sub(r"[*_`\s]+$", "", line).endswith(":")


def _blocks(body: str) -> list[list[str]]:
    """Paragraphs: runs of non-blank lines, with blank lines as the separator."""
    out: list[list[str]] = []
    current: list[str] = []
    for line in body.split("\n"):
        if line.strip() == "":
            if current:
                out.append(current)
                current = []
        else:
            current.append(line)
    if current:
        out.append(current)
    return out


def _items(block: list[str]) -> list[str] | None:
    """The items of the block, or `None` when the block is not a list this guard measures."""
    if _MARKER_ITEM.match(block[0]):
        return [line for line in block if _MARKER_ITEM.match(line)]
    if block[0].lstrip().startswith("|"):
        rows = [line for line in block if line.lstrip().startswith("|")]
        rows = [line for line in rows if not _TABLE_SEPARATOR.match(line.strip())]
        return rows[1:]  # a table's first row names the columns; it is not an item
    return None


def _claims(paragraph: str) -> list[tuple[int, str]]:
    """(stated count, the matched text) for every count claim in one paragraph."""
    return [(_as_count(m.group(1)), m.group(0)) for m in _CLAIM.finditer(paragraph)]


def _measure(templates: list[Path]) -> tuple[list[str], int]:
    """Every mismatch across the templates, and how many claims were actually measured."""
    findings: list[str] = []
    measured = 0
    for path in templates:
        for heading, body in _sections(path.read_text(encoding="utf-8")):
            blocks = _blocks(body)
            for i, paragraph in enumerate(blocks):
                if _MARKER_ITEM.match(paragraph[0]):
                    continue  # a list item is not an introducer
                if not _introduces(paragraph[-1]):
                    continue
                if _items(paragraph) is not None:
                    continue
                claims = _claims("\n".join(paragraph))
                if not claims:
                    continue
                following = blocks[i + 1] if i + 1 < len(blocks) else None
                if following is None:
                    continue
                items = _items(following)
                if items is None:
                    continue
                for stated, text in claims:
                    measured += 1
                    if len(items) != stated:
                        findings.append(
                            f"{path.name} [{heading}]: {text!r} states {stated}, and the list it "
                            f"introduces holds {len(items)} item(s): {items!r}"
                        )
    return findings, measured


def _templates(directory: Path = _TEMPLATE_DIR) -> list[Path]:
    """The templates to walk, refusing an empty directory: a clean walk over nothing is not a pass."""
    found = sorted(directory.glob("*_prompt.md"))
    assert found, (
        f"no prompt templates under {directory} — a clean walk over no files is not a pass, so this "
        "refuses rather than reporting one"
    )
    return found


def test_every_count_a_template_states_matches_the_list_it_introduces() -> None:
    """The one reading: walk every template, measure every count against its own list."""
    findings, measured = _measure(_templates())

    assert measured >= _MIN_CLAIMS_MEASURED, (
        f"only {measured} count claim(s) were measured across {len(_templates())} template(s); this "
        f"guard expects at least {_MIN_CLAIMS_MEASURED}. A walk that measures almost nothing passes "
        "for the wrong reason, so the floor is asserted rather than assumed"
    )
    assert not findings, (
        "a template states a count its own list does not match:\n  "
        + "\n  ".join(findings)
        + "\n\nThe sentence is what a round reads before it starts answering, so the list and the "
        "count move together: correct whichever of the two is wrong."
    )


def test_the_count_walker_measures_a_list_and_not_the_rest_of_the_section() -> None:
    """The arms, on synthetic text — the live tree is correct, so it cannot supply a failure.

    Each arm is one of the mistakes this guard's own construction made and had to be corrected for,
    kept here so a later change to the reader has to keep clearing them.
    """
    good = "**Three rules the round owes:**\n\n1. **A**\n2. **B**\n3. **C**\n"
    findings, measured = _measure_text(good)
    assert measured == 1 and not findings, "a correct count and a matching list must read clean"

    # a list grows and the sentence does not
    grown = good.replace("3. **C**", "3. **C**\n4. **D**")
    findings, _ = _measure_text(grown)
    assert findings and "states 3" in findings[0] and "holds 4" in findings[0]

    # the sentence grows and the list does not
    claims_more = good.replace("Three rules", "Four rules")
    findings, _ = _measure_text(claims_more)
    assert findings and "states 4" in findings[0] and "holds 3" in findings[0]

    # a continuation line inside an item is not another item — the paper template's own wrapping
    wrapped = "**Two rules the round owes:**\n\n1. **A**\n   continued under A\n2. **B**\n"
    findings, measured = _measure_text(wrapped)
    assert measured == 1 and not findings, (
        "an indented continuation line was counted as an item, so a correct list reads red"
    )

    # a table's body counts; its header does not
    table = (
        "**Three lines the summary owes:**\n\n"
        "| Line | The question |\n|---|---|\n| **A** | a |\n| **B** | b |\n| **C** | c |\n"
    )
    findings, measured = _measure_text(table)
    assert measured == 1 and not findings, "a table body is a list, counted without its header row"

    # a digit states a count exactly as a word does
    digits = good.replace("Three rules", "3 rules")
    findings, measured = _measure_text(digits)
    assert measured == 1 and not findings

    # a claim with no colon introducer is out of scope, and stays silent rather than guessing
    no_colon = "**Three rules the round owes.**\n\n1. **A**\n2. **B**\n3. **C**\n4. **D**\n"
    findings, measured = _measure_text(no_colon)
    assert measured == 0 and not findings, (
        "a paragraph that does not end in a colon is not an introducer; measuring it anyway is how "
        "the two false reds this guard was rewritten to avoid came back"
    )

    # a count of something defined elsewhere is not a list claim either
    inline = "It must answer the six questions below, two at a time: the first then the second.\n\n- **A**\n"
    findings, measured = _measure_text(inline)
    assert measured == 0 and not findings

    # a colon-introduced count with no list after it is not a failure: the one that exists today
    # introduces its items as a fenced code block, which is free-form and cannot be counted
    fenced = "**The loop is six phases:**\n\n```\nphase one\nphase two\n```\n"
    findings, measured = _measure_text(fenced)
    assert measured == 0 and not findings, (
        "a claim introducing a code block was counted as a defect; the template that does this is "
        "correct, so the guard must stay silent rather than guess at a fence's items"
    )


def _measure_text(text: str) -> tuple[list[str], int]:
    """`_measure` over a single synthetic document, without touching the real templates."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic_prompt.md"
        path.write_text(text, encoding="utf-8")
        return _measure([path])


def test_the_walker_refuses_a_directory_with_no_templates(tmp_path: Path) -> None:
    """A clean walk over nothing is not a pass, so the empty case refuses."""
    with pytest.raises(AssertionError, match="clean walk over no files is not a pass"):
        _templates(tmp_path)
