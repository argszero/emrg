"""The prompt states what the system's own survival is worth, and states it once.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the one carrier every session receives: `daemon._build_system_prompt`
renders it for a host conversation exactly as for a scheduled round. Measured before this change, its twelve
`##` sections all answer **how to do a round** — where you are, the skills you have, how you work, what to
plan before, how memory is written, how history is read, how a reply is written, how a rant is filed, where
the red lines are, and the shape of a round. None answered **what is worth doing**, and none said what the
system's continued existence is worth relative to anything else.

That gap is not a missing habit, it is a missing arbiter. A round that must choose between finishing the
small thing and the valuable one, answering and going to measure, or publishing and polishing, met a file
full of local rules with no stated priority between them, so the choice fell to whatever the round happened
to be looking at.

So the rule now sits in a `##` section of its own, immediately before `## How you work`: a reader meets what
the work is *for* before reading how the work is done.

What is asserted
----------------
1. The section is in `system.j2` **and in the render**, and exactly once in each. A block in the file that no
   render contains is a rule no session runs under, and a second copy is two copies free to drift.
2. Each of the four statements is asserted **on its own**, so an edit that deletes one is caught by a case
   naming it rather than by a longer assertion that happens to still be true.
3. **Placement**: its own `##` section, directly before `## How you work`, and not a bullet of it. Position
   is part of what the section says — among the bullets it reads as one more habit beside "read a file
   before you change it"; as its own block it is the premise the following rules are done under.
4. **No counterweight.** The section may not carry a clause that reopens what it just settled — a "however",
   an "unless", a "within reason", a "where possible". `but` is deliberately *not* in that list: the second
   statement's list of means is open-ended by construction ("includes, but is not limited to"), and that
   `but` widens the rule rather than narrowing it.
5. **Not copied anywhere else.** The first statement's sentence is looked for under `emrg/server/` — the
   task templates and the evolution prompt included — and `system.j2` must be its only carrier. A rule
   stated twice is free to disagree with itself, and the copy a reader meets first governs.

Named limits
------------
- This pins **placement and presence**, not obedience. No test can watch a round weigh two options and
  choose the one that raises survival probability; what it keeps true is that the rule is where every session
  reads it, once, unhedged.
- "No example" is not asserted. A worked example would narrow a rule that governs everything into a rule
  about the example, and the section carries none — but a future edit could add one in a shape no string
  comparison here would recognise, and this file does not pretend otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"
SERVER_ROOT = REPO_ROOT / "emrg" / "server"

#: The section's identity in `system.j2`. Written as the heading line, so a second copy appended
#: anywhere in the template is visible as a count of two.
SURVIVAL_HEADING = "## Survival"

#: The section it must sit directly before: a reader meets what the work is for, then how it is done.
FOLLOWING_HEADING = "## How you work"

#: The four statements, split into the phrases that carry them. Each is asserted on its own, so a
#: deletion names the part that went rather than failing one long comparison.
STATEMENTS: dict[str, tuple[str, ...]] = {
    "1. survival probability is continuing to exist": (
        "Survival probability is EMRG continuing to exist",
    ),
    "2. the means that raise it, open-ended": (
        "raises it includes, but is not limited to",
        "being useful",
        "being preferred by the person using it",
        "being used by more people",
        "delivering value that satisfies the people who use it",
    ),
    "3. the reverse of each lowers it": (
        "The reverse of each of those lowers it",
    ),
    "4. raising it is the highest priority, with its tie-break": (
        "Raising survival probability is the highest priority",
        "the one that raises it is chosen",
    ),
}

#: Clauses that would reopen what the section settles. `but` is not among them — see the module
#: docstring: the open-ended list of means is widened by its `but`, not narrowed.
COUNTERWEIGHTS = (
    "however",
    "unless",
    "within reason",
    "where possible",
    "if possible",
    "as long as",
    "when convenient",
)


def _block_after(text: str, heading: str) -> str:
    """The section's body: from its heading line to the next `## ` heading, or to the end.

    A `###` inside the block does not end it, so a subsection is part of the section it belongs to.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ in
    `trim_blocks` / `lstrip_blocks`, in the loader path, or in `autoescape`, and the prompt under test
    would then be a prompt nobody receives. The context is deliberately minimal: the section sits
    outside every conditional, so no context key can remove it, and a render that loses it because of a
    *context* difference is exactly what this checks for.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_template_carries_the_survival_section() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, SURVIVAL_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry a `## Survival` section: it is the only prompt "
        "every session is rendered under, and without it the file states how to do a round and "
        "never what the work is for"
    )


