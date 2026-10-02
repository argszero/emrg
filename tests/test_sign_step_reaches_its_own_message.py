"""The macOS sign step must reach its own explanation when it cannot sign.

Measured defect (2026-10-02). `Sign pkg (macOS only)` in `build-release.yml` resolves
the installer identity like this:

    INSTALLER_ID="$(security find-identity -v /tmp/ci.keychain | grep 'Developer ID
    Installer' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

Under the runner's `shell: bash` — `bash --noprofile --norc -eo pipefail` — a command
that exits non-zero **inside a command substitution aborts the step at that
assignment**, and `grep` is exactly such a command: it exits 1 when it matches
nothing, which is precisely the state the `::error::` block below it was written to
explain ("pkg signing needs a Developer ID Installer certificate … configure
MACOS_INSTALLER_IDENTITY + MACOS_INSTALLER_P12_BASE64, or export a p12 that carries
the Installer identity"). So the message, and its `exit 1`, were unreachable in the
one case they exist for; the operator saw `exit code 1` and nothing else.

That signature — a step failing with only "exit code 1" — is the same one that made
the v0.3.8 notarize failure unexplainable from the log (issue #1811), which is what
sent this cycle to look for the family elsewhere in the same workflow.

Executed here, not reasoned about: the body is read out of the parsed workflow and run
with the **runner's flags**, with `security`/`productsign`/`pkgutil` stubbed, in three
states. The middle one is the defect; the third is the distinction the fix must keep —
a keychain that cannot be read at all is not "no certificate is configured".
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

#: `security find-identity` printing an Installer identity, one that is not, and a
#: keychain that cannot be read.
_SECURITY_STUB = """#!/usr/bin/env bash
if [ "${STUB_SECURITY_RC:-0}" != "0" ]; then
  echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
  exit "$STUB_SECURITY_RC"
fi
if [ "${STUB_HAS_INSTALLER:-}" = "1" ]; then
  echo "  2 valid identities found"
  echo "  1) 1234ABCD \\"Developer ID Application: X (TEAM)\\""
  echo "     1) 5678EFGH \\"Developer ID Installer: X (TEAM)\\""
else
  echo "  1 valid identities found"
  echo "  1) 1234ABCD \\"Developer ID Application: X (TEAM)\\""
