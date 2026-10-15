#!/usr/bin/env python3
"""Check that every CI leg's Python-suite step finishes inside the budget.

The rule, and where it came from
--------------------------------
Rant 2026-10-15T11:08:07 (issue #2296, requirement R4). The same serial
`uv run pytest tests/ -v` ran on all six legs and took **16m55s on the Windows leg**
against 4m12s on Linux; the host's ruling was that "macOS cannot produce Windows
numbers" is not an excuse for leaving it unmeasured, and that the final check belongs
to CI. So the number this guard reads has to *be* CI's, and the only place CI's number
exists is the run's own record.

What it reads, and what it may never read
-----------------------------------------
The duration comes from the GitHub API's own step records: each job carries
`steps[]`, and each step carries `started_at` / `completed_at`. The number is
therefore the runner's own, not one copied into this file — the defect class this
repository calls "a derived number written where a guard can measure it". The single
duration written down here is `BUDGET_SECONDS`, and that is the *rule* the rant
states, not a measurement.

Nothing is parsed out of the log text. Line timestamps would work, but the log for a
four-platform matrix is megabytes per run and the API already hands over the two
instants, so the reading is the platform's own arithmetic rather than this file's
reconstruction of it.

Three states, the family's contract
-----------------------------------
* **0** — every suite step found is within the budget, and every declared workflow
  contributed a run this guard could read.
* **1** — at least one leg is over budget. The offending legs are named with their
  measured seconds.
* **2** — could not measure, with the reason. A workflow whose latest run contributes
  no readable suite step lands here rather than passing: a leg nobody could read is
  not a leg that passed, and reporting it as one is the failure this whole family
  exists to prevent.

Usage
-----
    uv run --no-sync python3 scripts/check-suite-durations.py
    uv run --no-sync python3 scripts/check-suite-durations.py --run 38679700467
    uv run --no-sync python3 scripts/check-suite-durations.py --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime

#: The repository whose runs are read, when none is named.
DEFAULT_REPO = "argszero/emrg"

#: The rule, in seconds: the rant asks for the whole suite inside three minutes on
#: every leg. This is the one duration this file is allowed to state — every figure in
#: the *reading* below comes from the API.
BUDGET_SECONDS = 180

#: The workflows whose suite steps the rule covers. `test.yml` runs on every pull
#: request and on master (ubuntu + windows, two legs); `build-release.yml` runs on a
#: tag and carries the four-platform matrix, so between them they are the six legs the
#: rant measured. Named rather than discovered, because a workflow that quietly left
#: this tuple would leave the rule without a subject — and that is exactly what the
#: per-workflow "contributed nothing" refusal below turns into rc 2.
WORKFLOWS = ("test.yml", "build-release.yml")

#: How a suite step is recognised in the API's step list. A step's API `name` is its
#: `name:` when the workflow gave it one, and the run command when it did not — which is
#: why the spellings differ across the six legs and why this is a list rather than one
#: word. Read off the two workflows: the ubuntu step is unnamed (`uv run pytest tests/
#: -v -n auto --dist loadfile`), and the two named ones are `Python tests (Windows)` and
#: `Python tests (regression gate)`.
#:
#: A rename that hid a step from this list would turn that workflow's reading into
#: "contributed nothing" (rc 2, never a pass), and
#: `tests/test_check_suite_durations.py::test_every_declared_workflow_carries_a_step_this_guard_can_see`
#: fails at the tree level before CI is ever asked.
SUITE_STEP_MARKERS = ("pytest", "python tests")


class Unmeasurable(Exception):
    """The reading could not be taken. Carries the reason the refusals print."""


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    `args` excludes the program name, which is prepended here so no call site can omit
    it. The failure is raised as `Unmeasurable` rather than returned as an empty
    document: an unreadable answer must reach the caller as "could not measure", and a
    `{}` a caller could mistake for "no runs" would print a pass.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError:
        raise Unmeasurable(
            "gh is not available, so no CI run can be read "
            "(install it, or run this where gh is on PATH)"
        ) from None
    except subprocess.TimeoutExpired:
        raise Unmeasurable(f"gh timed out reading: gh {' '.join(args)}") from None
    if proc.returncode != 0:
        # `gh api` exits 1 for a 404 as well as for a real failure, so the status is
        # read out of the message: a 404 means the run is not there, which is a
        # different sentence from "the network refused".
        raise Unmeasurable(
            f"gh failed (rc={proc.returncode}) reading `gh {' '.join(args)}`: "
            f"{proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Unmeasurable(f"gh returned no JSON reading `gh {' '.join(args)}`: {exc}") from None


def parse_stamp(value: object) -> datetime | None:
    """An ISO-8601 instant from the API, or `None` when it cannot be read.

    GitHub sends `...Z`; `fromisoformat` takes it from 3.11 on, and the explicit
    replacement keeps the reading identical on a build that does not.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def step_seconds(step: dict) -> float | None:
    """How long a step took, or `None` when the artefact does not say.

    A step that has not finished carries `completed_at: null`, and one that was
    skipped carries no instants at all. Both are unreadable rather than zero: a step
    that did not run has no duration, and reporting 0.0s for it would be this guard
    inventing the very number it exists to measure.
    """
    started = parse_stamp(step.get("started_at"))
    completed = parse_stamp(step.get("completed_at"))
    if started is None or completed is None:
        return None
    seconds = (completed - started).total_seconds()
    return seconds if seconds >= 0 else None


def is_suite_step(step: dict) -> bool:
    """Whether this step is one that runs the Python suite."""
    name = step.get("name")
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    return any(marker in lowered for marker in SUITE_STEP_MARKERS)


