"""The archive protocol is retired in the prompt, and the ruler that replaces it,
where a cycle reads it.

Why this file exists
--------------------
`scripts/archive-memory-index.py` moved MEMORY.md's oldest cycle rows into
`cycle-archive-YYYYMMDD.md` once an index passed 50 cycle rows, and the shipped
evolution prompt stated that protocol (hard cap of 50 cycle rows, the archive file,
"archive files are excluded from the system prompt", a row-cap catch-up check). The
host measured the mechanism's own rule set (`~/.emrg/designs/memory-index-compaction-design.md`
§3): an archived row left the prompt and landed in a file the prompt never embeds, so
the net effect was content leaving context rather than an index getting shorter — and
the count it watched was the wrong subset (the index that reached 195 lines satisfied
"≤50 cycle rows"). §4 retires the tool and the protocol with it, and replaces the
prompt's clause with the rule that is left: **an index past 100 lines is compacted in
place by the agent itself, every row staying one line of at most 512 chars.**

The retirement is what these tests hold, because a retirement that is only a commit
is one revert away from being undone in silence: the names of the removed protocol are
the exact strings a later cycle would paste back if it re-invented the mechanism.

Why the pin renders instead of grepping the template
----------------------------------------------------
The template is rendered with ``undefined=jinja2.Undefined``, so a name absent from
the builder's context renders as the empty string: text in the file can be missing
from the prompt an instance receives, and a file-level grep would still pass. These
tests render through the **real builder** (``TaskHandler._build_evolution_prompt``)
and read what a cycle is actually sent.

Why the absence test asserts the block is still there
-----------------------------------------------------
A test that says "these strings are gone from the prompt" passes for the wrong reason
if someone deletes the whole §6 hygiene block — an absent block contains none of them.
So the negative test first requires the block's own opening words in the rendered
prompt, and fails saying it cannot measure rather than passing quietly.

Named limit
-----------
Both numbers are pinned, each to the source of truth that acts on it: `512` to
`emrg/memory.py`'s `INDEX_TITLE_MAX_CHARS`, which truncates the line the writer
stores, and `100` to `emrg/server/daemon.py`'s `MEMORY_INDEX_ROW_CAP`, which is the
count the daemon's own compaction instruction renders (`cap=MEMORY_INDEX_ROW_CAP`) and
the count its trigger fires on. The design's §2 requires the prompt's `100` to come
from that constant and forbids a second spelling of it; the two are asserted equal
here, so a change to either one alone fails this file instead of leaving the shipped
prompt quietly wrong about when a cycle must compact. (When this file was first
written the constant did not exist in the tree — it landed with #1602 — and the
literal was the only spelling available; pinning it was the follow-up both that PR and
#1603 named, and it is done here.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from emrg.memory import INDEX_TITLE_MAX_CHARS
from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import TaskHandler

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"
TEMPLATE = PROMPTS_DIR / "evolution_prompt.md"
RETIRED_SCRIPT = REPO_ROOT / "scripts" / "archive-memory-index.py"
RETIRED_SCRIPT_TESTS = REPO_ROOT / "tests" / "test_archive_memory_index.py"

#: The retired protocol's load-bearing words, each taken verbatim from the clause §4
#: deletes. A later edit that reintroduces the mechanism has to write one of these.
RETIRED_TERMS = (
    "50 most recent cycle rows",
    "cycle-archive-YYYYMMDD.md",
    "Row-cap check",
    "archive-memory-index",
)

#: The replacement rule's opening words. Its presence is what makes the absences above
#: a measurement rather than an empty region.
BLOCK_START = "**Index hygiene protocol"
BLOCK_END = "\n- Keep the file format identical"

#: The blueprint the replacement points at (§4: "改指本文" — point at this document),
#: **as the reader can open it**. This clause is read by an agent whose cwd is the
#: repository, and the repository's `.emrg/` holds `memory/` and `sessions/` — no
#: `designs/`: measured 2026-09-25, `Path(".emrg/designs/<name>").resolve()` is
#: `<repo>/.emrg/designs/<name>` and does not exist, while the `~` spelling resolves to
#: the host's design and both `read` (`Path(...).expanduser()`) and `bash` expand it.
#: So a bare repo-relative form is a pointer the prompt's own reader cannot follow.
DESIGN_NAME = "memory-index-compaction-design.md"
DESIGN_RELATIVE = f".emrg/designs/{DESIGN_NAME}"
DESIGN_RESOLVABLE = re.compile(rf"~/\.emrg/designs/{re.escape(DESIGN_NAME)}")

#: The design's own threshold, §0: 行数 > 100 — deliberately not spelled here. The
#: number this file asserts is the one the daemon counts with (`MEMORY_INDEX_ROW_CAP`,
#: imported inside the test below), because the design's §2 forbids a second spelling
#: of the same number; a literal in this file would be that second spelling.
LINE_THRESHOLD = re.compile(r"\*\*(\d+) lines\*\*")
ROW_BOUND = re.compile(r"`INDEX_TITLE_MAX_CHARS`\s*\(\*\*(\d+)\*\*")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """The real template, rendered through the real builder with the real context."""
    tmp_path = tmp_path_factory.mktemp("index-rule")
    project_dir = tmp_path / "demoproj"
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "demoproj", "path": str(project_dir)}]), encoding="utf-8"
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = TaskHandler(
            name="demo-task",
            config={"project": "demoproj"},
            interval=300,
            identity=InstanceIdentity(),
            template_path=PROMPTS_DIR / "evolution_prompt.md",
        )
        return handler._build_evolution_prompt()
    finally:
        mod.config_dir = original


def _hygiene_block(rendered: str) -> str:
    """The §6 index-hygiene block as shipped, or a failure to measure — never a pass."""
    start = rendered.find(BLOCK_START)
    assert start != -1, (
        f"the rendered prompt no longer contains the {BLOCK_START!r} block — this test "
        "cannot measure what stands in its place, which is a failure to measure, not a "
        "pass"
    )
    end = rendered.find(BLOCK_END, start)
    assert end > start, (
        f"the block's end anchor {BLOCK_END!r} no longer follows {BLOCK_START!r} — the "
        "prompt was restructured, so the region this test reads is not the one it "
        "describes"
    )
    return rendered[start:end]


def test_the_retired_protocol_is_gone_from_the_shipped_prompt(rendered: str) -> None:
    """The clause a cycle reads states the rule that replaced the archive, not both."""
    block = _hygiene_block(rendered)
    survivors = [term for term in RETIRED_TERMS if term in block]
    assert not survivors, (
        f"emrg/server/evolution_prompt.md §6 still carries the retired archive "
        f"protocol: {survivors}. The host retired it by design (§4) because it moved "
        "rows out of the index without shortening it — reinstating it means a cycle "
        "archives again instead of compacting"
    )


def test_the_prompt_states_the_replacement_ruler_where_a_cycle_reads_it(rendered: str) -> None:
    """Line count first, then the per-row bound, then the blueprint that explains it.

    The threshold is pinned to the number the daemon counts with, not to a literal in
    this file: the design's §2 says the prompt's `100` is rendered from that constant
    and bans a second spelling of it. `MEMORY_INDEX_ROW_CAP` is the spelling the
    trigger acts on, and it is also what the daemon's own compaction instruction
    renders — so a cycle reading the prompt compacts at exactly the count that fires
    the instruction.
    """
    from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

    block = _hygiene_block(rendered)

    stated = LINE_THRESHOLD.findall(block)
    assert stated, (
        "the §6 block states no line threshold for the memory index (anchor: "
        "'**N lines**') — the retired protocol is gone and nothing names the ruler that "
        "replaced it, so a cycle has no target"
    )
    assert set(stated) == {str(MEMORY_INDEX_ROW_CAP)}, (
        f"the block states {sorted(set(stated))} lines and the trigger fires at "
        f"MEMORY_INDEX_ROW_CAP = {MEMORY_INDEX_ROW_CAP} (emrg/server/daemon.py) — one "
        "number, two spellings: the prompt would tell a cycle to compact at a line count "
        "the daemon does not count with. Move both together, or render the prompt's "
        "number from the constant"
    )
    assert DESIGN_RESOLVABLE.search(block), (
        f"the block no longer points at the blueprint as `~/` + `{DESIGN_RELATIVE}` — "
        "the *how* of compaction lives there and in the daemon's instruction, not in "
        "this clause. A repo-relative spelling is not equivalent: `.emrg/` in this "
        "checkout has no `designs/`, so it names a file the prompt's own reader cannot "
        "open (measured 2026-09-25)"
    )


def test_the_row_bound_the_prompt_states_is_the_bound_the_writer_enforces(rendered: str) -> None:
    """The other half of why 100 lines fit: the number here is the writer's number."""
    block = _hygiene_block(rendered)
    stated = ROW_BOUND.findall(block)
    assert stated, (
        "the §6 block no longer states a per-row character bound beside "
        "`INDEX_TITLE_MAX_CHARS` — the two numbers together are why 100 lines fit the "
        "embed budget, so one without the other is half a rule"
    )
    assert set(stated) == {str(INDEX_TITLE_MAX_CHARS)}, (
        f"the block states {sorted(set(stated))} chars per row and the writer truncates "
        f"at INDEX_TITLE_MAX_CHARS = {INDEX_TITLE_MAX_CHARS} (emrg/memory.py) — one "
        "bound, two spellings: the prompt would licence rows the writer cannot keep"
    )


def test_the_retired_tool_and_its_tests_stay_deleted() -> None:
    """A retirement is also an absence: the script cannot come back on its own."""
    assert not RETIRED_SCRIPT.exists(), (
        f"{RETIRED_SCRIPT.relative_to(REPO_ROOT)} is back — the protocol it implemented "
        "was retired by design §4 (it shortened nothing: an archived row left the prompt "
        "and stayed in a file the prompt never embeds). Reinstating the tool reinstates "
        "the protocol"
    )
    assert not RETIRED_SCRIPT_TESTS.exists(), (
        f"{RETIRED_SCRIPT_TESTS.relative_to(REPO_ROOT)} is back — its subject is gone"
    )
