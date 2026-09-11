"""A base branch can be any branch, not just `master`.

Why this module exists
----------------------
`test.yml` declared `pull_request: branches: [master]`, so a PR whose base is
another branch got **no `pull_request` run at all** - the gate ran only when a
human remembered `gh workflow run test.yml`, and nothing said so.

Measured 2026-09-11 (`cyc20260911-202014`): `#1148` (base
`feature/conflict-classifier-multiline-count`) had **zero** check runs from the
`pull_request` event - both of its runs came from `gh workflow run test.yml`
dispatched by hand during review - while its parent `#1147` (base `master`) was
double-green automatically. The blind spot is silent by construction: the
check-run-based vote helper reads a missing check as **no vote recorded** rather
than as a failure, so the missing gate biases the merge gate and is invisible in
every green dashboard. GitHub's own default for a `pull_request` trigger with no
`branches` filter is "every base branch", so the filter narrowed a default in a way
that silently exempted the repo's stacked-PR workflow.

The rule, and its boundary
--------------------------
* every workflow that runs on `pull_request` **must not narrow the trigger by base
  branch**. The assertion is on parsed YAML, not text: the file quotes this very
  shape in a comment, and a substring search over the file matches its own prose
  (the failure mode `tests/test_ci_workflow_toolchain.py` records one level up - a
  guard whose evidence is its own prose cannot notice the subject disappearing).
* `master` **push** is still allowed to be the only push trigger: PRs are the
  branch-side gate, and the post-merge run on master is a different signal.

Deliberately **not** asserted: that any particular workflow exists, or what it
runs. Only the trigger's reach is pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _pull_request_triggeres() -> list[tuple[Path, object]]:
    """(path, pull_request trigger value) for every workflow that reacts to PRs."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        # PyYAML parses the bare `on:` key as the boolean True (YAML 1.1).
        triggers = spec.get("on", spec.get(True))
        if not isinstance(triggers, dict):
            continue
        if "pull_request" in triggers:
            found.append((path, triggers["pull_request"]))
    return found


def test_at_least_one_workflow_reacts_to_pull_requests() -> None:
    """Negative control: the premise must be real, or the rule below is vacuous."""
    assert _pull_request_triggeres(), "no workflow declares a pull_request trigger"


def test_no_pull_request_trigger_is_narrowed_by_base_branch() -> None:
    offenders = []
    for path, trigger in _pull_request_triggeres():
        # A trigger with no options at all, or with any option other than a
        # branches filter, is fine - `pull_request:` and `pull_request: {types: ...}`
        # both mean "every base branch".
        if isinstance(trigger, dict) and "branches" in trigger:
            offenders.append(f"{path.name}: {trigger['branches']}")
    assert not offenders, (
        "these PR triggers restrict the base branch, so a PR based on another "
        "branch gets no CI run (a stacked PR gets none): "
        + "; ".join(offenders)
        + " - drop the `branches` filter to cover every base, as GitHub's default "
        "does"
    )


@pytest.mark.parametrize(
    "trigger,should_be_an_offender",
    [
        # GitHub's defaults: absent key, empty mapping, `types` only.
        (None, False),
        ({}, False),
        ({"types": ["opened", "synchronize"]}, False),
        # The measured shape, with the list and with a bare scalar.
        ({"branches": ["master"]}, True),
        ({"branches": "master"}, True),
    ],
)
def test_the_detector_fires_on_the_narrowed_shape_and_only_that(
    trigger: object, should_be_an_offender: bool
) -> None:
    """The predicate itself, both ways - a rule nobody can trip is not a rule."""
    is_offender = isinstance(trigger, dict) and "branches" in trigger
    assert is_offender is should_be_an_offender
