"""The files the daemon embeds as project context must name files this repository has.

The defect class, one carrier over from PR #1610
------------------------------------------------
`EmrgServer._collect_project_context` reads `PROJECT_CONTEXT_FILES` out of a session's
cwd and puts their text into the system prompt of every session whose cwd is this
checkout — the same render site a task session and a host conversation both go through.
For this repository that is `Agent.md` (embedded whole, and the file that *is* the
instruction an agent reads) and `MANIFESTO.md` (cut at the cap). Nothing in the tree
reads the paths those two name: retire or rename a gate and the brief keeps sending
every session after it to a file that is gone, with no failure anywhere — which is the
#1551 class (a record naming a script a merge had retired), in the carrier #1610 closed
for the prompt *templates*. This is the same rule over the other carrier the daemon
renders, sharing that scan (`tests/test_prompt_templates.py`) rather than restating it:
one rule, two carriers, one implementation.

Measured 2026-09-25 (cycle cyc20260925-160939) before the guard was written: the two
files this checkout ships name **19** repo-relative paths — 18 in `Agent.md` (every
merge gate the cycle loop is told to run by name, plus `tests/test_version_sync.py` and
`packaging/gen-assets.sh`) and 1 in `MANIFESTO.md` — and **all 19 resolve**. So this
guard is green today and its value is the next rename, not a repair.

Where the set comes from
------------------------
The scan reads `PROJECT_CONTEXT_FILES` from the daemon rather than listing the four
names again: a second spelling could disagree with the code that embeds them — the
same reason the cap beside it is imported (`tests/test_agent_md_prompt_cap.py`). The
test below drives the real collector, so the constant is pinned to what is embedded
rather than to a comment.

Named limits. The scan reads the same shape #1610's does: a path in one of the four
top-level directories a file can only mean as *this* repository's, written plainly —
an absolute path, a `{{ source_dir }}/`-qualified one, a `.emrg/`-nested one and a glob
are all outside what it can check. And it reads `Agent.md`/`MANIFESTO.md` as they are on
this checkout, which is what CI holds; a path that resolves only in an author's working
tree fails there.

The instrument's own both-ways controls — a retired path flagged, a real one and a glob
not — are where the instrument lives (`test_the_prompt_path_scan_answers_both_ways`), and
the property that makes *this* file's verdict meaningful is the one asserted first below:
the set it scans is the set the prompt embeds.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.server.daemon import PROJECT_CONTEXT_FILES, EmrgServer
from emrg.session import Session
from tests.test_prompt_templates import (
    _named_repo_paths_without_a_file,
    _repo_paths_named_in,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _embedded_here() -> list[Path]:
    """The files of the embedded set this checkout actually has, in the daemon's order."""
    return [p for p in (REPO_ROOT / name for name in PROJECT_CONTEXT_FILES) if p.is_file()]


def test_the_embedded_set_is_the_one_this_scan_reads() -> None:
    """The subject is the daemon's list, and this checkout ships a non-empty part of it.

    Without this the scan could pass over a set that is empty — green because it read
    nothing, which is the shape this project refuses to call a pass.
    """
    files = _embedded_here()
    assert files, (
        f"none of {PROJECT_CONTEXT_FILES} is a file at {REPO_ROOT} — there is nothing embedded "
        "here, so the scan below would pass vacuously"
    )
    assert {path.name for path in files} <= set(PROJECT_CONTEXT_FILES)
    assert "Agent.md" in {path.name for path in files}, (
        "this repository's brief is the file the scan is for; a checkout without it is "
        "not one this guard can speak about"
    )


def test_the_collector_embeds_exactly_the_files_the_constant_names(tmp_path: Path) -> None:
    """The list is the one the prompt is built from, and a file outside it is not embedded.

    Read through the production method rather than inferred from the constant's name: a
    constant nothing reads would keep this guard green while the prompt embedded some
    other set. `NOTES.md` is the control for the other direction — a markdown file in the
    session cwd that is *not* in the list must not reach the prompt, or "the set" would
    be "every file in the directory".
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    for name in PROJECT_CONTEXT_FILES:
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    (tmp_path / "NOTES.md").write_text("# not in the embedded set\n", encoding="utf-8")
    session = Session.create_with_id("project-context-set", tmp_path)

    got = [entry["name"] for entry in server._collect_project_context(session)]
    assert got == list(PROJECT_CONTEXT_FILES), (
        f"the prompt embedded {got}, and PROJECT_CONTEXT_FILES says "
        f"{list(PROJECT_CONTEXT_FILES)} — the guard that reads the constant would then be "
        "reading a list nothing renders"
    )


def test_every_repo_path_a_project_context_file_names_is_a_file_in_the_tree() -> None:
    """The brief's commands name files this repository has.

    Measured before the guard was written: 19 named paths across the two files this
    checkout ships, all of them resolving. The criterion is existence in the checkout,
    because that is what a fresh clone and CI hold — so a name that survives only in an
    author's working tree fails here.
    """
    files = _embedded_here()
    texts = {path.name: path.read_text(encoding="utf-8") for path in files}

    missing = {
        name: gone
        for name, text in texts.items()
        if (gone := _named_repo_paths_without_a_file(text))
    }
    assert not missing, (
        f"these project-context files name a path this repository does not have: {missing} — "
        "the daemon puts this text in the prompt of every session running in this checkout, so "
        "a citation that stopped resolving sends every session after it to a file that is gone"
    )

    named = {path for text in texts.values() for path in _repo_paths_named_in(text)}
    assert len(named) >= 5 and any(path.startswith("scripts/") for path in named), (
        f"the scan found {sorted(named)} — too few paths, and none under `scripts/`, to show "
        "that it is reading the briefs' commands at all"
    )
