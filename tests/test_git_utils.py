"""Unit tests for emrg.server.git_utils — git remote detection."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from emrg.server.git_utils import _detect_git_remote


# ── URL parsing (mocked subprocess) ──────────────────────────────

def _make_mock_run(stdout: str, returncode: int = 0) -> MagicMock:
    """Helper to create a mock subprocess.run result."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    return mock


def test_parse_ssh_url(monkeypatch):
    """Parses SSH-style git@github.com:owner/repo.git → owner/repo."""
    mock = _make_mock_run("git@github.com:argszero/emrg.git\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock)
    assert _detect_git_remote("/fake") == "argszero/emrg"


def test_parse_https_url(monkeypatch):
    """Parses HTTPS-style https://github.com/owner/repo.git → owner/repo."""
    mock = _make_mock_run("https://github.com/argszero/emrg.git\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock)
    assert _detect_git_remote("/fake") == "argszero/emrg"


def test_parse_https_url_no_dot_git(monkeypatch):
    """Parses HTTPS without .git suffix."""
    mock = _make_mock_run("https://github.com/argszero/emrg\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock)
    assert _detect_git_remote("/fake") == "argszero/emrg"


def test_parse_unknown_format(monkeypatch):
    """Returns empty string for unknown URL format."""
    mock = _make_mock_run("unknown-format-url\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock)
    assert _detect_git_remote("/fake") == ""


def test_parse_git_failure(monkeypatch):
    """Returns empty string when git remote fails."""
    mock = _make_mock_run("", returncode=128)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock)
    assert _detect_git_remote("/fake") == ""


# ── Real repo tests ──────────────────────────────────────────────

def test_detect_real_repo():
    """Verifies detection on the actual git repo (integration test)."""
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


# ── _cache_tool_paths (repo field, rant 2026-08-06T20:42:05) ──────


def test_cache_tool_paths_writes_repo_field(tmp_path, monkeypatch):
    """_cache_tool_paths persists git/gh paths AND the EMRG repo URL."""
    from emrg.server import git_utils as mod
    import json as _json

    info = tmp_path / "install-info.json"
    monkeypatch.setattr(mod, "INSTALL_INFO", info)

    mod._cache_tool_paths("/usr/bin/git", "/usr/bin/gh")

    data = _json.loads(info.read_text(encoding="utf-8"))
    assert data["git_path"] == "/usr/bin/git"
    assert data["gh_path"] == "/usr/bin/gh"
    assert data["repo"] == "https://github.com/argszero/emrg.git"


def test_cache_tool_paths_preserves_existing_fields(tmp_path, monkeypatch):
    """Existing fields in install-info.json are preserved on rewrite."""
    from emrg.server import git_utils as mod
    import json as _json

    info = tmp_path / "install-info.json"
    info.write_text(
        _json.dumps({"git_path": "/old/git", "custom": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "INSTALL_INFO", info)

    mod._cache_tool_paths("/usr/bin/git", "/usr/bin/gh")

    data = _json.loads(info.read_text(encoding="utf-8"))
    assert data["git_path"] == "/usr/bin/git"
    assert data["custom"] == 1  # preserved
    assert data["repo"] == "https://github.com/argszero/emrg.git"


# ── no_prompt_env / parse_gh_auth_user (rant 2026-08-07T10:17:27) ──


def test_no_prompt_env_sets_all_three_guards():
    """All three interactive-prompt guards are present."""
    from emrg.server.git_utils import no_prompt_env

    env = no_prompt_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_ASKPASS"] == ""


def test_no_prompt_env_preserves_parent_environment():
    """Parent environment variables must survive (PATH etc.)."""
    import os

    from emrg.server.git_utils import no_prompt_env

    env = no_prompt_env()
    assert env.get("PATH") == os.environ.get("PATH")


def test_parse_gh_auth_user_logged_in_as():
    from emrg.server.git_utils import parse_gh_auth_user

    out = "Logged in to github.com as octocat (keyring)\n"
    assert parse_gh_auth_user(out) == "octocat"


def test_parse_gh_auth_user_account_form():
    from emrg.server.git_utils import parse_gh_auth_user

    out = "Logged in to github.com account argszero using token\n"
    assert parse_gh_auth_user(out) == "argszero"


def test_parse_gh_auth_user_unauthenticated():
    from emrg.server.git_utils import parse_gh_auth_user

    out = "You are not logged into any GitHub hosts.\n"
    assert parse_gh_auth_user(out) is None


def test_parse_gh_auth_user_empty():
    from emrg.server.git_utils import parse_gh_auth_user

    assert parse_gh_auth_user("") is None
    assert parse_gh_auth_user(None) is None  # type: ignore[arg-type]
