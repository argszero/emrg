"""`gh` runs git, so the git reader's verdicts are owed to its spellings (issue #1533).

Both the write-target walk and the git-mutator reader answer about the **`git`
command word**, and `gh` is in none of their tables — so every gh spelling fell
through to ALLOW, including the two that write the local tree. Measured by the
issue's reporter through `BashTool.execute` at `read-only`, with the git twin
beside each: `gh repo clone <fork> /private/tmp/… -- --depth 1` wrote 16 MB and a
`.git` there while `git clone … <the same target>` was refused and created
nothing; `gh pr checkout 1524 --force` switched branches and discarded an
uncommitted edit that `git checkout` was refused for.

The arms below are therefore **pairs** — every gh spelling is asserted beside the
git spelling it performs, in both tiers — because the defect this rule can
acquire is not "the gh row regressed" but "the two readers drifted apart", which
one-directional rows cannot see.

Nothing here executes a command: `_check_sandbox` and `_extract_write_targets`
are pure (they parse text and `realpath` a string), so `OUTSIDE` and the `gh`
spellings are arguments to a predicate. The one execution this issue's report
rests on (`gh repo clone` really writing a tree, `gh pr checkout --force` really
discarding an edit) needs the network and a live repository and is the
reporter's measurement, not a test's — a suite that ran it would be measuring
GitHub's availability, and pinning the *refusal* is what stops the write.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data
# dir) and used only as an argument to the pure predicate — never executed.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def tiers(cmd):
    """Both tier verdicts for one command, as the toolbox would answer them."""
    return {
        tier: _check_sandbox(cmd, tier, WORKSPACE)[0]
        for tier in ("read-only", "workspace-write")
    }


# ── the pairs: (label, gh spelling, the git command it really runs) ─────────────
PAIRS = (
    # `gh repo clone <repository> [<directory>] [-- <gitflags>…]`
    ("clone, destination spelled",
     f"gh repo clone argszero/emrg {OUTSIDE}/dev",
     f"git clone https://github.com/argszero/emrg {OUTSIDE}/dev"),
    ("clone, destination omitted",
     "gh repo clone argszero/emrg",
     "git clone https://github.com/argszero/emrg"),
    ("clone, gitflags after --",
     f"gh repo clone argszero/emrg {OUTSIDE}/dev -- --depth 1",
     f"git clone --depth 1 https://github.com/argszero/emrg {OUTSIDE}/dev"),
    ("clone, value-taking flag in front",
     f"gh repo clone -u upstream-x argszero/emrg {OUTSIDE}/dev",
     f"git clone https://github.com/argszero/emrg {OUTSIDE}/dev"),
    ("clone, gh's own -R before the form",
     f"gh -R argszero/emrg repo clone other {OUTSIDE}/dev",
     f"git clone https://github.com/argszero/other {OUTSIDE}/dev"),
    # `gh pr checkout` is a branch switch in the repo the cwd is in
    ("pr checkout", "gh pr checkout 1524", "git checkout 1524"),
    ("pr checkout --force", "gh pr checkout 1524 --force", "git checkout -f 1524"),
    ("pr checkout --recurse-submodules",
     "gh pr checkout 1524 --recurse-submodules",
     "git checkout 1524"),
    # `gh repo sync` with no repository argument selects the *local* repository,
    # and what it does there is a fetch **followed by a reset of the checked-out
    # branch** — the reset is the half the git reader refuses, which is why the
    # twin is the pair and not the bare `git fetch` (that one is allowed in both
    # tiers, measured: a fetch updates no working-tree file).
    ("repo sync, local", "gh repo sync", "git fetch && git reset --hard"),
    ("repo sync --force, local", "gh repo sync --force", "git fetch --force && git reset --hard"),
    # `gh repo fork --clone` clones the fork it just created
    ("repo fork --clone", "gh repo fork --clone", "git clone"),
    ("repo fork --clone=true", "gh repo fork --clone=true", "git clone"),
    # a shell re-parsing the text is the same act
    ("through sh -c", "sh -c 'gh pr checkout 7'", "sh -c 'git checkout 7'"),
)


@pytest.mark.parametrize("label,gh_row,git_row", PAIRS, ids=[p[0] for p in PAIRS])
def test_each_gh_spelling_gets_its_git_twin_s_verdict(label, gh_row, git_row):
    """Both halves of one act are refused at read-only — the pair cannot drift quietly.

    Read-only is the tier the issue was filed from (a Contributor cycle) and the
    one the git reader already answered for every row below, so it is the shared
    verdict the gh reader has to reproduce. The two readers are asserted
    **separately** rather than through one equality, because they do not agree in
    the other tier at the clone rows — see
    `test_where_the_two_readers_part_company_is_pinned`.
    """
    assert tiers(gh_row)["read-only"] is False, (
        f"{label}: the gh spelling {gh_row!r} is not refused at read-only — "
        f"{tiers(gh_row)}"
    )
    assert tiers(git_row)["read-only"] is False, (
        f"{label}: the git twin {git_row!r} is no longer refused at read-only, so "
        "the pair no longer says what it was written to say"
    )


#: The rows that spell a destination — the ones whose refusal is about a path.
CLONING_ROWS = tuple(row for row in PAIRS if OUTSIDE in row[1])


@pytest.mark.parametrize("label,gh_row,_git_row", CLONING_ROWS, ids=[r[0] for r in CLONING_ROWS])
def test_a_refused_clone_is_allowed_once_it_lands_in_the_workspace(label, gh_row, _git_row):
    """The refusal is about *where* the write lands — the act itself stays allowed."""
    assert tiers(gh_row)["workspace-write"] is False, label
    # the same spelling with the destination inside the workspace
    inside = gh_row.replace(OUTSIDE, WORKSPACE)
    assert inside != gh_row, label
    assert tiers(inside) == {"read-only": False, "workspace-write": True}, label
    assert _extract_write_targets(inside) == [f"{WORKSPACE}/dev"]


def test_where_the_two_readers_part_company_is_pinned():
    """The gh rule names a clone destination; the git walk does not name it yet.

    Measured: `git clone <url> <dir>`'s destination is not an operand any reader
    of the git side names (only `gh repo clone`'s is, from `_gh_write_targets`),
    so at `workspace-write` the two spellings of one act answer differently —
    the gh one is refused for naming `<dir>`, the git one is allowed although it
    writes the same tree there. Pinned rather than described, in this direction:
    the gh side must **not** be relaxed to match the git side downwards (a write
    outside the workspace is a write), so the disagreement is closed by the git
    side learning the operand, and the fix flips the second assertion
    deliberately.
    """
    gh_row = f"gh repo clone argszero/emrg {OUTSIDE}/dev"
    git_row = f"git clone https://github.com/argszero/emrg {OUTSIDE}/dev"
    assert tiers(gh_row)["workspace-write"] is False, "the gh side lost its destination"
    assert _extract_write_targets(gh_row) == [f"{OUTSIDE}/dev"]
    # the git twin's gap, with its ground truth: it writes the same tree and is
    # allowed, and this assertion is what should change when that is fixed.
    assert _extract_write_targets(git_row) == [], (
        "`git clone`'s destination is named now — delete this residual and assert "
        "the two rows agree at workspace-write"
    )
    assert tiers(git_row)["workspace-write"] is True


def test_the_refusal_names_both_spellings():
    """The reason says which reader fired and what the command really runs."""
    allowed, reason, _enforcement = _check_sandbox(
        "gh pr checkout 1524", "read-only", WORKSPACE
    )
    assert allowed is False
    assert "'gh pr checkout'" in reason
    assert "'git checkout'" in reason
    assert "#1533" in reason


# ── the destination: `gh repo clone`'s directory is a write target ───────────────
CLONE_DESTINATIONS = (
    ("outside, after the repository",
     f"gh repo clone argszero/emrg {OUTSIDE}/dev", f"{OUTSIDE}/dev"),
    ("outside, with gitflags behind --",
     f"gh repo clone argszero/emrg {OUTSIDE}/dev -- --depth 1", f"{OUTSIDE}/dev"),
    ("outside, behind a value-taking flag",
     f"gh repo clone -u upstream-x argszero/emrg {OUTSIDE}/dev", f"{OUTSIDE}/dev"),
)


@pytest.mark.parametrize(
    "label,cmd,expected", CLONE_DESTINATIONS, ids=[c[0] for c in CLONE_DESTINATIONS]
)
def test_the_clone_destination_is_named(label, cmd, expected):
    """The issue's first criterion: the workspace-write tier must be able to see it.

    What the *git* twin does with the same destination at this tier is the
    asymmetry `test_where_the_two_readers_part_company_is_pinned` records — it is
    not asserted here, because it is not the same reading.
    """
    assert _extract_write_targets(cmd) == [expected]


@pytest.mark.parametrize(
    "label,cmd,expected", CLONE_DESTINATIONS, ids=[c[0] for c in CLONE_DESTINATIONS]
)
def test_the_named_destination_refuses_workspace_write_too(label, cmd, expected):
    """…which is what refuses the command for the same reason its clone is refused."""
    allowed, reason, _enforcement = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is False
    assert expected in reason


def test_no_other_gh_form_names_a_path():
    """The walk names a destination for `repo clone` and for nothing else.

    `gh pr checkout 5`'s operand is a pull-request number, and `gh repo sync
    owner/repo`'s is a repository — naming either as a file would refuse reads
    or name a path that does not exist.
    """
    for cmd in (
        "gh pr checkout 5",
        "gh pr view 5",
        "gh repo sync argszero/emrg",
        "gh repo clone argszero/emrg",
        "gh issue create --title x",
    ):
        assert _extract_write_targets(cmd) == [], cmd


def test_a_clone_into_the_workspace_is_still_allowed():
    """The rule names where the write lands; it does not refuse the act itself."""
    assert _extract_write_targets(f"gh repo clone argszero/emrg {WORKSPACE}/dev") == [
        f"{WORKSPACE}/dev"
    ]
    assert tiers(f"gh repo clone argszero/emrg {WORKSPACE}/dev")["workspace-write"] is True


# ── what must NOT be touched: the GitHub-side verbs and the non-local forks ─────
STILL_ALLOWED = (
    # a read-only Contributor cycle is supposed to be able to do these
    "gh pr create --title x --body y",
    "gh pr comment 1 --body x",
    "gh issue create --title x",
    "gh issue comment 1 --body x",
    "gh api repos/argszero/emrg",
    "gh api -X POST repos/argszero/emrg/issues -f title=x",
    # plain readers
    "gh pr view 1",
    "gh pr diff 1",
    "gh pr list",
    "gh issue list",
    "gh repo view",
    "gh release list",
    # GitHub-side writes that touch no local tree
    "gh repo sync argszero/emrg",
    "gh release create v9.9.9",
    # a fork that is not cloned locally
    "gh repo fork",
    "gh repo fork --clone=false",
    # a mention is not an invocation
    "grep -rn gh .",
    "echo gh repo clone x",
)


@pytest.mark.parametrize("cmd", STILL_ALLOWED)
def test_verbs_that_write_nothing_here_keep_their_verdict(cmd):
    assert tiers(cmd) == {"read-only": True, "workspace-write": True}, cmd
    assert _extract_write_targets(cmd) == []


def test_a_mention_of_gh_is_not_an_invocation():
    """`_runs_as_a_command`, the rule `_git_verbs` already applies, one word over."""
    assert bash_tool._find_gh_local_write("echo gh repo clone x") is None
    assert bash_tool._find_gh_local_write("grep -rn gh repo checkout .") is None
    assert bash_tool._find_gh_local_write("gh repo clone x") == (
        "gh repo clone", "git clone"
    )


def test_a_data_only_heredoc_is_not_read():
    """A heredoc body no shell executes mentions the act; it does not run it."""
    doc = "cat <<EOF\ngh pr checkout 1524\nEOF\n"
    assert bash_tool._find_gh_local_write(doc) is None
    assert bash_tool._find_gh_local_write("sh -c 'gh repo sync'") == (
        "gh repo sync", "git fetch + reset"
    )


# ── the discriminating arm: the refusal must hang on this predicate ─────────────
def test_the_refusal_hangs_on_the_gh_reader(monkeypatch):
    """Blind the gh reader and the hole comes back — on the gh rows and only those.

    Without this, the rows above could be passing because of the target walk
    (which names the clone destination) rather than because of the reader that
    refuses `gh pr checkout`, whose operands are not paths at all.
    """
    row = "gh pr checkout 1524 --force"
    assert tiers(row)["read-only"] is False, "the control: unblinded, refused"

    monkeypatch.setattr(bash_tool, "_find_gh_local_write", lambda cmd, _depth=0: None)
    assert tiers(row)["read-only"] is True, (
        "the row must hang on `_find_gh_local_write`, not on another reader"
    )
    # …and the git twin is still refused, i.e. the two readers are separate.
    assert tiers("git checkout -f 1524")["read-only"] is False
