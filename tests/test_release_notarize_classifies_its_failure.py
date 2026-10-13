"""The notarize step must name the cause it measured, and retry only what is transient.

Two shapes arrive as the same non-zero exit, and they have opposite remedies:

* **Apple answered and refused** — a credential verdict. Diagnosed with the preflight, and
  **never** retried: the answer will not change.
* **The exchange never completed** — a lost connection or a timeout. Apple never answered,
  so no credential was judged, and the shape is transient.

Until this change the step printed the first shape's sentence for both: *"the SUBMISSION was
refused"* plus the three credential causes, for a run whose only problem was the network.
Measured by driving the parsed step body with an `xcrun` stub that fails the way the release
actually failed on tag `v0.3.10` (run `38353839071`, where the *sibling* `Staple pkg` step
lost its connection to `api.apple-cloudkit.com` about six seconds in and exited 68).

These arms pin each direction, because either alone passes trivially: retrying a refusal
would be pointless traffic, and diagnosing a transport loss as a refusal sends the reader to
check credentials that were never refused. A fourth arm guards the retry's own hazard — a
connection lost *after* Apple returned a submission id must not re-send, or the release
double-submits.

The body is read from the **parsed** step and executed the way the runner runs it, with
`xcrun` and `sleep` stubbed — the shape `tests/test_release_notarize_reports_its_reason.py`
established. `sleep` is stubbed because the retry waits 20 s and 40 s, and nothing asserted
here is about wall-clock time.
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

STEP_NAME = "Notarize pkg (macOS only)"

# The runner's default flags for `shell: bash`; `-e` and `pipefail` are the conditions the
# step's own predicates are written against, so the arms must run under them.
RUNNER_SHELL = ["--noprofile", "--norc", "-eo", "pipefail"]

PKG_NAME = "EMRG-0.3.10-macos-arm64.pkg"

ATTEMPTS = 3

# The measured shapes, kept verbatim enough that a reader recognises the run they came from.
LOST_CONNECTION = (
    'error is Error Domain=NSURLErrorDomain Code=-1005 "The network connection was lost." '
    "... NSErrorFailingURLStringKey=https://api.apple-cloudkit.com/database/1/"
    "com.apple.gk.ticket-delivery/production/public/records/lookup"
)
APPLE_REFUSAL = "Error: HTTP status code: 401. Invalid credentials."

# The two sentences that must never appear in each other's place.
REFUSAL_CLAIM = "the SUBMISSION was refused"
TRANSPORT_CLAIM = "TRANSPORT failure"

# The third outcome's own sentence. It is not a refusal and not the transport-exhausted
# shape, and until this pin existed it was reported with the transport sentence — three of
# whose clauses are false on that path (one submit WAS taken, Apple DID answer, and the pkg
# WAS uploaded, contradicting the warning printed directly above).
UPLOAD_ID_CLAIM = "Apple ACCEPTED the upload"
TRANSPORT_ONLY_CLAIMS = ("Apple never answered", "nothing was uploaded")

_XCRUN_STUB = '''#!/usr/bin/env python3
"""Stand in for xcrun, and answer notarytool the way the scenario says."""
import json, os, sys

argv = sys.argv[1:]
calls = os.environ["STUB_CALLS"]
scenario = os.environ["NOTARY_SCENARIO"]


def record(line):
    with open(calls, "a", encoding="utf-8") as fh:
        fh.write(line + "\\n")


if argv[:1] != ["notarytool"]:
    sys.exit(f"stub only speaks notarytool, got {argv!r}")
record(" ".join(argv))
sub = argv[1] if len(argv) > 1 else ""

if sub == "log":
    sys.stdout.write("no rejection detail\\n")
    sys.exit(0)

if sub != "submit":
    sys.exit(f"stub does not know notarytool {sub!r}")

with open(calls, encoding="utf-8") as fh:
    submit_no = sum(1 for ln in fh if ln.split()[1:2] == ["submit"])


def accepted():
    json.dump({"id": "sub-1", "message": "Successfully uploaded", "status": "Accepted"}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)


if scenario == "refused":
    # Apple ANSWERED: a real credential refusal, with no submission id.
    sys.stderr.write(os.environ["APPLE_REFUSAL"] + "\\n")
    sys.exit(1)

if scenario == "transport_once":
    if submit_no > 1:
        accepted()
    sys.stderr.write(os.environ["LOST_CONNECTION"] + "\\n")
    sys.exit(68)

if scenario == "transport_always":
    sys.stderr.write(os.environ["LOST_CONNECTION"] + "\\n")
    sys.exit(68)

if scenario == "lost_after_id":
    # Apple accepted the upload and handed back an id; the WAIT then lost the connection.
    json.dump({"id": "sub-7", "message": "Successfully uploaded", "status": "In Progress"}, sys.stdout)
    sys.stdout.write("\\n")
    sys.stderr.write(os.environ["LOST_CONNECTION"] + "\\n")
    sys.exit(68)

sys.exit(f"stub does not know scenario {scenario!r}")
'''

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


def _notarize_step() -> dict:
    """The step, found by name in the parsed job — never by a text search.

    The sign and staple steps locate the pkg with the same `find`, so a document-level match
    cannot say which step was read.
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
    assert body.strip(), "the Notarize step body is nothing but comments"
    return body


