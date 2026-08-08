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


def test_cache_tool_paths_tolerates_corrupt_cache(tmp_path, monkeypatch):
    """A corrupt/partial install-info.json must not raise — degrades to {}.

    Regression for the flaky test_daemon::test_build_prompt_with_project
    (json.decoder.JSONDecodeError): the live daemon rewrites this shared
    file non-atomically; a concurrent reader could catch a partial write.
    """
    from emrg.server import git_utils as mod
    import json as _json

    info = tmp_path / "install-info.json"
    info.write_text('{"git_path": "/partial', encoding="utf-8")  # truncated JSON
    monkeypatch.setattr(mod, "INSTALL_INFO", info)

    mod._cache_tool_paths("/usr/bin/git", "/usr/bin/gh")  # must not raise

    data = _json.loads(info.read_text(encoding="utf-8"))
    assert data["git_path"] == "/usr/bin/git"
    assert data["repo"] == "https://github.com/argszero/emrg.git"


def test_cache_tool_paths_atomic_write_no_temp_leftover(tmp_path, monkeypatch):
    """Write is atomic: target is valid JSON and no .tmp file remains."""
    from emrg.server import git_utils as mod
    import json as _json

    info = tmp_path / "install-info.json"
    info.write_text(
        _json.dumps({"custom": "old"}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "INSTALL_INFO", info)

    mod._cache_tool_paths("/usr/bin/git", "/usr/bin/gh")

    assert not (tmp_path / "install-info.json.tmp").exists()
    data = _json.loads(info.read_text(encoding="utf-8"))
    assert data["git_path"] == "/usr/bin/git"
    assert data["custom"] == "old"


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


# ── HTTPS→SSH fallback helpers (2026-08-08) ───────────────────────

def test_https_to_ssh_url_dot_git():
    from emrg.server.git_utils import https_to_ssh_url

    assert https_to_ssh_url("https://github.com/argszero/emrg.git") == "git@github.com:argszero/emrg.git"


def test_https_to_ssh_url_no_dot_git():
    from emrg.server.git_utils import https_to_ssh_url

    assert https_to_ssh_url("https://github.com/argszero/emrg") == "git@github.com:argszero/emrg.git"


def test_https_to_ssh_url_rejects_ssh_url():
    from emrg.server.git_utils import https_to_ssh_url

    assert https_to_ssh_url("git@github.com:argszero/emrg.git") is None


def test_https_to_ssh_url_rejects_other_hosts_and_garbage():
    from emrg.server.git_utils import https_to_ssh_url

    assert https_to_ssh_url("https://gitlab.com/argszero/emrg.git") is None
    assert https_to_ssh_url("https://github.example.com/a/b.git") is None
    assert https_to_ssh_url("") is None
    assert https_to_ssh_url(None) is None  # type: ignore[arg-type]
    assert https_to_ssh_url("file:///tmp/repo") is None


def test_is_git_connection_error_matches_connection_failures():
    from emrg.server.git_utils import is_git_connection_error

    assert is_git_connection_error(
        "fatal: unable to access 'https://github.com/a/b.git/': "
        "Failed to connect to github.com port 443 after 10013 ms"
    )
    assert is_git_connection_error("ssh: connect to host github.com port 22: Connection refused")


def test_is_git_connection_error_matches_hung_up():
    """'remote end hung up' (network drop mid-transfer) is a connection error."""
    from emrg.server.git_utils import is_git_connection_error

    assert is_git_connection_error("fatal: the remote end hung up unexpectedly")
    assert is_git_connection_error(
        "error: RPC failed; curl 92 HTTP/2 stream 0 was not closed cleanly: "
        "PROTOCOL_ERROR (err 1)\nfatal: the remote end hung up unexpectedly"
    )


def test_is_git_connection_error_rejects_auth_and_404():
    from emrg.server.git_utils import is_git_connection_error

    assert not is_git_connection_error("remote: Repository not found.")
    assert not is_git_connection_error("Permission denied (publickey).")
    assert not is_git_connection_error("Authentication failed for 'https://github.com/a/b.git'")
    assert not is_git_connection_error("")
    assert not is_git_connection_error(None)  # type: ignore[arg-type]


def test_git_origin_url_real_repo():
    """Reads the raw origin URL from a real repo."""
    import subprocess as real_subprocess

    from emrg.server.git_utils import git_origin_url

    repo = real_subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if repo.returncode != 0:
        return  # not in a git repo (packaged source) — skip
    url = git_origin_url(repo.stdout.strip())
    assert isinstance(url, str)
    assert url  # the evolution workspace has an origin


def test_git_origin_url_missing_remote(tmp_path):
    import subprocess as real_subprocess

    from emrg.server.git_utils import git_origin_url

    real_subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert git_origin_url(str(tmp_path)) == ""
