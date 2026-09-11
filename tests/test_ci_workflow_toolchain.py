"""CI must be able to run the tool-dependent tests it declares.

Why this module exists
----------------------
The suite starts real Node runners, and some of those probes **skip** when the
toolchain is absent:

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
  file in the suite starts a real Node runner (detected from the suite's **source
  structure** - real calls, parsed with `ast` - not from a hardcoded file list and
  not from a text search, which a docstring can satisfy);
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

# Detection is AST-based, not a regex over the text. Measured during review of this
# guard: the regex form matched this module's **own docstring**, which quotes the
# skip line as an illustration - so the premise test below passed on prose while no
# probe existed anywhere in the tree. Deleting every real `pytest.skip` in the suite
# still left all four tests green. That is this repo's recurring shape one level up
# (matching a pattern *about* the thing rather than the thing): a guard whose
# evidence is its own prose cannot notice the subject disappearing.
_NODE_TOOLCHAIN_WORDS = ("npm", "node")


def _mentions_node_toolchain(node: ast.expr) -> bool:
    """Whether an argument node carries an npm/node spelling.

    Walks the node so f-strings count by their literal parts (`f"no node_modules
    under {root}"` mentions Node) - which is what the real skips look like.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            low = sub.value.lower()
            if any(word in low for word in _NODE_TOOLCHAIN_WORDS):
                return True
    return False


def _skip_on_missing_node_calls(path: Path) -> list[int]:
    """Line numbers of real `pytest.skip(...)` calls naming npm/node.

    A `skip` quoted in a docstring or a comment is text, not a call, so it does not
    satisfy the premise - which is the whole point of parsing instead of grepping.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "skip"):
            continue
        if any(_mentions_node_toolchain(arg) for arg in node.args):
            found.append(node.lineno)
    return found


def _starts_a_real_node_runner(path: Path) -> list[int]:
    """Line numbers of calls whose argv is a literal list naming npm/node.

    Covers the indirection the real probe actually uses (`mod._run(["npm", ...])`),
    not only the `subprocess.run([...])` spelling - the regex this replaced reached
    only the latter, so the "real runner" half could never match the probe it was
    written for. Measured: zero matches in `tests/test_check_node_test_count.py`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        argv = node.args[0]
        if isinstance(argv, ast.List) and any(
            _mentions_node_toolchain(elt) for elt in argv.elts
        ):
            found.append(node.lineno)
    return found


def _node_dependent_test_files() -> dict[str, list[int]]:
    """Test file -> the lines at which it starts Node or skips without npm."""
    found: dict[str, list[int]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        lines = _skip_on_missing_node_calls(path) + _starts_a_real_node_runner(path)
        if lines:
            found[path.relative_to(REPO_ROOT).as_posix()] = sorted(lines)
    return found


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
    return bool(_node_dependent_test_files())


def test_the_suite_really_does_depend_on_a_real_node_runner() -> None:
    """The premise: some test starts Node and skips when it is missing.

    Without this, the rule below could pass because the detection is broken
    (nobody needs Node) rather than because every job provides it - the two are
    indistinguishable from the verdict alone.

    The assertion is on **calls**, not on text. Its predecessor matched a regex
    over the file contents and was satisfied by this module's own docstring, so it
    stayed green with no probe left in the suite; that is recorded here because the
    difference is invisible from the verdict alone, which is the same reason this
    premise exists at all.
    """
    dependent = _node_dependent_test_files()
    assert dependent, (
        "no test file actually *calls* something that starts a real Node runner or "
        "skips without npm - if the probe was removed, this guard (and the CI setup "
        "it checks) can be retired with it. A mention of npm/node in a docstring or "
        "comment does not count"
    )
    skipping = {
        name: lines
        for name, lines in dependent.items()
        if _skip_on_missing_node_calls(REPO_ROOT / name)
    }
    assert skipping, (
        "the premise is 'the probe skips without npm', but no real skip-on-missing-"
        "node call was found - the probe may now fail loudly instead, which is "
        "better; update this guard rather than deleting it"
    )
    # And the guard must not satisfy its own premise: a rule about the suite cannot
    # be met by the rule's own file, or removing the subject leaves it green.
    assert not any(
        name == Path(__file__).name for name in skipping
    ), (
        f"{Path(__file__).name} is itself counted as a Node-dependent test file - "
        "the rule is measuring its own prose instead of the suite"
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
