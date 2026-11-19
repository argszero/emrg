"""A session is told to keep what it learned, not only to repair what it got wrong.

Why this file exists
--------------------
`system.j2`'s `## Memory` section stated *when* to write a memory in one passive sentence, and its
two precise rules were about repairing an entry whose conclusion had already been refuted. Both of
those fire on an **error**. A way of working that cost more than it had to, a better way that turned
up, a number somebody measured once, and a premise that turned out not to hold are findings too — and
none of them is a mistake, so neither rule fires. The finding went unwritten, and the next round
re-measured the number and re-discovered the better way.

What is asserted
----------------
Two legs, two questions, neither implying the other: the **file** leg asks whether the source that
ships states the rule, the **render** leg whether a session receives it. `system.j2` is a Jinja2
template, so a group moved inside a false `{% if %}` keeps every character in the file and reaches
nobody — the same pair `tests/test_memory_rewrite_prompt_reach.py` was written for.

The group is asserted **beside** the correction rules, not merged into them. "Record what you found"
and "repair what you wrote" are two instructions with two triggers, and folding one into the other
leaves a reader unable to tell which moment they are in. So the file carries the new group **and**
still carries both correction rules, in that order: creation, then repair.

One carrier
-----------
The daemon builds a second, separate memory prompt for the reflection turn (`hygiene_note`). A rule
stated in both drifts apart with nothing to notice, so the assertions here are read against the
daemon's source as well: the group's wording must not appear there. The reflection prompt does carry
one older sentence on refining an entry in place, worded differently — this file does not claim the
subject has only one carrier, only that the wording it pins has one.

Named limit
-----------
This pins the guidance a session is given, not what a writer does with it: no test reads a memory
and decides whether its shape came from a rule or from luck.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"
DAEMON = REPO_ROOT / "emrg" / "server" / "daemon.py"

#: The section that owns the subject. A copy anywhere else in the template is a drift this file
#: would not see, so the reading is taken from this block alone.
MEMORY_HEADING = "## Memory"

#: One verbatim anchor per rule, in the order the group states them. Each is a substring of the
#: shipped wording, so a rewrite cannot keep the subject and lose the instruction without failing
#: here: the four triggers; a measurement as knowledge; the three things an entry says; the look
#: before the work; improving rather than duplicating; no exhortation; which store.
LEARNED_TERMS = (
    "What a round learned is worth an entry, and not only what it got wrong",
    "a judgement that was wrong, a way of working that cost more than it had to, a better way that turned up, and a premise that turned out not to hold",
    "A number measured once is knowledge",
    "what is better or wrong instead, and how that is known",
    "Look for a same-kind entry before starting a kind of work",
    "improves the entry that already covers the subject rather than adding a second one beside it",
    "Write the action and the criterion, never an exhortation",
    "The store follows the reach",
)

#: The pair the group sits beside. A rewrite that absorbed learned knowledge into the correction
#: rules would leave the section saying one thing where it used to say two.
REFUTATION_TERMS = (
    "A refuted conclusion is replaced where it stands, never annotated",
    "What a refutation keeps is the error and its discriminator",
)


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` heading or EOF.

    ``""`` when the heading is absent — the caller asserts, so a missing block reads as a failure
    to measure rather than as a pass.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _render_system_prompt(has_memories: bool = True) -> str:
    """The prompt a session receives, rendered by the daemon's own Jinja environment.

    The daemon's `_get_jinja_env` is what `_build_system_prompt` uses; a fresh `Environment` could
    differ in `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would
    then be one nobody receives.

    Both branches are rendered, because the group sits **above** the `has_memories` gate rather than
    inside it. A session that has written nothing is the one deciding what is worth writing, so it is
    the reader the rules exist for, and a placement inside the gate would keep every character in the
    file while telling that reader nothing — the shape this section already paid for once, when the
    row bound and the line cap sat inside it and the round that created an index was told neither
    (`tests/test_memory_row_bound_reach.py` records that measurement).
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test",
        config_dir="/nonexistent",
        has_memories=has_memories,
        agent_memory_dir="/tmp/memory",
        agent_memory_index_path="/tmp/memory/MEMORY.md",
        session_memory_dir="/tmp/session-memory",
        session_memory_index_path="/tmp/session-memory/MEMORY.md",
        index_title_max_chars=512,
        memory_index_row_cap=100,
    )


def test_the_shipped_prompt_states_what_a_round_learned() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), MEMORY_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the Memory section — it is the only prompt every "
        "session is rendered under, so a rule stated anywhere else reaches one agent and no other."
    )
    missing = [term for term in LEARNED_TERMS if term not in block]
    assert not missing, f"the Memory section no longer states, or states differently: {missing}"


def test_the_group_is_stated_once_and_sits_before_the_correction_rules() -> None:
    """One declaration, and creation read before repair.

    The count is over the whole template rather than the section: the defect this reads for is a
    second copy elsewhere, which is exactly what a section-scoped reading cannot see.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    first = LEARNED_TERMS[0]
    assert text.count(first) == 1, (
        "the learned-knowledge rule is declared "
        f"{text.count(first)} times — one rule, one carrier, or a later edit fixes one copy and "
        "leaves the other saying the opposite"
    )
    block = _block_after(text, MEMORY_HEADING)
    positions = [block.find(term) for term in REFUTATION_TERMS]
    assert all(pos != -1 for pos in positions), (
        "the correction rules the new group sits beside are gone — 'record what you found' and "
        "'repair what you wrote' are two instructions and the section owes both"
    )
    assert block.find(first) < min(positions), (
        "the learned-knowledge group now comes after the correction rules; the section reads "
        "creation then repair, and a writer meeting it in the other order has already been told how "
        "to fix an entry before being told what earns one"
    )


def test_a_session_with_no_index_still_receives_what_a_round_learned() -> None:
    """The reach the group's placement buys, read on the branch that has no index.

    A session holding no memory is the one deciding what is worth writing, so it is the reader these
    rules exist for. Placed inside `{% if has_memories %}` they would keep every character in the
    file and reach exactly the session that needs them least — the shape this section has already
    paid for once, with the row bound and the line cap.
    """
    rendered = _render_system_prompt(has_memories=False)
    assert "*No memories yet." in rendered, (
        "this fixture no longer renders the no-index branch, so it is not the reading it claims to "
        "be — the branch marker is the whole subject of this test"
    )
    block = _block_after(rendered, MEMORY_HEADING)
    assert block, "the rendered prompt carries no Memory section"
    missing = [term for term in LEARNED_TERMS if term not in block]
    assert not missing, (
        "a session with no memory index is not told what earns an entry — the group sits inside the "
        f"`has_memories` gate, which is the one branch that has already written something: {missing}"
    )


def test_a_session_receives_the_learned_knowledge_rules() -> None:
    """The render leg: a group behind a false `{% if %}` reaches no session at all."""
    block = _block_after(_render_system_prompt(), MEMORY_HEADING)
    assert block, "the rendered prompt carries no Memory section"
    missing = [term for term in LEARNED_TERMS if term not in block]
    assert not missing, (
        "the rules are in the template but not in the rendered prompt — a block moved behind a "
        f"false conditional keeps every character in the file and reaches nobody: {missing}"
    )


def test_the_group_is_outside_the_index_gate_in_the_template() -> None:
    """The placement itself, read on the source: above the gate, not inside it.

    The two render tests above would both pass if a later edit moved the group inside the gate while
    keeping the no-index assertion's own subject intact — so the source is read for where the group
    sits relative to the conditional, not only for what it says.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, MEMORY_HEADING)
    gate = block.find("{% if has_memories %}")
    group = block.find(LEARNED_TERMS[0])
    assert gate != -1, "the Memory section no longer carries the has_memories conditional"
    assert group != -1, "the learned-knowledge group is gone from the Memory section"
    assert group < gate, (
        "the learned-knowledge group now sits inside the `has_memories` branch — the round that has "
        "written nothing decides what is worth writing, and it is the one reader the gate excludes"
    )


def test_the_reflection_prompt_does_not_restate_this_wording() -> None:
    """One carrier for this wording.

    The daemon appends its own memory prompt for the reflection turn. A rule copied into both is a
    rule that drifts, and the drift is silent: whichever copy the reader meets first wins. The
    reflection prompt has carried an older sentence about refining an entry in place for longer than
    this group has existed; that sentence is not this wording, and this assertion is what keeps the
    two from converging by accident.
    """
    source = DAEMON.read_text(encoding="utf-8")
    duplicated = [term for term in LEARNED_TERMS if term in source]
    assert not duplicated, (
        "the learned-knowledge wording also appears in the daemon's reflection prompt — two "
        f"carriers of one rule, and no test can tell which one a session was given: {duplicated}"
    )
