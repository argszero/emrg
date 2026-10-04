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

The report is remote text, so the way **out** gets the same care as the way in
-----------------------------------------------------------------------------
The tool decodes defensively on the way in (`errors="replace"` on both the HTTP body and
`gh`'s stdout) and that care was missing on the way out, where it costs more: this is the
one output in the family whose content is not the tree's own ASCII - a job log carries the
runner's own marks and, on this repo, mostly Chinese. Measured 2026-10-04, reproduced on
this host with `sys.stdout.reconfigure(encoding="gbk")` (the reviewer's Windows console,
reached deliberately): the excerpt loop raised

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705' in position 39

out of `main`, and an exception leaving `main` exits **1** - the code this docstring defines
as "the run has no failed job". So the crash did not merely lose the report; it answered the
question with the opposite of the truth, for the very run the remedy in
`check-release-published.py` names this tool to explain. 56 of that run's 6236 log lines
cannot survive a `gbk` console (`•` x19, `✓` x14, `✔` x12, `🍺` x6, `⚠` x3, `✅` x1).

Two flags, one call, at the top of `main`: `errors="replace"` keeps the console's own codec
(so those Chinese lines still render instead of mojibaking) and prints `?` for the handful of
characters it cannot carry; `line_buffering=True` is the family's ordering remedy, which this
tool needs for its own reason - it writes the report to stdout and the `could not read:`
refusal to stderr, so a piped reader would otherwise meet the refusal first. Both are pinned
by tests; neither is prose.

`gh` is preferred and `api.github.com` is the fallback, so **a tokenless host still gets
an answer for everything except the log itself**: `gh` refuses every call without a
token, while the public endpoints answer anonymously - the run's jobs (which name the
failed step) and a job's check-run annotations (the runner's `##[error]` text). The job
log is the one part GitHub will not serve anonymously (`403`), and the tool says so
rather than reporting "no cause". Network access to api.github.com is required; there is
no offline mode, because the whole question is about what a runner printed. Every report
names the channel that answered it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "argszero/emrg"

#: Identifies this tool to api.github.com, which refuses requests without one.
USER_AGENT = "emrg-read-run-failure"
#: One anonymous request's budget. Anonymous reads share a 60/hour bucket with every
#: other unauthenticated client on the host, so a hung request is not worth waiting out.
_TIMEOUT = 30

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


#: Which channel answered, so the reading names its own basis. A set, not a flag: one
#: run's jobs, its logs and its check-runs can be answered by different channels, and a
#: report claiming they all came one way would be wrong about half of them.
_TRANSPORT: set[str] = set()


def _note_transport(which: str) -> None:
    _TRANSPORT.add(which)


def transports_used() -> list[str]:
    """The channels that answered, sorted - the reading's basis, printed with it."""
    return sorted(_TRANSPORT)


class _PublicApiRefused(RuntimeError):
    """`api.github.com` answered, and refused: the status is the answer, not a hiccup."""