def latest_completed_run(repo: str, workflow: str) -> dict:
    """The most recent finished run of `workflow`, newest first.

    Deliberately **not** filtered by branch. `build-release.yml` is triggered by a tag
    rather than by a branch, so a `branch=master` filter would silently return nothing
    for that workflow and the four matrix legs would leave the reading without anyone
    noticing — the failure mode is an empty list, which reads as success. Which run was
    read is printed before the verdict instead, so the subject is always nameable.
    """
    payload = _gh_json([
        "api",
        f"repos/{repo}/actions/workflows/{workflow}/runs?status=completed&per_page=1",
    ])
    if not isinstance(payload, dict):
        raise Unmeasurable(f"{workflow}: the runs list was not an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not runs:
        raise Unmeasurable(f"{workflow}: no completed run to read")
    run = runs[0]
    if not isinstance(run, dict) or "id" not in run:
        raise Unmeasurable(f"{workflow}: the newest run carries no id")
    return run


def run_jobs(repo: str, run_id: object) -> list[dict]:
    """Every job of a run, in the order the API returned them."""
    payload = _gh_json(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"])
    if not isinstance(payload, dict):
        raise Unmeasurable(f"run {run_id}: the jobs list was not an object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise Unmeasurable(f"run {run_id}: the jobs list was not a list")
    return [job for job in jobs if isinstance(job, dict)]


def read_legs(repo: str, run: dict) -> list[dict]:
    """The suite steps of one run, each with its leg's job name and its duration."""
    legs = []
    for job in run_jobs(repo, run.get("id")):
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not is_suite_step(step):
                continue
            legs.append({
                "job": job.get("name") or "(unnamed job)",
                "step": step.get("name") or "",
                "seconds": step_seconds(step),
                # Carried raw so a reader can check this file's arithmetic against the
                # platform's own two instants, and so a PR can quote them without
                # re-asking the API.
                "started_at": step.get("started_at"),
                "completed_at": step.get("completed_at"),
                "conclusion": job.get("conclusion"),
            })
    return legs


def collect(repo: str, label: str, run: dict) -> tuple[dict, list[dict]]:
    """One run's suite-step readings, with `label` naming the workflow it came from."""
    legs = read_legs(repo, run)
    if not legs:
        raise Unmeasurable(
            f"{label} run {run.get('id')}: no step this guard can recognise as the "
            f"Python suite (markers: {', '.join(SUITE_STEP_MARKERS)}) — a leg nobody "
            "could read is not a leg that passed"
        )
    return run, legs


def subjects(repo: str, workflows: tuple[str, ...], run_id: str | None) -> list[tuple[str, dict]]:
    """The `(label, run)` pairs to read.

    With `--run` the subject is that one run, read **once**: a run belongs to exactly
    one workflow, so looping the declared workflows would read the same jobs once per
    workflow and report every leg as many times as the tuple has entries.
    """
    if run_id is not None:
        payload = _gh_json(["api", f"repos/{repo}/actions/runs/{run_id}"])
        if not isinstance(payload, dict) or "id" not in payload:
            raise Unmeasurable(f"run {run_id}: no such run")
        label = payload.get("path") or payload.get("name") or f"run {run_id}"
        return [(str(label), payload)]
    return [(workflow, latest_completed_run(repo, workflow)) for workflow in workflows]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read each CI leg's Python-suite step duration from the GitHub API and "
            f"assert it is within {BUDGET_SECONDS}s."
        )
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument(
        "--run",
        default=None,
        help="read this run id instead of the newest completed run of each workflow",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=BUDGET_SECONDS,
        help=f"the per-step budget in seconds (default {BUDGET_SECONDS})",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=None,
        help="restrict to this workflow file (repeatable; default: all declared ones)",
    )
    parser.add_argument("--json", action="store_true", help="emit the reading as JSON")
    args = parser.parse_args(argv)

    workflows = tuple(args.workflow) if args.workflow else WORKFLOWS

    print(f"repo: {args.repo}")
    print(f"budget: {args.budget}s per Python-suite step")

    reading: list[dict] = []
    over: list[dict] = []
    try:
        for label, run in subjects(args.repo, workflows, args.run):
            _run, legs = collect(args.repo, label, run)
            print(
                f"{label} run {run.get('id')} "
                f"({run.get('head_branch') or '(no branch)'}, "
                f"{run.get('created_at') or '(no timestamp)'})"
            )
            for leg in legs:
                seconds = leg["seconds"]
                if seconds is None:
                    print(f"  {leg['job']}: could not measure (no step instants)")
                    raise Unmeasurable(
                        f"{label}: job {leg['job']!r} ran the suite but the step "
                        "carries no started_at/completed_at, so its time is unknown"
                    )
                verdict = "OVER" if seconds > args.budget else "ok"
                print(
                    f"  {leg['job']}: {seconds:.1f}s "
                    f"({leg['started_at']} -> {leg['completed_at']}) - {verdict}"
                )
                row = {
                    **leg,
                    "workflow": label,
                    "run": run.get("id"),
                    "seconds": seconds,
                }
                reading.append(row)
                if seconds > args.budget:
                    over.append(row)
    except Unmeasurable as exc:
        if args.json:
            print(json.dumps({"ok": None, "reason": str(exc), "legs": reading}, indent=2))
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"ok": not over, "budget": args.budget, "legs": reading}, indent=2))

    if over:
        print(
            f"OVER: {len(over)} of {len(reading)} suite step(s) exceed "
            f"{args.budget}s - {', '.join(sorted({row['job'] for row in over}))}"
        )
        return 1

    print(f"OK: every Python-suite step is within {args.budget}s ({len(reading)} leg(s) read)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
