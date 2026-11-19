"""What a round *learns* must reach the next one, not only what a round got wrong.

The memory section told a session when to write an entry in one passive sentence ("write or update
one when something in the exchange belongs there"), and its only precise rules were about repairing
an entry whose conclusion had already been refuted. So a round that found something — a way of
working that cost more than it had to, a better way that turned up, a number somebody measured once,
a premise that turned out not to hold — had nowhere to put it: none of those is a mistake, so neither
correction rule fired, and the next round measured the same number and rediscovered the same better
way.

The group that states this is one block of the existing `## Memory` section, beside the refutation
pair rather than merged into it: the pair says how to replace a conclusion that was wrong, and this
says what is worth writing down when nothing was wrong at all. Both readings matter, because the
defect being guarded against here is a rewrite that swaps one for the other — a section that grows a
finding rule by absorbing the correction rule would pass a check that only looked for the new text.

Two legs, on purpose
--------------------
Every assertion here reads **the template** and **the prompt a session receives**, rendered through
`_build_system_prompt`, which is the artifact a session actually runs under. A block moved behind a
false `{% if %}` keeps every character in the file and reaches nobody, and that is not a hypothetical
in this section: the row bound and the line cap were once inside `{% if has_memories %}`, so the one
round that decided an index's shape — the round it was created in — was told neither
(`tests/test_memory_row_bound_reach.py` records that measurement). The group follows the same rule as
those two numbers: what earns an entry does not depend on an entry existing.

The anchors are verbatim, one per rule, so a rewrite cannot drift the wording away from the rule it
states without failing here. The wording is not otherwise pinned: a respelling that keeps each anchor
intact is a change this file allows.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section the group lives in. One `## Memory`, which
#: `tests/test_system_prompt_structure.py` refuses to see split.
MEMORY_HEADING = "## Memory"

#: The paragraph that opens the group. Its own lead-in is what makes the block a group rather than
#: more bullets under the rules above it.
GROUP_LEAD = "What earns an entry"

#: One anchor per rule the group states, in the order a reader meets them. Each is the phrase that
#: carries the rule itself, so this file measures the rule's presence rather than a sentence's
#: spelling.
RULE_ANCHORS = (
    "The trigger is the finding, not the error",
    "A measurement is knowledge",
    "An entry says three things",
    "Look before you start",
    "One entry per subject",
    "The store follows the reach",
    "Actions and criteria only",
)

#: The pair the group must sit beside and must not replace: the two rules for an entry whose
#: conclusion was already refuted. Both are what a reader needs when the earlier entry was *wrong*,
#: which is a different case from a round that learned something with nothing wrong.
REFUTATION_ANCHORS = (
    "A refuted conclusion is replaced where it stands",
    "What a refutation keeps is the error and its discriminator",
)

#: The clause that makes each rule a rule rather than a heading. An anchor alone is only the rule's
#: name: a rewrite could keep every name and empty each rule of its content — the trigger narrowed
#: back to mistakes, the "how that is known" requirement dropped, the pre-work look removed — and a
#: reading of names alone would report all seven present. These are what those mutations delete, so
#: each named mutation below has a clause here that stops being true when it is made.
RULE_CLAUSES = (
    # 1 — the trigger is the finding, and the finding has four kinds
    "a way of working that cost more than it had to",
    "a better way that turned up",
    "a premise that turned out not to hold",
    # 1 — and it is written when it is learned, not at some later tidy-up
    "written in the round it was learned in",
    # 2 — the measurement travels with the entry
    "carries the reading itself",
    # 3 — the third part is what separates knowledge from preference
    "what was being done",
    "how that is known",
    "it is a preference, not knowledge",
    # 4 — the look happens before the work
    "before starting a kind of work",
    # 5 — a later finding improves the entry rather than adding beside it
    "instead of adding a second one beside it",
    # 6 — the store is chosen by who will read it
    "what a later session would need",
    # 7 — an exhortation carries nothing checkable
    "nothing a later round can check",
)

#: A heading of any level. The group is a block inside the memory topic, never a topic of its own:
#: a `## ` here would be the split the structure guard exists for, and a `### ` would make the rules
#: look like a sibling of the section that owns them.
HEADING = re.compile(r"^#{2,3} .*$", re.MULTILINE)

#: A digit. The template refuses all of them (`test_the_template_carries_no_digit_at_all`): a figure
#: in the prefix is either history or a copied derived value, and the group has no need of either.
DIGIT = re.compile(r"[0-9]")


def _section(text: str, heading: str) -> str:
    """The region `heading` owns: from that line to the next `## ` heading at a line start."""
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end():]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _missing(text: str, anchors: tuple[str, ...]) -> list[str]:
    """The anchors `text` does not carry. Pure, so the control below can use it on a sample."""
    return [anchor for anchor in anchors if anchor not in text]


def _count(text: str, anchor: str) -> int:
    return text.count(anchor)


def _make_server() -> EmrgServer:
    """A minimal server with the agents log pointed away from the real one.

    The shape `tests/test_memory_row_bound_reach.py` uses, for the same reason: building a system
    prompt must never write the host's own agents file.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _session_with_an_index(tmp_path: Path) -> Session:
    """A session whose working directory carries an agent memory index."""
    memory_dir = tmp_path / ".emrg" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
        encoding="utf-8",
    )
    return Session.create_with_id("finding-reach", tmp_path)


