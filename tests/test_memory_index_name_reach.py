"""The memory index's *name* must reach the session that has to create it.

Measured 2026-11-21 on this host (issue #3117). `system.j2` renders the line that names the
index file only inside `{% if agent_memory_index_path %}`, and the daemon supplies that key only
when the file already exists (`_collect_memory_data`). So the one round in which a session has to
choose a name — the round after it is told "Create the agent memory directory with its index" —
was the one round not told the name. A registered agent on this host (the life-review judge) read
exactly that sentence, created `.emrg/memory/index.md` with a real `status: active` entry beside
it, and nothing here reads that filename: the prompt then kept saying "No memories yet" on every
later round, and kept not naming the file.

The same section already learned this lesson one level up, for the row bound and the line cap
(`tests/test_memory_row_bound_reach.py`): both numbers used to sit inside the `has_memories` gate,
so the round that creates the index was the round told nothing about how long a row may be. Those
were moved out and injected. The filename is that defect one step further along.

The pair this file pins
-----------------------
A statement in the template is not the fix — the *name* has to be the one the loader reads, so
the pair is measured together: what the prompt tells a session to create, and what
`_collect_memory_data` then finds. `test_the_name_the_prompt_gives_is_the_one_the_loader_reads`
creates the file the sentence names and requires the loader to report a memory; a template naming
anything else fails it. `test_a_different_name_still_reads_as_no_memories` is the same measurement
from the other side, and it is the case that actually happened: a plausibly-named file is read as
no memory at all.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.memory import MEMORY_INDEX_NAME
from emrg.server.daemon import EmrgServer
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The template's variable for the name. Named here so this file and the template cannot
#: disagree about which name is the one, the same way the row bound's variable is named.
NAME_VAR = "{{ memory_index_name }}"

#: The sentence a session with no memory reads, as a marker that survives rewording.
EMPTY_STATE_MARKER = "No memories yet"

#: A plausible filename an agent invented instead. Kept as a literal because it is the *wrong*
#: name: it is not in the code and must stay out of it.
INVENTED_NAME = "index.md"


def _make_server() -> EmrgServer:
    """A minimal server, with the agents log pointed away from the real one."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _session_without_an_index(tmp_path: Path) -> Session:
    """A session whose cwd carries no `MEMORY.md` — the prompt that tells it to create one."""
    return Session.create_with_id("index-name-no-index", tmp_path)


def _agent_memory_dir(session: Session) -> Path:
    """The directory `_collect_memory_data` reads the agent index from."""
    return session.agent.memory_dir


def _empty_state_sentence(rendered: str) -> str:
    """The one line of a render that asks for an index to be created."""
    for line in rendered.splitlines():
        if EMPTY_STATE_MARKER in line:
            return line
    return ""


def test_the_empty_state_names_the_index_file() -> None:
    """The template itself: the sentence carries the variable, not a bare "its index".

    Read in the template rather than only in a render because the defect was structural — the
    name lived exclusively in the branch that renders once the file exists. A render alone
    would not say which branch the name came from.
    """
    source = SYSTEM_PROMPT.read_text(encoding="utf-8")
    sentence = _empty_state_sentence(source)
    assert sentence, "the template must still carry the sentence that asks for an index"
    assert NAME_VAR in sentence, (
        "the sentence that tells a session with no memory to create its index must name the "
        "file, as " + NAME_VAR + "; it reads: " + sentence.strip()
    )


def test_the_name_the_prompt_gives_is_the_one_the_loader_reads(tmp_path: Path) -> None:
    """The discriminating pair: create what the sentence names, and the loader must find it.

    A template naming a file nobody reads is exactly the defect. This test cannot pass by the
    statement being present — it creates that file and requires `_collect_memory_data` to report
    a memory afterwards.
    """
    server = _make_server()
    session = _session_without_an_index(tmp_path)
    rendered = server._build_system_prompt(session)
    assert EMPTY_STATE_MARKER in rendered, (
        "this reading is about the branch that asks for an index; the prompt rendered the "
        "indexed block instead, so the case under test did not happen"
    )
    assert server._collect_memory_data(session) is None, (
        "the fixture already carries a readable index, so nothing is being tested"
    )

    sentence = _empty_state_sentence(rendered)
    names = re.findall(r"`([^`]+\.md)`", sentence)
    assert names, (
        "the rendered prompt must name the index file in backticks; the sentence reads: "
        + sentence.strip()
    )
    named = names[0]

    memory_dir = _agent_memory_dir(session)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / named).write_text(
        "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
        encoding="utf-8",
    )

    data = server._collect_memory_data(session)
    assert data is not None, (
        f"a session that created `{named}`, the file its own prompt named, must have a memory "
        "index; the loader found none, so the prompt and the loader disagree about the name"
    )
    assert data["agent_memory_index_path"].endswith(named), data


def test_a_different_name_still_reads_as_no_memories(tmp_path: Path) -> None:
    """The case that happened: a plausibly-named file is no memory at all.

    Written as a positive statement about the loader rather than a complaint about the agent:
    the daemon reads one name, so a directory holding only another one is a session with no
    memory, whatever its files say.
    """
    server = _make_server()
    session = _session_without_an_index(tmp_path)
    memory_dir = _agent_memory_dir(session)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / INVENTED_NAME).write_text("# Memory index\n", encoding="utf-8")

    assert server._collect_memory_data(session) is None, (
        f"a memory directory holding only `{INVENTED_NAME}` must read as no memory - that is "
        "the state an agent with no name instruction landed in, and it is why the prompt has "
        "to name the file"
    )


def test_the_name_is_spelled_once_in_the_code() -> None:
    """One spelling, so the template's name and the loader's cannot drift apart.

    The name was spelled nine times across two modules before this change. A guard that only
    checked the template would let a tenth spelling back in beside it, which is the shape this
    issue is about: two names for one file is how a memory becomes invisible.
    """
    literal = f'"{MEMORY_INDEX_NAME}"'
    offenders = []
    for path in sorted((REPO_ROOT / "emrg").rglob("*.py")):
        if path.name == "memory.py":
            continue  # the constant's own module, where the one spelling lives
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if literal in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "the index's file name must come from `emrg.memory.MEMORY_INDEX_NAME`, not from a "
        "second spelling - these sites hardcode it:\n" + "\n".join(offenders)
    )
