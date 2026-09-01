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
    check_workspace_write,
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
    for cmd in ("ls -la", "git status", "cat file.txt", "pwd", "echo hi",
                "git stash list", "git stash show -p",
                "git stash show stash@{0}", "git stash list | grep foo",
                "git worktree list", "git worktree list --porcelain",
                "git submodule status", "git submodule status | head"):
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
        "git stash push -m wip",
        "git stash drop stash@{0}",
        "git stash pop",
        "git stash clear",
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
        # working-tree writers (cycle 20260825-193548)
        "git apply patch.diff",
        "git am patch-series.mbox",
        "git archive --output=tree.tar HEAD",
        "git submodule update --init",
        "git worktree add ../wt master",
        # worktree/submodule MUTATORS stay blocked (cycle 20260825-200038)
        "git worktree remove ../wt",
        "git worktree move ../wt ../wt2",
        "git worktree prune",
        "git worktree lock ../wt",
        "git worktree unlock ../wt",
        "git submodule add https://example.com/repo.git sub",
        "git submodule deinit -f .",
        "git submodule sync",
        "git worktree list && git worktree remove ../wt",
        "git submodule status; git submodule update",
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


# ── workspace-write trusted zone (issue #1093 self-regression) ─────────────
# PR #1092 added check_workspace_write to write/edit (mirroring bash) and
# blocked absolute targets outside the injected workspace. The evolution task
# runs at workspace-write with workspace = the repo checkout, but writes its
# own cycle records to ~/.emrg/evolution/.emrg/memory/* — which is OUTSIDE that
# workspace. That was a self-regression (the evolution module couldn't record
# its own history). The fix trusts ~/.emrg/evolution/.emrg as a write zone and
# normalizes the OS-temp root for the Temp\<suffix> discrepancy. Both positive
# and negative states must be verified.


def test_workspace_write_allows_evolution_memory(monkeypatch, tmp_path):
    """write/edit/bash may target ~/.emrg/evolution/.emrg/memory (the evolution
    module's own data root) even though it is outside the repo workspace."""
    import emrg.tools.bash_tool as bt
    # Pin the user home + evolution data root so the test is hermetic and does
    # not depend on this host's real ~/.emrg layout.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Recompute the trusted zone against the pinned HOME.
    evo_data = os.path.realpath(os.path.expanduser("~/.emrg/evolution/.emrg"))
    ws = str(tmp_path / "ws")
    # check_workspace_write (write/edit tools)
    meta = evo_data + "/memory/cycle-20260901-000000.md"
    assert check_workspace_write(meta, ws) is None
    # bash _check_sandbox (redirect to memory index)
    allowed, reason, _ = _check_sandbox(
        f"echo x > {evo_data}/memory/MEMORY.md", "workspace-write", ws
    )
    assert allowed is True, f"should allow evolution memory write (got {reason!r})"


def test_workspace_write_still_blocks_emrg_home(monkeypatch, tmp_path):
    """Even with the trusted evolution-data zone, ~/.emrg itself is still
    blocked from destructive write (the guard is not widened)."""
    check = check_workspace_write("~/.emrg", str(tmp_path / "ws"))
    assert check is not None
    assert "daemon's data directory" in check
    allowed, reason, _ = _check_sandbox("rm -rf ~/.emrg", "workspace-write", str(tmp_path / "ws"))
    assert allowed is False
    assert "daemon's data directory" in reason


def test_workspace_write_still_blocks_protected_file():
    """Protected daemon state files remain blocked regardless of the trusted
    evolution-data zone."""
    check = check_workspace_write("~/.emrg/config.toml", "/workspace")
    assert check is not None
    assert "protected daemon file" in check


def test_workspace_write_temp_root_normalized(monkeypatch):
    r"""Temp\<suffix> discrepancy (Windows): when gettempdir() returns a
    Temp\<suffix> path, the parent Temp root is also allowed so helpers written
    to the plain Temp root are not blocked."""
    import tempfile as _tf
    import emrg.tools.bash_tool as bt
    fake_suffix = "/fake/Temp/2"
    fake_parent = "/fake/Temp"
    monkeypatch.setattr(_tf, "gettempdir", lambda: fake_suffix)
    # _temp_write_roots must include both the suffix path and the parent Temp root.
    roots = bt._temp_write_roots()
    assert any(r == os.path.realpath(fake_suffix) for r in roots)
    assert any(r == os.path.realpath(fake_parent) for r in roots)
    # A write to the parent Temp root is not blocked.
    check = check_workspace_write("/fake/Temp/emrg_probe.py", "/workspace")
    assert check is None


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
