"""The `Import signing certificate` step must print what `security import` said, including when it fails.

Measured defect (macOS 26.5.1, this tree):

    the step captures `security import … 2>&1` into `IMPORT_OUTPUT`, echoes it, and then
    explains in an `::error::` — "…import 输出：${IMPORT_OUTPUT}" — what a missing private
    key means. The runner starts the step as `bash --noprofile --norc -eo pipefail`, and
    `VAR="$(cmd)"` carries `cmd`'s status, so when `security import` **itself** exits
    non-zero the script aborts **at the assignment**: neither the `echo` nor the error
    message is reachable, and the job log holds only `exit code 1`.

That is not a corner. The failure it hides is the ordinary one — a wrong
`MACOS_SIGNING_P12_PASSWORD`, or a p12 exported with algorithms Apple's `security`
refuses (measured here: `openssl pkcs12 -export` without `-legacy` gives
`SecKeychainItemImport: MAC verification failed during PKCS12 import (wrong password?)`,
rc 1). Agent.md records that **all nine** v0.2.7 build failures were host-side p12
exports; this step's message was written for exactly that host, and the host is the one
reader who never sees it.

The two captures in this step are the same shape (the dual-p12 `INSTALLER_IMPORT` is a
copy), so the fix and the guard cover both.

What this file measures, and how:

* the **executed leg** parses the step out of the workflow and runs its real body with
  the runner's own flags (`--noprofile --norc -eo pipefail`) and `security` stubbed. A
  harness that drops the flags measures a *different program* — the defect is a property
  of `-e`, so `-e` has to be there for the test to be able to see it.
* the **structural leg** pins, on every platform, that no diagnostic capture in this step
  can abort the step before its message is printed.

Stated limits, so a later reader does not read more than was measured:

* the body writes its two p12s to the step's own fixed paths `/tmp/signing.p12` and
  `/tmp/installer.p12`; the executed leg therefore **skips** when either already exists
  (it will not clobber a file it did not create), and removes the ones it creates. The
  structural leg still runs there, so the guard is never *unmeasured and reported green*.
* the executed leg needs a POSIX shell and POSIX `/tmp`, so it skips on win32 — the same
  declared limit as `tests/test_skills_loader.py`. The workflow's own step is macOS-only.
* the stub returns one output line per import; the **real** `security import` output was
  read on this host (`1 identity imported.` with a private key, `1 certificate imported.`
  without one, rc 0 in both; the MAC-verification message and rc 1 for a bad password).
  No output form is invented here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/build-release.yml"
STEP_NAME = "Import signing certificate (macOS only)"

#: The shell GitHub starts a `shell: bash` step with; `-c` replaces the file argument.
RUNNER_FLAGS = ["--noprofile", "--norc", "-eo", "pipefail"]

#: The step's own fixed paths. `security` is stubbed, so these are the only host writes
#: an executed leg can make — hence the skip-if-present and the removal below.
P12_PATHS = (Path("/tmp/signing.p12"), Path("/tmp/installer.p12"))

#: What `security import` prints on this host, measured rather than assumed:
#: a p12 carrying a private key, a cert-only p12, and the refusal of a password the
#: p12 was not exported with (rc 1 — the case this step must report).
WITH_KEY = "1 identity imported."
CERT_ONLY = "1 certificate imported."
MAC_FAILURE = (
    "security: SecKeychainItemImport: MAC verification failed during PKCS12 import "
    "(wrong password?)"
)

_SECURITY_STUB = """#!/bin/sh
# Stub for `security`: the step is what is under test, not the keychain.
# The p12 in the argument list tells this step's two imports apart.
case "$1" in
  import)
    case "$*" in
      */tmp/installer.p12*) out="${STUB_INSTALLER_OUT:-}"; rc="${STUB_INSTALLER_RC:-0}" ;;
      *) out="${STUB_IMPORT_OUT:-}"; rc="${STUB_IMPORT_RC:-0}" ;;
    esac
    if [ -n "$out" ]; then printf '%s\\n' "$out"; fi
    exit "$rc"
    ;;
  find-certificate)
    printf '%s\\n' '  1) STUB "Developer ID Application: Stub (STUBTEAM)"'
    exit 0
    ;;
  list-keychains)
    printf '%s\\n' '"/tmp/ci.keychain"'
    exit 0
    ;;
