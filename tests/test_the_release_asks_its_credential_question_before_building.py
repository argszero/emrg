"""The release asks whether it can notarize **before** it spends the four-platform build.

Why this file exists
--------------------
Measured on tag `v0.3.8`, run `36956685533` (2026-10-02): all four platform legs built, the
macOS leg died at **Notarize pkg** three seconds into that step, the `release` job was
skipped because of it, and **nothing at all was published** — the host paid a full build
round, and then six days, to learn what one exchange with Apple answers in a second.

The exchange has a measurement: `scripts/check-notary-credentials.py` (issue #1819 /
PR #1820) sends exactly the three variables `build-release.yml` feeds the submit step and
reports `0` Apple accepted them / `1` Apple refused them / `2` the exchange did not
complete, so no verdict was reached. Its own docstring says the reading is "deliberately
the same exchange the release makes" and that exit 2 is "never 0, and the CI step must
fail on it too". Measured 2026-10-08 (`cyc20261008-130733`): no CI step ran it — its only
references were `DEVELOPMENT.md`, the failing step's remedy prose, and its own tests —
while `verify-tag`, the job that exists so a tag problem fails in seconds rather than
after the build, asked a different question.

So the workflow now has a job whose subject *is* that reading, and `build` needs it. This
file pins the four properties that make it a gate rather than a second opinion:

* it is on the path (the job that builds **needs** the job that asks);
* it measures the credential the release **spends** — the notarize step's own three
  secret names, not a set of its own;
* it fails closed on **every** non-zero answer, exit 2 included, and the fail-closed
  behaviour is *executed* rather than read: the step's own `run:` text is driven with a
  stand-in preflight that answers 0, 1 and 2;
* it is skipped exactly where the notarize step is skipped, so a host who never
  configured notarization keeps the documented degradation instead of gaining a release
  blocker — and it does not run at all on a branch dispatch, where there is no tag to
  publish.

Named limit
-----------
Nothing here reaches Apple or spends a credential: the stand-in replaces the preflight on
the path the step itself names, so the exchange, the transport and the keychain are not
exercised. What the preflight's own `0` looks like against a *valid* credential is
likewise unmeasured anywhere on a host that has none (the preflight says so itself); this
file asserts the workflow's *use* of the answer, never the answer's correctness. The
workflow-parsing gate (`actionlint`) is not on this host's `PATH`, so this file's
structural assertions are all that was verified here — a green run of this file is not a
green `actionlint` run, and `scripts/check-workflows.py` reports that as not measurable.
"""

from __future__ import annotations

import os
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

#: The reading whose answer this job exists to carry.
PREFLIGHT = "check-notary-credentials.py"

#: What *running* the preflight looks like: the path at the start of a line, optionally
#: behind an interpreter. Naming it is not running it — the notarize step's own remedy
#: prose names the script (and must keep doing so), so a discovery rule that matched the
#: bare name would call that step a second gate (measured while writing this file).
_INVOCATION = re.compile(
    r"(?m)^[ \t]*(?:uv run[^\n]*?\s)?(?:python3?\s+)?(?:\./)?scripts/check-notary-credentials\.py"
)

#: The step that spends the credential — its three variables are the set the gate must
#: measure, and its `if:` is the set the gate must be skipped by.
SPENDING_STEP = "Notarize pkg (macOS only)"

_ENV_TEST = re.compile(r"env\.([A-Z][A-Z0-9_]*)")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) if isinstance(s, dict)]


def _spending_step() -> dict:
    for step in _steps(_jobs()["build"]):
        if step.get("name") == SPENDING_STEP:
            return step
    raise AssertionError(
        f"build-release.yml's build job no longer has a {SPENDING_STEP!r} step — this file's "
        "subject set is derived from it, and a renamed step would leave the derivation "
        "measuring nothing (the notarize step is where the credential is spent)"
    )


