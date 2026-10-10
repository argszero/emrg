"""Tests for the tool base class."""

import stat

import pytest

from emrg.tools.base import ToolExecutor, special_file_kind


def test_tool_executor_is_abstract():
    """Verify ToolExecutor cannot be instantiated directly."""
    with pytest.raises(TypeError):
        ToolExecutor()  # type: ignore[abstract]


def test_concrete_subclass_works():
    """A concrete subclass implementing both abstract methods should work."""

    class _Concrete(ToolExecutor):
        from emrg.server.tool_types import ToolDefinition, ToolResult

        def definition(self):
            return self.ToolDefinition(
                name="test", description="test", parameters={}
            )

        async def execute(self, arguments):
            return self.ToolResult(name="test", content="ok")

    tool = _Concrete()
    d = tool.definition()
    assert d.name == "test"


# ── `special_file_kind`: which subjects a file tool may open ──
#
# The mode numbers are built from `stat.S_IF*` rather than read off real files, so the
# rule is pinned on the Windows leg too — where no FIFO or socket can be created, and
# where a FIFO test would have to skip. The real subjects are exercised per tool, by
# the named-pipe tests in `test_read_tool.py` / `test_write_tool.py` / `test_edit_tool.py`.


@pytest.mark.parametrize(
    "mode, expected",
    [
        (stat.S_IFREG | 0o644, None),
        (stat.S_IFREG | 0o600, None),
        (stat.S_IFDIR | 0o755, "a directory"),
        (stat.S_IFIFO | 0o644, "a FIFO (named pipe)"),
        (stat.S_IFSOCK | 0o644, "a socket"),
        (stat.S_IFCHR | 0o666, "a character device"),
        (stat.S_IFBLK | 0o660, "a block device"),
    ],
)
def test_special_file_kind_names_each_kind(mode, expected):
    """Every kind `stat` can report is named — a regular file is the one that is not."""
    assert special_file_kind(mode) == expected


def test_special_file_kind_is_a_whitelist():
    """An unnamed kind is refused rather than assumed readable.

    The predicate guards the *answer*, so an unrecognised mode must not fall through to
    `None` (which means "a regular file, open it"): a mode that names no kind is exactly
    the case where opening is least safe. `0` is the stand-in — no type bits at all.
    """
    assert special_file_kind(0) == "not a regular file"
    assert special_file_kind(0o777) == "not a regular file"


def test_special_file_kind_ignores_permission_bits():
    """Only the type bits decide; a read-only or world-writable regular file is regular."""
    for perms in (0o000, 0o400, 0o444, 0o777):
        assert special_file_kind(stat.S_IFREG | perms) is None
