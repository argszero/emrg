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
import shlex
import shutil
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
    env_file: str | None = None,
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
    if env_file is not None:
        # A path, not a file: whether it is readable is the scenario's own business, and
        # the arms that pass one deliberately pass paths that are not there.
        argv += ["--env-file", env_file]
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


# ── The `--env-file` half: an input that could not be read is never a verdict ──
#
# Measured 2026-10-07 (`cyc20261007-072231`) on the invocation `DEVELOPMENT.md` recommends
# - `--env-file ~/.emrg/notary.env`, the way to keep the secrets out of the shell history,
# with that file absent on this host:
#
#     Traceback (most recent call last): ... FileNotFoundError: '/Users/…/notary.env'
#     rc=1
#
# `1` is the code this tool's table gives **Apple's refusal**, whose documented remedy is
# an expired or revoked app-specific password. So the host troubleshooting the blocked
# release was sent credential-hunting over a mistyped path - the false diagnosis this
# preflight exists to remove, one code over. The arms below hold both halves: an input
# that cannot be read is `2` and says which file, while the refusal (above) stays `1`.
#
# The two *unreadable* arms are not `_posix_only`: that branch returns before `xcrun` is
# resolved, so nothing there executes the stand-in and both platforms measure the same
# reading. The control that a readable file reaches the call is **two** arms, split by what
# each one needs to execute:
#
#   * the file's values in the header `check()` prints before the probe runs - no stand-in,
#     so both platforms measure it;
#   * the exchange itself reached with those values - `_posix_only`, like every other arm
#     here that runs the stub.
#
# That split is not tidiness. Measured on run `37548845041` (this PR's first head, job
# `112559256441`): the single `rc == 0` control **failed on the Windows leg** (`assert 2 ==
# 0`) because the shebang stand-in cannot execute there, while its own failure output shows
# all three values having arrived from the file (`apple-id: dev@example.invalid`, `team-id:
# TEAMID1234`, `password: set`) and `not measurable` for the check that then could not run.
# So the reading the control exists for was there on Windows all along, and only the exit
# code was measuring the host - which is the same "a check reading the wrong half" defect
# this file pins elsewhere, found by CI rather than by the arm.


def test_an_env_file_that_is_not_there_is_unmeasured_never_a_refusal(tmp_path: Path) -> None:
    """The measured defect, in the shape a host hits: a path with no file behind it.

    Run **both ways round**, because the environment is the other place the three variables
    can come from and only one of the two shapes makes the fallback visible: with them unset
    a branch that printed the reason and then carried on would still land on exit 2 ("nothing
    to send"), so the arm that means something is the one where the environment is *set* and
    a verdict must still not be reached. That arm is not decoration - it was measured: this
    test passed a mutation that deleted the `return 2` while the credentials were unset, and
    a host with them exported (the documented first invocation) would have been handed a
    credential verdict about a set they had just said to read from a file.
    """
    missing = tmp_path / "notary.env"

    for credentials in (False, True):
        result = _run(tmp_path, "accepted", credentials=credentials, env_file=str(missing))
        where = f"credentials in the environment: {credentials}"

        assert result.returncode == 2, (
            f"an unreadable env file answered {result.returncode} ({where}) - `1` is this "
            f"table's code for Apple refusing the credentials, and nothing was ever sent to "
            f"Apple.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "Traceback" not in result.stderr, (
            "the input error reached the host as a traceback instead of a reading — the "
            f"failure that sent the diagnosis to the wrong cause ({where}).\n"
            f"stderr={result.stderr!r}"
        )
        assert "REFUSED" not in result.stdout and "OK:" not in result.stdout, (
            f"a verdict was printed for a file that was never read ({where}) - the reading "
            f"fell back to the environment.\nstdout={result.stdout!r}"
        )
        assert "not measurable" in result.stderr and str(missing) in result.stderr, (
            "the unmeasurable answer has to carry the reason and name the file, or the host "
            f"cannot act on it ({where}).\nstderr={result.stderr!r}"
        )
        assert "environment instead" in result.stderr, (
            "the fallback has to be refused out loud: a preflight that quietly read the "
            "environment instead would measure a credential set the caller did not name, "
            f"which reads as a verdict ({where}).\nstderr={result.stderr!r}"
        )


