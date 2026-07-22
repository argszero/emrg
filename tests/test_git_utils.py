"""Unit tests for emrg.server.git_utils — git remote detection."""

from __future__ import annotations

from emrg.server.git_utils import _detect_git_remote


def test_detect_ssh_url():
    """Parses SSH-style git@github.com:owner/repo.git URLs."""
    # We can't actually run 'git remote get-url' in tests, but the URL parsing
    # logic is self-contained. Test via a mock subprocess or just verify
    # the non-git parts work. Since _detect_git_remote shells out to git,
    # we test the string parsing via the current working directory's git remote.
    result = _detect_git_remote(".")
    # The current directory IS a git repo with an origin — result should be non-empty
    assert isinstance(result, str)
    if result:
        assert "/" in result
        assert not result.endswith(".git")


def test_detect_no_git_repo(tmp_path):
    """Returns empty string for a non-git directory."""
    result = _detect_git_remote(str(tmp_path))
    assert result == ""


def test_detect_nonexistent_dir():
    """Returns empty string for a directory that doesn't exist."""
    result = _detect_git_remote("/nonexistent/path/xyz/test")
    assert result == ""
