"""The plan is agreed before an execution that costs minutes, not reconstructed after it.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the one prompt **every** session is rendered under — a host
conversation and a scheduled task alike (`daemon._build_system_prompt`). Until this file's rule
landed, it carried a section about what a *round* costs (`## Plan before every call` — a round is
billed for re-sending the conversation) and **nothing** about what an *execution* costs.

The two are different subjects, and the gap between them was live: work whose output takes minutes
to capture, render or upload was started, and the plan arrived afterwards as a description of work
already under way. The stop then has to come from the host. Everything a review needs — is this the
artifact I want? — is available from a text plan before anything is spent, which is why the ordering
is a rule rather than a preference.

Why the rule is a method and not a project's value
-------------------------------------------------
A project keeps its own values: its shot list, its deploy target, its coordinates, its accounts. The
*ordering* — plan in text, host answers, then spend the minutes — is the same in every one of them,
and a rule that saves the minutes belongs to the class of work rather than to the installation that
learned it. It therefore goes into the one carrier every session receives, not into one project's
memory (the shape `tests/test_language_policy_reach.py` records for the same reason).

Why it is a section of its own
------------------------------
It is not a habit that holds on every call, which is what a bullet of `## How you work` says. It is a
condition on a class of work, and `## Plan before every call` was lifted out of that same section for
exactly this reason: as one bullet among the rules that hold everywhere, a condition reads as one more
habit and changes nobody's behaviour.

What is asserted
----------------
1. The rule is in `system.j2` **and in the render**, and once only: a block in the file that no render
   contains is not a rule any session runs under, and a second copy is two copies free to drift.
2. It carries every part of the ruling — the scope (which work it binds), the ordering (text plan
   first), the statement that nothing has run, the item-by-item rejectability, and the licence the
   host's answer grants.
3. **Placement**: its own `## ` section, before `## Hard limits` — because `## Plan before every call`
   is the file's last block by design, and a `## ` heading after it reddens that rule's own guard —
   and not a bullet of `## How you work`.
4. It restates no number: the section names no duration, because `tests/test_system_prompt_structure.py`
   pins the template to zero digits, and a duration copied into the prefix would be a measurement
   living away from the record that made it.

Named limits
------------
This pins the *presence* and *placement* of the rule in the artifact a session receives, not obedience
to it — no test can watch a model decide to produce a plan. What it keeps true is that a session is
told, in the prompt it actually runs under, which deliverable comes first.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section the rule owns, as its whole heading line.
RULE_SECTION = "## Before work that costs the host time"

#: The section the round-cost rule owns. It must stay the file's last block, so this section may not
#: sit after it — `tests/test_several_calls_rule_reach.py` asserts that position from its own side.
ROUND_RULE_SECTION = "## Plan before every call"

#: The section the rule may not be a bullet of.
HABITS_SECTION = "## How you work"

#: One term per part of the ruling, each a verbatim substring of the shipped wording, so this list
#: cannot drift away from the sentence it checks without this file saying so. They cover the scope
#: (the kinds of work), the ordering (the plan is first and it is text), the statement that nothing
#: has been spent, the item-by-item rejectability, and what the host's answer licenses.
RULE_TERMS = (
    "a capture, a render, an upload, a deployment, a bulk fetch, a long job",  # the scope
    "the order of the deliverables is itself the rule",                        # why the ordering is a rule
    "**The plan comes first, and it is text.**",                               # the ordering
    "which parts are an estimate rather than a measurement",                   # what the plan separates
    "**Say that nothing has been spent yet.**",                                # the undisclosed-cost defect
    "**Make it rejectable item by item**",                                     # rejectable per item
    "**Their answer is what licenses the minutes.**",                          # the licence
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


def _h2_headings(text: str) -> list[str]:
    """Every `## ` heading line in the text, in order."""
    return re.findall(r"^## .*$", text, re.MULTILINE)


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ in
    `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would then be a
    prompt nobody receives. The context is deliberately minimal: the block sits outside every
    conditional, so no context key can remove it.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_ordering() -> None:
    """The source leg: does the artifact that ships state it?"""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, RULE_SECTION)
    assert block, (
        f"`system.j2` must carry the {RULE_SECTION!r} section — it is the only prompt every session "
        "is rendered under, and without it a session is told what a round costs and nothing about "
        "what an execution costs"
    )
    missing = _missing_terms(block, RULE_TERMS)
    assert not missing, (
        f"the section must state the scope, the ordering, the undisclosed cost and the licence; "
        f"missing: {missing}"
    )
    assert text.count(RULE_SECTION) == 1, (
        f"the rule must be stated once; found {text.count(RULE_SECTION)} copies — a duplication "
        "costs prompt and leaves two copies free to drift apart"
    )


def test_the_rendered_prompt_carries_the_ordering_verbatim() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Reading the file answers "is the rule in `system.j2`?" — one level short of this file's question,
    "is it in the prompt a session runs under?". The comparison is the **block**, verbatim, rather
    than its terms one by one: a Jinja construct that wrapped or edited it (`{# … #}`) leaves every
    term in the file, and a construct that reflowed it would leave every term in the render while
    changing what the reader is told.
    """
    rendered = _render_system_prompt()
    source_block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), RULE_SECTION)
    assert source_block, "system.j2 has no rule section to compare the render against"
    assert source_block in rendered, (
        "the rendered session prompt carries a *different* section than the template does — "
        "something in the render path edited it, and the wording a session reads is then not the "
        "wording this repository reviewed"
    )
    missing = _missing_terms(_block_after(rendered, RULE_SECTION), RULE_TERMS)
    assert not missing, f"the rendered section is missing terms: {missing}"


