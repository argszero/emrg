"""`scripts/check-notary-credentials.py` — the host's half of the notarize diagnosis.

Measured on tag `v0.3.8`, run `36956685533` (2026-10-02): the **Notarize pkg** step died
three seconds in, the `release` job was skipped, and **nothing was published** while the
other three platforms built green. PR #1812 taught the step to print Apple's own refusal —
so the cause is visible now, but only in CI, only after a tag is pushed. A host can ask the
same question at home for nothing, which is the case `evolution_prompt.md` §4 names when it
requires a host-side counterpart for a check that lives in CI.

The pins below are of the three kinds that file's siblings use, because each is blind where
the others see:

* the **verdict in both directions, executed** — a refused credential must be `1` *and*
  say why, a working one must be `0`, and neither may be reachable by the other;
* the **lookalikes** — a 5xx, a DNS failure, an exit 0 that is not the asked-for JSON, and a
  host with no `xcrun` at all must each be **`2`, never `0`**. This is the load-bearing half:
  "exited non-zero" cannot tell a refused credential from a missing `notarytool`, which is
  exactly the conflation that made the v0.3.8 log unreadable. A preflight that passed
  because it could not measure would hand the host the same false confidence it exists to
  remove;
* the **premise** — the script must send the three variables the *release* sends, read under
  the secret names `build-release.yml` uses, or it would measure a different credential set
  than the one the build will spend.

The executed arms need a POSIX host (they run a stand-in `xcrun` written with a shebang),
so they carry the same gate `tests/test_release_notarize_reports_its_reason.py` puts on its
executed arms; the premise pin is a text read and runs everywhere. Nothing here reaches
Apple or reads a real credential.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-notary-credentials.py"
WORKFLOW = ".github/workflows/build-release.yml"

APPLE_ID_VAR = "APPLE_ID"
PASSWORD_VAR = "MACOS_NOTARY_APP_PASSWORD"
TEAM_ID_VAR = "MACOS_NOTARY_TEAM_ID"

PASSWORD = "app-specific-password"

# Apple's refusal, verbatim from the measurement in the script's docstring and from the
# submit step's own error text. What matters is that it reaches the host's terminal.
APPLE_REFUSAL = "Error: HTTP status code: 401. Invalid credentials."

_XCRUN_STUB = '''#!/usr/bin/env python3
"""Stand in for xcrun and answer `notarytool history` the way the scenario says."""
import json, os, sys

argv = sys.argv[1:]
scenario = os.environ["NOTARY_SCENARIO"]

if argv[:2] != ["notarytool", "history"]:
    sys.exit(f"stub only speaks `notarytool history`, got {argv!r}")

if scenario == "accepted":
    json.dump({"history": [{"id": "sub-1", "status": "Accepted"}]}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)
if scenario == "refused":
    sys.stderr.write("Error: HTTP status code: 401. Invalid credentials. "
                     "Use an app-specific password.\\n")
    sys.exit(1)
if scenario == "revoked":
    # Apple answers 403 for a revoked key. Still an authentication verdict, so still a
    # credential fault -- and still not something an exit code alone can see.
    sys.stderr.write("Error: HTTP status code: 403. Forbidden.\\n")
    sys.exit(1)
if scenario == "server_error":
    sys.stderr.write("Error: HTTP status code: 500. Internal Server Error\\n")
    sys.exit(1)
if scenario == "transport":
    sys.stderr.write("Error: unable to connect to Apple (DNS lookup failed)\\n")
    sys.exit(1)
if scenario == "not_json":
    # A wrapper or a wrong `xcrun` answering successfully with something else: rc 0 is not
    # a credentials verdict unless the asked-for answer came back.
    sys.stdout.write("usage: notarytool history ...\\n")
    sys.exit(0)
if scenario == "leaky":
    # What a refused credential looks like when the wrapper echoes the argument back. The
    # preflight must relay Apple's reply without relaying the password.
    sys.stderr.write("Error: HTTP status code: 401. Invalid credentials.\\n")
    sys.stderr.write("  (echoed argument: " + os.environ["MACOS_NOTARY_APP_PASSWORD"] + ")\\n")
    sys.exit(1)
sys.exit(f"stub does not know scenario {scenario!r}")
'''

# The stand-in is a shebang script, so these arms need a host that can execute one -- the
# same gate (and the same reason) as the notarize test's executed arms: without a real
# POSIX host the run measures the stub's absence, not the preflight.
_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the arms run a shebang stand-in for `xcrun`; on Windows that measures the stub's "
        "absence rather than the preflight. The script itself is a macOS-only concern."
    ),
)


def _run(
    tmp_path: Path,
    scenario: str,
    *,
    credentials: bool = True,
    xcrun: str | None = None,
    empty_path: bool = False,
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "xcrun"
    stub.write_text(_XCRUN_STUB, encoding="utf-8")
    stub.chmod(0o755)

    env = dict(os.environ)
    env["NOTARY_SCENARIO"] = scenario
    for name in (APPLE_ID_VAR, PASSWORD_VAR, TEAM_ID_VAR):
        env.pop(name, None)
    if credentials:
        env[APPLE_ID_VAR] = "dev@example.invalid"
        env[PASSWORD_VAR] = PASSWORD
        env[TEAM_ID_VAR] = "TEAMID1234"

    argv = [sys.executable, str(SCRIPT)]
    if xcrun is not None:
        argv += ["--xcrun", xcrun]
    elif empty_path:
        # No `--xcrun`: the script resolves it on PATH, and on a tree with no xcrun
        # anywhere that resolution must say "could not measure", not guess a pass.
        empty = tmp_path / "empty-bin"
        empty.mkdir(exist_ok=True)
        env["PATH"] = str(empty)
    else:
        argv += ["--xcrun", str(stub)]

    return subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ── The verdict, in both directions ───────────────────────────────────────────


@_posix_only
def test_a_refused_credential_is_a_fault_that_says_why(tmp_path: Path) -> None:
    """The measured v0.3.8 shape: Apple said no, and the host must see it before tagging."""
    result = _run(tmp_path, "refused")
    assert result.returncode == 1, (
        f"a refused credential was not a fault (exit {result.returncode}).\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert APPLE_REFUSAL in result.stdout, (
        "the refusal was reported without Apple's own reply — the reason the host runs "
        f"this at all.\nstdout={result.stdout!r}"
    )
    assert "app-specific password" in result.stdout, (
        "the usual causes were not named; a fault line a host cannot act on is a dead end"
    )


@_posix_only
def test_a_revoked_credential_is_also_a_fault(tmp_path: Path) -> None:
    """403 is the other shape Apple refuses with; a matcher that only knows 401 misses it."""
    result = _run(tmp_path, "revoked")
    assert result.returncode == 1, (
        f"a 403 refusal was not read as a credential fault (exit {result.returncode}).\n"
        f"stdout={result.stdout!r}"
    )


@_posix_only
def test_working_credentials_pass(tmp_path: Path) -> None:
    """The other direction: without this arm, a check that always failed would satisfy the
    refused arms, and every release would be blocked by its own preflight."""
    result = _run(tmp_path, "accepted")
    assert result.returncode == 0, (
        f"credentials Apple accepted did not pass (exit {result.returncode}).\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "history" in result.stdout or "OK" in result.stdout


# ── The lookalikes: a could-not-measure is never a pass ───────────────────────


@pytest.mark.parametrize(
    "scenario, why",
    [
        ("server_error", "a 5xx is Apple failing, not Apple refusing this credential"),
        ("transport", "a DNS failure never reached Apple at all"),
        ("not_json", "an exit 0 that is not the asked-for JSON is not a credentials verdict"),
    ],
)
@_posix_only
def test_an_answer_that_is_not_a_credentials_verdict_is_unmeasured(
    tmp_path: Path, scenario: str, why: str
) -> None:
    """Each of these is a *failure to measure*, and the contract for that is exit 2.

    Collapsing any of them to `0` sends the host to CI with the same false confidence the
    v0.3.8 log created; collapsing them to `1` blames credentials for a network outage.
    """
    result = _run(tmp_path, scenario)
    assert result.returncode == 2, (
        f"{scenario} answered exit {result.returncode}, expected 2 — {why}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "not measurable" in result.stdout


@_posix_only
def test_missing_credentials_are_unmeasured_rather_than_passed(tmp_path: Path) -> None:
    """A host who has not exported the three variables has measured nothing.

    The tempting shortcut — treat "no credentials configured" as "nothing to check, fine" —
    is the defect: the release *would* run the notarize step if the secrets are configured
    there, and this host cannot know.
    """
    result = _run(tmp_path, "accepted", credentials=False)
    assert result.returncode == 2, (
        f"absent credentials passed as exit {result.returncode} — nothing was measured.\n"
        f"stdout={result.stdout!r}"
    )
    assert PASSWORD_VAR in result.stdout, "the missing variable was not named"


@_posix_only
def test_no_xcrun_on_this_host_is_unmeasured(tmp_path: Path) -> None:
    """`notarytool` ships with the Xcode command line tools; without it the release step
    cannot run either, and that is a fact about the host, not about the credentials."""
    result = _run(tmp_path, "accepted", xcrun=str(tmp_path / "nope" / "xcrun"))
    assert result.returncode == 2, (
        f"an unrunnable xcrun answered {result.returncode}, expected 2.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@_posix_only
def test_a_path_without_xcrun_is_unmeasured_rather_than_passed(tmp_path: Path) -> None:
    """The resolution branch, not just the execution one: nothing on PATH to run."""
    result = _run(tmp_path, "accepted", empty_path=True)
    assert result.returncode == 2, (
        f"a host with no xcrun on PATH answered {result.returncode}, expected 2 — a "
        f"preflight that cannot run the exchange has measured nothing.\n"
        f"stdout={result.stdout!r}"
    )
    assert "no `xcrun` on PATH" in result.stdout


# ── The reading must not carry the secret ─────────────────────────────────────


@_posix_only
def test_the_password_never_reaches_the_terminal(tmp_path: Path) -> None:
    """Apple's reply is relayed; the argument that produced it is not.

    A wrapper (or Apple) can echo an argument back, and the whole reading is printed to a
    terminal a host may be sharing or logging.
    """
    result = _run(tmp_path, "leaky")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, f"the leaky refusal was not read as a fault: {combined!r}"
    assert PASSWORD not in combined, (
        f"the app-specific password reached the output — a reading is not worth a leaked "
        f"credential.\noutput={combined!r}"
    )
    assert APPLE_REFUSAL in combined


# ── The premise: it asks with the release's own three variables ───────────────


def test_the_preflight_sends_the_variables_the_release_sends() -> None:
    """The script's variable names against the ones the notarize step actually feeds.

    A preflight reading a *different* set — a `NOTARY_PASSWORD` of its own, say — would
    answer about credentials the release never uses, which is worse than no preflight
    because it reads as one.
    """
    workflow = (REPO / WORKFLOW).read_text(encoding="utf-8")
    assert "--apple-id" in workflow, "the notarize step is gone — this pin is stale"

    sent = {
        "--apple-id": set(re.findall(r'--apple-id\s+"?\$\{?(\w+)', workflow)),
        "--password": set(re.findall(r'--password\s+"?\$\{?(\w+)', workflow)),
        "--team-id": set(re.findall(r'--team-id\s+"?\$\{?(\w+)', workflow)),
    }
    for flag, names in sent.items():
        assert len(names) == 1, f"the workflow feeds {flag} from {names}, not one variable"

    script = SCRIPT.read_text(encoding="utf-8")
    declared = dict(re.findall(r'^([A-Z_]+_VAR)\s*=\s*"([^"]+)"', script, re.MULTILINE))
    assert len(declared) == 3, f"expected three variable constants, read {declared!r}"

    assert (
        set(declared.values())
        == {next(iter(n)) for n in sent.values()}
    ), (
        f"the preflight sends {sorted(declared.values())} but the release sends "
        f"{sorted(next(iter(n)) for n in sent.values())} — the host would be checking "
        f"credentials the build does not use"
    )