def _session_without_an_index(tmp_path: Path) -> Session:
    """A session that has written no memory at all — the round the group matters most in.

    This is a first round against a project that keeps no memory: it is about to decide what to
    write, and every rule about what earns an entry applies before any entry exists.
    """
    return Session.create_with_id("finding-reach-no-index", tmp_path)


# ── the template ─────────────────────────────────────────────────────────────


def test_the_template_states_every_rule_of_the_group_once() -> None:
    """One anchor per rule, each appearing exactly once — two copies are free to drift apart."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    missing = _missing(text, RULE_ANCHORS)
    assert not missing, (
        f"system.j2 no longer states these rules of the finding group: {missing} — the group is "
        "what tells a round to write down a finding that is not a mistake, and a rule that left it "
        "leaves the next round re-measuring what this one already knew"
    )
    for anchor in RULE_ANCHORS:
        assert _count(text, anchor) == 1, (
            f"{anchor!r} appears {_count(text, anchor)} times in system.j2: a rule stated twice is "
            "two rules free to drift apart"
        )


def test_the_group_is_a_block_of_the_memory_section_and_not_a_section() -> None:
    """Placement, both ways: inside `## Memory`, and not promoted to a heading of its own."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    section = _section(text, MEMORY_HEADING)
    assert section, "system.j2 has no `## Memory` heading line"
    assert GROUP_LEAD in section, (
        f"the finding group ('{GROUP_LEAD}') is outside the `## Memory` section — what earns an "
        "entry and how an entry is organised are one topic, and a reader who arrives at either must "
        "find the other without crossing an unrelated section"
    )
    promoted = [heading for heading in HEADING.findall(text) if GROUP_LEAD in heading]
    assert not promoted, (
        f"the finding group is a heading again ({promoted}): it is one block of the memory topic, "
        "not a topic of its own"
    )


def test_the_group_sits_beside_the_refutation_rules_inside_that_section() -> None:
    """Both halves present, in one region: the pair must survive the group's arrival.

    The failure this reads for is a rewrite that grows the finding rule by absorbing the correction
    rule — every anchor for the new text would be present and the reader would have lost the one
    case where the earlier entry was simply wrong.
    """
    section = _section(SYSTEM_PROMPT.read_text(encoding="utf-8"), MEMORY_HEADING)
    assert section, "system.j2 has no `## Memory` heading line"
    assert GROUP_LEAD in section and not _missing(section, RULE_ANCHORS), (
        "the finding group must sit inside `## Memory`"
    )
    assert not _missing(section, REFUTATION_ANCHORS), (
        f"the refutation rules are no longer beside the finding group: missing "
        f"{_missing(section, REFUTATION_ANCHORS)} — the group states what to write when nothing was "
        "wrong, the pair states how to repair an entry that was, and replacing one with the other "
        "loses a case rather than simplifying a rule"
    )
    assert section.index(REFUTATION_ANCHORS[0]) < section.index(GROUP_LEAD), (
        "the refutation pair is stated before the group it must sit beside; if the order moved, the "
        "anchors above are reading a different arrangement than the one reviewed"
    )


def test_the_group_carries_no_digit() -> None:
    """A figure in the prefix is history or a copied derived value; the group needs neither."""
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    region = text[text.index(GROUP_LEAD):]
    found = DIGIT.findall(region.split("\n## ")[0])
    assert not found, (
        f"the finding group carries a digit: it is either history or a derived value that belongs "
        f"in the constant that owns it — {found}"
    )


