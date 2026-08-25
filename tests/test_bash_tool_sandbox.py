"""Tests for the bash tool sandbox (rant 2026-08-20T15:46:50).

Covers the static file-level isolation check: three tiers
(danger-full-access / read-only / workspace-write), protected daemon state
files, honest enforcement reporting (full/partial), and blocked-result
feedback through execute().
"""

import asyncio
import os
import tempfile

import pytest

from emrg.tools.bash_tool import (
    BashTool,
    SANDBOX_MODES,
    _check_sandbox,
    _extract_write_targets,
)


def _run(coro):
    return asyncio.run(coro)


# ── constant ──────────────────────────────────────────────────────────────

def test_sandbox_modes_constant():
    assert SANDBOX_MODES == ("danger-full-access", "read-only", "workspace-write")


# ── _extract_write_targets ────────────────────────────────────────────────

def test_extract_rm_rf_target():
    assert _extract_write_targets("rm -rf /tmp/x") == ["/tmp/x"]
    assert _extract_write_targets("rm -r ./build") == ["./build"]
    assert _extract_write_targets("rm -rf /tmp/a; echo hi") == ["/tmp/a"]


def test_extract_rmdir_target():
    assert _extract_write_targets("rmdir /tmp/empty") == ["/tmp/empty"]


def test_extract_mv_destination():
    assert _extract_write_targets("mv /tmp/a /tmp/b") == ["/tmp/b"]


def test_extract_cp_r_destination():
    assert _extract_write_targets("cp -r src /tmp/dst") == ["/tmp/dst"]
    assert _extract_write_targets("cp -R ./a ./b") == ["./b"]


def test_extract_redirect_targets():
    assert _extract_write_targets("echo x > /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("echo x >> /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("cmd 2> err.txt") == ["err.txt"]
    assert _extract_write_targets("echo x > /dev/null") == ["/dev/null"]
    # 2>&1 is not a file redirect — must not be captured
    assert "&1" not in _extract_write_targets("echo x > /tmp/y 2>&1")


def test_extract_no_targets_for_plain_reads():
    assert _extract_write_targets("ls -la") == []
    assert _extract_write_targets("git status") == []
    assert _extract_write_targets("echo hello") == []


# ── _check_sandbox — danger-full-access ───────────────────────────────────

def test_check_danger_full_access_always_allowed():
    allowed, reason, enforcement = _check_sandbox("rm -rf /", "danger-full-access")
    assert allowed is True
    assert reason is None
    assert enforcement == "full"


def test_check_invalid_mode_blocked():
    allowed, reason, enforcement = _check_sandbox("echo hi", "sandboxed")
    assert allowed is False
    assert "invalid sandbox mode" in reason
    assert enforcement == "partial"


# ── _check_sandbox — read-only ────────────────────────────────────────────

def test_check_read_only_blocks_destructive_commands():
    for cmd in ("rm -rf /tmp/x", "rmdir /tmp/empty", "mv /tmp/a /tmp/b",
                "cp -r src /tmp/dst"):
        allowed, reason, enforcement = _check_sandbox(cmd, "read-only")
        assert allowed is False, cmd
        assert "read-only sandbox" in reason
        assert enforcement == "partial"


def test_check_read_only_blocks_redirects():
    allowed, _, _ = _check_sandbox("echo x > /tmp/y", "read-only")
    assert allowed is False
    allowed, _, _ = _check_sandbox("echo x >> /tmp/y", "read-only")
    assert allowed is False


def test_check_read_only_allows_dev_null_redirect():
    allowed, _, _ = _check_sandbox("echo hi > /dev/null", "read-only")
    assert allowed is True


def test_check_read_only_allows_read_commands():
    for cmd in ("ls -la", "git status", "cat file.txt", "pwd", "echo hi"):
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, cmd


def test_check_read_only_blocks_git_mutators():
    """Community issue #979: read-only must block git mutating commands —
    the 2026-08-20 data-loss killers (stash / checkout . / reset --hard /
    clean) escaped the rm/mv/cp target scan. Under read-only they must be
    structurally impossible, not merely discouraged by a prompt rule."""
    for cmd in (
        "git stash",
        "git stash list && git stash drop",
        "git checkout .",
        "git checkout -- src/main.py",
        "git restore .",
        "git clean -fd",
        "git reset --hard",
        "git reset --mixed HEAD~1",
        "git commit -m 'wip'",
        "git push origin master",
        "git pull --rebase",
        "git merge master",
        "git rebase master",
        "git cherry-pick abc123",
        "git revert abc123",
        "git rm foo.py",
        "git switch feature/x",
        "git branch -d old",
        "git branch -D old",
        "git tag -d v1",
    ):
        allowed, reason, enforcement = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} should be blocked"
        assert "read-only sandbox" in reason, cmd
        assert "git" in reason, cmd
        assert enforcement == "partial"


