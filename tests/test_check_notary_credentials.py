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
if scenario == "history_string":
    # Measured 2026-10-07 (`cyc20261007-131552`): `len("x") == 1`, so this answered
    # "1 past submission(s) on record" and exited **0** -- a count invented from a string
    # and a pass over a reading that never happened.
    json.dump({"history": "x"}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)
if scenario == "history_null":
    # The key is there and holds nothing. `len(None)` raised out of the process, and an
    # uncaught exception exits **1** -- this tool's code for *Apple refused the credentials*.
    json.dump({"history": None}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)
if scenario == "history_absent":
    # A well-formed reply with no `history` field at all. `parsed.get("history", [])`
    # answered `[]`, so this reported "0 past submission(s) on record" -- a count from a
    # field that is not in the reply.
    json.dump({"id": "no-history-key"}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)
if scenario == "history_two":
    # The control for the two arms above: a real list still yields the real count, so a
    # fix that simply stopped printing counts would fail here.
    json.dump({"history": [{"id": "sub-1"}, {"id": "sub-2"}]}, sys.stdout)
    sys.stdout.write("\\n")
    sys.exit(0)
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


# ── The submission-history count is printed only when a list was read ──────────
#
# Measured 2026-10-07 (`cyc20261007-131552`) on master `028289f3`, by running the real
# script against a stubbed `xcrun` that answers `notarytool history` with an exit-0 reply
# whose `history` field is not a list. One line of code (`len(parsed.get("history", []))`)
# produced three different wrong answers, and only one of them looked like an error:
#
#   `{"history": "x"}`   -> exit 0, "1 past submission(s) on record"   (a count from a string)
#   `{"history": null}`  -> traceback, exit 1                          (= "Apple refused you")
#   `{"id": ...}`        -> exit 0, "0 past submission(s) on record"   (a count from no field)
#
# The arms below pin each shape and, in the other direction, that a real list still yields
# its real count — so a "fix" that simply stopped printing counts cannot pass.


@_posix_only
def test_a_history_that_is_not_a_list_is_not_a_count(tmp_path: Path) -> None:
    """`len()` of a string is not a reading of the submission history.

    `{"history": "x"}` is the worst of the three because it is not an error at all: it
    printed a count as a fact (`len("x") == 1`) and exited **0**, so a reply whose shape
    nobody understood passed as "Apple answered the submission-history request". The lookup
    that exists to catch an unmeasured credential must not manufacture a measurement of its
    own, so this is `2` — could not measure — with the shape named.
    """
    result = _run(tmp_path, "history_string")
    assert result.returncode == 2, (
        f"a `history` that is not a list was not read as unmeasurable (exit "
        f"{result.returncode}).\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # `(str)`, not `str`: the field's own output is echoed into this same line for
    # reproducibility, and a bare `str` substring is satisfied by the words around it —
    # an arm that removed the type name survived a looser form of this assertion.
    assert "(str)" in result.stdout, (
        "the shape that arrived was not named, so a host cannot tell a string from a list "
        f"in the reply.\nstdout={result.stdout!r}"
    )
    assert "past submission(s) on record" not in result.stdout, (
        "a count was printed for a reply that holds no list — the fabricated fact this arm "
        f"exists to catch.\nstdout={result.stdout!r}"
    )


@_posix_only
def test_a_history_of_null_does_not_exit_with_apples_refusal_code(tmp_path: Path) -> None:
    """`len(None)` used to leave the process at **1** — the code for a refused credential.

    Everything a host has to go on is the exit code: `1` sends them to rotate an
    app-specific password Apple was never asked about, which is the false diagnosis this
    preflight exists to remove. A reply that cannot be read is `2`, and it says which shape
    arrived so the reading is reproducible.
    """
    result = _run(tmp_path, "history_null")
    assert result.returncode == 2, (
        f"a null `history` was not read as unmeasurable (exit {result.returncode}).\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # `NoneType` alone, and deliberately **not** `or "null" in stdout`: the reply is echoed
    # into this line for reproducibility, so a disjunction naming the field's own text is
    # satisfied by the echo — an arm that stripped the type name survived exactly that form.
    assert "NoneType" in result.stdout, (
        f"the shape that arrived was not named.\nstdout={result.stdout!r}"
    )
    assert "Traceback" not in result.stderr, (
        "the reply was answered with a traceback, so nothing in the output names the field "
        f"as the cause.\nstderr={result.stderr!r}"
    )


@_posix_only
def test_a_reply_without_a_history_field_does_not_report_zero_submissions(
    tmp_path: Path,
) -> None:
    """`parsed.get("history", [])` turned an absent field into "0 past submission(s)".

    Exit 0 from `notarytool history` is what "Apple accepted the credentials" looks like,
    so this stays a pass — but the parenthetical may not state a count that no field
    carried. The weak true wording is the one that belongs here.
    """
    result = _run(tmp_path, "history_absent")
    assert result.returncode == 0, (
        f"a well-formed reply with no `history` field stopped being a pass (exit "
        f"{result.returncode}).\nstdout={result.stdout!r}"
    )
    assert "0 past submission(s) on record" not in result.stdout, (
        "a count was printed for a field the reply does not contain.\n"
        f"stdout={result.stdout!r}"
    )


@_posix_only
def test_a_real_list_still_reports_its_real_count(tmp_path: Path) -> None:
    """The control: the fix must not be "print no counts at all".

    Without this arm, deleting the count entirely would satisfy the three above while
    removing the reading they are about.
    """
    result = _run(tmp_path, "history_two")
    assert result.returncode == 0, (
        f"a real history list stopped passing (exit {result.returncode}).\n"
        f"stdout={result.stdout!r}"
    )
    assert "2 past submission(s) on record" in result.stdout, (
        f"a two-entry history did not report its count.\nstdout={result.stdout!r}"
    )