def test_every_rule_carries_the_clause_that_makes_it_one() -> None:
    """Names are not rules. Each clause below is the content a named mutation deletes.

    The anchors above are each rule's *name*, and a rewrite can keep all seven names while emptying
    the rules: narrow the trigger back to mistakes and "the trigger is the finding" is still there,
    word for word. So the content is read too, and each entry of ``RULE_CLAUSES`` is a specific
    deletion this file has to notice.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    missing = _missing(text, RULE_CLAUSES)
    assert not missing, (
        f"the finding group no longer carries the content of a rule it still names: {missing} — a "
        "rule reduced to its lead-in says that something matters without saying what to do"
    )


def test_the_clauses_reach_a_session_too(tmp_path: Path) -> None:
    """The same content on the rendered leg: a clause behind a false gate is in no prompt."""
    rendered = _make_server()._build_system_prompt(_session_with_an_index(tmp_path))
    missing = _missing(rendered, RULE_CLAUSES)
    assert not missing, (
        f"the rendered session prompt drops the content of the finding rules: {missing}"
    )


# ── the prompt a session receives ────────────────────────────────────────────


def test_the_rendered_prompt_carries_the_group(tmp_path: Path) -> None:
    """The artifact a session runs under, through the daemon's own builder.

    Reading the template answers "is the group in `system.j2`?" — one level short of this file's
    question, "does a session receive it?". The render is where a `{% if %}` that swallowed the
    block shows up.
    """
    rendered = _make_server()._build_system_prompt(_session_with_an_index(tmp_path))
    missing = _missing(rendered, RULE_ANCHORS)
    assert not missing, (
        "the rendered session prompt must carry every rule of the finding group: these are in "
        f"system.j2 but reach no session — {missing}"
    )
    assert GROUP_LEAD in rendered, "the rendered prompt must carry the group's own lead-in"


def test_a_session_with_no_index_still_receives_the_group(tmp_path: Path) -> None:
    """The very round the group is for: a session that has written nothing yet.

    Measured precedent for this leg is the row bound's: with the rule gated on an index existing, the
    one round that decided the index's shape was told nothing about it. What earns an entry applies
    before any entry exists, so it renders whether or not one does.
    """
    rendered = _make_server()._build_system_prompt(_session_without_an_index(tmp_path))
    assert "No memories yet" in rendered, (
        "this reading is about the branch that tells a session to create an index; the prompt "
        "rendered the indexed block instead, so the case under test did not happen"
    )
    missing = _missing(rendered, RULE_ANCHORS)
    assert not missing, (
        "a session that has written no memory must still be told what earns one — the group must "
        f"render on the branch that asks it to create the index; missing — {missing}"
    )


def test_the_group_did_not_displace_the_refutation_rules_in_the_render(tmp_path: Path) -> None:
    """Both cases reach the reader: a finding to record, and an entry to repair."""
    rendered = _make_server()._build_system_prompt(_session_with_an_index(tmp_path))
    missing = _missing(rendered, REFUTATION_ANCHORS)
    assert not missing, (
        f"the refutation rules no longer reach a session: missing {missing} — the finding group "
        "states the case where nothing was wrong, and it must not be the only memory rule left"
    )


def test_the_group_stands_on_its_own_without_the_gated_rules(tmp_path: Path) -> None:
    """The group renders where the rules around it do not, so it may not point at them.

    Measured while writing this: on the branch that tells a session to create an index, the
    refutation pair is absent — those two bullets sit inside `{% if has_memories %}` while the group
    sits outside it. A sentence saying "the rules above do not fire for one" therefore points at
    nothing for exactly the reader this group was added for, the session about to write its first
    entry. The group is self-contained instead, and this reading keeps it that way.
    """
    rendered = _make_server()._build_system_prompt(_session_without_an_index(tmp_path))
    assert "No memories yet" in rendered, "the no-index branch is the case under test"
    region = rendered[rendered.index(GROUP_LEAD):]
    region = region.split("## Session and history")[0]
    dangling = [
        phrase
        for phrase in ("rules above", "rule above", "bullets above", "section above", "rules below")
        if phrase in region
    ]
    assert not dangling, (
        f"the finding group points at rules that may not have rendered beside it: {dangling} — on "
        "the no-index branch those bullets are absent, so a reader arriving at their first entry is "
        "sent to look at text that is not there"
    )


# ── the control ──────────────────────────────────────────────────────────────


def test_the_reading_notices_a_missing_rule() -> None:
    """The instrument discriminates: drop one rule from a sample and this file's reading sees it.

    Without this, every assertion above could be satisfied by a reader that finds nothing and
    reports nothing.
    """
    sample = "## Memory\n\n" + "\n".join(f"**{anchor}**" for anchor in RULE_ANCHORS) + "\n"
    assert _missing(sample, RULE_ANCHORS) == []
    incomplete = sample.replace(RULE_ANCHORS[2], "An entry says what it likes")
    assert _missing(incomplete, RULE_ANCHORS) == [RULE_ANCHORS[2]], (
        "the reading did not notice a rule rewritten away — it cannot tell a stated rule from an "
        "absent one"
    )
    assert _missing(sample.replace("## Memory", "## Memory Management\n\nx"), RULE_ANCHORS) == []
    # And the region reading: the same anchors under a second heading are not this section's.
    split = sample + "\n## Memory Management\n\n" + RULE_ANCHORS[2] + "\n"
    assert _section(split, "## Memory").count(RULE_ANCHORS[2]) == 1, (
        "the region reading spans a second `Memory` heading, which is the split it exists to see"
    )
