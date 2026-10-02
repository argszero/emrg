"""The notarize step's parse-failure message must be reachable, not just written.

The step carries an `::error::` for exactly one case — notarytool ran, exited 0, and its
output is not the JSON the step parses:

```bash
SUB_ID="$(printf '%s' "$NOTARY_OUT" | python3 -c "...json.load(sys.stdin)...")"
STATUS="$(printf '%s' "$NOTARY_OUT" | python3 -c "...json.load(sys.stdin)...")"
if [ -z "$SUB_ID" ] || [ -z "$STATUS" ]; then
  echo "::error::notarytool 输出解析失败（无法获取提交 ID/状态），请手动运行 notarytool submit 排查。原始输出：$NOTARY_OUT"
  exit 1
fi
```

That message exists so a reader never has to guess, it prints the raw output, and it is
**unreachable**. The job's shell is `bash --noprofile --norc -eo pipefail`, and a
`VAR="$(cmd)"` assignment carries `cmd`'s status: the first parser that fails to decode
the output aborts the script **at the assignment**, so the branch below — the one written
for that state — never runs.

Measured 2026-10-02 against a submission stub that exits 0 and prints `not json at all`
(`cyc20261002-113230`, on this branch and on master alike): the step exits non-zero, and
its whole output is the raw text. Nothing says the parse failed, so the two very different
states — *notarytool printed something unexpected* and *notarytool printed nothing* — read
the same, which is the defect class this step's own comment was written to remove.

The fix is one clause per capture (`|| true`): the emptiness of the two variables is the
signal the branch below already reads, so the parsers' exit statuses carry no information
and must not be allowed to abort. The failure itself is unchanged — the step still exits 1.

The harness below runs the step body **as the runner runs it** (`--noprofile --norc
-eo pipefail`), because `-e` is the mechanism of the defect: a harness without it would
report this step correct on the tree the defect was measured on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/build-release.yml"
STEP_NAME = "Notarize pkg (macOS only)"
PKG_NAME = "EMRG-9.9.9-macos-arm64.pkg"

#: GitHub starts every `shell: bash` step with these, on Linux and macOS alike.
RUNNER_FLAGS = ["--noprofile", "--norc", "-eo", "pipefail"]

NOT_JSON = "not json at all"
ACCEPTED = '{"id":"sub-1","message":"Successfully uploaded","status":"Accepted"}'
INVALID = '{"id":"sub-1","message":"see log","status":"Invalid"}'
REJECTION = "the bundle is not signed with a valid Developer ID"

_XCRUN_STUB = """#!/bin/sh
# Stub for `xcrun`. A `sh` shebang, not `#!/usr/bin/env python3`: the child this runs in
# has whatever PATH the test process has, and a suite that silently depends on a `python3`
# *executable* on that PATH goes red on a runner where the interpreter is only reachable
# through `uv run` (the shape measured on PR #1812's windows job, `cyc20261002-113230`).
if [ "$1" = "notarytool" ] && [ "$2" = "submit" ]; then
  printf '%s\\n' "${NOTARY_SUBMIT_OUT:-}"
  exit "${NOTARY_SUBMIT_RC:-0}"
fi
if [ "$1" = "notarytool" ] && [ "$2" = "log" ]; then
  printf '%s\\n' "${NOTARY_LOG_OUT:-}"
  exit "${NOTARY_LOG_RC:-0}"
