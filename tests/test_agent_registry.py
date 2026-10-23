"""The agent register: one directory, one row, one name — and no normalisation.

What these pin is the whole of the rule the daemon's older ``_touch_project`` did not
follow: ``path`` is the string the session holds, a directory under another Agent's
``.emrg/`` is that Agent's bookkeeping rather than a workspace, and a name is unique
because a task resolves its directory by name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emrg.server.agents import AgentRegistry, _unique_name


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(tmp_path / "config" / "agents.yml")


# ── the path is stored verbatim ─────────────────────────────────────────────

def test_a_directory_is_stored_exactly_as_the_session_holds_it(tmp_path):
    """No realpath, no trailing-slash trim: the row answers ``str(session.cwd)``."""
    reg = _registry(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    row = reg.for_dir(str(work))

    assert row["path"] == str(work)
    assert reg.entries()[0]["path"] == str(work)


def test_a_symlinked_directory_is_its_own_row(tmp_path):
    """The direct evidence that ``realpath`` is gone: two spellings, two rows."""
    reg = _registry(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    reg.for_dir(str(real))
    reg.for_dir(str(link))

    paths = {row["path"] for row in reg.entries()}
    assert paths == {str(real), str(link)}, (
        "the symlink was collapsed onto its target — that is the normalisation this "
        f"register no longer performs: {paths}"
    )


# ── one directory, one row ──────────────────────────────────────────────────

def test_a_second_session_refreshes_the_row_instead_of_adding_one(tmp_path):
    reg = _registry(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    first = reg.for_dir(str(work))
    second = reg.for_dir(str(work))

    rows = reg.entries()
    assert len(rows) == 1, rows
    assert second["last_active"] >= first["last_active"]
    assert second["name"] == first["name"]


def test_a_subdirectory_of_a_registered_directory_gets_its_own_row(tmp_path):
    """The direct evidence that the parent merge is gone.

    It used to refresh the parent's ``last_active`` and write nothing, unless the
    subdirectory happened to be a git root — so a package in a monorepo, a clone
    inside a larger checkout, and a plain subdirectory were all invisible.
    """
    reg = _registry(tmp_path)
    parent = tmp_path / "repo"
    child = parent / "src"
    child.mkdir(parents=True)

    reg.for_dir(str(parent))
    reg.for_dir(str(child))

    paths = [row["path"] for row in reg.entries()]
    assert str(child) in paths, f"the subdirectory was merged into its parent: {paths}"


def test_a_directory_under_an_agents_emrg_is_not_registered(tmp_path):
    """The sub-session slot and the boss session: that Agent's bookkeeping, not a workspace."""
    reg = _registry(tmp_path)
    parent = tmp_path / "repo"
    parent.mkdir()
    slot = parent / ".emrg" / "sessions" / "sid1" / "sub-agent" / "sub1"
    slot.mkdir(parents=True)

    reg.for_dir(str(parent))
    assert reg.for_dir(str(slot)) == {}

    assert [row["path"] for row in reg.entries()] == [str(parent)]


def test_the_home_directory_is_not_registered(tmp_path):
    """The one path exception kept: a session there would write beside the config."""
    reg = _registry(tmp_path)

    assert reg.for_dir(os.path.expanduser("~")) == {}
    assert reg.entries() == []


# ── names are unique ────────────────────────────────────────────────────────

def test_two_directories_with_one_basename_get_distinct_names(tmp_path):
    """A task resolves its directory *by name*, so a shared name is a misroute."""
    reg = _registry(tmp_path)
    first = tmp_path / "a" / "src"
    second = tmp_path / "b" / "src"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    one = reg.for_dir(str(first))
    two = reg.for_dir(str(second))

    assert (one["name"], two["name"]) == ("src", "src-2")
    by_name = {row["name"]: row["path"] for row in reg.entries()}
    assert by_name == {"src": str(first), "src-2": str(second)}, (
        "each name must resolve to its own directory — a task looks its agent up "
        f"by name, so a shared one is a silent misroute: {by_name}"
    )


def test_the_suffix_is_allocated_against_the_names_present_at_creation():
    assert _unique_name("src", set()) == "src"
    assert _unique_name("src", {"src"}) == "src-2"
    assert _unique_name("src", {"src", "src-2"}) == "src-3"
    assert _unique_name("src", {"src-3"}) == "src"


def test_removing_a_row_drops_it_and_frees_its_name(tmp_path):
    reg = _registry(tmp_path)
    first = tmp_path / "a" / "src"
    second = tmp_path / "b" / "src"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    reg.for_dir(str(first))
    reg.for_dir(str(second))

    assert reg.remove("src-2") is True
    assert [row["name"] for row in reg.entries()] == ["src"]
    assert reg.remove("src-2") is False, "a second removal finds nothing to remove"


def test_a_register_that_cannot_be_opened_answers_no_decision(tmp_path):
    """``None`` is not ``False``: nothing was read, so nothing was decided."""
    reg = _registry(tmp_path)
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.mkdir()  # a directory where the register belongs

    assert reg.remove("anything") is None
    assert reg.entries() == []


# ── the daemon's wire: registration happens where a session is opened ───────

def test_opening_a_session_registers_the_directory(tmp_path):
    from emrg.server.daemon import EmrgServer

    server = object.__new__(EmrgServer)
    server._agents_log = tmp_path / "config" / "agents.yml"
    work = tmp_path / "work"
    work.mkdir()

    server._get_or_create_session("sid1", work)

    rows = server._agents.entries()
    assert [row["path"] for row in rows] == [str(work)], (
        "registration belongs at session creation, not on an inbound message"
    )


def test_the_upgrade_session_registers_nothing(tmp_path):
    """It runs in the installation's own work directory, not one a host opened."""
    from emrg.server.daemon import EmrgServer

    server = object.__new__(EmrgServer)
    server._agents_log = tmp_path / "config" / "agents.yml"
    work = tmp_path / "upgrade-work" / "emrg"
    work.mkdir(parents=True)

    # A plain id on purpose: the real `emrg-upgrade` id is refused outright by the
    # hermeticity guard, whose subject is that id and not this keyword.
    server._get_or_create_session("an-upgrade-run", work, register=False)

    assert server._agents.entries() == []
