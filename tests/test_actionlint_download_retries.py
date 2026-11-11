"""A CI step that fetches a release asset survives a transient 5xx, and still fails closed.

Why this file exists
--------------------
`master` failed its Test workflow on **six consecutive runs** from `a85b1ddd` onward
(2026-11-08/09, runs 42467952308, 42468434733, 42468778799, 42469285691, 42469812076,
42469826926), and not one of them failed on a test. Every failure was a step downloading a
binary from `github.com`'s release-asset endpoint, which answered **504**::

    curl: (22) The requested URL returned error: 504
    gzip: stdin: unexpected end of file
    tar: Child returned status 1

Two steps on two legs were hit - this repository's `Actionlint (workflow lint)` step and
`test-windows`'s `astral-sh/setup-uv@v5` - which is what made it look like an outage rather
than a defect. It was neither: the same upstream answered #2759's own run green in the same
window, so the tree was fine and a single unretried fetch was the whole cause. A red leg is
not votable, so one upstream window froze every open PR at once (issue #2760).

Why the step is executed instead of read
----------------------------------------
A grep for `sleep 5` answers "does the text look right". The two properties that matter are
what the step *does*, and both are invisible to a text search:

* **it survives** - a transient failure is retried and the lint still runs;
* **it fails closed** - when no attempt succeeds the step exits non-zero and says which URL
  and how many attempts. A step that kept the gate's name while skipping the lint it could
  not download would be worse than a red job, because nothing would be visible.

So the step's own `run:` block is extracted from the workflow by name and executed under
`bash --noprofile --norc -eo pipefail` (GitHub's `shell: bash` shape) in a temporary tree,
with `curl` stubbed to answer a scripted number of 504s and the tarball it hands back
carrying a fake `actionlint`. Real `tar` unpacks it, so the extraction half is exercised
too. This is the idiom `tests/test_a_step_without_its_product_refuses.py` established for
`build-release.yml`'s steps, and for the same reason: the step is the single source of
truth, so a future edit to it is measured rather than remembered.

The failure direction is asserted on the **message**, not only on the exit code: a bare
`exit 1` is a refusal a reader cannot act on, and the workflow's own convention for that is
an `::error::` line. What the message is held to is what it **names** - the URL, and the
number of attempts it actually made - and not the words it names them with: a step that
printed `gave up fetching <url> after 3 tries` would have said everything the requirement
asks for, and a guard keyed on the literal word `attempt` would redden on it while the step
behaved exactly as required.

The second carrier of the same defect
------------------------------------
The step also *installs* `shellcheck` with apt, because the release binary does not bundle
it, and actionlint runs shellcheck over `run:` blocks only when it finds one. That install
was the same unretried network fetch one step later, so it now carries the same bounded
retry and the same fail-closed refusal - and, for the same reason, it is executed rather
than grepped: an arm that leaves a `shellcheck` on `PATH` never enters the loop, because the
loop's own `command -v` guard answers first. So the arms below run the step with `PATH`
rebuilt from the system's tools minus that one name, and a `sudo` stub that scripts the
mirror's answers. Three separate properties, three arms: the retry survives a window; a
download that never succeeds refuses and names the count; and an install that answers
success without leaving a linter still fails the step.

Named limits
------------
* The retry is bounded by what the step says it is; this file does not assert a particular
  number of attempts, only that a transient one is survived and a permanent one is not
  (the count it drives is read from the stub, and a step that gave up on the first 504
  fails the survive arm).
* The count in the failure message is asserted as a numeral outside the URL, so a count
  spelled out as a word (`after three tries`) is rejected. The step writes numerals, and a
  numeral is what a reader can read off the line without counting the retries themselves.
* Nothing here reaches the network, and no real binary is installed: the only `actionlint`
  is the one this file writes into the tarball, whose only job is to answer the step's
  `-version` probe and its self-probe the way a working linter would.
* The step is not run on Windows: a bare `bash` there is the WSL launcher, which exists and
  cannot run a script (measured 2026-10-02, #1812). The gate says so rather than measuring
  a different shell under the same name.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

#: The step under test, by the name GitHub shows in the run view - the same name a reader
#: of a red run sees, so the two cannot drift apart silently.
STEP_NAME = "Actionlint (workflow lint)"

#: The pin has to stay a release triple: the URL is built from it, and
#: `scripts/check-workflows.py` reads it out of this file to decide whether a local
#: actionlint is the one CI runs.
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the subject is GitHub's `shell: bash` step on ubuntu-latest; on windows-2025 a "
        "bare `bash` is the WSL launcher, which exists and cannot run a script (measured "
        "2026-10-02, #1812)"
    ),
)

#: A fake linter with the two answers the step depends on: it reports its version, and the
#: step's self-probe - a workflow with an unquoted expansion, handed over as `probe.yml` -
#: must come back with a finding, or the step declares its own shellcheck half dead.
_FAKE_ACTIONLINT = """#!/bin/sh
if [ "$1" = "-version" ]; then
  echo "1.7.12"
  exit 0
