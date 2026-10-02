"""The notarize step must print what notarytool said, including when it fails.

Measured defect (issue #1811, 2026-10-02, tag `v0.3.8`, run `36956685533`, macOS job):
the `Notarize pkg` step failed **3 seconds** after it started, with `##[error]Process
completed with exit code 1` and **nothing else** — no `xcrun`, no Apple reply, no hint
whether the submission had been refused or `notarytool` was simply absent. The step is
written to do the opposite: its own comment says an `Invalid` verdict still exits 0, so
the body parses `status` and fetches Apple's rejection log rather than letting a cryptic
error hide the cause. But an earlier failure destroys all of it:

    NOTARY_OUT="$(xcrun notarytool submit ... 2>&1)"
    echo "$NOTARY_OUT"

The runner starts this step as `bash --noprofile --norc -eo pipefail`, and a
`VAR="$(cmd)"` assignment carries `cmd`'s status — so under `-e` the script aborts **at
the assignment**, and the `echo` that exists to publish `$NOTARY_OUT` is unreachable
exactly when `$NOTARY_OUT` holds something worth reading. The captured diagnostic is
thrown away unprinted.

The harness below therefore does two things the existing release tests do not:

* it runs the step body with **the flags the runner uses** (`--noprofile --norc -eo
  pipefail`), because the defect is a property of `-e` and a harness that executes a
  step body without it measures a different program. The mutation arm recorded in this
  cycle proves the flag is load-bearing: dropping the `|| NOTARY_RC=$?` guard from the
  fixed body makes this file go red *only* because `-e` is present.
* it makes `xcrun` a stub that can **refuse the submission**, so the failing direction
  is executed rather than reasoned about.

Acceptance (issue #1811): a refused submission prints its captured output *and* still
fails; an `Accepted` submission behaves exactly as before; a non-`Accepted` status still
fetches the rejection log.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/build-release.yml"
STEP_NAME = "Notarize pkg (macOS only)"

#: The shell the runner starts a `shell: bash` step with, on Linux and macOS alike.
#: GitHub writes it as `bash --noprofile --norc -eo pipefail {0}`; `-c` in place of the
#: file is the only difference here.
RUNNER_FLAGS = ["--noprofile", "--norc", "-eo", "pipefail"]

ACCEPTED = (
    '{"id":"a1b2c3d4-0000-1111-2222-333344445555",'
    '"message":"Successfully uploaded file","status":"Accepted"}'
)
INVALID = (
    '{"id":"a1b2c3d4-0000-1111-2222-333344445555",'
    '"message":"See log for the reasons","status":"Invalid"}'
)
#: The shape of a submission notarytool itself refuses: no JSON at all, non-zero rc.
REFUSED = "Error: HTTP status code: 401. Invalid credentials"

_XCRUN_STUB = """#!/bin/sh
# Stub for `xcrun`: the notarize step is what is under test, not Apple.
if [ "$1" = "notarytool" ] && [ "$2" = "submit" ]; then
  if [ -n "${NOTARY_SUBMIT_OUT:-}" ]; then printf '%s\\n' "$NOTARY_SUBMIT_OUT" >&2; fi
  exit "${NOTARY_SUBMIT_RC:-0}"
fi
if [ "$1" = "notarytool" ] && [ "$2" = "log" ]; then
  printf '%s\\n' "notarization log: the package uses a deprecated API"
  exit "${NOTARY_LOG_RC:-0}"