def test_an_env_file_that_is_a_directory_is_unmeasured_too(tmp_path: Path) -> None:
    """The second shape of "cannot be read": a path that exists and is not a file."""
    target = tmp_path / "notary.env"
    target.mkdir()
    result = _run(tmp_path, "accepted", credentials=False, env_file=str(target))

    assert result.returncode == 2, (
        f"a directory handed to `--env-file` answered {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert str(target) in result.stderr
    assert "Traceback" not in result.stderr


def test_the_env_file_is_read_when_it_is_really_there(tmp_path: Path) -> None:
    """The control: the arms above must not be satisfiable by never reading the file.

    Read off the **header**, which `check()` prints before `probe()` resolves `xcrun`, so
    this is a reading on the file and nothing else - and therefore runs on both platforms.
    The three variables are absent from the environment, so a header naming ``dev@example.invalid``
    and ``TEAMID1234`` can only have got them from the file; a branch that refused every
    `--env-file` would print ``(unset)`` beside each name and fail here.

    The exit code is deliberately **not** asserted: that half of the control needs the
    stand-in to run, which is the Windows leg's business (measured on run `37548845041` -
    `assert 2 == 0` while this very header was correct), so it lives in the arm below, with
    the gate every stand-in-running arm in this file carries.
    """
    env_file = tmp_path / "notary.env"
    env_file.write_text(
        f"# the host's own file, not a shell script\n"
        f"{APPLE_ID_VAR}=dev@example.invalid\n"
        f"{PASSWORD_VAR}={PASSWORD}\n"
        f"{TEAM_ID_VAR}=TEAMID1234\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, "accepted", credentials=False, env_file=str(env_file))

    for line in (
        "apple-id: dev@example.invalid",
        "team-id:  TEAMID1234",
        "password: set",
    ):
        assert line in result.stdout, (
            f"the readable env file's values did not reach the reading (`{line}` is absent, "
            f"so the call was made with something other than what the file names).\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    assert "password: (unset)" not in result.stdout, (
        "the file's password did not reach the reading.\n" f"stdout={result.stdout!r}"
    )
    assert PASSWORD not in result.stdout + result.stderr


@_posix_only
def test_the_env_file_carries_its_values_into_the_exchange(tmp_path: Path) -> None:
    """The executed half of that control: the file's values are the call that was made.

    Split from the arm above rather than deleted, because the header alone cannot see a
    branch that reads the file for the report and sends the environment to `notarytool`:
    the header would be right and the credential set would be one the caller did not name,
    which is exactly the false confidence this preflight exists to remove.
    """
    env_file = tmp_path / "notary.env"
    env_file.write_text(
        f"{APPLE_ID_VAR}=dev@example.invalid\n"
        f"{PASSWORD_VAR}={PASSWORD}\n"
        f"{TEAM_ID_VAR}=TEAMID1234\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, "accepted", credentials=False, env_file=str(env_file))

    assert result.returncode == 0, (
        "a readable env file did not carry its values into the exchange - the stand-in "
        f"accepts exactly the three the file names.\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert PASSWORD not in result.stdout + result.stderr


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


# ── The host has to be able to find it ────────────────────────────────────────


def test_development_md_documents_the_preflight_and_its_contract() -> None:
    """A host-side counterpart the host cannot reach is not one, and this is the half
    that decays.

    The script asks the question CI can only ask *after* a tag is pushed, so it exists for
    a host whose release round just went red — and they look in the docs, not in the source
    tree. Measured 2026-10-03 (`cyc20261003-090905`): the file was named nowhere outside its
    own docstring, its test, and the evolution memory, which is a private record the host
    does not read. Exit codes are pinned with the command because the contract is the part
    a bare path cannot carry: `2` means no verdict was reached, and a host who reads that
    as "fine" pays the wasted build round the preflight exists to prevent.
    """
    text = (REPO / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "scripts/check-notary-credentials.py" in text, (
        "DEVELOPMENT.md does not name scripts/check-notary-credentials.py — the host "
        "troubleshooting a refused notarization has no documented way to reach the "
        "preflight, so it is a host-side counterpart only in name"
    )
    assert "never a pass" in text, (
        "DEVELOPMENT.md documents the command without its contract — exit 2 means no "
        "verdict was reached, and folding that into a pass is the conflation this file "
        "exists to keep out"
    )
    assert "MACOS_NOTARY_APP_PASSWORD" in text, (
        "the documented invocation must name the variables the release uses, so a host can "
        "run it without reconstructing the secret names from the workflow"
    )


# ── The documented command has to RUN ─────────────────────────────────────────
#
# Measured 2026-10-03 (`cyc20261003-133828`, and first reported by `pm25coder`'s review of
# `cyc20261003-115810` on PR #1828): the block this file's pin above blesses shipped with
# **two** backslash bytes at the end of each continued line. Inside a ```bash fence that is
# an escaped backslash rather than a line continuation, so the documented command ran `\` as
# a command twice and reached the preflight with **none** of the three variables set -
# producing the exit-`2` reading ("the exchange did not complete, so no verdict was reached
# - never a pass") that the paragraph directly beneath the block exists to prevent.
#
# The pin above could not see it: every word it asserts is present in a block nobody can
# run. Naming a command is not shipping one, which is what these two arms restore - one on
# the text, one on the executed shell.

#: Written as `chr(92)` so this file carries **no backslash literal at all**. The defect was
#: a doubled one, and a pin on a doubled literal is a pin a future edit can get wrong the
#: same way; `_BS * 2` cannot.
_BS = chr(92)

#: Every ```bash fenced block in a document, in order.
_FENCE = re.compile(r"```bash\n(.*?)```", re.S)

_PREFLIGHT_STUB = '''#!/usr/bin/env python3
"""Stand in for the preflight, and report which variables actually reached it."""
import os

for name in ("APPLE_ID", "MACOS_NOTARY_APP_PASSWORD", "MACOS_NOTARY_TEAM_ID"):
    print(f"{name}={os.environ.get(name, '<unset>')}")
'''


def _preflight_block() -> str:
    """The one fenced block in `DEVELOPMENT.md` that runs the preflight.

    Found by content rather than by position: the document carries several `bash` blocks,
    and a pin that read whichever came first would be about a different command the moment
    one is inserted above it.
    """
    text = (REPO / "DEVELOPMENT.md").read_text(encoding="utf-8")
    runs = [block for block in _FENCE.findall(text) if SCRIPT.name in block]
    assert len(runs) == 1, (
        f"expected exactly one fenced block naming {SCRIPT.name}, found {len(runs)} - the "
        f"pin cannot say which command it read"
    )
    return runs[0]


def test_the_documented_command_continues_each_line_with_one_backslash() -> None:
    """The text half: exactly one backslash, on a line that continues something.

    Two bytes make the shell escape a backslash and stop continuing, so the rest of the
    command becomes separate lines that never reach the preflight as its arguments.
    """
    lines = _preflight_block().splitlines()
    doubled = [line for line in lines if line.rstrip().endswith(_BS * 2)]
    assert not doubled, (
        "these lines end in TWO backslashes, which inside a bash fence is an escaped "
        "backslash and not a line continuation - the documented command does not run as "
        "written and the preflight is invoked without its variables, which answers the "
        f"exit-2 'no verdict was reached' reading the block exists to prevent: {doubled}"
    )
    continued = [line for line in lines if line.rstrip().endswith(_BS)]
    assert len(continued) >= 1, (
        "no line of the documented command continues onto the next - either the block "
        "stopped being a multi-line command, or this pin is reading the wrong thing"
    )


def _shell_path(path, *, nt: bool | None = None) -> str:
    """A path the shell reads back as one argument, on either platform.

    Measured on the Windows leg of run `37100806753` (`cyc20261003-155509`): interpolating
    `str(sys.executable)` **unquoted** into the documented block produced

        `documented-block.sh: line 1: D:aemrgemrg.venvScriptspython.exe: command not found`

    — exit 127 on a block that was correct. The backslashes that separate a Windows path are
    *escape characters* to bash, so they are consumed and the path collapses. The same class as
    the doubled backslash this file's pins are about, one layer out: a backslash where a shell
    reads it. Forward slashes plus one level of quoting is what a shell needs, and it costs a
    POSIX path nothing.

    `nt` is a parameter rather than a read of `os.name` so **both branches are testable from
    either platform** - a conversion that only runs on the CI leg that found the bug is exactly
    the code that regresses unnoticed.
    """
    text = str(path)
    if (os.name == "nt") if nt is None else nt:
        text = text.replace("\\", "/")
    return shlex.quote(text)


def _run_the_documented_block(shell: str, block: str, tmp_path, env: dict) -> subprocess.CompletedProcess[str]:
    """Write the block out and run it as a shell script, or skip if the shell cannot run one."""
    probe = subprocess.run(
        [shell, "--noprofile", "--norc", "-c", "echo probe-ok"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if "probe-ok" not in (probe.stdout or ""):
        # Measured precedent (CI, 2026-10-02, the Windows leg of run 36959438654): on that
        # runner `bash` resolved to the WSL launcher, which answers "Windows Subsystem for
        # Linux has no installed distributions" and exits 1. An arm that ran anyway would
        # measure the shell's absence rather than the block, so it reports instead of failing.
        pytest.skip(
            f"`{shell}` cannot run a script here ({probe.stdout!r} / {probe.stderr!r}), so a "
            f"run would measure the shell's absence rather than the documented block"
        )
    script = tmp_path / "documented-block.sh"
    script.write_text(block, encoding="utf-8")
    return subprocess.run(
        [shell, "--noprofile", "--norc", str(script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_a_windows_path_survives_being_handed_to_a_shell() -> None:
    """The conversion, measured against a real shell rather than asserted about a string.

    On Windows `sys.executable` is `D:\\a\\emrg\\emrg\\.venv\\Scripts\\python.exe`. Handed to
    bash unquoted, the backslashes are consumed and the path collapses to
    `D:aemrgemrg.venvScriptspython.exe` - measured on the Windows leg of run 37100806753. This
    is the arm that keeps the fix: it asks a real bash what it read back, on a platform where
    bash and the Windows path shape are both available.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available to read the path back")

    windows = "D:\\a\\emrg\\emrg\\.venv\\Scripts\\python.exe"
    # A space, so the *quoting* half is load-bearing too: unquoted, the shell splits this into
    # two words and the path that arrives is truncated at the space.
    posix = "/home/runner/work/my project/emrg/.venv/bin/python"

    for path, nt, expected in (
        (windows, True, "D:/a/emrg/emrg/.venv/Scripts/python.exe"),
        (posix, False, posix),
    ):
        quoted = _shell_path(path, nt=nt)
        read_back = subprocess.run(
            [shell, "--noprofile", "--norc", "-c", f"printf '%s' {quoted}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert read_back.stdout == expected, (
            f"a shell read {quoted!r} back as {read_back.stdout!r}, expected {expected!r} - "
            f"a path that arrives mangled is a command that never runs"
        )

    # And the shape that actually failed: without the conversion the shell loses the
    # separators, which is the measurement this helper exists for.
    collapsed = subprocess.run(
        [shell, "--noprofile", "--norc", "-c", f"printf '%s' {windows}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert collapsed.stdout == "D:aemrgemrg.venvScriptspython.exe", (
        f"the unquoted Windows path no longer collapses in this shell ({collapsed.stdout!r}) - "
        f"re-measure the failure this helper exists for before trusting it"
    )


def test_the_documented_command_carries_its_variables_into_the_preflight(tmp_path) -> None:
    """The executed half: run the documented block and read what reached the command.

    The text pin above names the property; this one proves it, because the failure it
    guards is a *shell* behaviour rather than a spelling. The preflight is replaced by a
    stand-in that prints the three variables it can see, and the three are removed from the
    environment first - so the only way they can reach it is along the documented line, and
    a block that fails to continue prints `<unset>` three times.

    The `<...>` placeholders are substituted before the run: they are the host's to fill,
    and leaving them in would exercise the shell's redirect syntax rather than the
    continuation this arm is about. Nothing here reaches Apple or spends a credential - the
    script is never invoked.
    """
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available to run the documented block")

    filled = _preflight_block()
    for placeholder, value in (
        ("APPLE_ID=<id>", "APPLE_ID=ID-VALUE"),
        ("MACOS_NOTARY_APP_PASSWORD=<app-specific-password>", "MACOS_NOTARY_APP_PASSWORD=PW-VALUE"),
        ("MACOS_NOTARY_TEAM_ID=<team>", "MACOS_NOTARY_TEAM_ID=TEAM-VALUE"),
    ):
        filled = filled.replace(placeholder, value)
    # The stand-in lives in a directory whose name contains a **space**, so an unquoted
    # interpolation is caught here rather than only on the Windows leg that found the bug:
    # unquoted, the shell splits the path at the space and runs a command that does not exist.
    stub = tmp_path / "stand in" / "preflight-stand-in.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(_PREFLIGHT_STUB, encoding="utf-8")
    filled = filled.replace(
        "uv run --no-sync python3 scripts/check-notary-credentials.py",
        f"{_shell_path(sys.executable)} {_shell_path(stub)}",
    )
    assert "check-notary-credentials.py" not in filled, (
        "the stand-in never replaced the preflight invocation, so this arm would run the real "
        "script - which spends a credential and asks Apple"
    )

    env = dict(os.environ)
    for name in (APPLE_ID_VAR, PASSWORD_VAR, TEAM_ID_VAR):
        env.pop(name, None)

    # Deliberately without the workflow's `-e`: this arm is about which variables arrive, not
    # about how the shell gives up. A host pasting the block into an interactive shell gets no
    # `-e` either.
    result = _run_the_documented_block(shell, filled, tmp_path, env)
    for name, value in (
        (APPLE_ID_VAR, "ID-VALUE"),
        (PASSWORD_VAR, "PW-VALUE"),
        (TEAM_ID_VAR, "TEAM-VALUE"),
    ):
        assert f"{name}={value}" in result.stdout, (
            f"the documented command did not carry {name} into the preflight.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
