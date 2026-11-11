"""A mirror window must not red the sandbox step, and the step must still fail closed.

Why this file exists
--------------------
On 2026-11-11 the `test` leg of every open pull request went red, and none of them went red
on a test. Master's own run `43051480347` failed at the step that installs the Linux sandbox
dialect's runner, and the log says why:

    Temporary failure resolving 'azure.archive.ubuntu.com'
    E: Failed to fetch mirror+file:/etc/apt/apt-mirrors.txt/pool/main/b/bubblewrap/bubblewrap_0.9.0-1ubuntu0.3_amd64.deb
    E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?
    ##[error]Process completed with exit code 100.

The same two lines took down `43043686605`, `43043484572` and `43051487300` in the same
window. The install was one unretried network fetch, while the step seventy lines above it
already retried its own download three times for exactly this reason - and that comment says
why it matters: a red leg is not votable, so one upstream window freezes every open pull
request at once with master reading red for no commit's fault.

Why the step is executed instead of read
----------------------------------------
A grep for `attempts=3` answers "does the text look right". The three properties that matter
are all invisible to a text search:

* **the install survives** a transient failure and the step still finishes;
* **the index refresh is inside the loop** - retrying only the install would spend every
  attempt against a list the failed update never refreshed, which is the failure the log
  itself ends with ("maybe run apt-get update");
* **it still fails closed** - when no attempt succeeds the step exits non-zero naming the
  number of attempts, and the `bwrap --ro-bind` assertion below it stays the reason a leg
  with no working sandbox cannot report green.

So the step's own `run:` block is extracted from the workflow by name and executed under
`bash --noprofile --norc -eo pipefail` (GitHub's `shell: bash` shape) in a temporary tree,
with `sudo` stubbed to answer a scripted sequence of failures and to create the `bwrap`
binary only when the install really succeeds. This is the idiom
`tests/test_actionlint_download_retries.py` established for the sibling step, and for the
same reason: the step is the single source of truth, so a future edit to it is measured
rather than remembered.

What each arm is for
--------------------
* **install fails twice, then succeeds** - the measured shape: `apt-get update` exits 0 with
  warnings, the fetch of the .deb fails. The step must finish, and `bwrap` must actually run.
* **the update itself fails, then everything succeeds** - the discriminating arm for the
  index refresh: a retry wrapped around only the install keeps the update outside the loop,
  survives the first arm and fails this one.
* **nothing ever succeeds** - the step must exit non-zero, name how many attempts it made,
  and never reach the assertion. A step that skipped itself once the install failed would
  keep the gate's name while checking nothing.

Named limits
------------
* Nothing here reaches the network, and nothing is installed on the host: the only `bwrap`
  is the stub this file's `sudo` writes, and the only apt-get is the stub that pretends.
* The step is not run on Windows: a bare `bash` there is the WSL launcher, which exists and
  cannot run a script (measured 2026-10-02, #1812).
* The AppArmor branch is not exercised: it is guarded by a `/proc` file that does not exist
  on this host, and the assertion's own stub answers when it is reached on a runner.
* The count is asserted as a numeral, not as a particular word: what the message must say is
  how many attempts it made, in a form a reader can read off the line.
* **The step is handed a `PATH` inside this test's own tree, and the premise is asserted
  rather than assumed.** The job that runs this file installs the real `bwrap` into
  `/usr/bin` seventy lines before pytest, so a `PATH` carrying `/usr/bin` let the step's own
  `command -v bwrap` guard short-circuit and left the three arms measuring a loop that never
  ran (measured 2026-11-12, run 43065169139: `installs=0`, `updates=0`, and the fail-closed
  arm exiting 0). On a host with no `bwrap` - every developer machine here - the old shape
  was green whether or not that guard worked, so the `test` leg is the one that discriminates.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

#: The step under test, by the name GitHub shows in the run view - the same name a reader of
#: a red run sees, so the two cannot drift apart silently.
STEP_NAME = "Ensure bubblewrap (the Linux sandbox dialect's runner)"

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the subject is GitHub's `shell: bash` step on ubuntu-latest; on windows-2025 a "
        "bare `bash` is the WSL launcher, which exists and cannot run a script (measured "
        "2026-10-02, #1812)"
    ),
)

#: Stub `sudo`. Every invocation is counted, and the two halves fail independently so each
#: arm can drive one of them:
#:
#: * `STUB_UPDATE_FAIL_FIRST` - the index refresh answers exit 100 that many times;
#: * `STUB_INSTALL_FAIL_FIRST` - the package fetch answers exit 100 that many times;
#: * a *successful* install creates the `bwrap` binary, which is the only way the step's
#:   `command -v bwrap` guard can be cleared and its assertion reached.
#:
#: The measured log is reproduced in the failure text, so a red arm here reads like the red
#: run it stands for.
_STUB_SUDO = """#!/bin/sh
# The stub calls the real `chmod`, so it puts the system directories back on its own PATH.
# The step that calls it is handed neither of them - see `_run_step`, which is where the
# reason lives.
PATH=/usr/bin:/bin
state="$STUB_STATE"
key="$1 $2"
case "$key" in
  "apt-get update")
    n=$(cat "$state/updates" 2>/dev/null || echo 0)
    n=$((n + 1))
    printf '%s' "$n" > "$state/updates"
    if [ "$n" -le "$STUB_UPDATE_FAIL_FIRST" ]; then
      echo "Err:1 http://azure.archive.ubuntu.com/ubuntu noble InRelease" >&2
      echo "  Temporary failure resolving 'azure.archive.ubuntu.com'" >&2
      echo "E: Some index files failed to download." >&2
      exit 100
    fi
    echo "W: Some index files failed to download. They have been ignored, or old ones used instead." >&2
    exit 0
    ;;
  "apt-get install")
    i=$(cat "$state/installs" 2>/dev/null || echo 0)
    i=$((i + 1))
    printf '%s' "$i" > "$state/installs"
    if [ "$i" -le "$STUB_INSTALL_FAIL_FIRST" ]; then
      echo "Err:2 mirror+file:/etc/apt/apt-mirrors.txt noble-updates/main amd64 bubblewrap" >&2
      echo "  Temporary failure resolving 'azure.archive.ubuntu.com'" >&2
      echo "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?" >&2
      exit 100
    fi
    printf '#!/bin/sh\\nprintf "ran\\\\n" >> "%s"\\nexit 0\\n' "$state/bwrap-ran" > "$STUB_BIN/bwrap"
    chmod +x "$STUB_BIN/bwrap"
    exit 0
    ;;
  "sysctl -w")
    exit 0
    ;;