fi
echo "stub xcrun: unexpected argv: $*" >&2
exit 99
"""


def _notarize_step() -> dict:
    """The step, read from the parsed workflow — never by matching the document's text.

    Two other jobs here carry a notarize-shaped `if:` and the neighbours (`Sign pkg`,
    `Staple pkg`) share its first line, so a text search cannot say which step lost the
    guard; the parsed step can.
    """
    path = REPO / WORKFLOW
    assert path.is_file(), f"{WORKFLOW} is missing — this guard cannot measure what it guards"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == STEP_NAME]
    assert len(named) == 1, f"expected exactly one {STEP_NAME!r} step, got {len(named)}"
    step = named[0]
    assert step.get("shell") == "bash", (
        f"the notarize step no longer runs under bash (shell={step.get('shell')!r}); the "
        "flags this file pins describe bash"
    )
    assert isinstance(step.get("run"), str) and step["run"].strip(), "the step has no body"
    return step


def _body() -> str:
    return _notarize_step()["run"]


def _contains(text: str, needle: str) -> bool:
    return needle in text


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A checkout-shaped tree: the workflow's own relative paths, and stubs on PATH."""
    (tmp_path / "dist" / "artifacts").mkdir(parents=True)
    (tmp_path / "dist" / "artifacts" / "EMRG-9.9.9-macos-arm64.pkg").write_text(
        "pkg", encoding="utf-8"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    xcrun = bindir / "xcrun"
    xcrun.write_text(_XCRUN_STUB, encoding="utf-8")
    xcrun.chmod(0o755)
    # The step parses JSON with `python3`. A shim to the interpreter running this test
    # keeps the parse real while making the harness independent of what the runner's PATH
    # happens to call python3 (the step's own parse-failure branch covers its absence).
    py = bindir / "python3"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return tmp_path


def run_notarize(
    tree: Path,
    *,
    rc: int = 0,
    out: str = ACCEPTED,
    log_rc: int = 0,
    flags: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the step body the way the runner does, with `xcrun` stubbed."""
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    bindir = tree / "bin"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "APPLE_ID": "ci@example.invalid",
        "MACOS_NOTARY_APP_PASSWORD": "not-a-real-password",
        "MACOS_NOTARY_TEAM_ID": "TEAMID1234",
        "NOTARY_SUBMIT_RC": str(rc),
        "NOTARY_SUBMIT_OUT": out,
        "NOTARY_LOG_RC": str(log_rc),
    }
    return subprocess.run(
        [shell, *(RUNNER_FLAGS if flags is None else flags), "-c", _body()],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _both(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


class TestARefusedSubmissionIsStillReadable:
    """The defect: the run that most needs a reason is the run that prints none."""

    def test_the_captured_output_is_printed_when_the_submission_fails(self, tree) -> None:
        result = run_notarize(tree, rc=1, out=REFUSED)
        everything = _both(result)

        assert result.returncode != 0, (
            "a refused submission must still fail this step — the fix is diagnostic, "
            f"never a relaxation. rc={result.returncode}\n{everything}"
        )
        # The discriminating assertion: on the unfixed step the `-e` abort happens at the
        # assignment, so this text exists only inside a variable nobody printed.
        assert _contains(everything, REFUSED), (
            "the step failed without printing notarytool's own reply — the captured output "
            f"is the only thing that says *why*. Output was:\n{everything!r}"
        )

    def test_the_reason_says_the_submission_itself_failed(self, tree) -> None:
        """Not just any message: the one that separates refused-submission from a verdict.

        A refused submission and an unparseable one both leave `$NOTARY_OUT` without a
        status, so the *paragraph* is what tells the reader which of the two happened.
        """
        result = run_notarize(tree, rc=1, out=REFUSED)
        everything = _both(result)

        assert "notarytool submit 自身失败" in everything, (
            "the step failed without naming the cause it observed (the submission itself "
            f"exited non-zero), so the log still cannot tell the two apart:\n{everything}"
        )
        # And it must not send the reader down the rejection-log path: there is no
        # submission id to fetch a log for.
        assert "notarytool log" not in everything, (
            f"a refused submission has no submission id, so no log can be fetched:\n{everything}"
        )

    def test_a_three_second_refusal_never_reaches_the_accepted_line(self, tree) -> None:
        result = run_notarize(tree, rc=1, out=REFUSED)
        assert "公证通过" not in _both(result), "a refused submission reported success"


class TestTheBehaviourThatMustNotChange:
    """The other three states were already right; a fix that moves them is a regression."""

    def test_an_accepted_submission_succeeds_and_says_so(self, tree) -> None:
        result = run_notarize(tree, rc=0, out=ACCEPTED)
        everything = _both(result)

        assert result.returncode == 0, f"an Accepted submission failed:\n{everything}"
        assert "公证通过" in everything, everything
        assert "notarytool submit 自身失败" not in everything, everything

    def test_a_non_accepted_status_fetches_the_rejection_log(self, tree) -> None:
        result = run_notarize(tree, rc=0, out=INVALID)
        everything = _both(result)

        assert result.returncode != 0, f"an Invalid verdict passed the step:\n{everything}"
        assert "status=Invalid" in everything, everything
        assert "notarization log: the package uses a deprecated API" in everything, (
            f"the rejection log was not fetched or not printed:\n{everything}"
        )

    def test_output_without_a_status_still_refuses_and_prints_the_raw_text(self, tree) -> None:
        result = run_notarize(tree, rc=0, out="not json at all")
        everything = _both(result)

        assert result.returncode != 0, everything
        assert "解析失败" in everything, everything
        assert "not json at all" in everything, everything

    def test_no_package_is_skipped_not_failed(self, tree) -> None:
        """The guard the step already had: an empty artifacts dir is not an error."""
        (tree / "dist" / "artifacts" / "EMRG-9.9.9-macos-arm64.pkg").unlink()
        result = run_notarize(tree)
        everything = _both(result)

        assert result.returncode == 0, f"a missing pkg must skip, not fail:\n{everything}"
        assert "no pkg found, skipping" in everything, everything


class TestTheHarnessReproducesTheRunnersShell:
    """The defect is a property of `-e`; a harness without it measures another program."""

    def test_the_flags_are_the_ones_the_defect_needs(self) -> None:
        """Stated once, where the run below reads them, and asserted rather than assumed."""
        assert RUNNER_FLAGS == ["--noprofile", "--norc", "-eo", "pipefail"], RUNNER_FLAGS

    def test_the_unfixed_shape_only_leaks_without_dash_e(self, tree) -> None:
        """Run the *pre-fix* shape both ways, so the flag's role is measured, not claimed.

        `pre_fix` is the delivered body with the added guard removed — on the fixed tree
        that reconstructs the failing statement exactly as the issue reports it, and on
        an unfixed tree the body already is that shape, so this runs green either way.
        What it asserts is about `-e`: without it the assignment's non-zero status runs
        on, the `echo` executes and the reply is printed; with it, nothing is. A harness
        that ran step bodies without the runner's flags would therefore report this step
        as correct on the very tree the issue was opened against — which is why the
        assertions above are only meaningful next to this one.
        """
        pre_fix = _body().replace(" || NOTARY_RC=$?", "").replace(
            ' 2>/dev/null)" || true', ' 2>/dev/null)"'
        )
        shell = shutil.which("bash")
        if shell is None:
            pytest.skip("no POSIX shell is available for the ground-truth run")

        def run(flags: list[str]) -> str:
            env = {
                "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
                "APPLE_ID": "ci@example.invalid",
                "MACOS_NOTARY_APP_PASSWORD": "not-a-real-password",
                "MACOS_NOTARY_TEAM_ID": "TEAMID1234",
                "NOTARY_SUBMIT_RC": "1",
                "NOTARY_SUBMIT_OUT": REFUSED,
            }
            done = subprocess.run(
                [shell, *flags, "-c", pre_fix],
                cwd=tree,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return _both(done)

        with_runner_flags = run(RUNNER_FLAGS)
        without_dash_e = run(["--noprofile", "--norc"])

        assert REFUSED not in with_runner_flags, (
            "with the runner's `-e` the pre-fix shape did print the reply — either the "
            "reconstruction above left a guard in place (then it is not the pre-fix shape "
            "and this test proves nothing) or `-e` no longer aborts the assignment:\n"
            f"{with_runner_flags!r}"
        )
        assert REFUSED in without_dash_e, (
            "without `-e` the pre-fix shape also failed to print — then `-e` is not the "
            f"cause and this file's premise is wrong:\n{without_dash_e!r}"
        )
