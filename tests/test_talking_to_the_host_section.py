"""The four rules for addressing the host must be the first thing the prompt says.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the one prompt **every** session is rendered under. Before this
change the rules for how a reply addresses the host were scattered: two of them were bullets of
`## Writing`, and the header's own sentence carried a third in half a clause. `## Writing` is about
text that leaves the reply and reaches someone else — a subtitle, a pull request, a slide — so the
rules a host conversation needs were filed under a subject that is not its own, in a position after
nine sections about tools, memory and sessions.

The host asked for them to be stated as the basic requirement of talking to them, and to be the
first `##` section of the file. This file pins that placement and that wording.

What is asserted
----------------
Four questions, each of which fails on its own:

1. **Is the section first?** The first `## ` heading of the file, top to bottom, is
   `## Talking to the host` — position is the claim, so a section present but filed at the end is
   the defect this reads for.
2. **Are the four rules there, verbatim?** Each of the four bullets is a substring of that section,
   character for character, once. A reworded rule is a different rule, and this is the copy a
   reviewer approved.
3. **Are the statements they absorb gone?** The three retired statements (`Be concise, direct and
   helpful` in the header's sentence, `Lead with the point`, and the `resolves to nothing` clause)
   appear nowhere in the file. Leaving one behind is the duplicate the change exists to remove, and
   it costs prompt on every round.
4. **Does the reader receive it?** The rendered prompt — not the file — carries the section first
   and the four rules verbatim. `system.j2` is a Jinja2 template: `{# … #}` or `{% if %}` removes a
   block from the render while the file keeps every character of it, so the file leg alone would
   pass on a prompt no session ever sees.

Named limit
-----------
This pins the placement and the wording of the four rules in the artifact a session receives, not
the shape of any reply written under it. No test can read a message and decide whether its first
sentence was the conclusion, or whether a term was one the host already had. What it keeps true is
that the instruction is there, first, and stated once — a future session that reads it and buries
the point anyway is a model-behaviour failure, and not one this file can see.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section these rules own, as its heading line.
TALKING_HEADING = "## Talking to the host"

#: The section that owns how text leaving the reply reads. It keeps its own rules, and the two
#: sections must not become copies of each other — the exemption for internal records lives there.
WRITING_HEADING = "## Writing"

#: The four rules, verbatim as the prompt states them. The bold lead of each is part of the copy:
#: it is what makes a bullet skimmable in a prompt that is read every round.
THE_FOUR = (
    "**Say it in words the host already has.** No term that exists only in your own notes or "
    "records, and no shorthand you coined: name the thing in the host's terms, and spell a short "
    "form's full name the first time it appears.",
    "**Say only what the host needs.** One sentence where one sentence is enough. A caveat that "
    "changes no decision is not worth its line.",
    "**Make the order carry the argument.** The conclusion first, then the evidence, then where "
    "that evidence comes from — a file and a line, a command and its output, or the record the "
    "claim rests on.",
    "**Put the point where it is seen.** Never bury the answer in the middle of a report. If the "
    "reply is one line, that line is the conclusion.",
)

#: Statements the four rules absorb, and the reason each must be absent: two of them said the same
#: thing in a second place, and the third was half a clause inside the identity sentence. A prompt
#: that states a rule twice pays for it on every round and leaves the two copies free to drift.
RETIRED = (
    "Be concise, direct and helpful",
    "Lead with the point",
    "resolves to nothing",
    "The host reads only your reply",
)

#: Rules `## Writing` owns. They are asserted here only to prove the deletion took the duplicates
#: and not the section's own subject — the copy register and the language policy are pinned in
#: their own files (`test_copy_register_rule_reach.py`, `test_language_policy_reach.py`).
WRITING_KEEPS = (
    "Outward-facing GitHub text is English",
    "Internal records keep their own language",
)


def _h2_headings(text: str) -> list[str]:
    """Every `## ` heading line, in the order the file states them."""
    return re.findall(r"^## .*$", text, re.MULTILINE)


def _section(text: str, heading: str) -> str:
    """The region `heading` owns: from that line to the next `## ` heading or Jinja line.

    A `{% … %}` control line ends the region too: what follows it is not this section's prose, and
    the template puts its section-wide guard between two headings. Returns ``""`` when the heading
    is absent, so a missing section reads as a failure to measure rather than as an empty block
    that passes.
    """
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    ends = [i for i in (rest.find("\n## "), rest.find("\n{%")) if i != -1]
    return rest if not ends else rest[: min(ends)]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own Jinja environment.

    `_get_jinja_env` is what `_build_system_prompt` uses; a fresh `Environment` could differ in
    `trim_blocks` / `lstrip_blocks` or in the loader path, and the subject under test would then be
    a prompt nobody receives. The context is minimal on purpose: these rules sit outside every
    conditional, so no context key can remove them.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_section_is_the_first_h2_of_the_prompt() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    headings = _h2_headings(text)
    assert headings, "system.j2 must carry `## ` sections; none was found"
    assert headings[0] == TALKING_HEADING, (
        f"the first `## ` section of the prompt must be {TALKING_HEADING!r}; it is {headings[0]!r}. "
        "Position is the claim: how the reply reads belongs before what the reply is about"
    )
    assert headings.count(TALKING_HEADING) == 1, (
        f"the section must be stated once; found {headings.count(TALKING_HEADING)} copies"
    )


def test_the_four_rules_are_verbatim() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _section(text, TALKING_HEADING)
    assert block, f"the prompt must carry a {TALKING_HEADING!r} section"
    for rule in THE_FOUR:
        assert rule in block, (
            f"the section must state this rule verbatim; it does not:\n{rule}"
        )
        assert text.count(rule) == 1, (
            f"this rule must appear once in the prompt; found {text.count(rule)} copies:\n{rule}"
        )


def test_the_statements_the_four_rules_absorb_are_gone() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    first_line = text.splitlines()[0]
    writing = _section(text, WRITING_HEADING)
    for retired in RETIRED:
        assert retired not in text, (
            f"the prompt still carries {retired!r}; the four rules state it, and a second copy is the "
            "duplication this change removes"
        )
        assert retired not in first_line, (
            f"the header sentence still carries {retired!r}"
        )
        assert retired not in writing, (
            f"`## Writing` still carries {retired!r}"
        )


def test_the_writing_section_kept_its_own_subject() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    writing = _section(text, WRITING_HEADING)
    assert writing, (
        "`## Writing` must still exist — the rules that left it were moved to "
        f"{TALKING_HEADING!r}, and its own subject (text that leaves the reply) was not"
    )
    missing = [term for term in WRITING_KEEPS if term not in text]
    assert not missing, (
        f"the prompt lost rules `## Writing` owns; missing: {missing}"
    )
    assert "**" in writing, "`## Writing` must keep its bold-lead bullet shape"


def test_the_reader_receives_the_section_first() -> None:
    rendered = _render_system_prompt()
    headings = _h2_headings(rendered)
    assert headings and headings[0] == TALKING_HEADING, (
        "the rendered prompt must open its `## ` sections with "
        f"{TALKING_HEADING!r}; it opens with {headings[0] if headings else None!r}"
    )
    block = _section(rendered, TALKING_HEADING)
    assert block, (
        "the section is in system.j2 but absent from the render a session receives"
    )
    missing = [rule for rule in THE_FOUR if rule not in block]
    assert not missing, (
        "the rendered section does not carry every rule verbatim — something in the render path "
        f"edited it; missing: {missing}"
    )
    for retired in RETIRED:
        assert retired not in rendered, (
            f"the rendered prompt still carries {retired!r}"
        )


def test_the_instrument_reports_absence() -> None:
    """The control: a prompt without the section must read as missing, not as healthy.

    A check that answers the same thing whatever it is given is not a check. This feeds the
    extractors a prompt with no such section, one that carries it last, and one that carries the
    heading twice, and requires each defect to be visible.
    """
    absent = "# A prompt with no talking section\n\n## Where you are\n\n- Must push\n"
    assert _section(absent, TALKING_HEADING) == ""
    assert _h2_headings(absent)[0] == "## Where you are"

    last = f"## Where you are\n\n- Must push\n\n{TALKING_HEADING}\n\n{THE_FOUR[0]}\n"
    assert _h2_headings(last)[0] != TALKING_HEADING, "a trailing section must not read as first"

    doubled = f"{TALKING_HEADING}\n\n{TALKING_HEADING}\n\n- Must push\n"
    assert _h2_headings(doubled).count(TALKING_HEADING) == 2

    partial = f"{TALKING_HEADING}\n\n{THE_FOUR[1]}\n"
    assert _section(partial, TALKING_HEADING).strip() == THE_FOUR[1]
    assert [rule for rule in THE_FOUR if rule not in partial] == [
        THE_FOUR[0], THE_FOUR[2], THE_FOUR[3]
    ]
