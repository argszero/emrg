"""Four pure-read git verbs were refused as mutating (issue #1240).

`_GIT_READ_VERBS` is an **allowlist**: a verb that is not on it is treated as a
mutator whatever it actually does. Measured on master `e6eaaee4`, `read-only`
tier: `git reflog`, `git notes list`, `git bisect log` and `git cherry master`
were all refused with the reason "blocked git mutating command" — a statement
that is not true of them. `reflog` expires entries only as `git reflog expire`;
`notes` writes only for `add`/`copy`/`append`/`edit`/`remove`/`prune`; `bisect`
writes `.git/BISECT_*` only for `start`/`good`/`bad`/`skip`/`reset`; `cherry` has
no writing form at all.

That costs a downgraded cycle exactly the commands that would explain *how the
tree got dirty* — `git reflog` is the ref log — which is the situation the guard
itself creates.

Both halves are asserted, because "allow more" is also what a broken guard does:
every writing shape of the same verbs must stay blocked, and an unrelated set of
mutators must be unaffected.
"""

import pytest

from emrg.tools.bash_tool import _check_sandbox

TIER = "read-only"


def _allowed(cmd: str) -> bool:
    allowed, _reason, _ = _check_sandbox(cmd, TIER)
    return allowed is True


@pytest.mark.parametrize("cmd", [
    "git reflog",
    "git reflog show",
    "git reflog show HEAD",
    "git reflog exists HEAD",
    "git -C . reflog show",
    "git reflog -n 5",
    "git reflog --all",
    "git reflog --date=iso show",
    "git reflog show -n 3",
])
def test_reflog_reads_are_allowed(cmd: str) -> None:
    """The ref log is the read a downgraded cycle most needs, and it writes nothing."""
    assert _allowed(cmd), f"{cmd!r} only prints, must be allowed"


@pytest.mark.parametrize("cmd", [
    "git notes list",
    "git notes show HEAD",
    "git notes",
    "git notes --ref refs/notes/x list",
    "git notes --ref=refs/notes/x list",
])
def test_notes_reads_are_allowed(cmd: str) -> None:
    assert _allowed(cmd), f"{cmd!r} only prints notes, must be allowed"


@pytest.mark.parametrize("cmd", ["git bisect log", "git bisect view", "git bisect visualize"])
def test_bisect_reporters_are_allowed(cmd: str) -> None:
    assert _allowed(cmd), f"{cmd!r} only prints the bisect log, must be allowed"


@pytest.mark.parametrize("cmd", ["git cherry", "git cherry master", "git cherry -v origin/master HEAD"])
def test_cherry_is_allowed(cmd: str) -> None:
    """`git cherry` reports commits not upstream; it has no writing form."""
    assert _allowed(cmd), f"{cmd!r} only prints, must be allowed"


@pytest.mark.parametrize("cmd", [
    # reflog: expiring or deleting entries rewrites the reflog
    "git reflog expire --expire=now --all",
    "git reflog delete HEAD@{1}",
    "git reflog drop HEAD@{1}",
    # notes: these write
    "git notes add -m x",
    "git notes -f add -m x",
    "git notes --ref refs/notes/x add -m y",
    "git notes --ref refs/notes/x remove HEAD",
    "git bisect --no-checkout start",
    "git notes remove HEAD",
    "git notes append -m x",
    "git notes copy a b",
    "git notes prune",
    "git notes merge origin/x",
    # bisect: these write .git/BISECT_* — and `replay` rewrites history
    "git bisect start",
    "git bisect good",
    "git bisect bad",
    "git bisect reset",
    "git bisect skip",
    "git bisect replay log.txt",
    "git bisect run make",
])
def test_writing_shapes_of_the_same_verbs_stay_blocked(cmd: str) -> None:
    """The half that keeps this a fix rather than a relaxation."""
    assert not _allowed(cmd), f"{cmd!r} writes; must stay blocked"


@pytest.mark.parametrize("cmd", [
    "git stash drop", "git checkout .", "git clean -fd", "git reset --hard",
    "git config user.name x", "git branch -D old", "git tag -d v1",
    "git commit -m x", "git push origin master",
])
def test_unrelated_mutators_are_unaffected(cmd: str) -> None:
    assert not _allowed(cmd), f"{cmd!r} writes; must stay blocked"


@pytest.mark.parametrize("cmd", [
    "git status", "git log -1", "git diff HEAD", "git stash list",
    "git worktree list", "git branch -a", "git config -l",
    "git count-objects -v", "git fsck", "git shortlog -sn",
    "git describe --tags", "git ls-files", "git rev-parse HEAD",
])
def test_reads_that_were_already_allowed_stay_allowed(cmd: str) -> None:
    """No regression: the widening must not disturb the existing read set."""
    assert _allowed(cmd), f"{cmd!r} only prints, must stay allowed"
