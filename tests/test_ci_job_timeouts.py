"""Every CI job declares a ceiling, so a hang becomes a failure instead of hours of `in_progress`.

Why this module exists
----------------------
`.github/workflows/test.yml` declared no `timeout-minutes` on either job, so both inherited the
platform default. Nothing outside the run distinguishes a hung job from a slow runner: a job that
never finishes keeps reporting `in_progress` until the platform stops it, and the reading that
governs a pull request — its checks — cannot conclude. A pull request whose check has not
concluded cannot be acted on, so the first evidence of a hang is a merge gate that says nothing
for hours.

The state that *does* have an action is a red leg, which is readable to a cause. A ceiling below
the platform default converts an unbounded wait into exactly that.

Both bounds are asserted, because either one alone is a defect
-------------------------------------------------------------
A ceiling **at or above the platform default** changes nothing — it is the default wearing a
number. A ceiling **tight enough to fire on a normal run** is worse than no ceiling at all: it
replaces an unbounded wait with a false red, and a red leg is not votable, so a value chosen too
tight would freeze every open pull request the way the defect it was meant to cure does.

The subject is `test.yml`, and the boundary is stated rather than implied
-----------------------------------------------------------------------
The jobs in `build-release.yml` are deliberately outside this guard. A tag build signs and
notarizes with real secrets and takes its own kind of time; a ceiling there needs its own
measurement rather than a value borrowed from the test jobs, and borrowing one would red a
release over a number nobody measured for it.

Parsed YAML, not a text search
------------------------------
`test.yml` explains this defect in its own comments, so a substring search over the file matches
its own prose — the failure this repository has already recorded twice (`tests/test_ci_workflow_toolchain.py`,
`tests/test_test_workflow_covers_pr_bases.py`): a guard whose evidence is its own prose cannot
notice the subject disappearing. The job table is read from the parsed document instead.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

#: The ceiling every job inherits when it declares none. GitHub's own default, and the number a
#: declared value has to be strictly below to be worth declaring.
PLATFORM_DEFAULT_MINUTES = 360

#: The smallest ceiling that still reports a slow runner as slow. The legs here take minutes; a
#: value under this window fires on a normal run and turns a slow runner into a false red.
FLOOR_MINUTES = 20


def _jobs(path: Path = WORKFLOW) -> dict:
    """The job table of a workflow document.

    `yaml.safe_load` parses the bare `on:` key as the boolean `True` (YAML 1.1), so this reads
    `jobs:` by name rather than treating every top-level key as a job.
    """
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = spec.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} declares no jobs to read"
    return jobs


def _ceiling_problem(job: object, platform_default: int = PLATFORM_DEFAULT_MINUTES) -> str | None:
    """Why one job's ceiling is unusable — presence, form, and the upper bound.

    Returns a description rather than a boolean so a failure names the value it read: "the job
    lost its ceiling" and "the ceiling was set to a number that does nothing" are different
    defects and a boolean reports neither.

    The lower bound is deliberately **not** here: it is a separate reading with its own failure
    message (`_tight_ceilings`), so a value chosen too tight reddens the test that is named for
    it rather than the one named for the platform default.
    """
    if not isinstance(job, dict):
        return "is not a mapping, so it declares nothing this guard can read"
    if "timeout-minutes" not in job:
        return (
            "declares no `timeout-minutes`, so it inherits the platform default and a hang "
            f"reports `in_progress` for up to {platform_default} minutes"
        )
    value = job["timeout-minutes"]
    # `isinstance(True, int)` is True in Python, and YAML 1.1 would parse a bare `true` as a
    # boolean, so the boolean case is rejected by name before the integer check.
    if isinstance(value, bool) or not isinstance(value, int):
        return f"`timeout-minutes: {value!r}` is not a whole number of minutes"
    if value <= 0:
        return f"`timeout-minutes: {value}` is not a positive number of minutes"
    if value >= platform_default:
        return (
            f"`timeout-minutes: {value}` is not below the platform default of "
            f"{platform_default}, so it changes nothing"
        )
    return None


def _tight_ceilings(jobs: dict, floor: int = FLOOR_MINUTES) -> dict:
    """The jobs whose ceiling is a usable number but too small for a normal run."""
    tight = {}
    for name, spec in jobs.items():
        if not isinstance(spec, dict):
            continue
        value = spec.get("timeout-minutes")
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 0 < value < floor:
            tight[name] = value
    return tight


def test_every_job_declares_a_ceiling_below_the_platform_default() -> None:
    """Presence, form, and the upper bound: a job without a usable ceiling is the defect."""
    jobs = _jobs()
    problems = {name: problem for name, spec in jobs.items() if (problem := _ceiling_problem(spec))}
    assert not problems, (
        "every job in test.yml must declare `timeout-minutes` as a whole number of minutes "
        "below the platform default; without it a hung job holds its pull request at "
        f"`in_progress` until the {PLATFORM_DEFAULT_MINUTES}-minute default fires. Offending "
        f"job(s): {problems}"
    )


def test_no_ceiling_fires_on_a_normal_run() -> None:
    """The lower bound, its own reading: too tight is the worse of the two failures.

    A red leg cannot be voted on, so a ceiling that fires on a normal run freezes every open
    pull request — the same outcome the missing ceiling produces, reached faster.
    """
    tight = _tight_ceilings(_jobs())
    assert not tight, (
        f"these ceilings are under the {FLOOR_MINUTES}-minute floor and would fire on a normal "
        f"run: {tight} — a slow runner has to report as slow, not as a hang"
    )


def test_the_scan_reports_absence_and_rejects_each_bad_form() -> None:
    """The instrument's control: every rejection above has to be reachable.

    A guard that reports the healthy answer whatever it is given is not a guard. Each form is a
    value this file's own job table could carry, and each must be named as a problem by the
    reader that owns it.
    """
    assert _ceiling_problem({"runs-on": "ubuntu-latest"}) is not None, "absence read as healthy"
    assert _ceiling_problem({"timeout-minutes": True}) is not None, "a boolean read as an integer"
    assert _ceiling_problem({"timeout-minutes": "30"}) is not None, "a quoted number read as one"
    assert _ceiling_problem({"timeout-minutes": 0}) is not None, "zero read as a ceiling"
    assert _ceiling_problem({"timeout-minutes": -5}) is not None, "a negative read as a ceiling"
    assert _ceiling_problem({"timeout-minutes": 360}) is not None, "the default read as a ceiling"
    assert _ceiling_problem({"timeout-minutes": 400}) is not None, "above the default read as fine"
    assert _ceiling_problem("not a mapping") is not None, "a non-mapping read as healthy"

    # The floor has its own reader, so it gets its own control: a tight value has to be visible
    # there, and a missing or unusable value has to stay out of it rather than be double-counted.
    assert _tight_ceilings({"test": {"timeout-minutes": 5}}) == {"test": 5}
    assert _tight_ceilings({"test": {"timeout-minutes": 30}}) == {}
    assert _tight_ceilings({"test": {"runs-on": "ubuntu-latest"}}) == {}
    assert _tight_ceilings({"test": {"timeout-minutes": True}}) == {}
    assert _tight_ceilings({"test": "not a mapping"}) == {}

    # And the healthy forms, so the rejections above are not the only thing these readers say.
    assert _ceiling_problem({"timeout-minutes": FLOOR_MINUTES}) is None
    assert _ceiling_problem({"timeout-minutes": PLATFORM_DEFAULT_MINUTES - 1}) is None
    assert _tight_ceilings({"test": {"timeout-minutes": FLOOR_MINUTES}}) == {}