fi
for arg in "$@"; do
  case "$arg" in
    *probe.yml) exit 1 ;;
  esac
done
echo "no problem"
exit 0
"""

#: Stub `curl`: answers 504 for the first `STUB_FAIL_FIRST` calls, then hands back the
#: tarball at the `-o` target. Every call is counted, so the number of attempts the step
#: really made is a reading rather than an assumption.
_STUB_CURL = """#!/bin/sh
count_file="$STUB_STATE/count"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s' "$n" > "$count_file"
if [ "$n" -le "$STUB_FAIL_FIRST" ]; then
  echo "curl: (22) The requested URL returned error: 504" >&2
  exit 22
fi
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    cp "$STUB_ARCHIVE" "$arg"
    exit 0
  fi
  prev="$arg"
done
echo "stub curl: no -o target among $*" >&2
exit 2
"""

#: Stub `sleep`: records that the step waited instead of hammering the endpoint, and costs
#: no wall clock. Nothing else here is about waiting.
_STUB_SLEEP = """#!/bin/sh
printf 'waited\\n' >> "$STUB_STATE/sleeps"
exit 0
"""

#: Stub `sudo`: answers the sequence of apt failures the arm asks for, and creates a
#: `shellcheck` on `PATH` only when the install really succeeds - so the step's own
#: `command -v` gate below the loop is what decides whether the linter is there. Every call
#: is counted, so how many attempts the step made is a reading and not an assumption.
_STUB_SUDO = """#!/bin/sh
count_file="$STUB_STATE/sudo"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s' "$n" > "$count_file"
if [ "$n" -le "$STUB_SUDO_FAIL_CALLS" ]; then
  echo "E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/main/s/shellcheck/shellcheck_0.9.0-1ubuntu0.1_amd64.deb  Temporary failure resolving 'azure.archive.ubuntu.com'" >&2
  exit 100
fi
case "$*" in
  *shellcheck*)
    if [ "$STUB_SUDO_CREATES" = "1" ]; then
      printf '#!/bin/sh\\nexit 0\\n' > "$STUB_TOOLS/shellcheck"
      chmod +x "$STUB_TOOLS/shellcheck"
    fi
    ;;
esac
exit 0
"""

#: What the step itself runs on: it is shell, but not a POSIX shell builtin-only script -
#: `tar` unpacks the asset, `mktemp` makes the probe directory, `find`/`sort` list the
#: workflows, and `cat` feeds the heredocs. A `PATH` rebuilt without them would measure
#: this file's sandbox instead of the step.
_TOOLS_NEEDED = ("cat", "chmod", "cp", "find", "mkdir", "mktemp", "rm", "sort", "tar")


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


def _archive(path: Path) -> Path:
    """A real gzipped tarball holding the fake linter, so the step's own `tar` runs."""
    body = _FAKE_ACTIONLINT.encode("utf-8")
    info = tarfile.TarInfo("actionlint")
    info.size = len(body)
    info.mode = 0o755
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(body))
    return path


