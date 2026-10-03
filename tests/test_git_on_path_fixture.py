"""The autouse git-repair fixture, pinned in both of the states it must tell apart.

`tests/conftest.py::_ensure_git_on_path` prepends a working `git` to PATH for the
tests that shell out to a bare `git`. It had **no test of its own** until
2026-10-03 (`cyc20261003-130332`), and the gap that let it survive is the one this
file closes.

What it got wrong
-----------------
The fixture skipped its repair when `shutil.which("git")` was truthy — "git already
reachable". That answers *is this name on PATH*, not *will this program run*, and
the two differ on a host whose `git` is a shim over a removed interpreter: `which`
returns its path, a bare `git` call exits 126 (`No such file or directory`), and
the repair the fixture exists for never happens. Measured with such a shim at the
front of PATH, the two tests the fixture was written for failed exactly the way its
docstring says it removes (`test_windows_scripts_are_crlf`,
`test_git_origin_url_missing_remote`, both rc=126).

This is not a hypothetical host shape: it is this machine's state for
`npm`/`node`/`npx`, whose asdf shims point at a deleted interpreter — the same
`which`-as-verdict defect #1826 carried into `test_check_node_test_count.py`.

What is pinned
--------------
* `starts` — the discriminator, `tests/tool_preflight.py` — in **both** directions, on
  real files this test creates, because a check that cannot produce its own failure
  state is the defect one level up.
* that `which` and `starts` really disagree on a dead shim (the measured pair the old
  gate collapsed), and
* the fixture's own decision on a PATH this test owns.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.tool_preflight import starts

REPO_ROOT = Path(__file__).resolve().parent.parent


def _conftest():
    """`tests/conftest.py`, from the copy pytest already loaded.

    Read out of `sys.modules` by file identity rather than `import conftest`:
    whether the tests directory is on `sys.path` depends on pytest's import mode,
    while the rootdir conftest is loaded under *some* name in every run — and a
    second copy executed through `importlib` would install the autouse guards twice.
    """
    wanted = Path(__file__).with_name("conftest.py").resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve() == wanted:
            return module
    raise AssertionError("tests/conftest.py is not loaded in this run")


def _dead_git(directory: Path) -> Path:
    """A `git` that resolves and cannot run — it hands off to a gone interpreter.

    The shape measured on this host for `npm`/`node`/`npx`: the file exists, has the
    execute bit, and the program it execs is not there.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "git"
    shim.write_text(
        '#!/bin/sh\nexec /nonexistent/interpreter-for-this-test "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_starts_reads_the_exit_code_not_the_name(tmp_path):
    """Both states, from a file this test creates — the discriminator itself.

    `_starts(<exists but cannot run>)` must be False and `_starts(<a program that
    runs>)` must be True. Asserting only the first would pass for a helper that
    always answers False, which would make the fixture repair on every host.
    """
    conftest = _conftest()

    dead = _dead_git(tmp_path / "deadbin")
    assert dead.exists(), "the shim has to exist for this test to be about running it"
    assert starts(str(dead)) is False, (
        "a program that exists and cannot run is not a program that starts — and "
        "that is exactly the state `shutil.which` calls present"
    )

    real = shutil.which("git") or sys.executable
    assert starts(real) is True, (
        f"{real!r} runs, so it must be reported as starting, or the fixture would "
        "repair a host that needs nothing"
    )
    assert starts(None) is False
    assert starts(str(tmp_path / "not-there" / "git")) is False


def test_which_and_starts_disagree_on_a_dead_shim(tmp_path, monkeypatch):
    """The measured pair the old gate collapsed into one answer.

    With the shim first on PATH, `shutil.which("git")` is truthy while a bare `git`
    call fails. The fixture's old gate read the first answer as "nothing to do".
    """
    conftest = _conftest()
    deadbin = _dead_git(tmp_path / "deadbin")
    monkeypatch.setenv("PATH", str(deadbin.parent) + os.pathsep + os.environ["PATH"])

    assert shutil.which("git") == str(deadbin), (
        "the shim must be what PATH resolves, or this test is about the host's git"
    )
    proc = subprocess.run(
        ["git", "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode != 0, (
        "the shim is supposed to be unable to run; a passing `git --version` means "
        "this test built a working git and so measures nothing"
    )
    assert starts(shutil.which("git")) is False, (
        "`which` found a path and the program did not start — the fixture must not "
        "read that as 'git already reachable'"
    )


def test_the_repair_is_reached_when_path_git_cannot_start(tmp_path, monkeypatch):
    """The fixture's own decision, on a PATH **and** tier order this test owns.

    Run against a dead shim, the fixture must not return having decided "git is
    fine": it has to prepend a directory whose git starts.

    The two tiers the fixture consults are monkeypatched rather than left to the
    host, because the first draft of this test was not: measured 2026-10-03, run
    with `HOME` pinned to a temporary directory (the shape a mutation arm uses),
    it failed on two of three tests because the real `HOME` was what supplied a
    working git. A probe that reads the host's `~/.emrg/install` to pass is not
    measuring the fixture.
    """
    import emrg.server.git_utils as git_utils

    conftest = _conftest()
    deadbin = _dead_git(tmp_path / "deadbin")

    # A `git` that certainly starts: a shim onto this interpreter, whose `--version`
    # exits 0. Named `git` because the fixture prepends its *directory*.
    goodbin = tmp_path / "goodbin"
    goodbin.mkdir()
    good = goodbin / "git"
    good.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    good.chmod(0o755)
    assert starts(str(good)) is True, "the stand-in git must start"

    monkeypatch.setenv("PATH", str(deadbin.parent))
    monkeypatch.setattr(git_utils, "_cached_tool_path", lambda tool: str(good))
    monkeypatch.setattr(git_utils, "_tool_in_install", lambda tool: None)

    conftest._ensure_git_on_path.__wrapped__(monkeypatch)

    resolved = shutil.which("git")
    assert resolved == str(good), (
        f"PATH resolves to {resolved!r}; with the shim unable to run and a working "
        "git available from the tiers, the fixture has to prepend it — leaving the "
        "dead shim in front is the failure it exists to remove"
    )
    assert _starts_here(resolved), f"the fixture prepended {resolved!r}, which does not start"


def _starts_here(exe: str) -> bool:
    """Whether `exe` runs, read without touching the fixture's own helper."""
    proc = subprocess.run(
        [exe, "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0


def test_the_repair_stays_off_when_path_git_starts(tmp_path, monkeypatch):
    """The other direction: a host that needs nothing must not be repaired.

    Without this, a fixture that always prepends something would satisfy the test
    above while breaking every host whose git is fine.
    """
    import emrg.server.git_utils as git_utils

    conftest = _conftest()
    goodbin = tmp_path / "goodbin"
    goodbin.mkdir()
    good = goodbin / "git"
    good.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    good.chmod(0o755)

    monkeypatch.setenv("PATH", str(goodbin))
    called: list[str] = []
    monkeypatch.setattr(git_utils, "_cached_tool_path", lambda tool: called.append(tool) or None)
    monkeypatch.setattr(git_utils, "_tool_in_install", lambda tool: called.append(tool) or None)

    conftest._ensure_git_on_path.__wrapped__(monkeypatch)

    assert shutil.which("git") == str(good), "PATH must be left alone"
    assert called == [], (
        f"the fixture consulted the tier order ({called}) for a git that already "
        "starts — that is the repair running where nothing is needed"
    )