def _gate() -> tuple[str, dict, dict]:
    """The job, and the step in it, that **runs** the preflight — derived, not named."""
    found = []
    for name, job in _jobs().items():
        for step in _steps(job):
            if _INVOCATION.search(str(step.get("run", ""))):
                found.append((name, job, step))
    assert len(found) == 1, (
        f"expected exactly one step running {PREFLIGHT} in build-release.yml, found "
        f"{[name for name, _j, _s in found]} — either nothing asks the credential question "
        "before the build, or more than one step does and this file cannot say which is the "
        "gate (issue: the release path spends a full build on a credential Apple refuses)"
    )
    return found[0]


def _spent_env_names() -> set[str]:
    """The variables the notarize step's own `if:` requires — the credential it spends."""
    names = set(_ENV_TEST.findall(str(_spending_step().get("if", ""))))
    assert len(names) == 3, (
        f"{SPENDING_STEP!r}'s `if:` names {sorted(names)}, not three variables — this "
        "file's equality assertions compare the gate's credential set with the set this "
        "step spends, so the derivation has to be the real one"
    )
    return names


def test_the_job_that_builds_needs_the_job_that_asks() -> None:
    """A gate beside the path is a second opinion; a gate on the path is a gate."""
    gate_name, _job, _step = _gate()
    needs = _jobs()["build"].get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    assert gate_name in needs, (
        f"{gate_name!r} runs the credential preflight, but the build job's `needs` is "
        f"{needs!r} — the build would run whatever the preflight answers, which is the "
        "arrangement that cost run 36956685533 a full build round for a refused "
        "credential (nothing was published)"
    )
    assert gate_name != "build", "the preflight must not be inside the job it is gating"


def test_the_gate_measures_the_credential_the_spending_step_spends() -> None:
    """Same variables or it is a different question — the preflight's own stated rule."""
    _name, job, _step = _gate()
    declared = set(job.get("env") or {})
    spent = _spent_env_names()
    assert spent <= declared, (
        f"the gate job declares env {sorted(declared)}, while {SPENDING_STEP!r} spends "
        f"{sorted(spent)} — a preflight reading a different credential set answers a "
        "question nobody asked (the preflight's docstring makes this its own rule)"
    )
    step_names = set(_ENV_TEST.findall(str(_gate()[2].get("if", ""))))
    assert step_names == spent, (
        f"the gate step's `if:` requires {sorted(step_names)}, but the credential is spent "
        f"by a step requiring {sorted(spent)} — the gate would be skipped where the "
        "release is not, or run where the release never notarizes"
    )


def test_the_gate_does_not_run_where_there_is_no_tag() -> None:
    """A tag run is the only run that publishes; a branch dispatch has no credential to spend."""
    _name, job, _step = _gate()
    condition = str(job.get("if", ""))
    assert "refs/tags/" in condition, (
        f"the gate job's `if:` is {condition!r} — without the tag test it would spend a "
        "macOS runner on every branch dispatch, where nothing is published"
    )


# ── The arms: the step's own text, driven ─────────────────────────────────────

#: What the stand-in preflight answers. `sys.exit` is the whole stand-in: the gate's
#: behaviour under a code is the property, never the exchange that produces it.
_STAND_IN = """\
import sys
print("stand-in preflight (this file never reaches Apple)")
sys.exit({rc})
"""