@pytest.mark.parametrize("statement", STATEMENTS, ids=list(STATEMENTS))
def test_each_statement_is_carried_by_the_template(statement: str) -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, SURVIVAL_HEADING)
    missing = [term for term in STATEMENTS[statement] if term not in block]
    assert not missing, (
        f"the `## Survival` section must carry statement {statement}; missing: {missing}. Four "
        "statements are the rule — a section carrying three of them states a rule nobody wrote"
    )


def test_the_rendered_prompt_carries_the_section_not_merely_the_template() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Reading the file answers "is the rule in `system.j2`?" — one level short of this file's question,
    "is it in the prompt a session runs under?". A block can be in the file and in no render: wrap it
    in `{% if false %}` and the file still carries every term while the prompt carries none.
    """
    rendered = _render_system_prompt()
    block = _block_after(rendered, SURVIVAL_HEADING)
    assert block, (
        "the rendered session prompt must carry the `## Survival` section: it is present in "
        "system.j2 but no render a session receives contains it"
    )
    missing = [term for terms in STATEMENTS.values() for term in terms if term not in block]
    assert not missing, f"the rendered survival section is missing: {missing}"


def test_the_section_is_stated_exactly_once_in_the_template_and_the_render() -> None:
    """A second copy is two statements of one rule, free to disagree."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    rendered = _render_system_prompt()
    assert text.count(SURVIVAL_HEADING) == 1, (
        f"system.j2 states `{SURVIVAL_HEADING}` {text.count(SURVIVAL_HEADING)} times; one rule, "
        "one carrier — the copy a reader meets first governs"
    )
    assert rendered.count(SURVIVAL_HEADING) == 1, (
        f"the rendered prompt states `{SURVIVAL_HEADING}` {rendered.count(SURVIVAL_HEADING)} times"
    )


def test_the_section_sits_directly_before_how_you_work() -> None:
    """Placement is part of what it says: the premise before the habits, not a bullet among them."""
    for carrier, text in (
        ("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
        ("the rendered prompt", _render_system_prompt()),
    ):
        start = text.find(SURVIVAL_HEADING)
        assert start != -1, f"{carrier} does not carry `{SURVIVAL_HEADING}`"
        after = text[start:]
        nxt = after.find("\n## ", 1)
        assert nxt != -1, f"{carrier} has no section after `{SURVIVAL_HEADING}`"
        following = after[nxt + 1:].split("\n", 1)[0]
        assert following == FOLLOWING_HEADING, (
            f"in {carrier} the section after `{SURVIVAL_HEADING}` is {following!r}, not "
            f"{FOLLOWING_HEADING!r}: the section states the premise the following rules are done "
            "under, so its position is part of the rule"
        )
        how = _block_after(text, FOLLOWING_HEADING)
        assert SURVIVAL_HEADING not in how, (
            f"in {carrier} the survival section reads as a bullet of `{FOLLOWING_HEADING}` — among "
            "the habits it is one more habit beside \"read a file before you change it\""
        )


def test_the_section_carries_no_counterweight() -> None:
    """A clause that reopens what the section settles makes the priority conditional."""
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), SURVIVAL_HEADING)
    found = [word for word in COUNTERWEIGHTS if word in block.lower()]
    assert not found, (
        f"the `## Survival` section reopens what it settles with {found}: the priority is stated "
        "unconditionally or it is not stated"
    )


def test_no_other_carrier_under_emrg_server_states_the_rule() -> None:
    """One rule, one carrier: a second copy under `emrg/server/` is free to drift from this one."""
    marker = STATEMENTS["1. survival probability is continuing to exist"][0]
    carriers: list[str] = []
    for path in sorted(SERVER_ROOT.rglob("*")):
        if path.is_dir() or path.suffix not in {".md", ".j2"} or path == SYSTEM_PROMPT:
            continue
        if marker in path.read_text(encoding="utf-8", errors="replace"):
            carriers.append(str(path.relative_to(REPO_ROOT)))
    assert not carriers, (
        f"the survival rule is stated in {carriers} as well as `system.j2`: it belongs to the one "
        "carrier every session receives, and every other copy is free to disagree with it"
    )
