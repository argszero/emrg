"""The memory index's per-row bound must reach every session, not only an evolution cycle.

Measured 2026-10-01 on this host: `.emrg/memory/MEMORY.md` carried four rows past
`INDEX_TITLE_MAX_CHARS` (600-1,139 characters), every one of them written by the *promote*
session. The number lived in `emrg/server/evolution_prompt.md` R9 alone — a carrier only an
evolution cycle reads — while the one prompt **every** session is rendered under
(`emrg/server/prompts/system.j2`) said "one short line per entry" with no bound. A rule with
no number has no edge, and the measurement that says so is the promote session's own
`system.md`: it carried the sentence without the number.

So the bound is stated in `system.j2` and **injected** from the store's constant rather than
written into the template, and this file measures both halves — that the shared template
states it, and that a session which really renders that template receives the constant's
value. The second half is the one that matters: the number is a bound on a line of a file
the *agent* writes, so a statement that never reaches the writer's prompt fixes nothing.

The *other* half of this defect — the daemon's compaction trigger, which reads the same rows
— is `tests/test_memory_index_embed_cap_ends.py`; the guard that prints the reading is
`tests/test_check_memory_index.py`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.memory import INDEX_TITLE_MAX_CHARS
from emrg.server.daemon import EmrgServer, _get_jinja_env
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The hygiene section's identity in `system.j2`, written as its heading line so a second
#: copy would be visible here rather than silently deepening the block.
HYGIENE_HEADING = "**Memory Hygiene**"

#: The row-bound sentence's identity: the line that states what a row is. Chosen as a phrase
#: that survives rewording of the number, so this file keeps measuring the *bound's* carrier
#: rather than one spelling of the sentence.
BOUND_MARKER = "pure index"

#: The variable the template must use. Named here so the assertion and the template cannot
#: disagree about which name is the one.
BOUND_VAR = "{{ index_title_max_chars }}"


def _hygiene_block(text: str) -> str:
    """The lines of the memory-hygiene section, heading through the last bullet.

    Read as a *block* rather than line by line because the nesting is the point: a bound
    stated under `{% if false %}` is in the file and in no render, and a check that scanned
    the whole file for a phrase would pass it.
    """
    lines = text.splitlines()
    try:
        # Prefix rather than equality: the heading line carries its rant citations after
        # the marker (`**Memory Hygiene** (PR #941, …)`), and an exact match would miss the
        # block it is looking for — the shape `test_language_policy_reach.py` records for
        # the same reason.
        start = next(i for i, line in enumerate(lines) if line.startswith(HYGIENE_HEADING))
    except StopIteration:
        return ""
    block: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("{%") or line.startswith("#") or not line.strip():
            break
        block.append(line)
    return "\n".join(block)


def _bound_line(text: str) -> str:
    """The one line of `text` that states the row bound, or `""`.

    `text` is a *render* in the second test and the template in the first, so this reads the
    same shape in both — the artifact's statement, not the file's.
    """
    for line in text.splitlines():
        if BOUND_MARKER in line:
            return line
    return ""


def _make_server() -> EmrgServer:
    """A minimal server, with the projects log pointed away from the real one.

    The shape `tests/test_daemon.py::_make_server` uses, for the same reason: building a
    system prompt must never write the host's `~/.emrg/projects.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _session_with_an_index(tmp_path: Path) -> Session:
    """A session whose cwd carries a project memory index, so the block really renders.

    The hygiene section sits under `{% if has_memories %}`, so a session with no index
    renders the *else* branch — a prompt in which the bound could be absent for a reason
    that has nothing to do with this rule, which is the false pass this helper removes.
    """
    memory_dir = tmp_path / ".emrg" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
        encoding="utf-8",
    )
    return Session.create_with_id("row-bound-reach", tmp_path)


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


def test_the_number_is_injected_not_written_in_the_template() -> None:
    """Both directions: the render's number follows its context, so there is one home for it.

    A number written into the template would pass every check above and still be a second
    copy of a derived value — free to drift from `INDEX_TITLE_MAX_CHARS` the moment either
    moves, with the guard (`scripts/check-memory-index.py`) measuring the other one. Driving
    a value the constant does not have is what tells the two apart: it can only appear in the
    render if the template reads its context.
    """
    probe = 4096
    assert probe != INDEX_TITLE_MAX_CHARS, "the probe must be a value the constant is not"
    rendered = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent", has_memories=True,
        index_title_max_chars=probe,
    )
    line = _bound_line(rendered)
    assert line, "the hygiene block must render when memories exist"
    assert str(probe) in line, (
        f"the bound must come from the render context, not a literal in the template; with "
        f"the context set to {probe} the line reads: {line!r}"
    )
    assert str(INDEX_TITLE_MAX_CHARS) not in line, (
        f"the line still carries {INDEX_TITLE_MAX_CHARS} with the context set to {probe} — "
        "that number is written into the template, and two copies of it can drift"
    )
