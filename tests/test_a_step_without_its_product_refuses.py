"""A release step whose subject is its product must refuse when the product is absent.

Why this file exists
--------------------
`build-release.yml`'s three macOS steps each began by locating the pkg and **skipping
silently** when it was absent::

    PKG="$(find dist/artifacts -maxdepth 1 -name 'EMRG-*-macos-*.pkg' | head -1)"
    if [ -z "$PKG" ]; then echo "no pkg found, skipping"; exit 0; fi

Measured on `8871851f` (issue #1913, cycle `cyc20261008-103801`), driving each step's own
`run:` block with an empty `dist/artifacts`: all three exited **0** printing
`no pkg found, skipping`. The Windows step in the same job does the opposite — it prints
`::error::no EMRG-*-windows-x64.exe found in dist/artifacts` and exits 1 — and the release
job's completeness assertion cannot see the difference either, because it derives its
expected asset set from the same files the build produced: driven against a build of four
assets that contained **no macOS pkg** (and a release carrying those same four), it exited
0 printing *"release v0.3.9: published, not prerelease, 4 asset(s) — matches the build"*
and *"release v0.3.9: Published as Latest"*. So a tag run could publish a Latest release
with no macOS product and every step would report success.

This is the repository's own family, fixed twice already on the Python side (issue #1872,
"an empty tree is not a clean reading"): a guard whose subject is absent must **refuse**,
not pass.

Why the steps are executed instead of read
------------------------------------------
A test that greps the workflow for `skipping` answers "does the text look right"; the
defect is what the step *does*, and the text-shaped assertion would go green the moment a
future edit kept the words while moving the `exit`. So each subject's own `run:` block is
extracted from the workflow and executed as a shell script under `bash --noprofile --norc
-eo pipefail` (GitHub's `shell: bash` shape) in a temporary tree, in both directions:

* no matching product in `dist/artifacts` → the step must exit non-zero **and** name the
  pattern in an `::error::` line (a refusal a reader can act on, not a bare `exit 1`);
* a matching product present → the step must **not** print that refusal, because a guard
  that refuses everything is not measuring its subject. What it then does (call a tool
  that the stubbed PATH refuses) is deliberately not asserted: that path belongs to the
  signing/notarization tests, and the stubs keep this file off the network.

The subjects are **derived from the workflow**, not listed here: every step whose `run:`
block searches the artifact directory for an `EMRG-*` product. A step added later is
covered without editing this file. The matrix's own `artifact:` field is the declaration
each leg owes; this file asserts that every declared product is one some step searches
for, so a leg that stops being guarded fails here rather than silently leaving the
discovery. The two Linux legs' AppImage is the one exemption, and it is stated rather
than implied: `packaging/make-installer.sh`'s Linux branch documents a leg with no
AppImage as its deliberate fallback ("Linux release 缺 AppImage（有 tar.gz 兜底）").

Named limit
-----------
Nothing here is measured about signing or notarization — only about the step's behaviour
towards a **missing** subject. A step that searches a *different* directory, or that stops
naming the product's pattern altogether, falls out of the discovery; the matrix-coverage
assertion below is what makes that a failure rather than a shrinking guard.

The steps are not run on Windows: `bash` there is WSL's, which is not the shell these
steps run under in CI (measured 2026-10-02, PR #1812) — the guard says so rather than
measuring a different shell under the same name.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"

#: What a step's search for its own product looks like: an artifact pattern beginning
#: `EMRG-` inside the directory the build job writes its products to.
_ARTIFACT_PATTERN = re.compile(r"EMRG-[^\s'\"]*")

#: Tools the macOS steps call that must never run for real here. Each stub refuses, so a
#: step that gets past its subject check still cannot sign, notarize or talk to Apple.
_STUBBED_TOOLS = ("xcrun", "codesign", "security", "productsign", "powershell", "spctl")

#: The environment every step is run with: the secrets the `if:` conditions read, so a
#: step is not skipped for the wrong reason when its body is executed by hand.
_STEP_ENV = {
    "APPLE_ID": "release-guard@example.invalid",
    "MACOS_NOTARY_APP_PASSWORD": "not-a-real-password",
    "MACOS_NOTARY_TEAM_ID": "NOTATEAM01",
    "MACOS_SIGNING_P12_BASE64": "bm90LWEtcmVhbC1wMTI=",
    "MACOS_SIGNING_P12_PASSWORD": "not-a-real-password",
    "MACOS_SIGNING_IDENTITY": "Developer ID Application: Release Guard",
    "MACOS_INSTALLER_IDENTITY": "Developer ID Installer: Release Guard",
}

#: The product each leg declares it owes. A leg whose product no step searches for is the
#: gap this file exists to keep closed — except the two known exemptions below.
_MATRIX_EXEMPT = {
    # make-installer.sh, Linux branch: documented fallback, "缺 AppImage（有 tar.gz 兜底）"
    "EMRG-*-x86_64.AppImage",
    "EMRG-*-aarch64.AppImage",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _build_steps() -> list[dict]:
    return _workflow()["jobs"]["build"]["steps"]


def _artifact_steps() -> list[dict]:
    """Every step whose `run:` block searches the artifact directory for a product."""
    found = []
    for step in _build_steps():
        run = step.get("run")
        if not isinstance(run, str):
            continue
        if "artifacts" not in run:
            continue
        if not _ARTIFACT_PATTERN.search(run):
            continue
        found.append(step)
    return found


def _patterns(run: str) -> list[str]:
    return [m.group(0).split("|")[0] for m in _ARTIFACT_PATTERN.finditer(run)]


def _matching_name(pattern: str) -> str:
    """A file name that satisfies `pattern` — what the step would find if it existed.

    Any segment satisfies a `*`, so one spelling covers every pattern the workflow uses;
    the step's own `find` is what proves the name was found.
    """
    return pattern.replace("*", "seg")


def _run_step(tmp_path: Path, step: dict, *, product: str | None) -> subprocess.CompletedProcess:
    """Execute the step's own `run:` block with `product` present (or nothing at all)."""
    if shutil.which("bash") is None:  # pragma: no cover - a POSIX-less host
        pytest.skip("no bash on PATH: this guard drives the step's shell text, which it cannot run")

    artifacts = tmp_path / "dist" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if product is not None:
        (artifacts / product).write_bytes(b"not a real installer\n")

    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for tool in _STUBBED_TOOLS:
        stub = stubs / tool
        stub.write_text(f"#!/bin/sh\necho 'stub: {tool} refuses to run'\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

    script = tmp_path / "step.sh"
    script.write_text(textwrap.dedent(step["run"]), encoding="utf-8")

    env = {"PATH": f"{stubs}:{Path('/usr/bin')}:{Path('/bin')}", "HOME": str(tmp_path)}
    env.update(_STEP_ENV)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        # The steps print UTF-8 (some of their refusal messages are Chinese), and the
        # locale codec of the host running this test is not a decoder for them
        # (issue #1132; the guard is tests/test_script_decode_is_locale_independent.py).
        encoding="utf-8", errors="replace", timeout=120,
    )