def _stub(directory: Path, name: str, body: str) -> None:
    exe = directory / name
    exe.write_text(body, encoding="utf-8")
    exe.chmod(0o755)


def _system_path_without_shellcheck(tmp_path: Path) -> Path:
    """A `PATH` made of the system's own tools, with no `shellcheck` on it.

    The step installs the linter only when `command -v shellcheck` fails, so an arm about
    that loop has to run where none can be found - and the runner the `test` leg uses ships
    one in `/usr/bin`. Dropping `/usr/bin` would take `tar`, `mktemp` and `find` with it, so
    the directory is rebuilt as symlinks to what the system has, minus that one name.
    Nothing is installed and nothing leaves the host.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    seen: set[str] = set()
    for source in ("/usr/bin", "/bin"):
        if not os.path.isdir(source):
            continue
        for name in sorted(os.listdir(source)):
            if name in seen or name == "shellcheck":
                continue
            seen.add(name)
            try:
                (tools / name).symlink_to(os.path.join(source, name))
            except OSError:
                # A name this process may not link is not one the step needs; the premise
                # assertion below names any that actually mattered.
                continue
    return tools


def _run_step(
    tmp_path: Path,
    *,
    fail_first: int,
    isolate_shellcheck: bool = False,
    sudo_fail_calls: int = 0,
    sudo_creates: bool = True,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Execute the step's own `run:` block, with `curl` failing `fail_first` times.

    `isolate_shellcheck` swaps the standing `shellcheck` stub for a `sudo` stub and rebuilds
    `PATH` so that no linter is findable. That is the only state in which the step's install
    loop is entered at all: with a `shellcheck` reachable, `command -v` answers, the loop is
    skipped, and an arm asserting anything about it measures the guard instead. The default
    is the stub, because the arms about the download never want to install anything.
    """
    step = _step()
    version = (step.get("env") or {}).get("ACTIONLINT_VERSION", "")

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _stub(stubs, "curl", _STUB_CURL)
    _stub(stubs, "sleep", _STUB_SLEEP)
    tools: Path | None = None
    if isolate_shellcheck:
        tools = _system_path_without_shellcheck(tmp_path)
        _stub(stubs, "sudo", _STUB_SUDO)
        path = f"{stubs}{os.pathsep}{tools}"
        missing = [name for name in _TOOLS_NEEDED if shutil.which(name, path=path) is None]
        if missing:
            pytest.skip(f"this host provides no {missing}, so the step cannot run here")
        found = shutil.which("shellcheck", path=path)
        assert found is None, (
            "this arm needs a PATH with no shellcheck on it, and one is reachable at "
            f"{found} - the step would skip its install loop, and every assertion about "
            "that loop would describe a guard it never entered"
        )
    else:
        # The step installs shellcheck with apt only because the release binary does not
        # bundle it; a stub on PATH keeps this guard off the network and off sudo.
        _stub(stubs, "shellcheck", "#!/bin/sh\nexit 0\n")
        path = f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin"
    archive = _archive(tmp_path / "actionlint.tar.gz")

    tree = tmp_path / "tree"
    workflows = tree / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text("name: t\non: push\njobs: {}\n", encoding="utf-8")

    script = tmp_path / "step.sh"
    script.write_text(textwrap.dedent(step["run"]), encoding="utf-8")

    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(runner_temp),
        "ACTIONLINT_VERSION": version,
        "STUB_STATE": str(state),
        "STUB_FAIL_FIRST": str(fail_first),
        "STUB_ARCHIVE": str(archive),
        "STUB_SUDO_FAIL_CALLS": str(sudo_fail_calls),
        "STUB_SUDO_CREATES": "1" if sudo_creates else "0",
        "STUB_TOOLS": str(tools) if tools is not None else "",
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        # The step prints UTF-8, and the locale codec of the host running this test is not
        # a decoder for it (issue #1132).
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return result, state, runner_temp


