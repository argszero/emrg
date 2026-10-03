#!/usr/bin/env python3
"""Why did this GitHub Actions run fail? One failed job's step and output, or "could not read".

The class this exists for
-------------------------
`evolution_prompt.md` step 0.4 makes reading a failed run's cause a per-cycle duty, and
the natural command for it cannot do the job on this host. Measured 2026-10-04
(`cyc20261004-040426`) on v0.3.8's Build Release (run 36956685533, the run that has been
failing at the notarize step since 2026-10-02):

    $ gh run view 36956685533 -R argszero/emrg --log-failed | wc -c
    0
    $ gh run view 36956685533 -R argszero/emrg --log | wc -c
    0
    $ gh run view 36956685533 -R argszero/emrg --job 110685686584 --log | wc -c
    0

All three exit **0** with zero bytes on stdout *and* stderr - a silent empty answer, which
reads exactly like "this run has no log" and is the same class as reporting "could not
measure" as a pass. It is not about that run: the identical three commands return 0 bytes
on a green run (`36658495939`, v0.3.7) and on a recent `Test` run (`37141994658`), so the
log path is unusable on this host for every run. The API is not: the one call this tool
makes returns the whole job log (689KB for the run above, measured).

So the log is one request away, and the difference between this tool and `gh run view
--log` is not convenience: this one distinguishes **"the run has no failure"** from **"the
log could not be read"**, which the silent empty answer cannot.

Two forms in the log, and only one of them is an annotation
-----------------------------------------------------------
A raw job log carries `::error::` seven times on the run above and `##[error]` once. The
`::error::` occurrences are **the workflow's own script text**, echoed inside the runner's
`##[group]Run ...` block; the runner rewrites a workflow command that *executes* into a
`##[error]` line (`##[error]Process completed with exit code 1.` on the run above is the
one). So a matcher for `::error::` would report seven lines of echoed script as
annotations - a signal that is not the one that discriminates - and this tool reads
`##[error]` instead, saying which it read.

Usage
-----
    uv run --no-sync python3 scripts/read-run-failure.py <run-id>
    uv run --no-sync python3 scripts/read-run-failure.py <run-id> --tail 60
    uv run --no-sync python3 scripts/read-run-failure.py <run-id> --json

`--repo` defaults to `argszero/emrg`; `--tail` bounds the log lines printed per failed job.

Exit codes
----------
    0  a failed job was found and its cause was printed
    1  the run has no failed job - a determinate reading, not a failure to measure
    2  the question could not be answered (bad run id, gh failed, no jobs listed, a job's
       log could not be fetched or came back empty) - fail loud; never report "no cause"
       for a log nobody read

The empty-log case is the one that looks like a pass and is not: the fetch succeeded, so
`gh` exits 0 and there is no error to surface, but a failed job with nothing in its log has
not explained itself. It is therefore printed (the `LOG EMPTY` line, so the reading is
visible) *and* counted as unmeasured (exit 2, by `main`'s `unreadable` set) - the two halves
of "never report could-not-measure as a pass".

`gh` is required, and so is network access to api.github.com. There is no offline mode:
the whole question is about what a runner printed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

REPO = "argszero/emrg"

# GitHub's conclusion for a job that failed to do its work. Named rather than tested with
# `!= "success"` so an unseen conclusion falls through to the fail-loud branch below
# instead of being silently treated as fine - the same reasoning `check-merge-freshness.py`
# records for its status sets.
_FAILED = frozenset({"failure", "timed_out", "startup_failure"})
# Concluded without doing the work and without a verdict: reported, never counted as a
# cause and never dropped, because "the job did not run" is a fact the reader needs.
_NEITHER = frozenset({"skipped", "cancelled", "neutral", "stale", "action_required"})

# The runner's own rendering of a workflow command that ran. See the module docstring:
# `::error::` in this text is usually the step's *source*, not an annotation.
_ANNOTATION = "##[error]"

# ANSI colour/grouping codes, which arrive in the stored log and would otherwise reach the
# terminal raw.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: How many annotation lines are printed per job, and how long a line may be: both are
#: terminal hygiene, and the count is stated so a truncated list is visible as one.
_MAX_ANNOTATIONS = 10
_MAX_LINE = 300


def _gh_api(path: str) -> str:
    """One `gh api` call, failing loud rather than returning an empty answer.

    The path is passed as an argument rather than a URL, so no call site can send the
    request anywhere else; the program name is prepended here so no call site can forget
    it (the failure `check-merge-freshness.py`'s `_gh_json` records: a helper that omitted
    `gh` ran the POSIX `pr` utility instead).
    """
    proc = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api {path} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _jobs(run_id: int, repo: str) -> list[dict]:
    """The run's jobs, as GitHub reports them - or a refusal if the listing is a page.

    The request asks for one page of at most 100 (`gh api` does not follow pagination on
    its own), and the payload's own `total_count` is what says whether that page was the
    whole listing. This is checked rather than ignored because the failure it prevents is
    the tool's own: a run with more than 100 jobs - a large build matrix - whose failure
    sits past the first page lists 100 successes here, and the caller then prints
    **"no failed job"** and exits 1. That is a determinate answer to a question the
    reading did not cover, which is the one thing this tool exists to keep apart from a
    real one ("never report could-not-measure as a pass"). Measured 2026-10-04 against a
    payload with `total_count` 150 and 100 successful jobs on the page.

    An **absent** `total_count` is not evidence of truncation - a fixture, or an older API
    shape - so it is left alone rather than treated as zero.
    """
    payload = json.loads(_gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise RuntimeError(
            f"the jobs listing for run {run_id} carried no 'jobs' list "
            f"(keys: {sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__})"
        )
    found = [job for job in jobs if isinstance(job, dict)]
    reported = payload.get("total_count")
    if isinstance(reported, int) and reported > len(found):
        raise RuntimeError(
            f"the jobs listing for run {run_id} is one page of {reported}: {len(found)} "
            "came back, so a job that failed past the first 100 would not be seen - this "
            "run's cause cannot be answered from one page"
        )
    return found


def _job_log(job_id: int, repo: str) -> str:
    """The job's whole log, exactly as `gh api` returns it.

    Raises only when the fetch itself failed (`_gh_api`'s contract). An **empty** answer is
    a successful fetch that answered nothing, and it is `main`'s business, not this
    function's: the caller must both print it and count it as unmeasured, because the two
    together are what keep an empty log from reading as "there was no cause".
    """
    return _gh_api(f"repos/{repo}/actions/jobs/{job_id}/logs")


def _clean(line: str) -> str:
    """One log line, ANSI stripped and bounded - the form this tool prints."""
    text = _ANSI.sub("", line).rstrip()
    return text if len(text) <= _MAX_LINE else text[: _MAX_LINE - 3] + "..."


def _step_cause(job: dict) -> tuple[list[str], list[str]]:
    """`(the steps that did not succeed, unknown conclusions)` for one job, as names.

    Read from the job payload's `steps`, which is where the *step* is named: the log alone
    cannot say which step failed (a step that fails silently leaves no line at all - that
    is exactly the v0.3.8 notarize case, whose log ends at the environment dump).
    """
    failed: list[str] = []
    unknown: list[str] = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        conclusion = str(step.get("conclusion") or "")
        if conclusion in {"success", "skipped", ""} or conclusion in _NEITHER:
            continue
        name = f"{step.get('number')}. {step.get('name')}"
        if conclusion in _FAILED:
            failed.append(f"{name} [{conclusion}]")
        else:
            unknown.append(f"{name} [{conclusion}]")
    return failed, unknown


def _annotation_lines(log: str) -> list[str]:
    """Every `##[error]` line in the log, cleaned and bounded.

    `##[error]` rather than `::error::`, for the reason the module docstring measures: the
    latter is overwhelmingly the step's own echoed source text.
    """
    found = [_clean(line) for line in log.splitlines() if _ANNOTATION in line]
    return found[:_MAX_ANNOTATIONS]


def _cause_excerpt(lines: list[str], tail: int) -> tuple[list[str], int, str]:
    """`(lines to print, how many the block held, which rule produced it)`.

    Anchored on the **failure point**, not on the end of the log. Measured 2026-10-04 on
    the v0.3.8 run: the failing step's own block ends at its `##[error]` line (line 6219 of
    6236) and the seventeen lines after it are the runner's teardown (`Post job cleanup`,
    `git config --local --unset-all ...`), so a plain tail shows cleanup and buries the
    cause. The excerpt is therefore the block from the last `##[group]` marker at or before
    that error up to the error line itself - the step's script header, its output, and the
    annotation - bounded to `tail` lines from its end.

    The annotation read is the **first** one, not the last. Steps run in order and the
    runner's teardown runs after all of them, so the earliest `##[error]` is the earliest
    point the log annotates as an error; a log whose post-job cleanup **also** errors
    carries a second annotation last, and anchoring there printed the cleanup block under
    this function's own "the failing step's block" label while burying the step that
    actually failed (measured 2026-10-04 with a two-annotation log - the v0.3.8 run has
    one, so this is a shape its own reading does not reach). `_annotation_lines` still
    lists every annotation up to `_MAX_ANNOTATIONS`, so a second one is visible in the
    output rather than being lost by this choice.

    The assumption this makes: the first annotation is the failing step's own. An earlier
    step that emits `::error::` and still succeeds would displace it, and that shape cannot
    be ruled out from here - job logs are `403` to an unauthenticated fetch (measured
    2026-10-04), so the only logs this rule was driven from are ones the payload's own
    failing-step list agrees with. It is preferred to the last annotation because the
    cleanup case is the one the *measured* log shows, while the displacing case is a shape
    no reading here has produced.

    When the log carries no `##[error]` line the whole tail is returned, and the third
    element names which rule answered, because the two are not the same reading: the first
    is "here is the step that failed", the second is "here is the end of a log that
    annotates nothing".
    """
    error_at = None
    for index, line in enumerate(lines):
        if _ANNOTATION in line:
            error_at = index
            break
    if error_at is None:
        return (lines[-tail:] if tail > 0 else [], len(lines), "the log tail (no annotation)")
    start = 0
    for index in range(error_at, -1, -1):
        if "##[group]" in lines[index]:
            start = index
            break
    block = lines[start : error_at + 1]
    return (block[-tail:] if tail > 0 else [], len(block), "the failing step's block")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="read-run-failure.py",
        description="Why did this GitHub Actions run fail? Prints the failed step and its output.",
    )
    parser.add_argument("run_id", type=int, help="the workflow run's numeric id")
    parser.add_argument("--repo", default=REPO, help=f"owner/name (default: {REPO})")
    parser.add_argument(
        "--tail",
        type=int,
        default=40,
        help="how many log lines to print per failed job (0 prints none)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    try:
        jobs = _jobs(args.run_id, args.repo)
    except Exception as exc:  # noqa: BLE001 - a report, not a crash
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not jobs:
        print(
            f"error: run {args.run_id} in {args.repo} listed no jobs - the run could not be "
            "read, which is not the same as a run that did nothing",
            file=sys.stderr,
        )
        return 2

    failed_jobs = [job for job in jobs if str(job.get("conclusion") or "") in _FAILED]
    other_jobs = [
        job
        for job in jobs
        if str(job.get("conclusion") or "") not in _FAILED
        and str(job.get("conclusion") or "") not in {"success"}
    ]

    if not args.json:
        # The subject first, before any verdict: the family's convention, and here it is
        # also the only place the reader learns which run answered.
        print(f"repo: {args.repo}, run {args.run_id}")
        summary = f"jobs: {len(jobs)} ({len(failed_jobs)} failed"
        if other_jobs:
            names = ", ".join(str(job.get("name")) for job in other_jobs)
            summary += f", {len(other_jobs)} concluded otherwise: {names}"
        print(summary + ")")

    if not failed_jobs:
        # A determinate reading: nothing failed. Exit 1, not 0 - "no failure" is not the
        # answer this tool was asked for, and a caller scripting it should see the
        # difference rather than a silent success.
        if args.json:
            print(json.dumps({"run": args.run_id, "repo": args.repo, "failed": False, "jobs": [
                {"name": j.get("name"), "conclusion": j.get("conclusion")} for j in jobs
            ]}, indent=2))
        else:
            print(f"no failed job in run {args.run_id} - nothing to explain")
        return 1

    reports: list[dict] = []
    unreadable: list[str] = []
    for job in failed_jobs:
        steps, unknown = _step_cause(job)
        name = str(job.get("name"))
        log_read = True
        try:
            log = _job_log(int(job["id"]), args.repo)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"{name}: {exc}")
            log = ""
            log_read = False
        lines = [_clean(line) for line in log.splitlines()]
        if log_read and not lines:
            # Fetched, and empty: the reading is real and is printed below, but a failed job
            # that printed nothing has not explained itself, so it also lands in the
            # unreadable set and the run exits 2. Either half alone is wrong - dropping the
            # message hides a fact, and leaving exit 0 reports an unexplained failure as a
            # reading that was taken.
            #
            # `log_read` guards this: a log whose **fetch** failed also arrives here as an
            # empty string, and without the guard the tool says *both* "gh failed" and "the
            # log came back empty" about one job - the second of which is a reading it never
            # took, and the exact confusion this tool exists to remove (measured 2026-10-04:
            # with the fetch forced to fail, `unreadable` held the same job twice and the
            # output was byte-identical to a genuinely empty log).
            unreadable.append(f"{name}: the log came back empty")
        if log_read:
            excerpt, block_lines, basis = _cause_excerpt(lines, args.tail)
        else:
            # No rule answered, because there was no log to answer from. `_cause_excerpt([])`
            # would return its "the log tail (no annotation)" label, which asserts a reading
            # this branch did not take - so the basis names what actually happened.
            excerpt, block_lines, basis = [], 0, "the log was never read"
        reports.append(
            {
                "job": name,
                "id": job.get("id"),
                "conclusion": job.get("conclusion"),
                "failed_steps": steps,
                "other_conclusions": unknown,
                "annotations": _annotation_lines(log),
                "excerpt": excerpt,
                "excerpt_basis": basis,
                "excerpt_of": block_lines,
                "log_lines": len(lines),
                # Said in the machine-readable form too, because the two shapes are the
                # same shape to a consumer reading only `log_lines` (both 0) and only one
                # of them is a reading: "the log came back empty" is a fact about the run,
                # "the log was never read" is a fact about this tool.
                "log_read": log_read,
            }
        )

    if args.json:
        print(json.dumps({"run": args.run_id, "repo": args.repo, "failed": True,
                          "jobs": reports, "unreadable": unreadable}, indent=2))
    else:
        for report in reports:
            print(f"\nFAILED JOB: {report['job']} [{report['conclusion']}]")
            if report["failed_steps"]:
                print("  step(s): " + "; ".join(report["failed_steps"]))
            else:
                # Said out loud: the payload did not name a failed step, so the job-level
                # conclusion is all there is. Never silently omitted.
                print("  step(s): the job payload names no failed step")
            if report["other_conclusions"]:
                print("  other: " + "; ".join(report["other_conclusions"]))
            if not report["log_read"]:
                # Not "LOG EMPTY": no fetch answered, so there is no reading to report - only
                # a reason, which is on stderr below. Printing the empty-log line here would
                # assert a fact about the run that this cycle never measured.
                print("  LOG NOT READ: the fetch failed, so no line of this job's log was "
                      "seen - the reason is on stderr, and this is not a cause")
            elif not report["log_lines"]:
                print("  LOG EMPTY: the log came back with no lines - this is not a cause")
            else:
                for line in report["annotations"]:
                    print(f"  annotation: {line}")
                if report["excerpt"]:
                    print(
                        f"  {report['excerpt_basis']}, last {len(report['excerpt'])} of "
                        f"{report['excerpt_of']} line(s) (of {report['log_lines']} in the log):"
                    )
                    for line in report["excerpt"]:
                        print(f"    {line}")

    if unreadable:
        # Loud, and last so it is not missed: a job whose log could not be read has no
        # cause printed, and reporting the run as explained would be the failure this
        # tool exists to prevent.
        print(f"\ncould not read: {'; '.join(unreadable)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
