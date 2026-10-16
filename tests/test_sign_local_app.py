"""packaging/sign-local-app.sh — the identity the local upgrade re-seals EMRG.app with.

Rant 2026-10-16T12:03:58 / issue #2322. The ad-hoc re-seal left the app with no
certificate identity, so macOS keyed it by the hash of its binary and every
rebuild looked like a different application to TCC — the host re-authorised EMRG
after every upgrade. Measured on the host:

    $ codesign -dvvv ~/Applications/EMRG.app   → Signature=adhoc, TeamIdentifier=not set
    $ codesign -d -r-  ~/Applications/EMRG.app → designated => cdhash H"3a455147…"

These tests drive the script's *decisions* through a stubbed PATH, so they are a
statement about the script rather than about the machine running them: a stubbed
`security` decides which identities exist, a stubbed `codesign` records what it
was asked to do and fails on demand. The real-machine half — that the designated
requirement names the certificate after a real signing — is not measurable here
and is reported as such in the PR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="sign-local-app.sh is the macOS re-seal step; POSIX shell only",
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packaging" / "sign-local-app.sh"
BASH = shutil.which("bash") or "/bin/bash"

# The names the host's keychain actually holds (measured 2026-10-16, issue #2322).
APPLE_DEVELOPMENT = "Apple Development: Created via API (V5N9YB9H35)"
APPLE_DISTRIBUTION = "Apple Distribution: anqing shao (Y55RQ6LU24)"
DEVELOPER_ID = "Developer ID Application: anqing shao (Y55RQ6LU24)"

_SECURITY_STUB = """#!/bin/sh
# Stub for `security find-identity -v -p codesigning`.
cat "$STUB_SECURITY_OUT"
"""

_CODESIGN_STUB = """#!/bin/sh
# Stub for `codesign`: records every invocation, fails when told to.
echo "$*" >> "$STUB_CODESIGN_LOG"
case "$1" in
  --verify)
    [ "${STUB_VERIFY_FAIL:-0}" = "1" ] && exit 1
    exit 0 ;;
esac
if [ -n "${STUB_SIGN_FAIL_MATCH:-}" ]; then
  case "$*" in
    *"$STUB_SIGN_FAIL_MATCH"*) exit 1 ;;
  esac
