"""`scripts/re-trigger-ci.sh` names the ref it will dispatch, or says why it has none.

Measured 2026-10-05 (`cyc20261005-040310`). The script defaults its `--ref` from the
checked-out branch with

    branch="${1:-$(git rev-parse --abbrev-ref HEAD)}"

and on a **detached HEAD** - which is what a CI checkout looks like, and what this
instance's own cycle workspace looks like - `git rev-parse --abbrev-ref HEAD` does not fail.
It prints the literal string `HEAD`, which names no branch. That string went straight into
`gh workflow run test.yml --ref HEAD`, so the reader met gh's own error about an unknown ref
instead of this script naming the cause - and the script's own header says re-triggering
exists precisely for the case where the push-event pipeline is broken and nothing is
watching. A remedy that fails with someone else's error message is a remedy the host cannot
act on.

The discriminator is `git symbolic-ref`'s exit code - the reading `scripts/review-queue.py`
already uses for the same question. Detached HEAD makes it fail (`fatal: ref HEAD is not a
symbolic ref`) and print nothing; a branch makes it print the branch name. The script now
reads it and refuses with the remedy when neither an argument nor a branch name exists.

Every arm below executes the real script with a stub `gh` on `PATH`, so no request reaches
GitHub and nothing older than the script is trusted. The arms are one behaviour each and the
two directions of the same question: on a branch it must dispatch *that* branch; detached it
must refuse and say what to do instead; and the remedy it names must actually work.

⚠️ Nothing here starts, stops or restarts a daemon (MANIFESTO 第四条附则二), and nothing
touches `~/.emrg`: the repository the script reads is one `tmp_path` creates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "re-trigger-ci.sh"

# The stub `gh` is a POSIX shell script and the script under test is `bash`, so these arms
# need a real POSIX shell. Gated rather than attempted, because on the Windows runner the
# name `bash` resolves to the WSL launcher (`C:\Windows\System32\bash.exe` precedes Git's
# bash on `PATH`), which answers "no installed distributions" and exits 1 - a run that then
# measures the shell's absence rather than the script. Same gate, same reason, as
# `tests/test_release_end_state.py` and `tests/test_release_notarize_reports_its_reason.py`.
_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the script under test is a POSIX shell script and these arms execute it; without "
        "a real bash the run measures the shell's absence, not the script"
    ),
)

#: Records the argv the script handed `gh`, one argument per line, so a test can read the
#: ref precisely rather than matching it out of a joined string. The shebang is filled in
#: with the shell this host really resolves.
_GH_STUB = """#!{shell}
printf '%s\\n' "$@" > "$GH_RECORD"
exit 0
"""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _repo_with_one_commit(tmp_path: Path) -> tuple[Path, str]:
    """A real repository on a real branch. Returns the repo and the branch's name.

    The name is read back rather than assumed (`main` vs `master` depends on the host's
    `init.defaultBranch`), so the assertion compares against what git really has.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(
        [
            "-c", "user.email=re-trigger@example.invalid",
            "-c", "user.name=re-trigger",
            "-c", "commit.gpgsign=false",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        repo,
    )
    branch = _git(["symbolic-ref", "--short", "HEAD"], repo).stdout.strip()
    assert branch, "the fixture could not read the branch it just created"
    return repo, branch


def _run_script(tmp_path: Path, cwd: Path, args: list[str] = ()) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the script with a stub `gh` first on `PATH`; return the result and the record."""
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available to run the script under test")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(_GH_STUB.format(shell=shell), encoding="utf-8")
    stub.chmod(0o755)

    record = tmp_path / "gh-argv.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GH_RECORD"] = str(record)

    result = subprocess.run(
        [shell, "--noprofile", "--norc", str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result, record


@_posix_shell_only
def test_the_premise_the_old_reading_answers_head_on_a_detached_checkout(tmp_path) -> None:
    """The measurement the fix exists for, pinned so it cannot quietly stop being true.

    `git symbolic-ref` and `git rev-parse --abbrev-ref` answer the same question about a
    checked-out branch and disagree on a detached HEAD: the first refuses, the second
    invents the name `HEAD`. If git ever changes this, the refusal below would be guarding
    nothing, and this arm is where that is noticed.
    """
    repo, branch = _repo_with_one_commit(tmp_path)
    _git(["checkout", "-q", "--detach", "HEAD"], repo)

    invented = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert invented.returncode == 0, (
        "`git rev-parse --abbrev-ref HEAD` now fails on a detached HEAD - the defect this "
        f"file pins is gone, and so is the reason for the refusal. stderr={invented.stderr!r}"
    )
    assert invented.stdout.strip() == "HEAD", (
        "a detached HEAD no longer answers `HEAD` - re-measure before trusting the refusal "
        f"below. stdout={invented.stdout!r}"
    )

    refused = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert refused.returncode != 0 and refused.stdout.strip() == "", (
        "`git symbolic-ref --short HEAD` no longer refuses a detached HEAD - it is the "
        f"reading the fix discriminates on. rc={refused.returncode} stdout={refused.stdout!r}"
    )

    # And the branch the fixture created still exists, so "detached" is the fixture's own
    # move rather than a branch that was never made. (`rev-parse --verify` rather than
    # `symbolic-ref`: the ref is a direct one, which `symbolic-ref` refuses by design.)
    back = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert back.returncode == 0, f"the fixture lost its branch {branch!r}"


@_posix_shell_only
def test_on_a_branch_it_dispatches_that_branch(tmp_path) -> None:
    """The happy path, read off the stub's argv: the ref handed to gh is the real branch."""
    repo, branch = _repo_with_one_commit(tmp_path)

    result, record = _run_script(tmp_path, repo)

    assert result.returncode == 0, (
        f"the script failed on a plain branch.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert branch in result.stdout, (
        f"the script did not name the branch it was about to dispatch ({branch!r}).\n"
        f"stdout={result.stdout!r}"
    )
    assert record.exists(), "the stub `gh` never ran - the script did not dispatch at all"
    assert record.read_text(encoding="utf-8").splitlines() == [
        "workflow", "run", "test.yml", "--ref", branch,
    ], (
        "the ref handed to `gh workflow run` is not the branch that is checked out - the "
        f"whole point of the default. record={record.read_text(encoding='utf-8')!r}"
    )


@_posix_shell_only
def test_a_detached_head_refuses_and_names_the_remedy(tmp_path) -> None:
    """Nothing to name is not a name: it must refuse, before gh is ever reached.

    Both halves matter and either alone passes trivially: exiting 0 while dispatching a ref
    that resolves to nothing is the defect, and refusing without saying what to pass instead
    leaves the host with a differently-worded dead end.
    """
    repo, _ = _repo_with_one_commit(tmp_path)
    _git(["checkout", "-q", "--detach", "HEAD"], repo)

    result, record = _run_script(tmp_path, repo)

    assert result.returncode == 2, (
        "a detached HEAD did not refuse - a reading that could not name a ref was turned "
        f"into a dispatch anyway.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert not record.exists(), (
        "the stub `gh` ran on a detached HEAD - the refusal must come before the dispatch, "
        "or gh answers with its own error and the script's message is never reached"
    )
    assert "Dispatching" not in result.stdout, (
        f"the script claimed to dispatch a ref it could not name.\nstdout={result.stdout!r}"
    )
    assert "re-trigger-ci.sh" in result.stderr, (
        f"the refusal does not name the command whose usage it is stating.\nstderr={result.stderr!r}"
    )
    assert "branch" in result.stderr, (
        "the refusal does not name the remedy - what to pass, or what to check out - so it "
        f"is a dead end with a better message.\nstderr={result.stderr!r}"
    )


@_posix_shell_only
def test_an_explicit_argument_dispatches_even_when_detached(tmp_path) -> None:
    """The remedy the refusal names has to work, or it is a phrase rather than a remedy."""
    repo, branch = _repo_with_one_commit(tmp_path)
    _git(["checkout", "-q", "--detach", "HEAD"], repo)

    result, record = _run_script(tmp_path, repo, args=[branch])

    assert result.returncode == 0, (
        "passing the branch explicitly did not get past the refusal.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert record.exists(), "the stub `gh` never ran on the explicit-argument path"
    assert record.read_text(encoding="utf-8").splitlines() == [
        "workflow", "run", "test.yml", "--ref", branch,
    ], (
        "the explicit argument did not reach `--ref` intact. "
        f"record={record.read_text(encoding='utf-8')!r}"
    )