esac
echo "stub sudo: unexpected arguments: $*" >&2
exit 2
"""

#: Stub `sleep`: records that the step waited instead of hammering the mirror, and costs no
#: wall clock. Nothing here is about waiting.
_STUB_SLEEP = """#!/bin/sh
printf 'waited\\n' >> "$STUB_STATE/sleeps"
exit 0
"""


def _step() -> dict:
    """The one step this file is about, found in the workflow by its name."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step
    raise AssertionError(
        f"{WORKFLOW.relative_to(REPO_ROOT)} has no step named {STEP_NAME!r}. This guard "
        "would then have no subject and pass without measuring anything - if the step was "
        "renamed, rename it here too."
    )


def _run_step(tmp_path: Path, *, install_fail_first: int, update_fail_first: int = 0):
    """Execute the step's own `run:` block against a scripted sequence of apt failures.

    Returns the completed process, the state directory (which holds what the stub recorded),
    and the script that ran - so a failure can quote the subject instead of describing it.
    """
    stubs = tmp_path / "bin"
    stubs.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for name, body in (("sudo", _STUB_SUDO), ("sleep", _STUB_SLEEP)):
        exe = stubs / name
        exe.write_text(body, encoding="utf-8")
        exe.chmod(0o755)

    # The step's PATH is this tree and nothing else. It matters because the job that runs
    # this file runs the step under test seventy lines earlier, and that step installs the
    # real `bwrap` into `/usr/bin`: with `/usr/bin` on this PATH the step's own
    # `command -v bwrap` guard short-circuits, the install loop never runs, and all three
    # arms below measure nothing (measured 2026-11-12, run 43065169139). The shim carries
    # the one external command the step itself calls - `cat` in its AppArmor branch - and
    # the stubs restore the system directories on their own PATH, which they need for
    # `chmod`.
    shim = tmp_path / "shim"
    shim.mkdir()
    cat = shutil.which("cat")
    assert cat is not None, "no `cat` on this host, so the step's AppArmor branch cannot run"
    (shim / "cat").symlink_to(cat)

    # The interpreter is resolved here and passed absolutely: it is what runs the step, not
    # a command the step looks up, so it has no business on the PATH under test.
    bash = shutil.which("bash")
    assert bash is not None, "no `bash` on this host, so the step cannot be executed at all"

    script = tmp_path / "step.sh"
    script.write_text(_step()["run"], encoding="utf-8")

    env = {
        "PATH": f"{stubs}:{shim}",
        "STUB_STATE": str(state),
        "STUB_BIN": str(stubs),
        "STUB_INSTALL_FAIL_FIRST": str(install_fail_first),
        "STUB_UPDATE_FAIL_FIRST": str(update_fail_first),
        "HOME": str(tmp_path),
    }
    # The premise, asserted rather than assumed: a host that can see a `bwrap` from here
    # turns every arm below into a measurement of that host.
    assert shutil.which("bwrap", path=env["PATH"]) is None, (
        "the step can see a `bwrap` on the PATH it was handed, so its install loop is "
        f"skipped and nothing below measures the retry. PATH was: {env['PATH']}"
    )
    assert all(
        Path(entry).resolve().is_relative_to(tmp_path.resolve())
        for entry in env["PATH"].split(":")
    ), (
        "the step's PATH names a directory outside this test's own tree, which is how the "
        f"runner's own `bwrap` gets in. PATH was: {env['PATH']}"
    )
    proc = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return proc, state, script


