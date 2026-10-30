"""The memory index's per-row bound must reach every session, not only an evolution cycle.

Measured 2026-10-01 on this host: `.emrg/memory/MEMORY.md` carried four rows past
`INDEX_TITLE_MAX_CHARS` (600-1,139 characters), every one of them written by the *promote*
session. The number lived in `emrg/server/evolution_prompt.md` R9 alone — a carrier only an
evolution cycle reads — while the one prompt **every** session is rendered under
(`emrg/server/prompts/system.j2`) said "one short line per entry" with no bound. A rule with
no number has no edge, and the measurement that says so is the promote session's own
`system.md`: it carried the sentence without the number.

R9 names **two** numbers for the same file — the per-row bound and the line cap ("those two
numbers are why 100 lines fit the embed budget") — and the every-session template carried
*neither*: it said "one short line per entry" and "if a memory index has grown long". So both
are stated in `system.j2` and **injected** from the constant the daemon also measures with,
rather than written into the template, and this file measures each in both halves — that the
shared template states it, and that a session which really renders that template receives the
constant's value. The second half is the one that matters: the number is a bound on a file the
*agent* writes, so a statement that never reaches the writer's prompt fixes nothing.

The *other* half of this defect — the daemon's compaction trigger, which reads the same rows
— is `tests/test_memory_index_embed_cap_ends.py`; the guard that prints the reading is
`tests/test_check_memory_index.py`.

The gate that hid the last writer
---------------------------------
Both numbers reached every session that *had* an index, and neither reached a session that did
not: the sentence sat inside `{% if has_memories %}`, while the else branch says "Create the agent
memory directory with its index". Measured 2026-10-31 through the daemon's own builder, on a
session whose cwd carries no `MEMORY.md`: `has 'No memories yet': True`, `carries row bound 512 :
False`, `carries line cap 100 : False`. So the one round in which the bound decides the file's
shape — the round the index is made in, which is the promote task's first round against a project
that keeps no memory yet — was the one round told nothing about it. A rule about how long a row
may be does not depend on the row existing, so it now renders outside the gate, and
`test_a_session_with_no_index_still_receives_both_numbers` reads it on exactly that session.
`_session_with_an_index` remains for the rest of the block, which still needs an index to render.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.memory import INDEX_TITLE_MAX_CHARS
from emrg.server.daemon import MEMORY_INDEX_ROW_CAP, EmrgServer, _get_jinja_env
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section's identity in `system.j2`, written as its heading line so a second copy would be
#: visible here rather than silently deepening the block. One `## Memory` section holds the format,
#: the two numbers and the organisation rules; a second heading starting with `Memory` is the split
#: `tests/test_system_prompt_structure.py` refuses.
HYGIENE_HEADING = "## Memory"

#: The row-bound sentence's identity: the line that states what a row is. Chosen as a phrase
#: that survives rewording of the number, so this file keeps measuring the *bound's* carrier
#: rather than one spelling of the sentence.
BOUND_MARKER = "The index is a pointer list"

#: The variable the template must use. Named here so the assertion and the template cannot
#: disagree about which name is the one.
BOUND_VAR = "{{ index_title_max_chars }}"

#: The line cap's identity: the sentence that asks for compaction. Chosen as a phrase that
#: survives rewording of the number, so this file keeps measuring the *cap's* carrier rather
#: than one spelling of the sentence.
LINE_CAP_MARKER = "it is consolidated"

#: The variable the line-cap sentence must use — the same name/value pairing as `BOUND_VAR`.
LINE_CAP_VAR = "{{ memory_index_row_cap }}"


def _hygiene_block(text: str) -> str:
    """The `## Memory` region, heading through the bullet list, excluding the next `## ` section.

    Read as a *region* rather than searched file-wide because the boundaries are the point: a
    bound stated under `{% if false %}` is in the file and in no render, and a rewrite that moved
    one of the two numbers into an unrelated section would still pass a scan of the whole file.
    Both numbers must be inside this one section — they belong to the same list.
    """
    start = text.find(HYGIENE_HEADING)
    if start == -1:
        return ""
    rest = text[start + len(HYGIENE_HEADING):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _bound_line(text: str) -> str:
    """The one line of `text` that states the row bound, or `""`.

    `text` is a *render* in the second test and the template in the first, so this reads the
    same shape in both — the artifact's statement, not the file's.
    """
    for line in text.splitlines():
        if BOUND_MARKER in line:
            return line
    return ""


def _line_cap_line(text: str) -> str:
    """The one line of `text` that states the line cap, or `""`. The row bound's sibling."""
    for line in text.splitlines():
        if LINE_CAP_MARKER in line:
            return line
    return ""