_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the step body is a POSIX shell script and this arm executes it; without a real "
        "bash the run measures the shell's absence, not the step. Gated to the platforms "
        "that can run one — the step itself is macOS-only."
    ),
)


def _run_step(tmp_path, scenario: str):
    """Run the step body as the runner runs it, with `xcrun` and `sleep` stubbed."""
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (("xcrun", _XCRUN_STUB), ("sleep", _SLEEP_STUB)):
        stub = bindir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)

    (tmp_path / "dist" / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dist" / "artifacts" / PKG_NAME).write_bytes(b"pkg")

    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")

    script = tmp_path / "step.sh"
    script.write_text(_body(_notarize_step()), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_CALLS"] = str(calls)
    env["NOTARY_SCENARIO"] = scenario
    env["LOST_CONNECTION"] = LOST_CONNECTION
    env["APPLE_REFUSAL"] = APPLE_REFUSAL
    env["APPLE_ID"] = "dev@example.invalid"
    env["MACOS_NOTARY_APP_PASSWORD"] = "app-specific-password"
    env["MACOS_NOTARY_TEAM_ID"] = "TEAMID1234"

    result = subprocess.run(
        [shell, *RUNNER_SHELL, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    result.calls = calls.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    result.output = result.stdout + result.stderr  # type: ignore[attr-defined]
    return result


def _submits(result) -> int:
    return sum(1 for ln in result.calls if ln.split()[1:2] == ["submit"])


@_posix_shell_only
def test_a_lost_connection_is_retried_and_the_step_recovers(tmp_path) -> None:
    """The measured shape: attempt one loses the connection, attempt two finds Apple."""
    result = _run_step(tmp_path, "transport_once")
    assert result.returncode == 0, (
        "a transient loss of the connection to Apple's notary service reddened the step — "
        "the macOS leg fails, `release: needs: build` skips the release job, and the tag is "
        f"left bare.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert _submits(result) == 2, (
        f"expected exactly two submit attempts, saw {_submits(result)}: {result.calls!r}"
    )
    assert "retrying in" in result.output, (
        "the retry left no trace, so a reader of a green run cannot tell a first-attempt "
        f"success from a recovered one.\noutput={result.output!r}"
    )


@_posix_shell_only
def test_a_lost_connection_on_every_attempt_names_the_transport_not_the_credentials(
    tmp_path,
) -> None:
    """The defect itself: the sentence must be the one its cause earned.

    This is the arm that fails on the pre-change step — measured, by driving both trees'
    bodies with this same stub: the old body answered *"the SUBMISSION was refused"* and
    listed the credential causes.
    """
    result = _run_step(tmp_path, "transport_always")
    assert result.returncode != 0, (
        "the step PASSED with Apple never reached on any attempt — the release would "
        "publish an un-notarized pkg"
    )
    assert _submits(result) == ATTEMPTS, (
        f"expected exactly {ATTEMPTS} attempts before giving up, saw {_submits(result)} — an "
        f"unbounded retry hangs the release job on a real outage. Calls: {result.calls!r}"
    )
    assert TRANSPORT_CLAIM in result.output, (
        "a connection that never completed was not named as a transport failure — the "
        f"reader is left without the cause.\noutput={result.output!r}"
    )
    assert REFUSAL_CLAIM not in result.output, (
        "the step told the reader the SUBMISSION was refused when Apple never answered — "
        "the credentials were not judged, and this sends them to check the wrong thing "
        f"(issue #2199).\noutput={result.output!r}"
    )


@_posix_shell_only
def test_a_refused_credential_is_not_retried_and_keeps_its_own_sentence(tmp_path) -> None:
    """The other direction: a refusal is Apple's answer, so re-asking changes nothing."""
    result = _run_step(tmp_path, "refused")
    assert result.returncode != 0, "a refused submission passed the step"
    assert _submits(result) == 1, (
        f"a credential refusal was retried ({_submits(result)} attempts) — Apple answered, "
        f"and re-sending cannot change the answer. Calls: {result.calls!r}"
    )
    assert not [ln for ln in result.calls if ln.startswith("sleep")], (
        f"a credential refusal waited between attempts: {result.calls!r}"
    )
    assert REFUSAL_CLAIM in result.output, (
        f"a real refusal lost its own sentence.\noutput={result.output!r}"
    )
    assert TRANSPORT_CLAIM not in result.output, (
        "a credential refusal was reported as a transport failure — the two diagnoses must "
        f"not swap places either.\noutput={result.output!r}"
    )
    assert "scripts/check-notary-credentials.py" in result.output, (
        "the refusal sentence no longer names the host-side preflight, which another guard "
        f"pins (tests/test_release_notarize_reports_its_reason.py).\noutput={result.output!r}"
    )


@_posix_shell_only
def test_a_connection_lost_after_an_upload_id_is_not_re_sent(tmp_path) -> None:
    """The retry's own hazard: Apple may have accepted the upload before the wait dropped.

    A submission id in the output is what proves the upload landed, so re-sending there
    would submit the pkg twice. The arm pins that the id suppresses the retry — and that
    the suppression is reported with the sentence its own cause earned, because the
    transport sentence is false here in three of its clauses.
    """
    result = _run_step(tmp_path, "lost_after_id")
    assert _submits(result) == 1, (
        "the pkg was re-submitted after Apple had already returned a submission id — the "
        f"retry can double-submit.\nCalls: {result.calls!r}\noutput={result.output!r}"
    )
    assert "NOT re-sent" in result.output, (
        "the step suppressed the retry without saying so, so the log cannot explain why the "
        f"failure was not retried.\noutput={result.output!r}"
    )
    assert UPLOAD_ID_CLAIM in result.output, (
        "the outcome where Apple accepted an upload and its verdict was never read was "
        "reported with another outcome's sentence — measured on the head before this pin, "
        f"the transport sentence ran here.\noutput={result.output!r}"
    )
    assert TRANSPORT_CLAIM not in result.output, (
        "the upload-id outcome was told it was a TRANSPORT failure. On this path exactly one "
        "submit was taken (so 'every attempt was retried' is false), Apple answered with an "
        "id and 'Successfully uploaded' (so 'Apple never answered' is false), and the upload "
        f"was accepted (so 'nothing was uploaded' is false).\noutput={result.output!r}"
    )
    for clause in TRANSPORT_ONLY_CLAIMS:
        assert clause not in result.output, (
            f"the upload-id outcome printed {clause!r} — a claim about a run where the pkg "
            f"was not uploaded and Apple did answer.\noutput={result.output!r}"
        )