esac
exit 0
"""


def _step() -> dict:
    """The step, read from the parsed workflow — never by matching the document's text.

    `Sign pkg`, `Staple pkg` and this step share a first line and an `if:`, so a text
    search cannot say which step lost the guard; the parsed step can.
    """
    path = REPO / WORKFLOW
    assert path.is_file(), f"{WORKFLOW} is missing — this guard cannot measure what it guards"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == STEP_NAME]
    assert len(named) == 1, f"expected exactly one {STEP_NAME!r} step, got {len(named)}"
    step = named[0]
    assert step.get("shell") == "bash", (
        f"the import step no longer runs under bash (shell={step.get('shell')!r}); the "
        "flags this file pins describe bash"
    )
    assert isinstance(step.get("run"), str) and step["run"].strip(), "the step has no body"
    return step


def _body() -> str:
    return _step()["run"]


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A stub `security` on PATH, and the step's own cwd."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    security = bindir / "security"
    security.write_text(_SECURITY_STUB, encoding="utf-8")
    security.chmod(0o755)
    return tmp_path


def run_import_step(
    tree: Path,
    *,
    import_rc: int = 0,
    import_out: str = WITH_KEY,
    installer_rc: int = 0,
    installer_out: str = WITH_KEY,
    dual_p12: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the step body the way the runner does, with `security` stubbed."""
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    for path in P12_PATHS:
        if path.exists():
            pytest.skip(f"{path} already exists — this run would clobber a file it did not create")
    bindir = tree / "bin"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "MACOS_SIGNING_P12_BASE64": "c3R1YmJlZC1wMTI=",
        "MACOS_SIGNING_P12_PASSWORD": "not-a-real-password",
        "STUB_IMPORT_RC": str(import_rc),
        "STUB_IMPORT_OUT": import_out,
        "STUB_INSTALLER_RC": str(installer_rc),
        "STUB_INSTALLER_OUT": installer_out,
    }
    if dual_p12:
        env["MACOS_INSTALLER_P12_BASE64"] = "c3R1YmJlZC1pbnN0YWxsZXI="
        env["MACOS_INSTALLER_P12_PASSWORD"] = "not-a-real-password"
    try:
        return subprocess.run(
            [shell, *RUNNER_FLAGS, "-c", _body()],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        for path in P12_PATHS:
            path.unlink(missing_ok=True)


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return f"{proc.stdout}\n{proc.stderr}"


# ── the executed leg (POSIX only: the step's paths are `/tmp/...`) ──────────────


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_failed_import_prints_what_security_said(tree: Path) -> None:
    """The diagnostic this step exists to publish survives `security import` failing."""
    proc = run_import_step(tree, import_rc=1, import_out=MAC_FAILURE)
    out = _output(proc)
    assert proc.returncode != 0, f"a failed import must still fail the step; got rc=0\n{out}"
    assert "MAC verification failed" in out, (
        "`security import` failed and the step printed nothing about it — the captured "
        f"diagnostic never reached the log (only `exit code 1` would be there):\n{out}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_failed_import_reports_the_exit_code(tree: Path) -> None:
    """The message reports a reading — the status the step actually saw — not a cause."""
    proc = run_import_step(tree, import_rc=7, import_out=MAC_FAILURE)
    out = _output(proc)
    assert proc.returncode != 0, out
    assert "rc=7" in out, (
        f"the step's message must carry the status it saw (rc=7); got:\n{out}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_cert_only_p12_still_fails_the_step(tree: Path) -> None:
    """The check the guard must not loosen: rc 0 without an identity line is still a failure.

    This is the v0.2.7 root cause the message names — a p12 that imports only a
    certificate chain — and it must keep exiting non-zero.
    """
    proc = run_import_step(tree, import_rc=0, import_out=CERT_ONLY)
    out = _output(proc)
    assert proc.returncode != 0, (
        f"a cert-only p12 (no private key) must still fail the step; got rc=0\n{out}"
    )
    assert "rc=0" in out, (
        f"rc=0 with no identity line is the cert-only case and must be reported as such:\n{out}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_nonzero_exit_fails_the_step_even_when_its_output_looks_right(tree: Path) -> None:
    """The status is a signal in its own right, not only a reason to read the output.

    `security import` exiting non-zero is a failure whatever it printed, so the rc guard
    must not be a way to *ignore* the status — it must be a way to *report* it.
    """
    proc = run_import_step(tree, import_rc=1, import_out=WITH_KEY)
    out = _output(proc)
    assert proc.returncode != 0, (
        f"a non-zero `security import` must fail the step even with an identity line:\n{out}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_healthy_import_lets_the_step_finish(tree: Path) -> None:
    """The direction that must pass: an identity imports, the step completes."""
    proc = run_import_step(tree, import_rc=0, import_out=WITH_KEY)
    out = _output(proc)
    assert proc.returncode == 0, f"a healthy import must not fail the step:\n{out}"
    assert "::error::" not in out, out


@pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
def test_a_failed_installer_import_is_reported_too(tree: Path) -> None:
    """The dual-p12 branch is a copy of the same shape, so it is measured too."""
    proc = run_import_step(
        tree, import_rc=0, installer_rc=1, installer_out=MAC_FAILURE, dual_p12=True
    )
    out = _output(proc)
    assert proc.returncode != 0, out
    assert "MAC verification failed" in out, (
        "the Installer p12 import failed and its diagnostic never reached the log:\n" + out
    )
    # The first import succeeded, so the failure is attributable to the second one only.
    assert "INSTALLER" in out, out


# ── the structural leg (every platform) ────────────────────────────────────────

#: The opening of a capture assignment. The command inside may span lines (this step's
#: does — it is one `security import` continued with `\`), so the statement is delimited
#: by the closing `)"` rather than by a line ending.
_CAPTURE_OPEN = re.compile(r'(?P<var>[A-Z_][A-Z0-9_]*)="\$\(')


def _captures(body: str) -> list[tuple[str, str, str]]:
    """`(name, statement, rest-of-line)` for every `VAR="$(…)"` assignment in `body`.

    The stated shape this reads: the command substitution is closed by the first `)"`
    after the opening — true for every assignment in this step, and a rewrite that
    breaks it makes the count assertion below fail loudly rather than read as a pass.
    """
    found = []
    for match in _CAPTURE_OPEN.finditer(body):
        close = body.find(')"', match.end())
        assert close != -1, f"unbalanced capture in the step body: {match.group(0)!r}"
        close += 2
        eol = body.find("\n", close)
        found.append((match["var"], body[match.start() : close], body[close : eol if eol != -1 else len(body)]))
    return found


def test_every_diagnostic_capture_in_this_step_is_guarded() -> None:
    """No capture in this step may abort the step before its own message is printed.

    Runs everywhere, including the platforms where the executed leg skips — a guard that
    is unmeasurable on a platform is skipped and said so; it is never quietly green.
    """
    body = _body()
    all_captures = _captures(body)
    diagnostic = [c for c in all_captures if "2>&1" in c[1]]
    assert len(diagnostic) == 2, (
        "expected the step's two diagnostic captures (`IMPORT_OUTPUT`, `INSTALLER_IMPORT`); "
        f"found {[c[0] for c in diagnostic]} of {[c[0] for c in all_captures]} assignments — "
        "if the step was rewritten, re-derive this guard from the new body"
    )
    for var, statement, tail in diagnostic:
        assert re.search(r"\|\|\s*[A-Z_][A-Z0-9_]*=\$\?", tail), (
            "this capture can abort the step at the assignment, so the message that "
            f"reports it is unreachable:\n    {statement.strip()} …\n"
            f"give it an rc guard (`… )\" || {var}_RC=$?`) and report that rc"
        )
        # Printed *in the failure message*: the `::error::` line is what GitHub puts in
        # the run summary, which is where a reader who was not watching the log lands.
        error_lines = [ln for ln in body.splitlines() if "::error::" in ln]
        assert any(re.search(rf"\$\{{?{var}\b", ln) for ln in error_lines), (
            f"{var} is captured but no `::error::` line names it — a reader who sees only "
            "the annotation is told the step failed and not why"
        )
