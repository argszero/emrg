"""A session id is one path component, and the delete path is where it must hold.

A session id arrives in a client message and is pasted onto
`<cwd>/.emrg/sessions/` before anything opens, reads or removes it. The kernel
resolves `..` and an absolute id at that point, so an id of `../..` names the
project tree rather than a session directory inside it.

Measured 2026-10-21 through `Session.delete("../..", <project>)`: the call walked
`<project>/.emrg/sessions/../..`, left the project tree empty — a `victim/`
directory and `.emrg/` alike — and then raised from `shutil.rmtree`'s final
`os.rmdir` on the unresolved path. These tests pin the refusal and, in the other
direction, that a real id still deletes its own directory.
"""

import pytest

from emrg.session import Session, is_safe_session_id


@pytest.mark.parametrize("session_id", [
    "emrg-task",
    "s_260727_1103_866d",
    "a",
    "sess-1.2",
    "会话一",
])
def test_a_plain_name_is_one_component(session_id):
    """Every id this daemon generates, and a client-chosen name beside them."""
    assert is_safe_session_id(session_id) is True


@pytest.mark.parametrize("session_id", [
    "",           # nothing to name
    ".",          # the sessions directory itself
    "..",         # its parent
    "../..",      # the project tree
    "a/b",        # a path, not a name
    "/abs",       # an absolute path replaces the join entirely
    "a\\b",       # a separator on Windows, an ordinary character on POSIX
    "..\\..",
    "C:evil",     # a drive-relative path: one component nowhere
    "x\0y",       # a name no filesystem accepts
    None,
    3,
])
def test_a_name_that_is_not_one_component_is_refused(session_id):
    assert is_safe_session_id(session_id) is False


def _project(tmp_path):
    """A project tree: one session directory, and a neighbour that must survive."""
    (tmp_path / ".emrg" / "sessions" / "keep").mkdir(parents=True)
    (tmp_path / "victim").mkdir()
    (tmp_path / "victim" / "precious.txt").write_text("keep me", encoding="utf-8")
    return tmp_path


def test_an_escaping_id_deletes_nothing(tmp_path):
    """The measured shape: the id resolves to the project tree, so nothing may move."""
    project = _project(tmp_path)

    assert Session.delete("../..", project) is False

    assert (project / "victim" / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert (project / ".emrg" / "sessions" / "keep").is_dir()


def test_a_real_id_still_deletes_its_own_directory(tmp_path):
    """The other direction: the refusal must not cost the feature."""
    project = _project(tmp_path)

    assert Session.delete("keep", project) is True

    assert not (project / ".emrg" / "sessions" / "keep").exists()
    assert (project / "victim" / "precious.txt").exists()