def _names_url_and_count(line: str, attempts: int) -> bool:
    """Does this error line name the URL, and the number of attempts it made?

    Asserted on the content of the line rather than on its wording, because the wording is
    not what the step promises. `gave up fetching <url> after 3 tries` names both, and so
    does `cannot download <url> after 3 attempt(s)`; a line that names only the URL, or
    names a different number, says less than a reader of a red run needs to act on.

    The number is the one the stub counted, so this reads the reading rather than a word:
    a count in the URL itself (`v1.7.12`, `..._1.7.12_linux_amd64.tar.gz`) is stripped
    before the digits are matched, and cannot stand in for the count of attempts.
    """
    if "releases/download" not in line:
        return False
    without_url = re.sub(r"https?://\S+", " ", line)
    return str(attempts) in re.findall(r"\d+", without_url)


def test_the_step_this_file_is_about_is_the_gate_and_keeps_its_pin() -> None:
    """The premise: the step exists, downloads the pin, and may not excuse itself.

    Without this, every arm below would be about nothing, and `continue-on-error` is the
    one shape that would make "it survives" true the wrong way - a step that failed and
    was forgiven reads exactly like a step that retried successfully.
    """
    step = _step()
    run = step["run"]
    assert "curl" in run and "releases/download" in run, (
        "the step no longer downloads a release asset - this guard's subject is the fetch, "
        "so update it rather than deleting it"
    )
    version = (step.get("env") or {}).get("ACTIONLINT_VERSION", "")
    assert _RELEASE.match(version), (
        f"ACTIONLINT_VERSION is {version!r}, which is not a release triple; the URL is built "
        "from it, and scripts/check-workflows.py reads it from this file"
    )
    assert not step.get("continue-on-error"), (
        "the step may not be forgiven on failure: a download that never succeeded would "
        "then leave this gate reading green while linting nothing"
    )
    assert "|| true" not in run, (
        "`|| true` on the fetch swallows exactly the failure this guard asserts is visible"
    )


@POSIX_ONLY
def test_a_transient_504_is_retried_and_the_lint_still_runs(tmp_path) -> None:
    """Two 504s then a success: the step proceeds, having waited between attempts."""
    result, state, runner_temp = _run_step(tmp_path, fail_first=2)
    assert result.returncode == 0, (
        "the step failed on a transient upstream error, which is the defect this guard is "
        f"about:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert (runner_temp / "actionlint").exists(), (
        "the step exited 0 but no linter was unpacked - it would report the gate as run "
        "while running nothing"
    )
    attempts = int((state / "count").read_text(encoding="utf-8").strip())
    assert attempts == 3, f"expected three attempts (two failures, then one success), got {attempts}"
    assert (state / "sleeps").exists(), (
        "the retries were fired back to back with no pause; a 5xx window is hundreds of "
        "milliseconds wide and a pause is what makes the retry worth having"
    )


@POSIX_ONLY
def test_a_download_that_never_succeeds_fails_the_step_and_names_it(tmp_path) -> None:
    """The other direction: every attempt fails, so the step refuses - loudly."""
    result, state, runner_temp = _run_step(tmp_path, fail_first=99)
    assert result.returncode != 0, (
        "the step exited 0 although no attempt could obtain the linter - that is a gate "
        f"keeping its name while checking nothing:\nstdout:\n{result.stdout}"
    )
    errors = [line for line in result.stdout.splitlines() if "::error::" in line]
    assert errors, (
        "no `::error::` line: a bare exit is a refusal a reader of the run view cannot act "
        f"on.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert not (runner_temp / "actionlint").exists(), (
        "a linter exists although the download failed - the stub is not measuring a refusal"
    )
    attempts = int((state / "count").read_text(encoding="utf-8").strip())
    assert attempts > 1, (
        f"the step gave up after {attempts} attempt(s), so it is not retrying at all"
    )
    assert any(_names_url_and_count(line, attempts) for line in errors), (
        "the error must name the URL and the number of attempts it made "
        f"({attempts}), in whatever words it chooses; got {errors}"
    )


