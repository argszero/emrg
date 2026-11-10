"""A refuted conclusion must be replaced where it stands, not annotated with a correction.

Why this file exists
--------------------
A memory is read **index row → title → opening**, and those are the positions that decide what
the next reader believes. The guidance that shipped in `system.j2`'s Memory section said the
opposite of that: *"When in doubt, append and note what changed"*, with how *settled* an entry
is framing appending as the safe default. A writer following it leaves a conclusion it has just
disproved in the body — often in the opening, and in the index row that repeats it — and appends
a section headed "void" / "expired" / "corrected" at the end. That is not a repair: a correction
below the fold is read last or not read at all, so the falsehood keeps the two positions a reader
actually reaches and stays quotable by whoever stops early, while the file grows a layer that the
next writer has to read before learning one fact.

What is asserted
----------------
Two legs, two questions, and neither implies the other: the **file** leg asks whether the source
that ships states it, the **render** leg whether a session receives it — `system.j2` is a Jinja2
template, so a block moved inside a false `{% if %}` keeps every character in the file and reaches
nobody (the same pair `tests/test_plain_language_rule_reach.py` was written for).

The removal is asserted as hard as the addition, and it is the half that decays silently: a test
that only checks the new rule is present says nothing about the old instruction still standing
beside it, and two contradictory sentences in one section are one instruction the reader picks
from at random. So the file must carry the replace rule **and** no longer tell a writer to append.

Both scopes are pinned together, because either alone over-corrects. "Replace the sentence" without
"an entry refuted as a whole is superseded" turns a whole wrong memory into a rewritten one that
is still wrong; "superseded" without "replace where it stands" retires a sound entry because two
lines of it went stale.

Named limit
-----------
This pins the guidance a session is given, not what any writer does with it afterwards. A session
that reads the rule and appends a correction anyway is a model-behaviour failure this file cannot
see — no test reads a memory and decides whether its shape came from a patch or a rewrite.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section that owns the subject. A second copy anywhere else in the template is a drift this
#: file would not see, so the reading is taken from this block alone.
MEMORY_HEADING = "## Memory"

#: Load-bearing fragments, each a verbatim substring of the shipped wording: the rule, the repair it
#: asks for, the "already patched" case, the whole-entry case, the index row, and the one thing a
#: refutation keeps. They cannot drift away from the sentence they check without this file failing.
REPLACE_TERMS = (
    "A refuted conclusion is replaced where it stands, never annotated",
    "delete the wrong sentence and write the right one in its place",
    "an entry already carrying such sections is rewritten whole",
    "takes `status: superseded` and leaves the active index",
    "whose row follows the same rule as the body it points at",
    "never the timeline or the effort",
)

#: The instruction this change removes. Its absence is the other half of the rule: leaving it in
#: place would give one session two contradictory answers, and the older one is the shorter advice.
GONE_TERMS = (
    "When in doubt, append and note what changed",
    "How settled an entry is decides how freely it is rewritten",
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


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own Jinja environment.

    The daemon's `_get_jinja_env` is what `_build_system_prompt` uses; a fresh `Environment` could
    differ in `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would
    then be one nobody receives. `has_memories` is true because the rule sits in the branch that
    speaks to a session which already holds memories — the only session a refutation can arise in.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test",
        config_dir="/nonexistent",
        has_memories=True,
        agent_memory_dir="/tmp/memory",
        agent_memory_index_path="/tmp/memory/MEMORY.md",
        session_memory_dir="/tmp/session-memory",
        session_memory_index_path="/tmp/session-memory/MEMORY.md",
        index_title_max_chars=512,
        memory_index_row_cap=100,
    )


def test_the_shipped_prompt_replaces_a_refuted_conclusion() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), MEMORY_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the Memory section — it is the only prompt every "
        "session is rendered under, so a rule stated anywhere else reaches one agent and no other."
    )
    missing = [term for term in REPLACE_TERMS if term not in block]
    assert not missing, f"the Memory section no longer states, or states differently: {missing}"


def test_the_shipped_prompt_no_longer_instructs_an_append() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), MEMORY_HEADING)
    assert block
    surviving = [term for term in GONE_TERMS if term in block]
    assert not surviving, (
        "the Memory section still carries the guidance this rule replaces, which leaves one session "
        f"with two answers and the shorter one wins: {surviving}"
    )


def test_a_session_receives_the_replace_rule() -> None:
    block = _block_after(_render_system_prompt(), MEMORY_HEADING)
    assert "A refuted conclusion is replaced where it stands" in block, (
        "the rule is in the template but not in the rendered prompt — a block moved behind a false "
        "`{% if %}` keeps every character in the file and reaches no session."
    )
    assert "When in doubt, append and note what changed" not in block


def test_the_render_carries_the_rule_and_not_the_append() -> None:
    """One reading, both directions, on the artifact a session actually gets."""
    block = _block_after(_render_system_prompt(), MEMORY_HEADING)
    assert block
    missing = [term for term in REPLACE_TERMS if term not in block]
    surviving = [term for term in GONE_TERMS if term in block]
    assert not missing and not surviving, f"missing={missing} surviving={surviving}"