def _absent_error(step: dict, result: subprocess.CompletedProcess) -> list[str]:
    """The `::error::` lines that name a product of `step`'s own as missing."""
    patterns = _patterns(step.get("run", ""))
    return [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "::error::" in line and any(p in line for p in patterns)
    ]


def test_the_discovery_names_the_steps_that_search_for_a_product():
    """The guard's own subject list, stated: an empty discovery would make it vacuous."""
    assert _artifact_steps(), (
        "no step in build-release.yml's build job searches the artifact directory for an "
        "EMRG-* product — this guard has nothing to measure, which is a failure to measure "
        "rather than a pass (the steps were renamed or rewritten: see issue #1913)"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell these steps run under in "
    "CI (measured 2026-10-02, PR #1812)",
)
def test_a_step_that_finds_no_product_refuses_rather_than_skipping(tmp_path):
    """Direction one: an absent subject is a failure, and the failure says what is absent."""
    for step in _artifact_steps():
        result = _run_step(tmp_path / re.sub(r"\W+", "-", step["name"]), step, product=None)
        assert result.returncode != 0, (
            f"{step['name']!r} exited 0 with no EMRG product in dist/artifacts — the step "
            f"reports success for a release that does not carry what the step exists to "
            f"produce. Output: {(result.stdout + result.stderr).strip()[:300]!r}"
        )
        named = _absent_error(step, result)
        assert named, (
            f"{step['name']!r} failed without naming the product it could not find "
            f"({_patterns(step['run'])}) — a bare exit code leaves the reader to guess "
            f"which artifact was absent. Output: "
            f"{(result.stdout + result.stderr).strip()[:300]!r}"
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell these steps run under in "
    "CI (measured 2026-10-02, PR #1812)",
)
def test_a_step_that_finds_its_product_does_not_refuse_it(tmp_path):
    """Direction two: the refusal above is about the subject, not an unconditional `exit 1`.

    With a file that satisfies the step's own pattern present, the step must not report it
    as missing. What it does next is left unasserted on purpose — the stubbed PATH above
    guarantees nothing real is signed, notarized or uploaded to Apple.
    """
    for step in _artifact_steps():
        patterns = _patterns(step["run"])
        product = _matching_name(patterns[0])
        result = _run_step(tmp_path / re.sub(r"\W+", "-", step["name"]), step, product=product)
        assert not _absent_error(step, result), (
            f"{step['name']!r} reported {product!r} as missing even though the file was "
            f"there — the refusal is not measuring its subject. Output: "
            f"{(result.stdout + result.stderr).strip()[:300]!r}"
        )


def test_every_product_a_leg_declares_is_guarded_here():
    """The matrix declares what each leg owes; a leg nothing searches for must fail here.

    This is what keeps the derivation above from shrinking silently: the workflow's own
    `artifact:` per leg is the list of products, and each one has to be a product some step
    searches for. The Linux AppImages are the stated exemption, not an oversight.
    """
    declared = [entry["artifact"] for entry in _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]]
    searched = [p for step in _artifact_steps() for p in _patterns(step["run"])]
    unguarded = [
        product
        for product in declared
        if product not in _MATRIX_EXEMPT
        and not any(fnmatch.fnmatch(product, pattern) for pattern in searched)
    ]
    assert not unguarded, (
        f"these declared products are searched for by no step: {unguarded}. A leg whose "
        f"product nothing looks for can finish green without producing it, and the release "
        f"assertion cannot see the difference (issue #1913)"
    )