def test_the_rule_is_a_section_of_its_own_before_the_round_rule() -> None:
    """Placement is part of what the rule says, so it is asserted rather than assumed.

    Two directions. It may not be a bullet of `## How you work` — a condition that holds on a class of
    work reads as one more habit when it sits among the rules that hold on every call. And it may not
    follow `## Plan before every call`, whose own guard asserts that section is the file's last block:
    a heading after it would leave that rule present and its position wrong.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    headings = _h2_headings(text)
    assert RULE_SECTION in headings, f"the rule has no `## ` section of its own: {headings}"
    assert _section_of(text, HABITS_SECTION).find("**The plan comes first") == -1, (
        f"the plan rule is back inside {HABITS_SECTION!r}, where it reads as one more habit"
    )
    assert headings.index(RULE_SECTION) < headings.index(ROUND_RULE_SECTION), (
        "the plan section must sit before the round-cost section: that one is the file's last block, "
        "and its position is a property its own guard asserts"
    )


def test_the_section_names_no_duration() -> None:
    """A duration is a measurement, and the prefix carries none.

    Two-sided on purpose: the wording states the cost in words ("minutes"), which is what makes the
    scope readable, and the digits that would pin it to one reading of one run stay out.
    """
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), RULE_SECTION)
    assert block, "no section to read"
    assert "minutes" in block, (
        "the section no longer names the cost in words — without it `rejectable item by item` has no "
        "subject"
    )
    assert not re.search(r"[0-9]", block), (
        "the section carries a literal number: a duration in the prefix is a measurement living away "
        "from the record that made it (`tests/test_system_prompt_structure.py` pins the count at zero)"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the section must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the
    extractors a prompt with no such section and requires the absence to be visible, then a partial
    section and requires exactly the missing terms to be named.
    """
    sample = "# A prompt with no plan section\n\n- Must push\n"
    assert _block_after(sample, RULE_SECTION) == ""
    assert _missing_terms(sample, RULE_TERMS) == list(RULE_TERMS)
    partial = f"{RULE_SECTION}\n\n**The plan comes first, and it is text.**\n"
    assert "**The plan comes first, and it is text.**" not in _missing_terms(partial, RULE_TERMS)
    assert len(_missing_terms(partial, RULE_TERMS)) == len(RULE_TERMS) - 1
    assert RULE_SECTION not in _h2_headings(sample)


def _section_of(text: str, heading: str) -> str:
    """The region ``heading`` owns: from that line to the next `## ` heading at a line start."""
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]
