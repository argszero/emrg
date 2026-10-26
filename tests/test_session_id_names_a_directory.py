"""A session id becomes a directory, so a name no directory can carry is refused here.

Measured on Windows Server 2022, master `6ddb7cc`: `is_safe_session_id("con")` answered
`True`, and `Session.create_with_id("con", <agent>)` then raised `NotADirectoryError
[WinError 267]` out of the mkdir — an operating-system error about a path, for a name the
host chose. `nul` got one step further and failed on the `memory` sub-directory
(`FileNotFoundError [WinError 3]`), and an id ending in a space or a dot is *folded* by
Windows rather than refused, so it names a different directory than the id says.

The accepted half is the discriminating one: `console`, `con-artist`, `com10` and `lpt0`
sit beside the reserved set without being in it, and a predicate that refused them would
cost the host names the platform accepts. Nothing here bans a space or a non-ASCII name —
`My Task` and `中文任务` are ordinary session names and stay so.
"""

import pytest

from emrg.session import (
    Session,
    UnsafeSessionId,
    is_safe_session_id,
    session_id_refusal,
)


@pytest.mark.parametrize("session_id", [
    "con",
    "CON",
    "Con",
    "con.txt",
    "nul",
    "nul.tar.gz",
    "prn",
    "aux",
    "com1",
    "com9",
    "lpt1",
    "lpt9",
])
def test_a_reserved_device_name_is_refused(session_id):
    """Windows cannot make a directory of these, with or without an extension."""
    assert is_safe_session_id(session_id) is False
    assert session_id_refusal(session_id) is not None


@pytest.mark.parametrize("session_id", [
    "com10",     # not in the documented set: the set stops at COM9
    "lpt0",      # ... and at LPT9; LPT0 is not reserved
    "com0",
    "console",   # a prefix is not a name
    "con-artist",
    "con_task",
    "my con",
    "cont",
])
def test_a_name_beside_the_reserved_set_is_accepted(session_id):
    """The other direction: refusing a name the platform accepts costs the host a name."""
    assert is_safe_session_id(session_id) is True
    assert session_id_refusal(session_id) is None


@pytest.mark.parametrize("session_id", ["a ", "a.", "sess ", "sess.", "con ", "con."])
def test_a_name_ending_in_a_space_or_a_dot_is_refused(session_id):
    """Windows strips them, so the directory is not the one the id names."""
    assert is_safe_session_id(session_id) is False


@pytest.mark.parametrize("session_id", ["My Task", "\u4e2d\u6587\u4efb\u52a1", "sess-1.2", "a.b"])
def test_an_ordinary_name_is_accepted(session_id):
    """A space and a non-ASCII name are session names, not defects — and a dot inside a
    name is fine, only a trailing one is folded away."""
    assert is_safe_session_id(session_id) is True


def test_creating_a_session_with_a_device_name_is_refused_by_name(tmp_path):
    """The refusal names the rule, and no directory is attempted."""
    with pytest.raises(UnsafeSessionId) as refusal:
        Session.create_with_id("con", tmp_path)

    assert "reserved device name" in str(refusal.value)
    assert not (tmp_path / ".emrg" / "sessions" / "con").exists()
    assert not (tmp_path / ".emrg" / "sessions").exists()


def test_creating_a_session_with_an_ordinary_name_still_works(tmp_path):
    """The control: the refusal must not cost the feature it guards."""
    session = Session.create_with_id("My Task", tmp_path)
    assert session.session_id == "My Task"
    assert (tmp_path / ".emrg" / "sessions" / "My Task" / "meta.json").is_file()


def test_the_two_refusals_are_two_sentences():
    """A device name *is* one path component, so the shape sentence would be false."""
    shape = session_id_refusal("../../../evil")
    device = session_id_refusal("con")

    assert shape == "a session id is one path component"
    assert device is not None and device != shape
    assert "device" in device