fi
exit 0
"""


class Harness:
    """A stub bin directory, the fake app, and the two logs the stubs write."""

    def __init__(self, tmp_path: Path, identities: list[str]) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.security_out = tmp_path / "security.out"
        self.codesign_log = tmp_path / "codesign.log"
        self.codesign_log.write_text("")
        self.app = tmp_path / "EMRG.app"
        self.app.mkdir()

        # `security` always exists: identity *discovery* is what the cases vary.
        _write_stub(self.bin / "security", _SECURITY_STUB)
        self.security_out.write_text(
            "".join(f'  {i + 1}) {"%040X" % (i + 1)} "{name}"\n' for i, name in enumerate(identities))
        )
        self._install_codesign()

    def _install_codesign(self) -> None:
        _write_stub(self.bin / "codesign", _CODESIGN_STUB)

    def without_codesign(self) -> None:
        (self.bin / "codesign").unlink()

    def run(self, **env_overrides: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        # The stub dir leads, so the stubs win; the rest of PATH still supplies
        # sed/grep/head that the script itself uses.
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["STUB_SECURITY_OUT"] = str(self.security_out)
        env["STUB_CODESIGN_LOG"] = str(self.codesign_log)
        env.pop("EMRG_SIGN_IDENTITY", None)
        for key, value in env_overrides.items():
            env[key] = value
        return subprocess.run(
            [BASH, str(SCRIPT), str(self.app)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )

    def sign_argv(self, sign_value: str) -> list[str]:
        """Every recorded codesign invocation that signed with `sign_value`."""
        return [
            line
            for line in self.codesign_log.read_text().splitlines()
            if f"--sign {sign_value} " in line + " "
        ]


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


# ── the identity a rebuild is sealed with ────────────────────────────────────


def test_a_certificate_present_means_the_bundle_is_sealed_with_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, [APPLE_DEVELOPMENT])
    result = h.run()

    assert result.returncode == 0, result.stderr
    assert f"sign-local-app: identity={APPLE_DEVELOPMENT}" in result.stdout
    assert h.sign_argv(APPLE_DEVELOPMENT), (
        "the bundle must be signed with the certificate this machine holds — an "
        f"ad-hoc re-seal is what voids the host's grants. Log: {h.codesign_log.read_text()}"
    )
    assert not h.sign_argv("-"), (
        "with an identity available the script must NOT fall back to an ad-hoc "
        f"signature. Log: {h.codesign_log.read_text()}"
    )
    assert "WILL BE VOIDED" not in result.stdout


@pytest.mark.parametrize(
    ("identities", "expected"),
    [
        ([DEVELOPER_ID, APPLE_DEVELOPMENT, APPLE_DISTRIBUTION], DEVELOPER_ID),
        ([APPLE_DISTRIBUTION, APPLE_DEVELOPMENT], APPLE_DEVELOPMENT),
        ([APPLE_DISTRIBUTION], APPLE_DISTRIBUTION),
    ],
)
def test_the_discovery_order_is_fixed_so_the_identity_does_not_move_between_upgrades(
    tmp_path: Path, identities: list[str], expected: str
) -> None:
    """Several identities, one choice, always the same one.

    A choice that moved between upgrades would move the designated requirement
    with it — the defect this script exists to remove.
    """
    h = Harness(tmp_path, identities)
    result = h.run()

    assert result.returncode == 0, result.stderr
    assert f"sign-local-app: identity={expected}" in result.stdout
    assert h.sign_argv(expected)
    assert not h.sign_argv("-")


def test_the_identity_can_be_pinned_explicitly(tmp_path: Path) -> None:
    """EMRG_SIGN_IDENTITY wins over discovery, so a host with several can pin one."""
    h = Harness(tmp_path, [DEVELOPER_ID, APPLE_DEVELOPMENT])
    result = h.run(EMRG_SIGN_IDENTITY=APPLE_DISTRIBUTION)

    assert result.returncode == 0, result.stderr
    assert f"sign-local-app: identity={APPLE_DISTRIBUTION}" in result.stdout
    assert h.sign_argv(APPLE_DISTRIBUTION)


# ── the fallback is loud, and the upgrade is never blocked by it ─────────────


def test_no_identity_falls_back_to_adhoc_loudly_and_still_succeeds(tmp_path: Path) -> None:
    """The upgrade proceeds, but the consequence is written down, never silent."""
    h = Harness(tmp_path, [])
    result = h.run()

    assert result.returncode == 0, (
        "a machine with no certificate must still upgrade — the fallback is loud, "
        f"not blocking. stderr: {result.stderr}"
    )
    assert "sign-local-app: identity=adhoc" in result.stdout
    assert "WILL BE VOIDED" in result.stdout, (
        "the ad-hoc fallback must name the consequence in the log the upgrade "
        "session keeps — a silent downgrade is half the reported defect"
    )
    assert h.sign_argv("-")


def test_signing_with_the_identity_failing_falls_back_to_adhoc(tmp_path: Path) -> None:
    h = Harness(tmp_path, [APPLE_DEVELOPMENT])
    result = h.run(STUB_SIGN_FAIL_MATCH="Apple Development")

    assert result.returncode == 0, result.stderr
    assert "falling back to ad-hoc" in result.stderr
    assert "sign-local-app: identity=adhoc" in result.stdout
    assert h.sign_argv("-"), (
        "a failed certificate signature must still leave a sealed bundle — "
        f"unsealed is what macOS 26 trashes. Log: {h.codesign_log.read_text()}"
    )


# ── the two ways this may not hand the artifact on ──────────────────────────


def test_a_bundle_that_does_not_verify_is_a_fault(tmp_path: Path) -> None:
    h = Harness(tmp_path, [APPLE_DEVELOPMENT])
    result = h.run(STUB_VERIFY_FAIL="1")

    assert result.returncode == 1, (
        "a bundle that does not verify must not be copied — the pre-existing rule "
        f"(rant 2026-08-25T09:18:19). stdout: {result.stdout} stderr: {result.stderr}"
    )
    assert "do not copy" in result.stderr


def test_without_codesign_nothing_is_measured_and_the_upgrade_proceeds(tmp_path: Path) -> None:
    """`could not measure` is its own answer, never a pass and never a fault."""
    h = Harness(tmp_path, [APPLE_DEVELOPMENT])
    h.without_codesign()
    env = dict(os.environ)
    env["PATH"] = str(h.bin)  # a PATH that holds only the `security` stub
    env["STUB_SECURITY_OUT"] = str(h.security_out)
    env["STUB_CODESIGN_LOG"] = str(h.codesign_log)
    result = subprocess.run(
        # bash by absolute path: the PATH handed to the script deliberately holds
        # nothing but the stub, so `bash` itself is not findable through it.
        [BASH, str(SCRIPT), str(h.app)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )

    assert result.returncode == 2, (
        "the house contract is 0 ok / 1 fault / 2 could not measure — 'no codesign "
        f"here' is the third. stdout: {result.stdout} stderr: {result.stderr}"
    )
    assert "verdict=could-not-measure" in result.stdout


def test_a_missing_bundle_is_a_fault(tmp_path: Path) -> None:
    h = Harness(tmp_path, [APPLE_DEVELOPMENT])
    result = subprocess.run(
        [BASH, str(SCRIPT), str(tmp_path / "absent.app")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ),
        timeout=60,
    )

    assert result.returncode == 1
    assert "does not exist" in result.stderr


# ── the one option that could stop the GUI launching ────────────────────────


@pytest.mark.parametrize("identities", [[APPLE_DEVELOPMENT], []])
def test_the_hardened_runtime_option_is_never_passed(tmp_path: Path, identities: list[str]) -> None:
    """--options runtime would let library validation block Electron's libraries.

    The script's header records this as the one way this change could break the
    GUI; the assertion is on the invocations, so a future edit cannot slip it in.
    Both paths are exercised — the certificate one and the ad-hoc fallback.
    """
    h = Harness(tmp_path, identities)
    h.run()

    assert h.codesign_log.read_text().strip(), "the run signed nothing to assert about"
    assert "--options runtime" not in h.codesign_log.read_text()
