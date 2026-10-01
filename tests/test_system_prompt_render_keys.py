"""The render context must fill every variable `system.j2` reads.

`emrg/server/prompts/system.j2` is the one prompt **every** session is rendered under,
and Jinja2's default `Undefined` prints an unfilled variable as the **empty string** —
no exception, no warning. `_get_jinja_env()` uses that default deliberately (the sibling
reach tests render it with a deliberately minimal context, `tests/test_no_background_process_reach.py`
and `tests/test_upgrade_chain_red_line.py`), so the failure mode this file closes is not
"a render raised" but "a render succeeded and the sentence lost its number".

That is the defect class issue #1795 names. Its third acceptance item asks for a hygienic
check that no `system.j2` render site other than `_build_system_prompt` exists, "a second
render would supply neither key and print the numbers blank" (measured 2026-10-01: a render
with `has_memories` set and those two keys absent prints `at most  characters per line` —
silently). The check below is the same invariant from the other end and for **every**
variable rather than those two: the builder's context, rendered under Jinja2's
`StrictUndefined`, must raise nothing — so a variable a later edit adds to the template
without a matching key in `_build_system_prompt` fails here instead of reaching a session
as a blank.

**What this does not do, measured rather than assumed**: run against `#1796`'s tree it
passes, because that PR does supply both keys. So it is not a substitute for #1795's third
acceptance item — that one forbids a *second render site*, while this one forbids a
`system.j2` variable the builder never fills. They are the two directions of the same
silence, and a reader should not take either for the other.

The context is the **builder's own**, not a hand-written dict: a render assembled by this
file would answer "is this dict complete?", which is not the question. Every conditional in
the template is made live (a project index, a session index, project context files) so that
a key sitting behind `{% if %}` is still accessed — a variable inside a false branch is never
read, and StrictUndefined would not raise for it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import jinja2
import pytest

from emrg.config import LlmConfig
from emrg.server import daemon
from emrg.server.daemon import EmrgServer
from emrg.session import Session

PROMPTS_DIR = Path(daemon.__file__).resolve().parent / "prompts"


def _strict_env() -> jinja2.Environment:
    """The daemon's own environment with one property changed: an undefined name raises.

    The loader, `autoescape` and the two whitespace flags are copied from
    `_get_jinja_env` — the daemon's render, minus the tolerance this file exists to remove.
    A hand-built environment that differed in a flag would be measuring a different
    template.
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(PROMPTS_DIR),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )


def _make_server() -> EmrgServer:
    """A minimal server, with the projects log pointed away from the real one.

    The shape `tests/test_daemon.py::_make_server` uses, for the same reason: building a
    system prompt must never write the host's `~/.emrg/projects.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _session_with_every_block_live(tmp_path: Path) -> Session:
    """A session whose cwd and memory dir make the template's every branch render.

    Both indexes and the project-context files are written, because the keys behind those
    conditionals are the ones a "does a plain session render?" test would never reach: a
    variable read only when memories exist is exactly the variable that reaches the writer
    of a *promote* session, which is what issue #1795 measured (`INDEX_TITLE_MAX_CHARS` and
    `MEMORY_INDEX_ROW_CAP` lived in a carrier only an evolution cycle reads).
    """
    memory_dir = tmp_path / ".emrg" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
        encoding="utf-8",
    )
    (tmp_path / "Agent.md").write_text("# conformance notes\n", encoding="utf-8")
    session = Session.create_with_id("render-keys", tmp_path)
    session.memory_dir.mkdir(parents=True, exist_ok=True)
    (session.memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [a note](a-note.md) — one row\n", encoding="utf-8"
    )
    return session


def test_the_builders_context_fills_every_variable_the_template_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole prompt, through the daemon's own builder, with nothing left undefined.

    A pass here is the statement "every name `system.j2` reads is in the dict
    `_build_system_prompt` builds". The mutation that shows this test can fail is the other
    direction and is not written here: delete any `ctx[...] = ...` line from the builder
    and this render raises `UndefinedError`, because every conditional is live.
    """
    monkeypatch.setattr(daemon, "_get_jinja_env", _strict_env)
    server = _make_server()

    rendered = server._build_system_prompt(_session_with_every_block_live(tmp_path))

    assert rendered, "the builder returned nothing: the strict render cannot have passed"
    assert "You are EMRG" in rendered


def test_an_omitted_key_is_silent_under_the_daemons_own_env() -> None:
    """What the test above buys, both directions, on a line measured rather than imagined.

    `system.j2:23-24` reads `{% if os_name %}**Operating system**: ``{{ os_name }}``
    ({{ platform_detail }})`. Given `os_name` and **no** `platform_detail`, the daemon's own
    environment renders that line happily — the platform value is simply gone and the line
    still reads like a sentence:

        **Operating system**: `test` ()

    No exception, no marker, and nothing in this tree reads the prompt back to notice. Under
    `StrictUndefined` the same render raises and names the key. So a key omitted from
    `_build_system_prompt` is not "a cosmetic gap": it is a silent false statement in the
    one prompt every session receives, which is why completeness is asserted rather than
    assumed. The pair also pins the mechanism itself — if `_get_jinja_env()` ever grew
    `StrictUndefined`, the tolerant half of this test would start raising and say so.
    """
    context = {"os_name": "test", "config_dir": "/nonexistent"}

    tolerant = daemon._get_jinja_env().get_template("system.j2").render(**context)
    assert "**Operating system**: `test` ()" in tolerant, (
        "the line that reads an unsupplied key must be visible in the render: if this "
        "assertion fails the demonstration below is measuring a different line"
    )

    with pytest.raises(jinja2.UndefinedError) as excinfo:
        _strict_env().get_template("system.j2").render(**context)
    assert "platform_detail" in str(excinfo.value), str(excinfo.value)
