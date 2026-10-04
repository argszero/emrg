"""`git_cmd` must return its CompletedProcess, not raise on the repository's bytes.

The defect this pins (measured on master 2026-10-04, cycle `cyc20261004-151935`)
-------------------------------------------------------------------------------
`git_cmd` pinned a codec and no policy:

    subprocess.run([exe, *args], ..., text=True, encoding="utf-8", ...)

`encoding=` chooses the codec; it says nothing about a byte that codec rejects, so
the default `strict` applies and the command raises *instead of* returning. With a
repository holding one latin-1 file:

    git_cmd("diff") -> UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9
                       in position 97: invalid continuation byte

Two things make this a defect rather than an exotic input. Git's stdout is not
UTF-8 *by construction* the way `gh`'s JSON is - a repository's content is bytes,
and the content commands (`diff`, `show`, `log -p`) emit them verbatim - so the
failure is a property of the tree, not of the host's locale, and it is reachable on
every platform. And an exception out of `git_cmd` is not the `CompletedProcess`
each caller destructures: `repo_scope` reads `.stdout` of `rev-parse
--show-toplevel` to root a writer, `_exclude_path` reads `rev-parse --git-path`.

Why the test asserts the *bytes*, not just "did not raise"
----------------------------------------------------------
`errors="replace"` would also stop the raise, and it is the policy used elsewhere
in this repository for remote text. It is wrong here for the reason the function
docstring gives - a path this function returns is *opened*, and U+FFFD denotes no
file - so a test that only asserted "no exception" would accept the repair that
silently corrupts every path it touches. The assertions below re-encode what came
back with `surrogateescape` and require the original byte, which is exactly the
property `replace` does not have.

The three legs
--------------
1. **content** — a repo with a latin-1 file: `git diff` returns text whose byte
   survives. Red on master (`UnicodeDecodeError`), green on the fix.
2. **control** — `git status --porcelain` on the same repo: text, no raise, on both
   trees. Without it a "fix" that broke every call would still look green here.
3. **the default really is strict** — the same bytes through `subprocess.run` with
   `encoding=` and no `errors=` must raise, so the test proves it is pinning a
   behaviour rather than a spelling that happens to be absent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emrg.server.git_utils import git_cmd  # noqa: E402

# One byte that is not valid UTF-8 and is ordinary latin-1: 'é'. A repository can
# hold this and git will emit it verbatim.
LATIN1_BYTE = 0xE9


def _repo_with_a_latin1_file(tmp_path: Path) -> Path:
    """A git repository whose committed and working copy of f.txt hold 0xE9."""
    repo = tmp_path / "latin1"
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
        )

    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    target = repo / "f.txt"
    target.write_bytes(b"caf" + bytes([LATIN1_BYTE]) + b" one\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    target.write_bytes(b"caf" + bytes([LATIN1_BYTE]) + b" two\n")
    return repo


def test_the_content_commands_return_the_repositorys_bytes(tmp_path: Path) -> None:
    """Leg 1: `git diff` over a latin-1 file returns text, and the byte survives.

    `errors="replace"` passes the first half of this and fails the second, which is
    why both are asserted.
    """
    repo = _repo_with_a_latin1_file(tmp_path)
    result = git_cmd("diff", cwd=str(repo))
    assert result.returncode == 0, result.stderr
    assert "caf" in result.stdout, result.stdout
    # The read produced a string, and it carries the original byte through the
    # surrogate round trip rather than a U+FFFD that denotes no file.
    assert "\ufffd" not in result.stdout, (
        "the byte was replaced rather than carried: a path decoded this way names "
        "no file, so `replace` is not an acceptable policy for this function"
    )
    assert bytes([LATIN1_BYTE]) in result.stdout.encode(
        "utf-8", errors="surrogateescape"
    ), (
        "the repository's own byte must survive the decode: re-encoding with "
        "surrogateescape has to give back 0xE9"
    )


def test_a_command_whose_output_is_ascii_is_unaffected(tmp_path: Path) -> None:
    """Leg 2 (control): the ordinary path still returns text on both trees.

    Without this, a change that made every `git_cmd` call raise would leave leg 1
    looking like a successful fix.
    """
    repo = _repo_with_a_latin1_file(tmp_path)
    result = git_cmd("status", "--porcelain", cwd=str(repo))
    assert result.returncode == 0, result.stderr
    assert "f.txt" in result.stdout, result.stdout


def test_the_default_policy_really_is_the_failure(tmp_path: Path) -> None:
    """Leg 3: the same bytes through the unpinned-policy shape raise.

    This is the leg that keeps leg 1 honest: it shows the test is pinning a
    behaviour (`strict` raising) and not merely a spelling that happens to be
    absent from the source. If a future Python stops raising here, this fails and
    the reasoning above has to be re-measured rather than assumed.
    """
    repo = _repo_with_a_latin1_file(tmp_path)
    git_exe = "git"
    completed_stdout = subprocess.run(
        [git_exe, "-C", str(repo), "diff"],
        capture_output=True, check=True,
    ).stdout
    assert bytes([LATIN1_BYTE]) in completed_stdout, (
        "the fixture must really put a non-UTF-8 byte in git's stdout, or this "
        "test proves nothing about the policy"
    )
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(
            [git_exe, "-C", str(repo), "diff"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