fi
"""

_PRODUCTSIGN_STUB = "#!/bin/sh\nfor last; do :; done\ntouch \"$last\"\n"
_PKGUTIL_STUB = "#!/bin/sh\nexit 0\n"


def _sign_step() -> dict:
    """`Sign pkg`'s step, from the parsed workflow — never from the document text.

    Read from the job because a body read by regex out of the file cannot tell which
    step it came from, and several steps here share the `PKG="$(find …)"` opening.
    """
    doc = yaml.safe_load((REPO / WORKFLOW).read_text(encoding="utf-8"))
    steps = doc["jobs"]["build"]["steps"]
    # By **name**, not by a word in the body: `productsign` also occurs in the comment
    # that explains the `Import signing certificate` step, and matching on it selected
    # two steps (measured on this guard's first run).
    matches = [s for s in steps if s.get("name") == "Sign pkg (macOS only)"]
    assert len(matches) == 1, (
        f"expected exactly one `Sign pkg (macOS only)` step, found {len(matches)} — "
        "this guard cannot measure which one it guards"
    )
    step = matches[0]
    assert "productsign" in str(step.get("run", "")), (
        "the step named `Sign pkg (macOS only)` no longer signs the pkg; this guard is "
        "now measuring a step it does not describe"
    )
    assert step.get("shell") == "bash", (
        f"the step's shell is {step.get('shell')!r}; this guard executes the body under "
        "the flags `shell: bash` implies, so it must stop if that changed"
    )
    return step


def test_the_sign_step_reaches_its_own_message(tmp_path: Path) -> None:
    """Three states, one executed body, under `-e -o pipefail`."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash is available for the ground-truth run")

    body = str(_sign_step()["run"])
    work = tmp_path / "work"
    (work / "dist" / "artifacts").mkdir(parents=True)
    # The step opens by finding a pkg; with the directory present and a pkg in it, that
    # `find` succeeds, so what these runs measure is the identity resolution below it
    # and nothing else.
    (work / "dist" / "artifacts" / "EMRG-9.9.9-macos-arm64.pkg").write_text("x")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (
        ("security", _SECURITY_STUB),
        ("productsign", _PRODUCTSIGN_STUB),
        ("pkgutil", _PKGUTIL_STUB),
    ):
        path = bindir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def run(**env_extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            # The step's `if:` is not evaluated here (only `run` is executed), so the
            # secret-shaped variables are not needed; MACOS_INSTALLER_IDENTITY stays
            # unset so the lookup path is the one under test.
            "HOME": str(tmp_path),
            **env_extra,
        }
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", body],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    # ── 1. An Installer identity is present: the step signs and passes ────────
    ok = run(STUB_HAS_INSTALLER="1")
    assert ok.returncode == 0, (
        f"the step fails even with a Developer ID Installer identity present: "
        f"rc={ok.returncode}\n{ok.stdout}{ok.stderr}"
    )

    # ── 2. No Installer identity: the message must be the thing that is seen ──
    # This is the defect's own case. On the unfixed body `grep` exits 1, `-e` aborts at
    # the assignment, and neither this text nor the `exit 1` below it is reached.
    missing = run()
    assert missing.returncode != 0, (
        "the step signs a pkg with no Developer ID Installer identity — that is the "
        f"state the refusal exists for: rc={missing.returncode} {missing.stdout}"
    )
    combined = missing.stdout + missing.stderr
    assert "::error::pkg 签名需要 Developer ID Installer 证书" in combined, (
        "the sign step failed without saying why: the message it was written to print "
        f"when the identifier cannot be resolved never appeared.\nstdout: {missing.stdout}\n"
        f"stderr: {missing.stderr}"
    )
    assert "MACOS_INSTALLER_IDENTITY" in combined and "MACOS_INSTALLER_P12_BASE64" in combined, (
        "the message must name the two ways to configure the identity — that is its "
        f"whole purpose. Got: {combined}"
    )

    # ── 3. The keychain cannot be read at all: a *different* message ──────────
    # "No certificate is configured" is a definite bad configuration; a keychain that
    # cannot be read is a reading that failed. Reporting the second as the first is the
    # §AT mistake (a failed read printed as a definite state).
    unreadable = run(STUB_SECURITY_RC="51")
    assert unreadable.returncode != 0, "a keychain that cannot be read must not sign"
    u = unreadable.stdout + unreadable.stderr
    assert "could not be read" in u, (
        "a failed `security find-identity` must not be reported as a missing "
        f"certificate — the two are different failures. Got: {u}"
    )
    assert "MACOS_INSTALLER_IDENTITY" not in u, (
        "a keychain read failure is being answered with the 'configure a certificate' "
        f"remedy, which the failure does not establish. Got: {u}"
    )
    assert "could not be found in the keychain" in u, (
        "the message must carry security's own words — the only thing that can tell an "
        f"absent keychain from a locked one. Got: {u}"
    )


def test_the_identity_lookup_status_is_read_apart_from_the_pipeline() -> None:
    """The two statuses must not be merged into one reading.

    A pinned shape rather than an executed one, because it is about *which* status is
    read: the fix exists because `grep`'s exit 1 (no match — a legitimate answer) and
    `security`'s failure (a reading that did not happen) both surface as "the pipeline
    failed". If a future edit collapses them back into a single `|| true` without the
    separate read, the executed test above still passes and this one must not.
    """
    run_body = str(_sign_step()["run"])
    assert "|| SECURITY_RC=$?" in run_body, (
        "the identity lookup no longer captures `security`'s own status, so a keychain "
        "that cannot be read would be reported as a missing certificate"
    )
    assert 'grep \'Developer ID Installer\'' in run_body, (
        "the Installer-identity lookup moved; this guard is now measuring a step it "
        "does not describe"
    )
    # The lookup that may legitimately fail is the one that must not abort the step.
    lookup = [
        line for line in run_body.splitlines()
        if "grep 'Developer ID Installer'" in line
    ]
    assert len(lookup) == 1, f"expected one lookup line, found {len(lookup)}"
    assert "|| true" in lookup[0], (
        "the grep lookup would abort the step at its assignment again: "
        f"{lookup[0].strip()!r}"
    )
