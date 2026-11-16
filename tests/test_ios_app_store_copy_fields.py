"""The ios-app template must say what each store field is for, not only that copy must match.

Why this file exists
--------------------
`emrg/server/ios_app_prompt.md` §5.2 already carries the invariant that everything the copy claims
has to be shown by the build it describes, and §5.3 carries which fields the platform freezes and
when. Neither says **what each field a submission writes is for**, so a round that found some
sentences false went first to the review notes — the one surface a reviewer reads, and the store
page is the one everybody reads.

The gap this file pins is the missing half of the rule the template does carry: the invariant says
copy must match the build, and nothing says which field carries which part of it, or that a change
to the *shape* of the build (a list, a page count, who is allowed to do what) moves every field at
once rather than the notes alone.

What is asserted
----------------
Two paragraphs and their load-bearing sentences, in §5.2:

1. **a job per field** — description, promotional text, subtitle, review notes, and the
   privacy/support pages, each with what it is for and what is not its job;
2. **the shape-change rule** — stated as the consequence of the invariant §5.2 already carries, and
   saying the notes are corrected last rather than only.

The last leg is the control: a document without these sentences must read as missing them, so the
instrument is known to be reading rather than answering the healthy answer whatever it is given.

Named limit
-----------
This pins that the template states the division of labour between fields and the shape-change rule.
It cannot read a future round's submission and decide whether the description it wrote was about the
product or about its parameters. What it keeps true is that the round was told.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "ios_app_prompt.md"

#: The subsection that owns the store page.
STORE_HEADING = "#### 5.2 The store page is where a small app is found"

#: The sentences this rule depends on, each a verbatim substring of the shipped wording. They cover
#: the division of labour (one per field) and the consequence of the build changing shape, so a
#: rewrite that keeps one of the two halves fails here rather than passing as a wording change.
REQUIRED = (
    "Each field the page publishes has one job",                    # the division of labour, stated
    "the parameters \u2014 how many per page, how far the list scrolls \u2014 are not a selling point",
    "The promotional text answers one question only",                # promotional text's job
    "The subtitle compresses that sentence into the single line",     # subtitle's job
    "The review notes are not copy at all",                          # notes are facts, not copy
    "The privacy and support pages make claims like every other page",  # pages are claims too
    "A change to the shape of the build moves every field that describes it",   # the consequence
    "the notes are corrected last rather than only",                  # which field is not the first
)


def _section(text: str, heading: str) -> str:
    """The region `heading` owns: from that line to the next heading of the same or a higher level.

    Returns ``""`` when the heading is absent, so a missing subsection reads as a failure to measure
    rather than as an empty block that passes.
    """
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    end = re.search(r"^#{1,4} ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _missing(text: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this text does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in text]


def test_the_store_section_names_a_job_per_field() -> None:
    section = _section(TEMPLATE.read_text(encoding="utf-8"), STORE_HEADING)
    assert section, (
        f"the template must carry {STORE_HEADING!r}; it is the subsection that owns the store page, "
        "and the rule about its fields has nowhere else to live"
    )
    missing = _missing(section, REQUIRED)
    assert not missing, (
        f"§5.2 must state the division of labour between the store fields and the shape-change rule; "
        f"missing: {missing}"
    )


def test_the_rule_is_stated_once() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.count("Each field the page publishes has one job") == 1, (
        "the division of labour must be stated once; a second copy is free to drift from this one"
    )
    assert text.count("A change to the shape of the build moves every field that describes it") == 1


def test_the_rule_sits_beside_the_invariant_it_completes() -> None:
    """The shape-change rule is the consequence of the invariant, so it lives where that is."""
    section = _section(TEMPLATE.read_text(encoding="utf-8"), STORE_HEADING)
    assert "shown by the build it describes" in section, (
        "§5.2 must keep the invariant this rule completes \u2014 the copy has to be shown by the build "
        "it describes; without it the shape-change paragraph is an orphan instruction"
    )


def test_the_platform_mechanics_stay_in_their_own_subsection() -> None:
    """§5.3 owns the refusals. A copy rule that restates them puts one fact in two places."""
    text = TEMPLATE.read_text(encoding="utf-8")
    section = _section(text, STORE_HEADING)
    for mechanic in ("409 STATE_ERROR", "reviewSubmissions", "422"):
        assert mechanic not in section, (
            f"§5.2 must not restate the platform mechanics ({mechanic}); §5.3 owns them, and a "
            "second copy is the drift this repository keeps paying for"
        )


def test_the_instrument_reports_absence() -> None:
    """The control: a text without the rule must read as missing, not as healthy."""
    sample = f"{STORE_HEADING}\n\nAt low install volume the store page is where an app is found.\n"
    section = _section(sample, STORE_HEADING)
    assert section, "the extractor must find a section that is present"
    assert _missing(section, REQUIRED) == list(REQUIRED), (
        "a section carrying none of the sentences must report every one of them missing"
    )
    assert _section("#### 5.1 Something else\n\n- text\n", STORE_HEADING) == ""
