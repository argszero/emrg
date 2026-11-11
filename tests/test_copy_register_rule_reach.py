"""Copy the host will pass on is written in spoken register, and the numbers stay exact.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the one prompt **every** session is rendered under. Its `## Writing`
section carried a rule about **vocabulary** — do not use a term only your records understand — and
nothing about **register**: the rhythm and stance of a sentence built from entirely ordinary words. A
line passes the vocabulary rule and still reads as machine-written, and that is the class this rule is
about.

It surfaced the way a carrier gap does: the rule lived in one project's memory, because that project's
copy had been rejected on exactly this ground. The information in the rejected lines was right; the
cadence was not. Copy for the host to read and pass on — a subtitle, a slide, page text, a delivery
note — is the class, and it is not one project's business.

Why the counterweight is pinned with the rule
---------------------------------------------
"Write it the way a person says it" is, read alone, an instruction to relax — and a session that
relaxes the wrong thing will round a measured value or write "about" in front of a figure to make the
sentence flow. That trades a defect in tone for a defect in truth, and the second is worse: a
colloquial sentence stays checkable, a rounded number stops being. So the rule and the clause that pins
the numbers are asserted **as a pair**, and a rewrite that keeps the pleasant half reddens with the
reason rather than passing as a wording change.

What is asserted
----------------
1. The rule is in `system.j2` and **in the render**, and the section states it once — a bullet in the
   file that no render contains is not a rule any session runs under.
2. It carries each part of the ruling: the class of text, the spoken register, the concrete action in
   place of the summary, and the refusal of symmetry built for its own sake.
3. The rule and the numbers-stay-exact clause are pinned together, in both the file and the render.
4. It is a bullet of `## Writing` — not a `## ` section of its own — because that section already owns
   how text reads, and a second heading on the same subject is the split the section's own guard
   refuses.

Named limits
------------
This pins the *presence* and *placement* of the rule in the artifact a session receives, not obedience
to it — no test can read a sentence and decide whether it sounds machine-written. What it keeps true is
that a session is told, in the prompt it runs under, which register its produced copy is written in, and
that it may not pay for that register with a number.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section that owns how text reads. The rule is a bullet of it, never a heading of its own.
WRITING_HEADING = "## Writing"

#: Load-bearing terms, each a verbatim substring of the shipped wording, so this list cannot drift away
#: from the sentence it checks without this file saying so. They cover the class of text, the register,
#: the concrete action in place of the summary, and the refusal of symmetry for its own sake.
RULE_TERMS = (
    "written the way a person says it",          # the rule, in one phrase
    "A subtitle, a slide, a line of page text, a delivery note",  # the class of text it binds
    "spoken register, not document register",    # the axis: register, not vocabulary
    "the concrete action instead of the summary of it",  # the repair
    "a balanced pair of phrases built for symmetry",     # the failure it names
)

#: The counterweight. Asserted on its own because it is the half a well-meaning rewrite drops: a rule
#: that asks only for a relaxed tone licenses a rounded number, and a rounded number is a worse defect
#: than an awkward sentence.
NUMBERS_TERMS = (
    "The register changes; the numbers do not",
    "stays exact inside a colloquial sentence",
    "rounding it to fit the tone makes the sentence wrong rather than plain",
)


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` heading or EOF.

    Returns ``""`` when the heading is absent — the caller asserts, so a missing block reads as a
    failure to measure rather than as a pass.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _missing_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this text does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in text]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ in
    `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would then be a
    prompt nobody receives.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_register_rule() -> None:
    """The source leg: does the artifact that ships state it?"""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, WRITING_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the `## Writing` section — it is the only prompt "
        "every session is rendered under"
    )
    missing = _missing_terms(block, RULE_TERMS)
    assert not missing, (
        f"the writing section must state the class of text, the register, the repair and the failure; "
        f"missing: {missing}"
    )


def test_the_rendered_prompt_carries_the_rule_verbatim() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    A Jinja construct that wrapped the bullet (`{# … #}`) leaves every term in the file, so the
    comparison is the **block**, verbatim — the wording a session reads, not the wording the file holds.
    """
    rendered = _render_system_prompt()
    source_block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), WRITING_HEADING)
    assert source_block, "system.j2 has no writing section to compare the render against"
    assert source_block in rendered, (
        "the rendered session prompt carries a *different* writing section than the template does — "
        "something in the render path edited it"
    )
    missing = _missing_terms(_block_after(rendered, WRITING_HEADING), RULE_TERMS + NUMBERS_TERMS)
    assert not missing, f"the rendered writing section is missing terms: {missing}"


def test_the_rule_and_the_numbers_are_pinned_together() -> None:
    """Both halves, because either one alone is a defect in the other direction.

    The rule without its counterweight licenses a rounded number to make a sentence flow, and a
    rounded number stops being checkable — the defect the register rule exists to avoid is a defect in
    tone, which is the smaller one. This asserts the pair explicitly so a rewrite that keeps only the
    pleasant half fails here with the reason.
    """
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), WRITING_HEADING)
    for term in NUMBERS_TERMS:
        assert term in block, (
            f"the writing section no longer pins the numbers ({term!r}) — read alone, 'write it the "
            "way a person says it' is an instruction to relax, and the first thing a relaxed session "
            "relaxes is a measured value"
        )
    assert "spoken register" in block, (
        "the numbers clause survives but the rule it counterweights is gone — a clause about exactness "
        "with nothing asking for plain register is the state this rule replaced"
    )


def test_the_rule_is_a_bullet_of_the_writing_section() -> None:
    """Placement, because a second heading on one subject is the split that section refuses.

    `## Writing` already owns how text reads, and `tests/test_plain_language_rule_reach.py` asserts the
    heading appears exactly once. A `## ` section of its own here would either duplicate that heading or
    scatter one subject across two of them.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    assert headings.count(WRITING_HEADING) == 1, (
        f"`{WRITING_HEADING}` appears {headings.count(WRITING_HEADING)} times: two copies are free to "
        "drift apart, and one of them would be the register rule's"
    )
    register_headings = [h for h in headings if "register" in h.lower() or "copy" in h.lower()]
    assert not register_headings, (
        f"the register rule became a section of its own ({register_headings}): it is one bullet of the "
        "section that already owns how text reads"
    )
    block = _block_after(text, WRITING_HEADING)
    assert "- **" in block and "written the way a person says it" in block, (
        "the rule must be a bullet of `## Writing`, in the section's own bold-lead shape"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the rule must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the
    extractors a prompt with no such section, then a section carrying one term, and requires exactly
    the missing terms to be named.
    """
    sample = "# A prompt with no writing section\n\n- Must push\n"
    assert _block_after(sample, WRITING_HEADING) == ""
    assert _missing_terms(sample, RULE_TERMS) == list(RULE_TERMS)
    partial = f"{WRITING_HEADING}\n\n- written the way a person says it\n"
    assert "written the way a person says it" not in _missing_terms(partial, RULE_TERMS)
    assert len(_missing_terms(partial, RULE_TERMS)) == len(RULE_TERMS) - 1
    # And the counterweight's own control, so "both halves are pinned" is not a claim about a matcher
    # that returns a constant: a block carrying the rule and none of the numbers terms reports all of
    # them missing.
    assert _missing_terms(partial, NUMBERS_TERMS) == list(NUMBERS_TERMS)