fi
echo "stub xcrun: unexpected argv: $*" >&2
exit 99
"""


def _notarize_step() -> dict:
    """The step, from the parsed workflow — never from a text search over the file.

    `find dist/artifacts ... -name 'EMRG-*-macos-*.pkg'` occurs in four steps of this job,
    so a document-level match cannot say which one was read.
    """
    path = REPO / WORKFLOW
    assert path.is_file(), f"{WORKFLOW} is missing — this guard cannot measure what it guards"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    named = [s for s in steps if isinstance(s, dict) and s.get("name") == STEP_NAME]
    assert len(named) == 1, f"expected exactly one {STEP_NAME!r} step, got {len(named)}"
    step = named[0]
    assert step.get("shell") == "bash", f"unexpected shell: {step.get('shell')!r}"
    assert isinstance(step.get("run"), str) and step["run"].strip(), "the step has no body"
    return step


def _body() -> str:
    return _notarize_step()["run"]


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "dist" / "artifacts").mkdir(parents=True)
    (tmp_path / "dist" / "artifacts" / PKG_NAME).write_bytes(b"pkg")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "xcrun"
    stub.write_text(_XCRUN_STUB, encoding="utf-8")
    stub.chmod(0o755)
    # The step parses with `python3`. Point that name at the interpreter running this
    # test, so the parse is real and the reading does not depend on the runner's PATH —
    # the dependency that made PR #1812's windows job red at cyc20261002-113230. Forward
    # slashes, because this file is executed by the POSIX shell on every platform and a
    # Windows `C:\...\python.exe` would have its backslashes read as escapes.
    interpreter = str(sys.executable).replace("\\", "/")
    py = bindir / "python3"
    py.write_text(f'#!/bin/sh\nexec "{interpreter}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return tmp_path


def run_step(
    tree: Path,
    *,
    out: str = ACCEPTED,
    rc: int = 0,
    log_out: str = REJECTION,
    body: str | None = None,
    flags: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    env = {
        "PATH": f"{tree / 'bin'}:/usr/bin:/bin",
        "APPLE_ID": "ci@example.invalid",
        "MACOS_NOTARY_APP_PASSWORD": "not-a-real-password",
        "MACOS_NOTARY_TEAM_ID": "TEAMID1234",
        "NOTARY_SUBMIT_OUT": out,
        "NOTARY_SUBMIT_RC": str(rc),
        "NOTARY_LOG_OUT": log_out,
    }
    return subprocess.run(
        [shell, *(RUNNER_FLAGS if flags is None else flags), "-c", _body() if body is None else body],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _both(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def re_search_capture(line: str) -> bool:
    """Whether this line is one of the two captures that feed the emptiness check."""
    stripped = line.strip()
    return (
        ("SUB_ID=" in stripped or "STATUS=" in stripped)
        and "$(printf" in stripped
        and "python3 -c" in stripped
    )


class TestTheParseFailureSaysSo:
    """The state the branch exists for: notarytool answered, and it was not JSON."""

    def test_unparseable_output_prints_the_parse_failure_and_the_raw_text(self, tree) -> None:
        result = run_step(tree, out=NOT_JSON)
        everything = _both(result)

        assert result.returncode != 0, (
            "an unparseable submission output must fail the step — nothing about this fix "
            f"relaxes that. Output:\n{everything}"
        )
        assert "解析失败" in everything, (
            "the step exited without its parse-failure message: the capture above aborts "
            "the script at the assignment, so the branch written for this state is "
            f"unreachable. Output was:\n{everything!r}"
        )
        assert any("解析失败" in line and NOT_JSON in line for line in everything.splitlines()), (
            "the parse-failure message must carry the raw output on its own line. Asserting "
            "the text appears *somewhere* is satisfied by the unconditional `echo "
            '"$NOTARY_OUT"` above it, so it measures nothing about the message — an arm '
            "that replaces the message's tail with 「（略）」 survives it (measured "
            f"cyc20261002-113230). Output was:\n{everything!r}"
        )
        # It must not pretend the submission was refused, nor fetch a log for an id it
        # never read: neither was observed.
        assert "notarytool log" not in everything, everything

    def test_the_parse_failure_is_named_by_the_line_that_reads_the_variables(self) -> None:
        """A pin on the shape, so the executed arm above cannot be satisfied by wording.

        The claim is that the *emptiness check* is what decides, and that it is reachable.
        A body that printed the message unconditionally (say, right after the submit) would
        pass the arm above while the real parse branch stayed dead.
        """
        body = _body()
        lines = body.splitlines()
        captures = [i for i, ln in enumerate(lines) if re_search_capture(ln)]
        assert len(captures) == 2, (
            "expected the two `id`/`status` captures, found "
            f"{len(captures)} — the guard cannot name what it read"
        )
        for index in captures:
            assert "|| true" in lines[index], (
                "a capture that feeds the emptiness check has no `|| true`: under the job's "
                "`-e` its parser's non-zero status aborts the script at the assignment, so "
                f"the check below it never runs. Line: {lines[index]!r}"
            )
        guard = [i for i, ln in enumerate(lines) if '-z "$SUB_ID"' in ln]
        assert guard, "the emptiness check the message hangs off is gone"
        assert guard[0] > captures[-1], (
            "the emptiness check no longer follows the captures it reads"
        )


class TestWhatMustNotChange:
    """The three states that were already correct, re-measured after the change."""

    def test_an_accepted_submission_still_passes(self, tree) -> None:
        result = run_step(tree, out=ACCEPTED)
        assert result.returncode == 0, _both(result)
        assert "公证通过" in _both(result), _both(result)

    def test_a_rejected_verdict_still_fetches_the_log(self, tree) -> None:
        """The state that justifies parsing at all: rc 0, status Invalid, log fetched."""
        result = run_step(tree, out=INVALID)
        everything = _both(result)

        assert result.returncode != 0, f"an Invalid verdict passed the step:\n{everything}"
        assert "status=Invalid" in everything, everything
        assert REJECTION in everything, (
            f"the rejection log was not fetched or not printed:\n{everything}"
        )

    def test_no_package_is_still_skipped(self, tree) -> None:
        (tree / "dist" / "artifacts" / PKG_NAME).unlink()
        result = run_step(tree)
        everything = _both(result)

        assert result.returncode == 0, f"a missing pkg must skip, not fail:\n{everything}"
        assert "no pkg found, skipping" in everything, everything

    def test_the_empty_string_output_reaches_the_same_message(self, tree) -> None:
        """An empty capture is the other half of the same state, and is not an error."""
        result = run_step(tree, out="")
        everything = _both(result)

        assert result.returncode != 0, everything
        assert "解析失败" in everything, (
            "an empty notarytool output must reach the parse-failure message rather than "
            f"dying at the assignment:\n{everything!r}"
        )


class TestTheHarnessReproducesTheRunnersShell:
    """`-e` is the mechanism: measure it rather than assuming it."""

    def test_the_runner_flags_are_the_ones_the_defect_needs(self) -> None:
        assert RUNNER_FLAGS == ["--noprofile", "--norc", "-eo", "pipefail"], RUNNER_FLAGS

    def test_without_the_new_clauses_the_branch_is_dead_under_dash_e(self, tree) -> None:
        """Run the pre-fix shape both ways — the reason the fix is a clause, not wording.

        `pre_fix` is the delivered body with the `|| true` clauses removed — on this tree
        that reconstructs master's shape exactly; on a tree that does not carry the fix the
        replacement is a no-op and the body already *is* that shape, so this runs green
        either way. With the runner's `-e` the message must be absent; without `-e` it must
        be present, because then the assignment's non-zero status does not abort. A harness
        that ran step bodies without the runner's flags would read the defect as fixed.
        """
        pre_fix = _body().replace(' 2>/dev/null)" || true', ' 2>/dev/null)"')

        with_flags = run_step(tree, out=NOT_JSON, body=pre_fix)
        without_flags = run_step(tree, out=NOT_JSON, body=pre_fix, flags=["--noprofile", "--norc"])

        assert with_flags.returncode != 0, "the pre-fix shape must still fail"
        assert "解析失败" not in _both(with_flags), (
            "with the runner's `-e` the pre-fix shape reached the message — then `-e` is not "
            f"the mechanism and this file's premise is wrong:\n{_both(with_flags)!r}"
        )
        assert "解析失败" in _both(without_flags), (
            "without `-e` the pre-fix shape also failed to reach the message — the abort is "
            f"not what hides it:\n{_both(without_flags)!r}"
        )