def test_check_read_only_blocks_git_mv():
    """`git mv a b` is blocked by the write-target scan (mv destination) —
    the git-mutator reason isn't required, the block is what matters."""
    allowed, reason, _ = _check_sandbox("git mv a b", "read-only")
    assert allowed is False
    assert "read-only sandbox" in reason


def test_check_read_only_allows_git_reads():
    """Read-only keeps read-only git reads available — the read-only cycle
    still needs status/fetch/log/diff for scanning and review."""
    for cmd in (
        "git status --short --branch",
        "git fetch origin master",
        "git log --oneline -3",
        "git diff",
        "git diff --cached",
        "git show HEAD --stat",
        "git branch -a",
        "git remote -v",
        "git rev-parse --abbrev-ref HEAD",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} should be allowed (got {reason!r})"


def test_check_workspace_write_allows_git_mutators():
    """workspace-write is the normal working tier — tasks must still be able
    to commit/push there. Only read-only blocks git mutation."""
    for cmd in ("git stash", "git checkout .", "git reset --hard",
                "git commit -m x", "git push origin master"):
        allowed, _, _ = _check_sandbox(cmd, "workspace-write")
        assert allowed is True, cmd


# ── _check_sandbox — workspace-write ──────────────────────────────────────

def test_check_workspace_write_allows_relative_writes():
    # Relative targets are assumed in-workspace (cwd = workspace root).
    allowed, _, _ = _check_sandbox("echo x > out.txt", "workspace-write")
    assert allowed is True
    allowed, _, _ = _check_sandbox("rm -rf ./build", "workspace-write")
    assert allowed is True


def test_check_workspace_write_allows_temp_and_workspace_abs():
    allowed, _, _ = _check_sandbox(f"echo x > {tempfile.gettempdir()}/y", "workspace-write")
    assert allowed is True
    allowed, _, _ = _check_sandbox(
        "echo x > /workspace/out.txt", "workspace-write", workdir="/workspace"
    )
    assert allowed is True


def test_check_workspace_write_blocks_protected_daemon_file():
    allowed, reason, enforcement = _check_sandbox(
        "echo x > ~/.emrg/config.toml", "workspace-write"
    )
    assert allowed is False
    assert "protected" in reason
    assert enforcement == "partial"


def test_check_workspace_write_blocks_emrg_home_rm():
    allowed, reason, _ = _check_sandbox("rm -rf ~/.emrg", "workspace-write")
    assert allowed is False
    assert "daemon's data directory" in reason


def test_check_workspace_write_blocks_outside_workspace():
    allowed, _, _ = _check_sandbox(
        "rm -rf /etc/hosts", "workspace-write", workdir="/workspace"
    )
    assert allowed is False
    allowed, _, _ = _check_sandbox(
        "echo x > /etc/hosts", "workspace-write", workdir="/workspace"
    )
    assert allowed is False


# ── execute() integration ─────────────────────────────────────────────────

def test_execute_read_only_blocks_rm_rf():
    tool = BashTool()
    result = _run(tool.execute({
        "command": "rm -rf /tmp/emrg-sandbox-test",
        "sandbox": "read-only",
    }))
    assert result.error is True
    assert "sandbox" in result.content
    assert "not executed" in result.content


def test_execute_workspace_write_blocks_protected_file():
    tool = BashTool()
    result = _run(tool.execute({
        "command": "echo x > ~/.emrg/config.toml",
        "sandbox": "workspace-write",
    }))
    assert result.error is True
    assert "sandbox" in result.content


def test_execute_sandboxed_success_tags_output():
    tool = BashTool()
    result = _run(tool.execute({
        "command": "echo hi",
        "sandbox": "workspace-write",
    }))
    assert not result.error
    assert "[sandbox:workspace-write enforcement=partial] ok" in result.content
    assert "hi" in result.content


def test_execute_danger_full_access_unchanged():
    tool = BashTool()
    result = _run(tool.execute({"command": "echo hello", "sandbox": "danger-full-access"}))
    assert not result.error
    assert "hello" in result.content
    assert "sandbox" not in result.content


def test_execute_no_sandbox_key_unchanged():
    """Default (no sandbox key) = danger-full-access = current behavior."""
    tool = BashTool()
    result = _run(tool.execute({"command": "echo hello"}))
    assert not result.error
    assert "hello" in result.content
    assert "sandbox" not in result.content