def _count(state: Path, name: str) -> int:
    """How many times the stub recorded `name` as an integer counter."""
    path = state / name
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip() or 0)


def _waits(state: Path) -> int:
    """How many times the step waited: the sleep stub appends one line per call."""
    path = state / "sleeps"
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


@POSIX_ONLY
def test_a_transient_mirror_failure_is_retried_and_the_step_finishes(tmp_path: Path) -> None:
    """The measured shape: two failed fetches, then the install succeeds.

    This is the arm that reproduces the red run. Without the retry it fails exactly as
    `43051480347` did; with it the step finishes and the assertion below it runs.
    """
    proc, state, script = _run_step(tmp_path, install_fail_first=2)

    assert proc.returncode == 0, (
        "a mirror window that clears on a later attempt must not fail the step.\n"
        f"exit: {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\n"
        f"script that ran:\n{script.read_text(encoding='utf-8')}"
    )
    assert _count(state, "installs") == 3, (
        "the step did not take exactly the attempts the stub scripted; the loop is not "
        f"re-entered the way it claims. installs={_count(state, 'installs')}"
    )
    assert _waits(state) == 2, (
        "the step retried without waiting between attempts, which hammers an upstream that "
        f"is already failing. waits={_waits(state)}"
    )
    assert (state / "bwrap-ran").exists(), (
        "the step returned without running the binary it installed - the assertion that "
        "keeps this gate honest never executed"
    )


@POSIX_ONLY
def test_the_index_refresh_is_inside_the_retry(tmp_path: Path) -> None:
    """The discriminating arm: a failed `apt-get update` must also be retried.

    The log this issue comes from ends with apt's own advice - "maybe run apt-get update" -
    because the index it was about to install from had never refreshed. A retry wrapped
    around the install alone survives the arm above and fails here.
    """
    proc, state, script = _run_step(tmp_path, install_fail_first=0, update_fail_first=1)

    assert proc.returncode == 0, (
        "the index refresh failed once and the step gave up: the retry does not cover the "
        "update the install depends on.\n"
        f"exit: {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\n"
        f"script that ran:\n{script.read_text(encoding='utf-8')}"
    )
    assert _count(state, "updates") == 2, (
        f"expected the refresh to be attempted twice, saw {_count(state, 'updates')}"
    )
    assert (state / "bwrap-ran").exists(), "the assertion never ran after the retry"


@POSIX_ONLY
def test_a_permanent_failure_names_its_attempts_and_never_reaches_the_assertion(
    tmp_path: Path,
) -> None:
    """The fail-closed direction: no attempt succeeds, so the step must refuse loudly."""
    proc, state, script = _run_step(tmp_path, install_fail_first=99)

    assert proc.returncode != 0, (
        "the step reported success while the sandbox runner was never installed - a leg "
        "that keeps the gate's name while checking nothing is worse than a red job.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "::error::" in proc.stdout, (
        "the step failed without an `::error::` line, which is the convention every remedy "
        f"in this repository uses so the reason is visible in the run view.\n{proc.stdout}"
    )
    assert "azure.archive.ubuntu.com" in (proc.stdout + proc.stderr), (
        "the failure does not quote what the mirror said, so a reader cannot tell an "
        f"upstream fault from a broken step.\n{proc.stdout}\n{proc.stderr}"
    )
    attempts = _count(state, "installs")
    assert attempts >= 2, (
        f"the step gave up after {attempts} attempt(s) - it is not retrying at all"
    )
    numerals = [int(n) for n in re.findall(r"\b\d+\b", proc.stdout)]
    assert attempts in numerals, (
        f"the message does not name the {attempts} attempt(s) it made, in a form a reader "
        f"can read off the line. numerals seen: {numerals}\n{proc.stdout}"
    )
    assert not (state / "bwrap-ran").exists(), (
        "the assertion ran even though nothing was installed - the step's own guard is not "
        "guarding anything"
    )


@POSIX_ONLY
def test_the_step_still_asserts_the_runner_it_installed() -> None:
    """The gate's teeth, read from the step that ships: installing is not the same as working.

    `bwrap` needs an unprivileged user namespace, and Ubuntu 23.10+ restricts it through
    AppArmor; a step that installed the binary and stopped there would report green on a
    runner where every sandbox test skips. The assertion is the reason the step exists, so a
    change that dropped it while adding a retry is the one this test refuses.
    """
    script = _step()["run"]
    assert "bwrap --ro-bind / / -- true" in script, (
        "the step no longer runs bwrap after installing it, so a leg whose sandbox cannot "
        "start would report a green skip instead of a red failure"
    )
    assert "|| true" not in script, (
        "the assertion was softened into a command that cannot fail - the retry must never "
        "become an `or skip` path"
    )
