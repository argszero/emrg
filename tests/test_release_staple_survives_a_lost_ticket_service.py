"""The release's Staple step must survive a lost ticket service, not lose the release.

Measured on tag `v0.3.10`, run `38353839071` (2026-10-13): the `Staple pkg` step of
`build-release.yml` reddened although nothing about the artifact was wrong. `stapler
staple` reported "The staple and validate action worked!", and the *following* `stapler
validate` lost its connection to `api.apple-cloudkit.com` about six seconds in and exited
68. The job's `release` neighbour declares `needs: build`, so that single leg skipped the
`release` job and left the tag bare — invisible to the upgrade chain, which reads the
releases API only.

Both `stapler` calls fetch Apple's ticket over the network, so the step now retries them a
bounded number of times. These arms prove both directions, because either alone passes
trivially: a transient loss must still end the step **green**, and a permanent one must
still end it **red** after a bounded number of attempts. A retry that swallowed the second
case would publish an unstapled pkg.

The body is read from the **parsed** step and executed the way the runner runs it, with
`xcrun`, `spctl` and `sleep` stubbed — the shape
`tests/test_release_notarize_reports_its_reason.py` established. `sleep` is stubbed rather
than shortened by a test-only knob: the arms still record every requested delay, so
"bounded, and it actually waits" is measured instead of assumed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

WORKFLOW = ".github/workflows/build-release.yml"

STEP_NAME = "Staple pkg (macOS only)"

# The runner's default flags for `shell: bash` — `-e` is what turns a failed call into a
# red step, so the arms must run under them and not under a friendlier interpreter default.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

PKG_NAME = "EMRG-0.3.10-macos-arm64.pkg"

# The measured failure text, verbatim enough that a reader recognises the run it came from.
LOST_CONNECTION = (
    'error is Error Domain=NSURLErrorDomain Code=-1005 "The network connection was lost." '
    "... NSErrorFailingURLStringKey=https://api.apple-cloudkit.com/database/1/"
    "com.apple.gk.ticket-delivery/production/public/records/lookup"
)
VALIDATE_FAILED = "The validate action failed! Error 68."

# The bound the step declares. Spelled here so the arm fails loudly when the two drift,
# rather than silently accepting any number at all.
ATTEMPTS = 5

_XCRUN_STUB = """#!/usr/bin/env python3
\"\"\"Stand in for xcrun, and answer `stapler` the way the scenario says.\"\"\"
import os, sys

argv = sys.argv[1:]
calls = os.environ["STUB_CALLS"]


def record(line):
    with open(calls, "a", encoding="utf-8") as fh:
        fh.write(line + "\\n")


if argv[:1] != ["stapler"]:
    sys.exit(f"stub only speaks stapler, got {argv!r}")
record(" ".join(argv))
sub = argv[1] if len(argv) > 1 else ""

if sub == "staple":
    # The measured run: THIS call succeeded and said so.
    sys.stdout.write("Processing: " + argv[-1] + "\\n")
    sys.stdout.write("The staple and validate action worked!\\n")
    sys.exit(0)

if sub == "validate":
    scenario = os.environ["STAPLE_SCENARIO"]
    with open(calls, encoding="utf-8") as fh:
        seen = sum(1 for ln in fh if ln.split()[1:2] == ["validate"])
    if scenario == "transient" and seen > 1:
        # A second attempt over a working connection: the ticket IS there.
        sys.stdout.write("Processing: " + argv[-1] + "\\n")
        sys.stdout.write("The staple and validate action worked!\\n")
        sys.exit(0)
    sys.stdout.write("Processing: " + argv[-1] + "\\n")
    sys.stderr.write(os.environ["LOST_CONNECTION"] + "\\n")
    sys.stderr.write(os.environ["VALIDATE_FAILED"] + "\\n")
    sys.exit(68)

sys.exit(f"stub does not know stapler {sub!r}")
"""

_SPCTL_STUB = """#!/usr/bin/env python3
\"\"\"The Gatekeeper verdict: accepted, and recorded so the arm can order the calls.\"\"\"
import os, sys

with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("spctl " + " ".join(sys.argv[1:]) + "\\n")
sys.stdout.write("dist/artifacts/pkg: accepted\\n")
sys.exit(0)
"""

_SLEEP_STUB = """#!/usr/bin/env python3
\"\"\"Record the delay instead of serving it: the arm measures the schedule, not the clock.\"\"\"
import os, sys

