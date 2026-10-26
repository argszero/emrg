"""A session id must name a directory on the platform that runs it.

`is_safe_session_id` answers "is this one entry inside the sessions directory", and on
Windows a whole class of name passes that question and still cannot be a directory: the
reserved device names. Measured on Windows Server 2022 at master `6ddb7cc` —
`is_safe_session_id('con')` returned True, and `Session.create_with_id('con', <agent>)`
raised `NotADirectoryError [WinError 267] The directory name is invalid`, naming the
operating system instead of the name. `CON`, `con.txt` and `com1` fail the same way; `nul`
gets one step further and fails on the `memory` subdirectory.

The trailing half is quieter and fails without raising: Windows strips trailing spaces and
dots from a name, so `a ` and `a.` name a directory the id does not spell. Measured:
`.. ` mkdir'd "successfully" (it resolved to the parent) and the first write into it
raised `FileNotFoundError`.

Both directions are pinned here, because a rule that refused everything would pass the
refusing half on its own: names that are ordinary on every platform stay accepted, the
reserved set is exactly the documented one, and a directory already on disk still loads.
"""

import sys

import pytest

from emrg.session import (
    Session,
    UnsafeSessionId,
    is_safe_session_id,
    session_id_refusal_reason,
)


@pytest.mark.parametrize("session_id", [
    "emrg-task",
    "s_260727_1103_866d",
    "sess-1.2",       # a dot inside the name is ordinary
    "My Task",        # spaces inside the name, not at the end
    "中文任务",
    "console",        # contains a reserved base, and is not one
    "con-artist",
    "com0",           # not in the documented set
    "com10",
    "lpt0",
    "auxiliary",
])
def test_a_name_a_directory_can_carry_is_accepted(session_id):
    """The names the daemon generates, host-chosen names, and the near-misses."""
    assert is_safe_session_id(session_id) is True
    assert session_id_refusal_reason(session_id) is None


@pytest.mark.parametrize("session_id", [
    "con",
    "CON",
    "Con",
    "con.txt",        # the extension does not help: it is the console too
    "CON.tar.gz",
    "prn",
    "aux",
    "nul",
    "nul.txt",
    "com1",
    "COM9",
    "lpt1",
    "LPT9",
    "a ",             # Windows strips the space, so this names `a`
    "a.",
    "con ",
])
def test_a_name_no_directory_can_carry_is_refused(session_id):
    assert is_safe_session_id(session_id) is False
    assert session_id_refusal_reason(session_id)


@pytest.mark.parametrize("session_id, expected", [
    # The shapes that were refused before this rule keep their sentence, byte for byte:
    # the daemon's own test pins it, and a path escape really is more than one component.
    ("../..", "a session id is one path component"),
    ("a/b", "a session id is one path component"),
    ("C:evil", "a session id is one path component"),
    (".", "a session id is one path component"),
    # The two new classes say something else, because for them that sentence is false.
    ("con", "a device name is one path component"),
    ("a ", "Windows strips a trailing space or dot"),
])
def test_the_reason_names_the_rule_that_fired(session_id, expected):
    """A refusal a reader cannot act on is only half a refusal."""
    assert session_id_refusal_reason(session_id).startswith(expected)


def test_an_ordinary_name_still_creates_its_directory(tmp_path):
    """The other direction: the new rules must not refuse the names hosts use."""
    session = Session.create_with_id("My Task", tmp_path)
    assert session.session_id == "My Task"
    assert (tmp_path / ".emrg" / "sessions" / "My Task" / "meta.json").is_file()


@pytest.mark.parametrize("session_id", ["con", "nul", "com1", "a ", "a."])
def test_a_refused_name_never_reaches_the_filesystem(session_id, tmp_path):
    """Requirement 3: a named error before a directory is attempted, not after it failed.

    Constructing a `Session` is what creates the directory, so the refusal has to come
    first — and the assertion is that no directory was left behind, which is what a caller
    sees when the platform error is replaced by this one.
    """
    with pytest.raises(UnsafeSessionId) as raised:
        Session.create_with_id(session_id, tmp_path)
    assert "cannot name a session" in str(raised.value)
    assert not (tmp_path / ".emrg" / "sessions" / session_id).exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the directory cannot exist on Windows, which is the whole point of the rule",
)
def test_a_directory_already_on_disk_still_loads(tmp_path):
    """The rule is about creating a name, not about reading what is already there."""
    target = tmp_path / ".emrg" / "sessions" / "con"
    target.mkdir(parents=True)
    (target / "meta.json").write_text('{"session_id": "con"}', encoding="utf-8")
    loaded = Session.load("con", tmp_path)
    assert loaded.session_id == "con"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the directory cannot exist on Windows, which is the whole point of the rule",
)
def test_deleting_such_a_directory_is_refused_by_the_same_name_rule(tmp_path):
    """The stated cost of a platform-independent rule, pinned so it is a decision.

    An id the predicate refuses cannot be deleted through `Session.delete` even where its
    directory exists, because the refusal is about the name. Nothing outside the sessions
    directory is reachable either way, and the alternative — a platform-conditional rule —
    would certify on POSIX an id that Windows cannot create.
    """
    target = tmp_path / ".emrg" / "sessions" / "con"
    target.mkdir(parents=True)
    (target / "meta.json").write_text('{"session_id": "con"}', encoding="utf-8")
    assert Session.delete("con", tmp_path) is False
    assert (target / "meta.json").is_file()