def _public_api(path: str) -> str:
    """The same path, read from `api.github.com` with no credentials at all.

    Its own function rather than inlined into `_gh_api`, because it is the second seam a
    test needs to drive (patching `subprocess` would replace it for every other module in
    the process, which the sibling suite's `FakeGh` docstring already records).

    The body is returned on 200 and nothing else. A 403/404 is raised as
    `_PublicApiRefused` carrying the status: GitHub answers **public** endpoints
    anonymously and refuses private ones (and the job logs - see below) with the same
    403, so the status is a fact about the endpoint that the caller must not lose.
    """
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise _PublicApiRefused(
            f"api.github.com answered HTTP {exc.code} for {path} anonymously"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a report, not a crash
        raise _PublicApiRefused(
            f"api.github.com could not be reached for {path} anonymously: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _gh_api(path: str) -> str:
    """One API read, failing loud rather than returning an empty answer.

    The path is passed as an argument rather than a URL, so no call site can send the
    request anywhere else; the program name is prepended here so no call site can forget
    it (the failure `check-merge-freshness.py`'s `_gh_json` records: a helper that omitted
    `gh` ran the POSIX `pr` utility instead).

    **`gh` first, then `api.github.com` with no credentials.** Measured 2026-10-04
    (`cyc20261004-072223`) on the tokenless host this runs on: `gh api <any path>`
    answers nothing at all - not a failed request, a refusal to try -

        To get started with GitHub CLI, please run:  gh auth login
        Alternatively, populate the GH_TOKEN environment variable ...

    - while the *same* paths answer `200` anonymously: this run's job listing (which
    names the failed step) and the job's check-run annotations (which carry the runner's
    `##[error]` text). The job **log** is the exception: it is `403` to an
    unauthenticated fetch. So without this fallback the tool reported "could not read"
    for a question three of whose four parts were one public request away - the failure
    mode this whole file is written against. Both channels' reasons are raised together
    when both refuse, because the remedies differ (a token, versus a retry).
    """
    proc = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        _note_transport("gh")
        return proc.stdout
    gh_reason = proc.stderr.strip() or f"gh exited {proc.returncode} with no message"
    try:
        body = _public_api(path)
    except _PublicApiRefused as exc:
        raise RuntimeError(
            f"could not read {path}: gh api failed (rc={proc.returncode}): {gh_reason} | "
            f"and {exc}"
        ) from exc
    _note_transport("api.github.com (no token)")
    return body


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


def _check_run_id(job: dict) -> int | None:
    """The job's check-run id, from the payload's own `check_run_url`.

    That URL is the only place the id appears: the jobs endpoint names it `check_run_url`
    and nothing else, and the annotations endpoint needs the id rather than the job id.
    """
    tail = str(job.get("check_run_url") or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _api_annotations(job: dict, repo: str) -> list[str] | None:
    """The runner's own annotations for one job, or `None` if they could not be read.

    A **second channel for the same text**: `_annotation_lines` scrapes `##[error]` out of
    the log, and this asks GitHub for the annotation list it built from them. They answer
    the same question and only this one survives without a log - measured 2026-10-04
    (`cyc20261004-072223`): the job log is `403` to an unauthenticated fetch while this
    endpoint answers `200` and carries the annotation text (for v0.3.8's failed job, the
    runner's own `Process completed with exit code 1.`). So a tokenless host can read the
    failing step *and* what the runner annotated, which is the whole of the cause when the
    step died without printing anything.

    `None` rather than `[]` when the request failed, so "no annotations" and "the list
    could not be read" stay apart - the distinction the rest of this file is built on.
    """
    check_run = _check_run_id(job)
    if check_run is None:
        return None
    try:
        payload = json.loads(_gh_api(f"repos/{repo}/check-runs/{check_run}/annotations"))
    except Exception:  # noqa: BLE001 - a second channel failing must not end the report
        return None
    if not isinstance(payload, list):
        return None
    found: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        level = str(item.get("annotation_level") or "")
        if level not in {"failure", "warning"}:
            continue
        title = str(item.get("title") or "").strip()
        message = _clean(str(item.get("message") or "").strip())
        found.append(f"[{level}] " + (f"{title}: " if title else "") + message)
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

    # Before anything is printed: the report echoes remote text, so an unencodable
    # character must not be able to leave through `main`'s exit code (see the module
    # docstring - a crash exits 1, which this tool defines as "the run has no failed
    # job"), and the refusal on stderr must not overtake the report on stdout.
    #
    # `errors="replace"` rather than `encoding="utf-8"`: the console's own codec is kept,
    # because these logs are mostly Chinese on a gbk host and forcing utf-8 would render
    # all of it as mojibake; only the few marks it cannot carry become `?`.
    #
    # Guarded because a stream can legitimately be one that cannot be reconfigured (a
    # bare writer an embedder installed, a closed pipe). `AttributeError` and `ValueError`
    # are "this stream is not a reconfigurable TextIOWrapper" - not "the fix failed".
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except (AttributeError, ValueError):
        pass

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
        # The basis, beside the subject: `gh` and the anonymous public API do not carry
        # the same fields, so a reader deciding whether to quote a field needs to know
        # which channel produced it. Printed only when it is not the obvious one, so the
        # ordinary token-bearing report is unchanged.
        if any(channel != "gh" for channel in transports_used()):
            print(f"transport: {', '.join(transports_used())}")

    if not failed_jobs:
        # A determinate reading: nothing failed. Exit 1, not 0 - "no failure" is not the
        # answer this tool was asked for, and a caller scripting it should see the
        # difference rather than a silent success.
        if args.json:
            print(json.dumps({"run": args.run_id, "repo": args.repo, "failed": False,
                              "transport": transports_used(),
                              "jobs": [
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
        # Only when the log is not in hand: with it, `_annotation_lines` already carries
        # this text, and a second identical list would read as a second finding.
        api_annotations = None if log_read else _api_annotations(job, args.repo)
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
                "annotations_from_api": api_annotations,
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
                          "transport": transports_used(),
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
                annotations = report["annotations_from_api"]
                if annotations:
                    # GitHub's own annotation list, which does not need the log. Printed
                    # under its own label, because the channel is part of the reading.
                    print(f"  annotation (from the check run, {len(annotations)}):")
                    for line in annotations:
                        print(f"    {line}")
                elif annotations is None:
                    print("  annotations: the check run's list could not be read either")
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