def _run_the_gate(rc: int, tmp_path: Path) -> subprocess.CompletedProcess:
    """Execute the gate step's own `run:` text with a stand-in answering `rc`.

    The stand-in lives at the *relative* path the step's own text names
    (`scripts/check-notary-credentials.py`) inside a temporary cwd, so the text is executed
    unchanged and the real preflight — which would ask Apple — cannot be reached.
    """
    if shutil.which("bash") is None:  # pragma: no cover - a POSIX-less host
        pytest.skip("no bash on PATH: this file drives a workflow step's shell text, which it cannot run")

    _name, _job, step = _gate()
    script_text = str(step["run"])
    assert "scripts/" + PREFLIGHT in script_text, (
        "the gate step no longer invokes the preflight by the relative path this arm "
        f"replaces, so the arm would reach the real script and ask Apple: {script_text!r}"
    )

    stand_in = tmp_path / "scripts" / PREFLIGHT
    stand_in.parent.mkdir(parents=True, exist_ok=True)
    stand_in.write_text(_STAND_IN.format(rc=rc), encoding="utf-8")

    script = tmp_path / "step.sh"
    script.write_text(textwrap.dedent(script_text), encoding="utf-8")

    # `shell: bash` is what the step declares, and GitHub runs it as `bash -e {0}`; the
    # step's own `set -euo pipefail` then applies from inside.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", str(script)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        capture_output=True, text=True,
        # The step prints UTF-8; the locale codec of the host running this test is not a
        # decoder for it (issue #1132, and this suite's locale-independence guard).
        encoding="utf-8", errors="replace", timeout=120,
    )


def _error_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [ln for ln in (result.stdout + result.stderr).splitlines() if "::error::" in ln]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell these steps run under in "
    "CI (measured 2026-10-02, PR #1812)",
)
def test_a_refused_credential_stops_the_release_before_any_build(tmp_path) -> None:
    """Direction one: exit 1 is Apple's refusal, and it must stop the tag run here."""
    result = _run_the_gate(1, tmp_path)
    assert result.returncode != 0, (
        "the gate passed a preflight that answered 1 (Apple refused the credentials) — the "
        f"build would then be spent, and the release would publish nothing. Output: "
        f"{(result.stdout + result.stderr).strip()[:300]!r}"
    )
    errors = _error_lines(result)
    assert errors and "answered 1" in errors[0], (
        "the gate failed without saying that Apple refused the credential (the preflight's "
        f"own reading is the only actionable half). Output: "
        f"{(result.stdout + result.stderr).strip()[:300]!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell these steps run under in "
    "CI (measured 2026-10-02, PR #1812)",
)
def test_a_credential_that_could_not_be_measured_stops_it_too(tmp_path) -> None:
    """Direction two, and the load-bearing one: exit 2 is not a pass.

    This is the arm the preflight's own contract asks for ("never 0, and the CI step must
    fail on it too")). A gate that lets an unmeasured exchange through is the "unpublished
    release while green" the workflow's `release` comment forbids — the same class as an
    empty tree read as a clean one.
    """
    result = _run_the_gate(2, tmp_path)
    assert result.returncode != 0, (
        "the gate passed a preflight that answered 2 (the exchange did not complete, so no "
        "verdict was reached) — 'could not measure' is not 'everything is fine', and this "
        f"run would have gone on to build four platforms. Output: "
        f"{(result.stdout + result.stderr).strip()[:300]!r}"
    )
    errors = _error_lines(result)
    assert errors and "answered 2" in errors[0], (
        "the gate failed without carrying the preflight's own code into the message, so a "
        "reader cannot tell a refused credential from an exchange that never reached a "
        f"verdict. Output: {(result.stdout + result.stderr).strip()[:300]!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell these steps run under in "
    "CI (measured 2026-10-02, PR #1812)",
)
def test_an_accepted_credential_lets_the_tag_run_proceed(tmp_path) -> None:
    """Direction three, the control: a gate that fails everything is not measuring anything."""
    result = _run_the_gate(0, tmp_path)
    assert result.returncode == 0, (
        "the gate failed a preflight that answered 0 (Apple accepted the credentials) — a "
        "release that cannot notarize is blocked for the wrong reason, and the gate would "
        f"have to be removed to publish. Output: "
        f"{(result.stdout + result.stderr).strip()[:300]!r}"
    )
    assert not _error_lines(result), (
        "the gate reported an ::error:: for an accepted credential: "
        f"{(result.stdout + result.stderr).strip()[:300]!r}"
    )