def _make_server() -> EmrgServer:
    """A minimal server, with the agents log pointed away from the real one.

    The shape `tests/test_daemon.py::_make_server` uses, for the same reason: building a
    system prompt must never write the host's `~/.emrg/agents.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _session_with_an_index(tmp_path: Path) -> Session:
    """A session whose cwd carries an agent memory index, so the whole block renders.

    The block's remaining bullets — the two stores and their paths, "a write is two steps", the
    organisation rules — are under `{% if has_memories %}`, so they need an index to render at
    all. The bound is no longer one of them (2026-10-31): it states how long a row may be, which
    does not depend on a row existing, and `_session_without_an_index` reads it where the
    section's *else* branch renders.
    """
    memory_dir = tmp_path / ".emrg" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
        encoding="utf-8",
    )
    return Session.create_with_id("row-bound-reach", tmp_path)


def _session_without_an_index(tmp_path: Path) -> Session:
    """A session whose cwd carries no `MEMORY.md` — the prompt that tells it to create one.

    Nothing is created here: the point of the reading is the branch that says "Create the agent
    memory directory with its index", which is what a promote task sees the first time it lands in
    a project that keeps no memory yet.
    """
    return Session.create_with_id("row-bound-no-index", tmp_path)


def test_the_shared_template_states_the_bound() -> None:
    """`system.j2` is the one prompt every session is rendered under, so the bound is there."""
    block = _hygiene_block(SYSTEM_PROMPT.read_text(encoding="utf-8"))
    assert block, (
        "emrg/server/prompts/system.j2 must carry the memory-hygiene block — it is the only "
        "prompt every session is rendered under, and the run the bound exists for is the "
        "promote session's"
    )
    assert BOUND_VAR in block, (
        f"the hygiene block must state the per-row bound as `{BOUND_VAR}`, injected from the "
        "store's constant; a hygiene block that says only 'one short line per entry' is the "
        "state that let four rows reach 600-1,139 characters"
    )


def test_the_shared_template_states_the_line_cap() -> None:
    """R9 names two numbers for the file and the every-session carrier said neither.

    The row bound's sibling: `MEMORY_INDEX_ROW_CAP` was stated in `evolution_prompt.md` alone
    (a carrier only an evolution cycle reads), while `system.j2` left the compaction sentence
    at "if a memory index has grown long" — a threshold no writer could measure against. The
    same injection, for the same reason.
    """
    block = _hygiene_block(SYSTEM_PROMPT.read_text(encoding="utf-8"))
    assert block, "emrg/server/prompts/system.j2 must carry the memory-hygiene block"
    assert LINE_CAP_VAR in block, (
        f"the hygiene block must state the line cap as `{LINE_CAP_VAR}`, injected from the "
        "daemon's own constant; 'if a memory index has grown long' is a threshold with no "
        "number, and R9's second number therefore reached no session but an evolution cycle"
    )


def test_the_rendered_prompt_carries_the_stores_number(tmp_path: Path) -> None:
    """The artifact the writer receives, measured through the daemon's own builder.

    Reading the template answers "is the bound in `system.j2`?" — one level short of this
    file's question, "does a session run under it?". The context is the real one, because
    an injected value is only as good as the key the builder supplies: a template variable
    no render fills is a bound that reads as blank.
    """
    server = _make_server()
    rendered = server._build_system_prompt(_session_with_an_index(tmp_path))
    line = _bound_line(rendered)
    assert line, (
        "the rendered session prompt must state the row bound: the marker is present in "
        "system.j2 but no render a session receives carries it"
    )
    assert str(INDEX_TITLE_MAX_CHARS) in line, (
        f"the rendered bound must be the store's number ({INDEX_TITLE_MAX_CHARS}); the line "
        f"reads: {line!r} — an empty value here means `_build_system_prompt` supplies no key "
        "for it"
    )


def test_the_rendered_prompt_carries_the_stores_line_cap(tmp_path: Path) -> None:
    """The line cap, read at the same level as the row bound: the render a session receives."""
    server = _make_server()
    rendered = server._build_system_prompt(_session_with_an_index(tmp_path))
    line = _line_cap_line(rendered)
    assert line, (
        "the rendered session prompt must state the line cap: the marker is present in "
        "system.j2 but no render a session receives carries it"
    )
    assert str(MEMORY_INDEX_ROW_CAP) in line, (
        f"the rendered cap must be the daemon's number ({MEMORY_INDEX_ROW_CAP}); the line "
        f"reads: {line!r} — an empty value here means `_build_system_prompt` supplies no key "
        "for it"
    )


def test_a_session_with_no_index_still_receives_both_numbers(tmp_path: Path) -> None:
    """The writer that has not written yet: the else branch, which asks it to create the index.

    Measured 2026-10-31, with both numbers inside `{% if has_memories %}`: this session's prompt
    said "Create the agent memory directory with its index" and carried neither bound — so the
    first index was made under a rule with no edge, which is the round the bound matters most in.
    The else branch is asserted to be the branch that rendered, so this cannot pass by the full
    block having appeared for some other reason.
    """
    server = _make_server()
    rendered = server._build_system_prompt(_session_without_an_index(tmp_path))
    assert "No memories yet" in rendered, (
        "this reading is about the branch that tells a session to create an index; the prompt "
        "rendered the indexed block instead, so the case under test did not happen"
    )
    bound_line = _bound_line(rendered)
    assert bound_line, (
        "a session told to create an index must be told how long a row may be — the bound now "
        "renders whether or not one exists"
    )
    assert str(INDEX_TITLE_MAX_CHARS) in bound_line, (
        f"the bound must be the store's number ({INDEX_TITLE_MAX_CHARS}); the line reads: "
        f"{bound_line!r}"
    )
    cap_line = _line_cap_line(rendered)
    assert cap_line, (
        "a session told to create an index must be told the line cap it is compacted at; a "
        "threshold with no number is a licence to keep appending"
    )
    assert str(MEMORY_INDEX_ROW_CAP) in cap_line, (
        f"the cap must be the daemon's number ({MEMORY_INDEX_ROW_CAP}); the line reads: "
        f"{cap_line!r}"
    )


def test_the_number_is_injected_not_written_in_the_template() -> None:
    """Both directions, for both numbers: the render follows its context, so one home each.

    A number written into the template would pass every check above and still be a second
    copy of a derived value — free to drift from the constant the guard (or the daemon's own
    compaction trigger) measures, the moment either moves. Driving a value the constant does
    not have is what tells the two apart: it can only appear in the render if the template
    reads its context. Measured for the row bound and the line cap together, because they
    share one carrier and a fix that reached only one of them is the state this file exists
    to prevent.
    """
    row_probe = 4096
    cap_probe = 977
    assert row_probe != INDEX_TITLE_MAX_CHARS, "the probe must be a value the constant is not"
    assert cap_probe != MEMORY_INDEX_ROW_CAP, "the probe must be a value the constant is not"
    rendered = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent", has_memories=True,
        index_title_max_chars=row_probe, memory_index_row_cap=cap_probe,
    )
    bound_line = _bound_line(rendered)
    cap_line = _line_cap_line(rendered)
    assert bound_line, "the hygiene block must render when memories exist"
    assert str(row_probe) in bound_line, (
        f"the row bound must come from the render context, not a literal in the template; "
        f"with the context set to {row_probe} the line reads: {bound_line!r}"
    )
    assert str(INDEX_TITLE_MAX_CHARS) not in bound_line, (
        f"the line still carries {INDEX_TITLE_MAX_CHARS} with the context set to {row_probe} "
        "— that number is written into the template, and two copies of it can drift"
    )
    assert cap_line, "the line-cap sentence must render when memories exist"
    assert str(cap_probe) in cap_line, (
        f"the line cap must come from the render context, not a literal in the template; "
        f"with the context set to {cap_probe} the line reads: {cap_line!r}"
    )
    assert str(MEMORY_INDEX_ROW_CAP) not in cap_line, (
        f"the line still carries {MEMORY_INDEX_ROW_CAP} with the context set to {cap_probe} "
        "— that number is written into the template, and two copies of it can drift"
    )

    # The same two readings with the gates off. The sentence is about the shape of a row, not
    # about an index that already exists, so it has to follow its context in the render a session
    # with no index receives as well — that render is the one the bound used to be absent from,
    # and a fix that stated the numbers there as literals would be the drift this file refuses,
    # one branch further in.
    gated_off = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent", has_memories=False,
        index_title_max_chars=row_probe, memory_index_row_cap=cap_probe,
    )
    assert "No memories yet" in gated_off, (
        "the gated-off render must be the else branch; otherwise these two readings repeat the "
        "ones above and measure nothing new"
    )
    off_bound = _bound_line(gated_off)
    off_cap = _line_cap_line(gated_off)
    assert off_bound and str(row_probe) in off_bound, (
        "with no index the bound must still follow the render context; the line reads: "
        f"{off_bound!r}"
    )
    assert off_cap and str(cap_probe) in off_cap, (
        "with no index the line cap must still follow the render context; the line reads: "
        f"{off_cap!r}"
    )
