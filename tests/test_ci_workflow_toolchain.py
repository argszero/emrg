"""CI must be able to run the tool-dependent tests it declares.

Why this module exists
----------------------
`tests/test_check_node_test_count.py` starts real Node runners in the probe that
matters most (`test_a_bare_name_starts_the_real_runner`), and **skips** when
`shutil.which("npm")` is `None`:

    if mod.shutil.which("npm") is None:
        pytest.skip("npm is not on PATH")

That skip is correct in isolation - a missing toolchain is not a defect in `_run`
- but it turns "does this CI job have Node?" into a silent condition. Measured
2026-09-10/11: `test-windows` ran `pytest` with **no `setup-node` step at all**,
so the probe written for Windows executed there only because the `windows-2025`
runner image happens to ship Node (22.23.2 / npm 10.9.8). The image is a
third-party input: an image update that drops Node, or a runner rollback, would
convert the assertion that covers the #1132 defect into a skip, and the job would
stay green while no longer testing the thing it was fixed for.

This is the same class the repo has already been bitten by once (#1125's note in
`test.yml`: the node-count gate lived in the ubuntu job only while the defects it
guards were Windows-only). A gate that *can* silently not run is not a gate.

The rule, and its boundary
--------------------------
* a job that runs the test suite **must set up the Node toolchain** if any test
  file in the suite starts a real Node runner (detected from the source, not from
  a hardcoded file list);
* a job is exempt when it is declared not to need Node - `test-windows` was, until
  this guard's first run, exactly that. The exemption is explicit and asserted
  live, so it cannot go stale silently.

Deliberately **not** asserted: that every job installs `node_modules`. `uv run
pytest` in both jobs runs before any `npm ci` on purpose (the pytest guards are
static; the runtime pairing lives in the node-count gate), and the runner-probe
fixture uses a temporary directory so it does not need the real tree's modules.
Node itself, though, has to be there - otherwise the probe is a skip.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
TESTS = REPO_ROOT / "tests"

# A call that would fail (not skip) if the Node toolchain is absent. Detected by
# this marker rather than by the test's name, so renaming a probe cannot silently
# drop it out of the rule's reach.
_SKIP_ON_MISSING_NODE = re.compile(
    r"""pytest\.skip\([^)]*(?:npm|node)[^)]*\)""", re.I
)
_REAL_NODE_RUNNER = re.compile(r"""subprocess\.run\(\s*\[[^\]]*["'](?:npm|node)["']""")


def _workflow_jobs() -> dict[str, str]:
    """Job name -> raw job text, parsed from the workflow by indentation.

    A real YAML parser is not a dependency of this repo's pytest job, and the
    shape needed here ("which steps belong to which job") is exactly one level of
    indentation. The parse is asserted non-trivial below, so a workflow that stops
    matching the expected shape fails loudly instead of yielding zero jobs.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    jobs: dict[str, list[str]] = {}
    in_jobs = False
    current: str | None = None
    for line in lines:
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
            continue
        if current is not None:
            jobs[current].append(line)
    return {name: "\n".join(body) for name, body in jobs.items()}


def test_the_workflow_parse_finds_the_jobs_and_their_steps() -> None:
    """Positive control for the parser: a mis-parse must not read as "no jobs".

    Every rule below is of the form "for each job, ...". If the parse returned an
    empty mapping, all of them would pass vacuously - the standard way a
    structure guard rots. So the parser is pinned against the real file first.
    """
    jobs = _workflow_jobs()
    assert set(jobs) >= {"test", "test-windows"}, (
        f"the workflow parse lost a job - the rules below would pass vacuously; "
        f"got {sorted(jobs)}"
    )
    assert "runs-on" in jobs["test"], "steps were not attached to the `test` job"
    assert "actions/checkout" in jobs["test"], "the `test` job's steps look empty"
    # And the boundary: content before `jobs:` must not be attributed to a job.
    assert "pull_request" not in jobs["test"], (
        "the parse leaked the `on:` block into the first job"
    )


def _suite_starts_a_real_node_runner() -> bool:
    """Whether any test file would start a real Node process (and skip without it)."""
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if _SKIP_ON_MISSING_NODE.search(source) or _REAL_NODE_RUNNER.search(source):
            return True
    return False


def test_the_suite_really_does_depend_on_a_real_node_runner() -> None:
    """The premise: some test starts Node and skips when it is missing.

    Without this, the rule below could pass because the detection is broken
    (nobody needs Node) rather than because every job provides it - the two are
    indistinguishable from the verdict alone.
    """
    assert _suite_starts_a_real_node_runner(), (
        "no test file was found that starts a real Node runner or skips without "
        "npm - if the probe was removed, this guard (and the CI setup it checks) "
        "can be retired with it"
    )
    skipping = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted(TESTS.glob("test_*.py"))
        if _SKIP_ON_MISSING_NODE.search(p.read_text(encoding="utf-8"))
    ]
    assert skipping, (
        "the premise is 'the probe skips without npm', but no skip-on-missing-node "
        "call was found - the probe may now fail loudly instead, which is better; "
        "update this guard rather than deleting it"
    )


# Jobs declared not to exercise the Node-dependent probes. Empty by design: a
# claim that a job does not need the toolchain has to be argued for here.
_NO_NODE_NEEDED: dict[str, str] = {}

# Markers that a job provides the Node toolchain. `setup-node` is the explicit
# form; using Node as a *step command* counts too, since that requires it on PATH.
_NODE_SETUP = re.compile(r"actions/setup-node|^\s*-?\s*run:\s*(?:npm|node|npx)\b", re.M)


def test_every_job_that_runs_the_suite_can_start_the_node_runner() -> None:
    """A job running `pytest tests/` must have Node, or be explicitly exempt.

    This is the rule that would have caught the #1132 setup: `test-windows` ran
    the suite on the platform the defect belonged to, and the probe covering it
    only ran by runner-image coincidence.
    """
    offenders: list[str] = []
    for name, body in _workflow_jobs().items():
        runs_suite = re.search(r"pytest\s+tests/", body) is not None
        if not runs_suite or name in _NO_NODE_NEEDED:
            continue
        if _NODE_SETUP.search(body) is None:
            offenders.append(name)

    assert not offenders, (
        "these jobs run `pytest tests/` but never set up the Node toolchain: "
        f"{offenders}. The suite contains a probe that starts a real `npm` and "
        "SKIPS when it is missing, so on such a job the assertion covering the "
        "#1132 Windows argv defect silently does not run - and a green job then "
        "does not mean what it appears to. Add `actions/setup-node`, or list the "
        "job in _NO_NODE_NEEDED with the reason it genuinely cannot need it."
    )


def test_every_exemption_is_rationale_backed_and_still_needed() -> None:
    """An exemption must carry a reason, and must still describe a real job.

    Same discipline as `_CONSOLE_DECODE_ALLOWED` in the decode guard: an
    allowlist without a live check is a blind spot that widens on its own.
    """
    jobs = _workflow_jobs()
    for name, reason in _NO_NODE_NEEDED.items():
        assert name in jobs, (
            f"{name} is exempted from the Node-toolchain rule but no such job "
            f"exists any more - remove the exemption (reason given: {reason})"
        )
        assert reason.strip(), f"{name} is exempted with an empty reason"
