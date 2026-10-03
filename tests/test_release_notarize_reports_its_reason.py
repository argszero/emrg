"""The notarize step must say *why* it failed, not just that it did.

Measured on tag `v0.3.8`, run `36956685533` (2026-10-02), reproduced on a re-run of the
same job: the **Notarize pkg** step of `build-release.yml` reported "Process completed
with exit code 1" **three seconds** after it started, with nothing else in the log. The
pkg was fine (`productbuild` wrote it, `productsign` signed it, `pkgutil
--check-signature` confirmed it), the other three platforms built green, and because
macOS failed the `release` job was **skipped** — nothing was published at all.

The step is *for* surfacing the reason: its comment records that an `Invalid` verdict
still exits 0, which is why it parses `status` and fetches Apple's rejection log. But the
call sits in a command substitution in an assignment —

    NOTARY_OUT="$(xcrun notarytool submit "$PKG" ... 2>&1)"

— and the job's shell is `bash --noprofile --norc -eo pipefail`. Under `-e` the assignment
inherits the command's non-zero status and the script aborts **before** the `echo` that
follows it, so the `2>&1` capture is discarded unprinted. A refused submission and a
missing `notarytool` produce the same log.

A three-second failure is the *submission* being refused — credentials, argument, or
Apple's side — never a verdict, which takes minutes, exits 0 and reports `status=Invalid`.
The two must be distinguishable from the log, and the step must still fail either way:
this pins a diagnostic, and the arms below include the one that would catch it being
turned into a relaxation.

The executed arms mirror `tests/test_release_end_state.py`: the body is read from the
**parsed** step and run as the runner runs it, with `xcrun` stubbed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.tool_preflight import starts

REPO = Path(__file__).resolve().parent.parent

WORKFLOW = ".github/workflows/build-release.yml"

# The runner's default flags for `shell: bash` — `-e` is the defect's mechanism, so the
# arms must run under them and not under a friendlier interpreter default. The program
# itself is resolved in `_run_step`, never spelled here.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

PKG_NAME = "EMRG-0.3.8-macos-arm64.pkg"

# What Apple says when it refuses a submission. Any text will do; what matters is that it
# reaches the log, which is exactly what the step used to discard.
APPLE_REFUSAL = "Error: HTTP status code: 401. Invalid credentials."

_XCRUN_STUB = '''#!/usr/bin/env python3
"""Stand in for xcrun, and answer notarytool the way the scenario says."""
import json, os, sys

argv = sys.argv[1:]
scenario = os.environ["NOTARY_SCENARIO"]

if argv[:1] != ["notarytool"]:
    sys.exit(f"stub only speaks notarytool, got {argv!r}")
sub = argv[1] if len(argv) > 1 else ""

if sub == "submit":
    if scenario == "refused":
        # A refused submission: Apple's reply on stderr, non-zero exit. This is the
        # measured shape, and the message is what the step used to throw away.
        sys.stderr.write(os.environ["APPLE_REFUSAL"] + "\\n")
        sys.exit(1)
    if scenario == "invalid":
        json.dump({"id": "sub-1", "message": "see log", "status": "Invalid"}, sys.stdout)
        sys.stdout.write("\\n")
        sys.exit(0)
    json.dump({"id": "sub-1", "message": "Successfully uploaded", "status": "Accepted"}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)

if sub == "log":
    sys.stdout.write(os.environ.get("APPLE_REJECTION", "no rejection detail") + "\\n")
    sys.exit(0)

sys.exit(f"stub does not know notarytool {sub!r}")
'''


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} is missing — the guard cannot measure what it guards"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty — a vacuous read must not read as a pass"
    return text


def _notarize_step() -> dict:
    """The step, found by name in the parsed job — never by a text search.

    The file carries several steps that submit nothing, and `find dist/artifacts ...`
    occurs in more than one of them (the sign, the staple and the two verification steps
    all locate the pkg), so a document-level match cannot say which step was read.
    """
    jobs = yaml.safe_load(_read(WORKFLOW)).get("jobs")
    assert isinstance(jobs, dict) and "build" in jobs, "build-release.yml has no `build` job"
    steps = jobs["build"].get("steps")
    assert isinstance(steps, list) and steps, "the `build` job carries no steps"
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == "Notarize pkg (macOS only)"]
    assert len(named) == 1, f"expected exactly one Notarize step, got {len(named)}"
    step = named[0]
    assert step.get("run"), "the Notarize step carries no `run:` body"
    return step


def _body(step: dict) -> str:
    """The body with comment lines dropped — a read must be a read, not a mention of one."""
    body = "\n".join(
        ln for ln in str(step["run"]).splitlines() if not ln.strip().startswith("#")
    )
    assert body.strip(), "the Notarize step body is nothing but comments"
    return body


def test_the_submit_call_recaptures_the_exit_code_instead_of_aborting_silently() -> None:
    """The whole defect is `VAR="$(cmd)"` under `-e` — pinned on the statement, not a word.

    The arm below is what decides; this is the half that fails fast and names the line.
    A comment saying "we print the output" would satisfy a substring pin, so the pin is on
    the assignment's own shape: the capture is followed by an `|| NOTARY_RC=$?` recovery.
    """
    body = _body(_notarize_step())
    lines = body.splitlines()
    starts = [i for i, ln in enumerate(lines) if "NOTARY_OUT=\"$(xcrun notarytool submit" in ln]
    assert len(starts) == 1, (
        f"expected exactly one `NOTARY_OUT=\"$(xcrun notarytool submit ...)` assignment, "
        f"found {len(starts)} — the guard cannot name what it read"
    )
    start = starts[0]
    # The statement ends where the command substitution closes — anchored on `2>&1)"`,
    # the substitution's own close, so it survives the call being re-wrapped across a
    # different number of lines and does not depend on where the recovery clause sits.
    ends = [i for i in range(start, len(lines)) if '2>&1)"' in lines[i]]
    assert ends, 'the submit call never closes its substitution with `2>&1)"` — cannot slice it'
    statement = "\n".join(lines[start : ends[0] + 1])
    assert "notarytool submit" in statement, f"the slice is not the submit call: {statement!r}"
    assert "NOTARY_RC" in statement, (
        "the submit call's exit code is not captured — under the job's `-e` a refused "
        "submission aborts the script before the `echo` on the next line, so Apple's own "
        "reply is discarded and a refused submission reads exactly like a missing "
        f"notarytool (measured on v0.3.8, run 36956685533). Statement: {statement!r}"
    )
    assert "echo \"$NOTARY_OUT\"" in body, (
        "the captured output is never printed — capturing it is only useful if it reaches "
        "the log"
    )


def test_no_variable_reference_touches_a_non_ascii_byte() -> None:
    """The second measured cause: `$SUB_ID）` drops the value in a UTF-8 locale.

    Bash scans the longest identifier it can, and under a UTF-8 locale the first byte of the
    fullwidth `）` (U+FF09) qualifies as one — so `$SUB_ID）` reads the variable named
    `SUB_ID\\xef`, which is unset. Measured on this host, `SUB_ID=sub-1` then
    `echo "[$SUB_ID）]"`:

        LC_ALL=en_US.UTF-8  ->  []          the value is GONE, tail renders as stray bytes
        LC_ALL=C            ->  [sub-1）]    correct

    So it did **not** cause the v0.3.8 failure — the runner sets no locale, and the arms
    below therefore cannot pin it: an unbraced reference passes them under `C`. This is a
    text-level pin on the rule, which is the only locale-independent reading available:
    a variable reference must not be immediately followed by a non-ASCII byte.
    """
    body = _body(_notarize_step())
    offenders = _UNBRACED_BEFORE_NON_ASCII.findall(body)
    assert not offenders, (
        f"{offenders} reference a variable immediately followed by a non-ASCII byte — in a "
        "UTF-8 locale the leading byte joins the name and the value is lost. Brace it "
        "(`${VAR}`), which is what this step now does for the submission id."
    )


# `$VAR` (not `${VAR}`) whose very next byte is >= 0x80. Deliberately the general rule
# rather than "must be braced", so any wording that separates the reference from the byte
# passes.
_UNBRACED_BEFORE_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[\x80-\U0010ffff])")


# The arms that follow execute the step body, so they need a POSIX shell — the same gate
# `tests/test_release_end_state.py` puts on its executed arm. Written once and applied
# three times, because a reason copied to three sites is a reason free to drift.
#
# Measured, and the reason this gate exists (CI, 2026-10-02, the Windows leg of run
# `36959438654`): `bash` there resolves to the WSL launcher — `C:\Windows\System32\bash.exe`
# precedes Git's bash on `PATH` — which answers "Windows Subsystem for Linux has no installed
# distributions." and exits 1. All three arms therefore failed while measuring WSL's absence
# rather than the step, and the PR went red on a change that is not wrong. The two pins above
# are text reads over the parsed step, need no shell, and did run green on that leg.
_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script and this arm executes it; without a real "
        "bash the run measures the shell's absence, not the step. Gated to the platforms "
        "that can run one — the step itself is macOS-only."
    ),
)


def _run_step(tmp_path, scenario: str, rejection: str = "no rejection detail"):
    """Run the step body as the runner runs it, with `xcrun` stubbed.

    The shell is resolved rather than spelled `bash`: on the Windows runner that name means
    the WSL launcher, and a run that measures its absence says nothing about the step.
    """
    shell = shutil.which("bash")
    if not starts(shell):
        pytest.skip("no POSIX shell that starts is available for the ground-truth run")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "xcrun"
    stub.write_text(_XCRUN_STUB, encoding="utf-8")
    stub.chmod(0o755)

    (tmp_path / "dist" / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dist" / "artifacts" / PKG_NAME).write_bytes(b"pkg")

    script = tmp_path / "step.sh"
    script.write_text(_body(_notarize_step()), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["NOTARY_SCENARIO"] = scenario
    env["APPLE_REFUSAL"] = APPLE_REFUSAL
    env["APPLE_REJECTION"] = rejection
    env["APPLE_ID"] = "dev@example.invalid"
    env["MACOS_NOTARY_APP_PASSWORD"] = "app-specific-password"
    env["MACOS_NOTARY_TEAM_ID"] = "TEAMID1234"

    return subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@_posix_shell_only
def test_a_refused_submission_prints_apples_reply_and_still_fails(tmp_path) -> None:
    """The measured failure: a 3-second non-zero exit that used to say nothing.

    Both halves are asserted together, because either alone passes trivially: printing the
    reply while exiting 0 would publish an unnotarized pkg, and failing while printing
    nothing is the defect.
    """
    result = _run_step(tmp_path, "refused")
    assert APPLE_REFUSAL in result.stdout, (
        "a refused submission printed nothing about why — Apple's own reply was captured by "
        f"`2>&1` and then discarded by the abort.\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert result.returncode != 0, (
        "the step PASSED on a refused submission — the release would publish an "
        "unnotarized pkg"
    )


@_posix_shell_only
def test_an_accepted_submission_keeps_the_step_green(tmp_path) -> None:
    """The other direction: the fix must not turn the step red on the happy path.

    Without this arm, a body that always exited non-zero — or one that never reached the
    success line — would satisfy the test above.
    """
    result = _run_step(tmp_path, "accepted")
    assert result.returncode == 0, (
        f"an Accepted submission did not pass the step.\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "Accepted" in result.stdout


@_posix_shell_only
def test_a_rejected_verdict_still_fetches_the_log_and_fails(tmp_path) -> None:
    """The pre-existing path, kept honest by the same execution.

    A verdict failure exits 0 from `notarytool` (that is why the step parses `status`), so
    this arm proves the exit-code capture did not replace the status check with an
    exit-code check that cannot see it.
    """
    marker = "the bundle is not signed with a valid Developer ID"
    result = _run_step(tmp_path, "invalid", rejection=marker)
    assert result.returncode != 0, "an Invalid verdict passed the step"
    assert marker in result.stdout, (
        "an Invalid verdict did not fetch Apple's rejection log — the reason the status "
        f"is parsed at all.\nstdout={result.stdout!r}"
    )