def _names_attempt_count(line: str, attempts: int) -> bool:
    """Does this error line say how many attempts it made, as a numeral?

    The count, not the wording: `after 3 attempt(s)` and `on its third try` are the same
    promise to a reader of a red run, and a guard keyed on the literal word `attempt` would
    redden on a step that behaved exactly as required while measuring nothing.
    """
    return str(attempts) in re.findall(r"\d+", line)


@POSIX_ONLY
def test_the_linter_install_is_retried_and_the_step_goes_on(tmp_path) -> None:
    """A mirror window fails twice, then answers: the install happens and the step continues.

    The shape is the one measured on 2026-11-11, when every open pull request went red and
    none of them on a test: `apt-get update` exits 0 with warnings, and the fetch of the
    `.deb` behind it fails. The install is the same network fetch the download above it
    already retried, so it gets the same treatment - and this arm is what holds it there.
    """
    result, state, runner_temp = _run_step(
        tmp_path, fail_first=0, isolate_shellcheck=True, sudo_fail_calls=2
    )
    assert result.returncode == 0, (
        "the step failed on a transient mirror error, so one upstream window reds every "
        f"open pull request again:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert (tmp_path / "tools" / "shellcheck").exists(), (
        "the step exited 0 with no linter installed - `actionlint` would then lint without "
        "its shellcheck half and the gate would keep its name while checking less"
    )
    calls = int((state / "sudo").read_text(encoding="utf-8").strip())
    assert calls == 4, (
        "expected four apt calls (two failed index refreshes, then a refresh and an "
        f"install that succeed), got {calls} - fewer means the index refresh moved outside "
        "the loop, which spends every attempt against a list the failure never refreshed"
    )
    assert (state / "sleeps").exists(), (
        "the install was retried back to back with no pause; a mirror window is what the "
        "pause is for"
    )


@POSIX_ONLY
def test_an_install_that_never_succeeds_fails_the_step_and_names_the_attempts(tmp_path) -> None:
    """The other direction: no attempt installs, so the step refuses rather than skipping."""
    result, state, runner_temp = _run_step(
        tmp_path, fail_first=0, isolate_shellcheck=True, sudo_fail_calls=99
    )
    assert result.returncode != 0, (
        "the step exited 0 although nothing was ever installed - that is a gate reporting "
        f"itself as run while checking nothing:\nstdout:\n{result.stdout}"
    )
    assert (runner_temp / "actionlint").exists(), (
        "the linter itself was never unpacked here, so this arm is not about the install - "
        "the failure above came from the download, not from the loop under test"
    )
    assert not (tmp_path / "tools" / "shellcheck").exists(), (
        "a linter exists although no attempt installed one - the stub is not measuring a "
        "refusal"
    )
    errors = [line for line in result.stdout.splitlines() if "::error::" in line]
    assert errors, (
        "no `::error::` line: a bare exit is a refusal a reader of the run view cannot act "
        f"on.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert any("shellcheck" in line and _names_attempt_count(line, 3) for line in errors), (
        "the error must name what could not be installed and the number of attempts it "
        f"made (3), in whatever words it chooses; got {errors}"
    )


@POSIX_ONLY
def test_an_install_that_answers_success_without_a_linter_fails_the_step(tmp_path) -> None:
    """The assertion under the loop: apt said 0, and there is still no linter on `PATH`.

    Without that line the step would take the install's exit code as evidence, then run
    `actionlint` with no shellcheck anywhere - the half-of-the-gate failure the step's own
    comment says it exists to prevent. It is a separate carrier from the loop, so it gets a
    separate arm: a loop that retries correctly cannot cover it.
    """
    result, state, runner_temp = _run_step(
        tmp_path, fail_first=0, isolate_shellcheck=True, sudo_fail_calls=0, sudo_creates=False
    )
    assert result.returncode != 0, (
        "the step exited 0 although the install left no shellcheck findable on PATH - the "
        f"gate would then run with half its checks:\nstdout:\n{result.stdout}"
    )
    assert not (tmp_path / "tools" / "shellcheck").exists(), (
        "the stub did not measure what this arm means to measure"
    )
