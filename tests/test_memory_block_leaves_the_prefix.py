"""Writing a memory must not move the bytes of the system prompt.

Rant 2026-10-15T12:57:39 (issue #2292): `system.j2` rendered the two memory indexes
into the system prompt, and the system prompt is the prefix a provider's prompt cache
matches on — so every memory write moved the prefix from the index onward. Measured on
the session that filed the rant: the indexes occupied 47,351 of the prompt's 81,064
characters and a further 14,695 static characters followed them, so one new row
re-sent ~61,700 characters that had not changed, on every request of every round.

The cure is placement — the bodies ride in the per-round `[context]` message, the
placement `current_time` took on 2026-08-23 (rant 2026-08-23T13:54:14) — and this file
measures the placement, not the template's prose. The subject is the *writer*: a
session appends a row to `MEMORY.md` as it works, so the claim is about what that
append does to the prompt the next request carries.

Three readings, because any one of them alone is satisfied by a wrong tree:

1. the prefix is byte-identical across the write — the claim itself;
2. the builder really collected the new row — otherwise (1) holds just as well on a
   tree that lost the index altogether;
3. the row reaches the session in the context message — otherwise (1) holds on a tree
   where a session can no longer read its own memory.

Restoring the defect (bodies back in the render context *and* back in the template)
falsifies (1) and nothing else, which is what makes this the guard rather than a
restatement of the code that happens to pass.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer
from emrg.session import Session

#: The index's own heading, as the fixture writes it. The body must not be in the
#: prefix, and the first line of it is the cheapest thing to look for.
INDEX_HEADING = "# Memory Index"

ROW_ONE = "- [row-one](row-one.md) — present when the first prompt was built\n"
ROW_TWO = "- [row-two](row-two.md) — written after that prompt was built\n"


def _make_server() -> EmrgServer:
    """A minimal server, with the projects log pointed away from the real one.

    The shape `tests/test_memory_row_bound_reach.py::_make_server` uses, for the same
    reason: building a system prompt must never write the host's `~/.emrg/projects.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _session_with_an_index(tmp_path: Path) -> Session:
    memory_dir = tmp_path / ".emrg" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        f"{INDEX_HEADING}\n\n{ROW_ONE}", encoding="utf-8"
    )
    return Session.create_with_id("mem-cache-prefix", tmp_path)


def _append_a_row(session: Session) -> Path:
    """The write a session really makes — `open(..., "a")`, as `MemoryIndex` does."""
    index = session.cwd / ".emrg" / "memory" / "MEMORY.md"
    with index.open("a", encoding="utf-8") as fh:
        fh.write(ROW_TWO)
    return index


def _first_difference(before: str, after: str) -> str:
    """Where two renders first disagree — a bare `==` failure is otherwise unreadable."""
    for i, (a, b) in enumerate(zip(before, after)):
        if a != b:
            return (
                f"first difference at char {i}: {before[i:i + 60]!r} vs "
                f"{after[i:i + 60]!r}"
            )
    return f"nothing differs in the first {min(len(before), len(after))} chars; lengths {len(before)} vs {len(after)}"


def test_a_memory_write_does_not_move_the_system_prefix(tmp_path: Path) -> None:
    """Reading (1): the same builder, twice, with one row written in between."""
    server = _make_server()
    session = _session_with_an_index(tmp_path)

    before = server._build_system_prompt(session)
    _append_a_row(session)
    after = server._build_system_prompt(session)

    # Reading (2): the write is real and the builder can see it. Without this the
    # equality below would hold on a tree that collected no index at all.
    data = server._collect_memory_data(session)
    assert data is not None and ROW_TWO in data["project_memory_index"], (
        "the fixture did not take: the second row is not in the index the builder reads"
    )

    assert after == before, (
        "the system prompt moved when a memory was written, so the provider's prompt "
        f"cache misses on every request after a write — {_first_difference(before, after)}"
    )


def test_the_prefix_carries_the_paths_and_not_the_rows(tmp_path: Path) -> None:
    """The other half of reading (1): stability by *placement*, not by dropping the index.

    A prompt that simply stopped mentioning memory would satisfy the test above, so the
    paths stay and the rows are asserted out of it — the same split the template states.
    """
    server = _make_server()
    session = _session_with_an_index(tmp_path)

    rendered = server._build_system_prompt(session)

    assert str(session.cwd / ".emrg" / "memory" / "MEMORY.md") in rendered, (
        "the prefix must still name the index path — the agent needs to know the file "
        "exists even though its rows arrive in the context message"
    )
    assert INDEX_HEADING not in rendered, (
        "the index body is back in the system prefix: writing a memory now moves a "
        "prefix the provider's cache matches on"
    )
    assert ROW_ONE.strip() not in rendered


def test_the_rows_reach_the_session_in_the_context_message(tmp_path: Path) -> None:
    """Reading (3): the stability above must not cost the session its own memory."""
    server = _make_server()
    session = _session_with_an_index(tmp_path)
    _append_a_row(session)

    ctx = server._build_context_message(session)

    assert ctx is not None and ctx["role"] == "user"
    text = ctx["content"]
    for row in (ROW_ONE, ROW_TWO):
        assert row.strip() in text, (
            f"the row is in neither the prefix nor the context message: {row!r} — a "
            "session that cannot read its own memory is the price the fix must not pay"
        )
    assert INDEX_HEADING in text
    # Not merely the rows: the index reaches the model as the same bytes it holds on
    # disk, capped by `_cap_memory_index` and by nothing else.
    data = server._collect_memory_data(session)
    assert data["project_memory_index"] in text, (
        "the context message carries only part of the index — content loss is not a "
        "cache win (issue #2292, the 'never cheaper by being thinner' constraint)"
    )


def test_a_row_written_inside_the_freeze_window_is_visible_at_once(tmp_path: Path) -> None:
    """The freeze is about the clock; memory is not part of it (issue #2292, item 2).

    `context_refresh_interval_ms` freezes the time snapshot so consecutive requests stay
    byte-identical, and the writable-boundary line already rides *outside* it for the
    same reason (rant 2026-10-09T09:43:39 §8): a host who changes the boundary mid-window
    must not wait for it to expire. A memory row is stamped with no clock at all, so
    freezing it would hide the row the agent just wrote — from the very next round, for
    a window the host sets.
    """
    server = _make_server()
    session = _session_with_an_index(tmp_path)
    server.llm.config.context_refresh_interval_ms = 600_000

    first = server._build_context_message(session)["content"]
    assert ROW_ONE.strip() in first and ROW_TWO.strip() not in first

    _append_a_row(session)
    second = server._build_context_message(session)["content"]

    assert ROW_TWO.strip() in second, (
        "the row is invisible until the refresh window expires: the freeze is about "
        "the clock, and memory must not be dragged into it"
    )


def test_the_block_does_not_shadow_the_context_marker(tmp_path: Path) -> None:
    """`[context]` counts the *context frame*; the memory block must not add one.

    `tests/test_sandbox_extra_roots.py` reads the boundary line by counting that marker,
    so a second one here would make its reading a different question than it asks.
    """
    server = _make_server()
    session = _session_with_an_index(tmp_path)

    text = server._build_context_message(session)["content"]

    assert text.count("[context]") == 1, (
        "the memory block introduced a second `[context]` marker; the boundary line's "
        "own guard counts occurrences of that marker"
    )