with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("sleep " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(0)
"""


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _staple_step() -> dict:
    """The step, found by name in the parsed job — never by a text search.

    The sign, notarize and staple steps all locate the pkg with the same `find`, so a
    document-level match cannot say which step was read.
    """
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "build" in jobs, "build-release.yml has no `build` job"
    steps = jobs["build"].get("steps")
    assert isinstance(steps, list) and steps, "the `build` job carries no steps"
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == STEP_NAME]
    assert len(named) == 1, f"expected exactly one {STEP_NAME!r} step, got {len(named)}"
    step = named[0]
    assert step.get("run"), f"the {STEP_NAME!r} step carries no `run:` body"
    return step


def _body(step: dict) -> str:
    """The body with comment lines dropped — a read must be a read, not a mention of one."""
    body = "\n".join(
        ln for ln in str(step["run"]).splitlines() if not ln.strip().startswith("#")
    )
    assert body.strip(), "the Staple step body is nothing but comments"
    return body


_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script and this arm executes it; without a real "
        "bash the run measures the shell's absence, not the step. Gated to the platforms "
        "that can run one — the step itself is macOS-only."
    ),
)


def _run_step(tmp_path, scenario: str, with_pkg: bool = True):
    """Run the step body as the runner runs it, with `xcrun`, `spctl` and `sleep` stubbed.

    `sleep` is stubbed because the real schedule costs minutes on a permanent failure and
    the arms assert the schedule anyway; the body itself is untouched.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (("xcrun", _XCRUN_STUB), ("spctl", _SPCTL_STUB), ("sleep", _SLEEP_STUB)):
        stub = bindir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)

    artifacts = tmp_path / "dist" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if with_pkg:
        (artifacts / PKG_NAME).write_bytes(b"pkg")

    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")

    script = tmp_path / "step.sh"
    script.write_text(_body(_staple_step()), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_CALLS"] = str(calls)
    env["STAPLE_SCENARIO"] = scenario
    env["LOST_CONNECTION"] = LOST_CONNECTION
    env["VALIDATE_FAILED"] = VALIDATE_FAILED

    result = subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result.calls = calls.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    return result


def _count(calls: list[str], subcommand: str) -> int:
    return sum(1 for ln in calls if ln.split()[1:2] == [subcommand])


@_posix_shell_only
def test_a_transient_ticket_service_loss_does_not_lose_the_release(tmp_path) -> None:
    """The measured failure, replayed: the second attempt finds the ticket.

    Without this arm the whole change is unmeasured; without the permanent-failure arm
    below it would be a relaxation rather than a retry.
    """
    result = _run_step(tmp_path, "transient")
    assert result.returncode == 0, (
        "a transient loss of Apple's ticket service still reddened the step — the macOS "
        "leg fails, `release: needs: build` skips the release job, and the tag is left bare."
        f"\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert _count(result.calls, "validate") == 2, (
        "the step did not retry the failed `stapler validate` exactly once — the calls were "
        f"{result.calls!r}"
    )
    assert "retrying in" in result.stdout + result.stderr, (
        "the retry left no trace in the log, so a reader of a green run cannot tell a "
        "first-attempt success from a recovered one.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@_posix_shell_only
def test_a_permanent_loss_still_fails_the_step_after_a_bounded_number_of_attempts(
    tmp_path,
) -> None:
    """The other direction, and the one that keeps the fix from being a silent pass.

    A retry that ended green on a ticket it never fetched would publish an unstapled pkg.
    The bound is asserted too: an unbounded loop would hang the release job on a real
    outage rather than failing it.
    """
    result = _run_step(tmp_path, "permanent")
    assert result.returncode != 0, (
        "the step PASSED with Apple's ticket service unreachable on every attempt — the "
        "release would publish an unstapled pkg"
    )
    assert _count(result.calls, "validate") == ATTEMPTS, (
        f"expected exactly {ATTEMPTS} validate attempts before giving up, saw "
        f"{_count(result.calls, 'validate')} — an unbounded retry hangs the release job on a "
        f"real outage. Calls: {result.calls!r}"
    )
    delays = [ln.split()[1] for ln in result.calls if ln.split()[:1] == ["sleep"]]
    assert len(delays) == ATTEMPTS - 1 and all(d.isdigit() and int(d) > 0 for d in delays), (
        "the retries do not wait between attempts (or do not wait at all) — an immediate "
        f"re-ask would fail on the same blip. Recorded delays: {delays!r}"
    )
    assert VALIDATE_FAILED in result.stdout + result.stderr, (
        "the step gave up without Apple's own error in the log — the reason the retry "
        f"exists is to make the remaining failure diagnosable.\nstdout={result.stdout!r}"
    )


@_posix_shell_only
def test_the_missing_pkg_guard_still_fails_the_step(tmp_path) -> None:
    """`spctl` is not reached on a missing pkg, and the retry must not have hidden that.

    The step's subject IS the pkg (issue #1913): with no pkg built, an exit 0 here would
    stamp a release that has no macOS product. The retry sits below this guard, so this
    arm also proves the guard was not moved inside it.
    """
    result = _run_step(tmp_path, "transient", with_pkg=False)
    assert result.returncode != 0, (
        "a release with no macOS pkg passed its own Staple step"
    )
    assert "no EMRG-*-macos-*.pkg found" in result.stdout, (
        f"the guard failed without naming the pkg it could not find.\nstdout={result.stdout!r}"
    )
    assert _count(result.calls, "staple") == 0 and _count(result.calls, "validate") == 0, (
        f"a retry ran with no pkg to staple: {result.calls!r}"
    )
