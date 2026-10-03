"""Tests for scripts/read-run-failure.py - what did the runner actually print?

Why these tests exist, and what each one pins
---------------------------------------------
The tool exists because the natural command for this question answers **nothing**
on this host with exit 0, and an empty answer reads as "there is no cause":

    gh run view <id> --log-failed   ->  0 bytes, rc 0
    gh run view <id> --log          ->  0 bytes, rc 0
    gh run view <id> --job <id> --log -> 0 bytes, rc 0

(measured 2026-10-04, `cyc20261004-040426`, on run 36956685533 - and the same three
return 0 bytes on a green run and on a recent `Test` run, so it is the host, not the
run; the API returns the whole log, 689KB for that job). Every test below pins one way
the replacement could still be the same kind of instrument:

* `test_a_failed_step_and_its_output_are_reported` - the working case, driven from a
  fixture built out of the **real** log's shape (group block, env dump, `##[error]`).
* `test_the_excerpt_anchors_on_the_failure_not_the_end_of_the_log` - the measured
  shape: 17 lines of `Post job cleanup` follow the failure, so a plain tail buries it.
* `test_a_run_with_no_failed_job_is_a_determinate_answer_not_a_failure` - exit 1 with
  the words to match, so "nothing failed" cannot be read as "could not measure".
* `test_an_empty_job_log_is_never_reported_as_a_cause` - the empty answer, which is
  the defect this file is about: printed so the reading is visible, **and** exit 2 so a
  failed job that explained nothing is never a run that was explained.
* `test_an_unreadable_log_makes_the_run_unmeasurable` - exit 2, and it must not be 0
  while a failed job went unexplained - **and** a fetch that failed must not be reported
  as a log that came back empty, which is a reading nobody took (measured 2026-10-04,
  `cyc20261004-070930`; `test_the_two_ways_a_log_can_be_absent_are_told_apart_in_json`
  pins the machine-readable half).
* `test_a_silent_step_is_still_named_by_the_job_payload` - the v0.3.8 case exactly:
  the step printed nothing at all, so the log cannot name it and only the payload can.
* `test_the_matcher_reads_the_annotation_form_the_runner_writes` - and, the
  discriminating half, does **not** read `::error::`, which in a raw log is the step's
  own echoed source (7 occurrences on the measured run, all script text).
* `test_an_unknown_step_conclusion_is_reported_not_dropped` - a new GitHub conclusion
  for a step must not be silently treated as success.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "read-run-failure.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("read_run_failure", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec, as the sibling suites do: the module is loaded by path and
    # anything that resolves annotations through sys.modules needs it there.
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


# --- the fixture: a run shaped like the real one ----------------------------

#: The real log's group block for the failing step, reduced: the step's script header, the
#: runner's environment dump, and the annotation the runner writes when the step exits 1.
_STEP_BLOCK = """2026-10-02T03:06:17.8654770Z ##[group]Run PKG="$(find dist/artifacts -maxdepth 1 -name 'EMRG-*-macos-*.pkg' | head -1)"
2026-10-02T03:06:17.8761500Z shell: /bin/bash --noprofile --norc -e -o pipefail {0}
2026-10-02T03:06:18.5038140Z   APPLE_ID: ***
2026-10-02T03:06:18.5039780Z ##[endgroup]
2026-10-02T03:06:20.9834000Z ##[error]Process completed with exit code 1.
"""

#: What follows the failure in the real log, and what a plain tail would print instead.
_TEARDOWN = "\n".join(
    f"2026-10-02T03:06:2{i}.0000000Z Post job cleanup step {i}" for i in range(17)
) + "\n"


def _job(name="build", conclusion="failure", steps=None, job_id=1) -> dict:
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "steps": steps
        if steps is not None
        else [
            {"number": 17, "name": "Make installer", "conclusion": "success"},
            {"number": 18, "name": "Notarize pkg (macOS only)", "conclusion": "failure"},
            {"number": 19, "name": "Staple pkg", "conclusion": "skipped"},
        ],
    }


class FakeGh:
    """`_gh_api` as the tool uses it: routed by path, recording every call.

    Patched at the module's own seam rather than at `subprocess.run`: `mod.subprocess` is
    the shared stdlib module, so patching its attribute would replace `subprocess.run` for
    everything else running in the process during the test.
    """

    def __init__(self, jobs: list[dict], logs: dict[int, str] | None = None,
                 jobs_rc: int = 0, total_count: int | None = None):
        self.jobs = jobs
        self.logs = logs or {}
        self.jobs_rc = jobs_rc
        self.total_count = total_count
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        self.calls.append(path)
        assert path.startswith("repos/"), path
        if "/jobs?" in path:
            if self.jobs_rc:
                raise RuntimeError(
                    f"gh api {path} failed (rc=1): gh: Not Found (HTTP 404)"
                )
            payload = {"jobs": self.jobs}
            if self.total_count is not None:
                payload["total_count"] = self.total_count
            return json.dumps(payload)
        if "/logs" in path:
            job_id = int(path.split("/actions/jobs/")[1].split("/")[0])
            if job_id not in self.logs:
                raise RuntimeError(
                    f"gh api {path} failed (rc=1): gh: log not available (HTTP 404)"
                )
            return self.logs[job_id]
        raise AssertionError(f"unexpected gh api path: {path}")


def _run(mod, monkeypatch, fake, argv) -> int:
    monkeypatch.setattr(mod, "_gh_api", fake)
    return mod.main(argv)


# --- the working case -------------------------------------------------------


def test_a_failed_step_and_its_output_are_reported(mod, monkeypatch, capsys):
    """The reading asked for: which step failed, and what the runner printed there."""
    fake = FakeGh([_job()], {1: _STEP_BLOCK + _TEARDOWN})
    rc = _run(mod, monkeypatch, fake, ["42"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "18. Notarize pkg (macOS only)" in out, "the step is named from the job payload"
    assert "##[error]Process completed with exit code 1." in out
    assert "the failing step's block" in out, "the excerpt says which rule produced it"


def test_the_excerpt_anchors_on_the_failure_not_the_end_of_the_log(mod, monkeypatch, capsys):
    """Measured shape: the teardown follows the failure, so a tail shows `Post job cleanup`.

    The real run's failing step ends at line 6219 of 6236, and the 17 lines after it are
    the runner's own teardown. A tool that printed the log's last lines would show `git
    config --local --unset-all ...` and bury the cause - which is exactly what the first
    version of this file did before the measurement.
    """
    fake = FakeGh([_job()], {1: _STEP_BLOCK + _TEARDOWN})
    rc = _run(mod, monkeypatch, fake, ["42", "--tail", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Post job cleanup" not in out, (
        "the excerpt must stop at the failure: teardown lines are not the cause"
    )
    assert "##[error]" in out


def test_a_silent_step_is_still_named_by_the_job_payload(mod, monkeypatch, capsys):
    """The v0.3.8 case: the failing step printed nothing, so only the payload names it.

    Its log goes from the environment dump straight to the runner's exit-1 annotation -
    no line from the step itself. A reader who only had the log could not say which step
    failed; the payload's `steps` is what answers, which is why both are read.
    """
    fake = FakeGh([_job()], {1: _STEP_BLOCK})
    rc = _run(mod, monkeypatch, fake, ["42"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "18. Notarize pkg (macOS only)" in out
    assert "APPLE_ID: ***" in out, "the step's own block is what got printed"


# --- the three-valued outcome ----------------------------------------------


def test_a_run_with_no_failed_job_is_a_determinate_answer_not_a_failure(
    mod, monkeypatch, capsys
):
    """`no failed job` is a reading with its own exit code, never a silent success."""
    fake = FakeGh([_job(conclusion="success"), _job(name="release", conclusion="success", job_id=2)])
    rc = _run(mod, monkeypatch, fake, ["42"])
    out = capsys.readouterr().out
    assert rc == 1, "not 0: the answer this tool was asked for was not produced"
    assert "no failed job" in out
    assert "nothing to explain" in out


def test_an_unreadable_log_makes_the_run_unmeasurable(mod, monkeypatch, capsys):
    """A failed job whose log cannot be fetched is exit 2, and never reported as explained.

    The negative half is the one that was missing (measured 2026-10-04, `cyc20261004-070930`):
    a fetch that **fails** also arrives at `main`'s loop as an empty string, so the tool used
    to report *both* "gh api ... failed" **and** "the log came back empty" about one job - and
    the second of those is a reading it never took. With the fetch forced to fail, the output
    was byte-identical to a genuinely empty log: `LOG EMPTY` on stdout, "came back empty" on
    stderr. That is the defect this tool exists to remove, committed by the tool itself, so
    both halves are asserted: "no such reading was taken" is as load-bearing as "exit 2".
    """
    fake = FakeGh([_job()], {})  # no log for job 1 -> gh returns rc 1
    rc = _run(mod, monkeypatch, fake, ["42"])
    captured = capsys.readouterr()
    assert rc == 2, "one unexplained failed job is not a run that was explained"
    assert "could not read" in captured.err
    assert "came back empty" not in captured.err, (
        "the fetch failed - saying the log 'came back empty' reports a reading that was "
        "never taken, and makes the failure indistinguishable from a genuinely empty log"
    )
    assert "LOG EMPTY" not in captured.out, (
        "no log was fetched, so there are no 'no lines' to report"
    )
    assert "LOG NOT READ" in captured.out, (
        "the reader of stdout must be told the log was never in hand, not left to infer it "
        "from stderr"
    )
    assert captured.err.count("build") == 1, (
        "the same job is listed once with its real reason, not a second time with a "
        "fabricated one"
    )


def test_the_two_ways_a_log_can_be_absent_are_told_apart_in_json(
    mod, monkeypatch, capsys
):
    """`log_read` is the field that separates them, because both report `log_lines: 0`.

    A machine consumer reading only `log_lines` sees the same shape twice, and the two are
    not the same fact about the run: "the log came back empty" is a measurement, "the log was
    never read" is a limitation of the reading. Asserted in **both** directions, since a field
    that is always `False` (or always `True`) separates nothing.
    """
    failed_fetch = FakeGh([_job()], {})
    assert _run(mod, monkeypatch, failed_fetch, ["42", "--json"]) == 2
    unread = json.loads(capsys.readouterr().out)["jobs"][0]

    empty_log = FakeGh([_job()], {1: ""})
    assert _run(mod, monkeypatch, empty_log, ["42", "--json"]) == 2
    empty = json.loads(capsys.readouterr().out)["jobs"][0]

    assert unread["log_read"] is False and empty["log_read"] is True, (
        "the fetch that failed and the log that answered nothing must not read alike"
    )
    assert unread["log_lines"] == empty["log_lines"] == 0, (
        "the premise: the old field genuinely cannot tell them apart"
    )
    assert "never read" in unread["excerpt_basis"]
    assert "never read" not in empty["excerpt_basis"], (
        "for a log that was fetched, the basis is the rule that answered it"
    )


def test_an_empty_job_log_is_never_reported_as_a_cause(mod, monkeypatch, capsys):
    """The empty answer that this whole tool exists for: said out loud, and not a pass.

    The fetch succeeded, so there is no error to raise and `gh` exits 0 - which is exactly
    why this shape is dangerous: an empty log for a **failed** job reaches the reader as
    "nothing to see". Both halves are asserted, because either alone still ships the
    defect: the `LOG EMPTY` line makes the reading visible, and exit **2** keeps it from
    being counted as a run that was explained (the same reason `check-notary-credentials.py`
    refuses to call an unmeasured exchange a pass).
    """
    fake = FakeGh([_job()], {1: ""})
    rc = _run(mod, monkeypatch, fake, ["42"])
    captured = capsys.readouterr()
    assert "LOG EMPTY" in captured.out
    assert "not a cause" in captured.out
    assert rc == 2, "a failed job with an empty log was not explained"
    assert "came back empty" in captured.err


def test_a_truncated_jobs_listing_is_not_an_answer(mod, monkeypatch, capsys):
    """One page of a longer listing must not read as "no failed job".

    `gh api` does not follow pagination, so the request asks for at most 100 jobs; the
    payload's own `total_count` is what says whether that page was the whole run. Without
    the check a large build matrix whose failure sits past the first page lists 100
    successes here, and the tool prints "no failed job" and exits **1** - a determinate
    answer to a question the reading never covered, which is precisely the confusion this
    tool exists to prevent. Measured 2026-10-04 with `total_count` 150 and 100 successful
    jobs on the page.
    """
    fake = FakeGh(
        [_job(name=f"build{i}", conclusion="success", job_id=i) for i in range(100)],
        total_count=150,
    )
    rc = _run(mod, monkeypatch, fake, ["42"])
    captured = capsys.readouterr()
    assert rc == 2, "a partial listing is not a run with nothing failed"
    assert "no failed job" not in captured.out
    assert "one page of 150" in captured.err, captured.err


def test_a_complete_listing_still_answers_normally(mod, monkeypatch, capsys):
    """Control leg: when `total_count` matches the page, nothing changes."""
    fake = FakeGh([_job()], {1: _STEP_BLOCK}, total_count=1)
    rc = _run(mod, monkeypatch, fake, ["42"])
    assert rc == 0
    assert "FAILED JOB: build" in capsys.readouterr().out


def test_a_failing_post_job_step_does_not_displace_the_cause(mod, monkeypatch, capsys):
    """The teardown's own annotation must not become "the failing step's block".

    Steps run in order and the runner's post-job cleanup runs after all of them, so a log
    whose cleanup **also** errors carries its second `##[error]` last. Anchoring the
    excerpt there printed the cleanup block under this tool's own "the failing step's
    block" label and dropped the step that actually failed. The v0.3.8 run has a single
    annotation (its post-job steps all succeeded), so this shape is driven from a log built
    the way the real one is: a step block, its annotation, then a cleanup group that fails.
    """
    cleanup = (
        "2026-10-02T03:06:21.0000000Z ##[group]Post job cleanup\n"
        "2026-10-02T03:06:21.1000000Z   removing temp credentials\n"
        "2026-10-02T03:06:21.2000000Z ##[endgroup]\n"
        "2026-10-02T03:06:21.3000000Z ##[error]Post job cleanup failed\n"
    )
    fake = FakeGh([_job()], {1: _STEP_BLOCK + cleanup})
    rc = _run(mod, monkeypatch, fake, ["42"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "APPLE_ID: ***" in out, (
        "the excerpt must be the failing step's block, not the cleanup that followed it"
    )
    # The cleanup's annotation is still listed (a second annotation is not hidden)...
    assert "##[error]Post job cleanup failed" in out
    # ...but the excerpt itself anchors on the step that failed.
    excerpt = out.split("the failing step's block")[1]
    assert "Post job cleanup" not in excerpt, excerpt
    assert "Process completed with exit code 1." in excerpt


def test_a_run_that_cannot_be_listed_is_unmeasurable(mod, monkeypatch, capsys):
    """A bad run id is exit 2, not "no failure"."""
    fake = FakeGh([], jobs_rc=1)
    rc = _run(mod, monkeypatch, fake, ["99999999"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "404" in err


def test_a_run_listing_no_jobs_is_unmeasurable(mod, monkeypatch, capsys):
    """An empty jobs list means the run was not read, not that it did nothing."""
    fake = FakeGh([])
    rc = _run(mod, monkeypatch, fake, ["42"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "listed no jobs" in err


# --- the matcher, and the form it must not accept ---------------------------


def test_the_matcher_reads_the_annotation_form_the_runner_writes(mod):
    """`##[error]` is the runner's rendering; `::error::` in the log is the step's source.

    Measured on the real log: `::error::` occurs **7 times**, all of them the workflow's
    own echoed script inside a `##[group]Run ...` block, while the one executed annotation
    appears as `##[error]`. A matcher for the workflow-command form would therefore report
    seven lines of script text as annotations - a signal that does not discriminate.
    """
    assert mod._ANNOTATION == "##[error]"
    log = (
        '2026-10-02T03:00:05.9172770Z echo "::error::MACOS_SIGNING_P12_BASE64 未包含..."\n'
        "2026-10-02T03:06:20.9834000Z ##[error]Process completed with exit code 1.\n"
    )
    found = mod._annotation_lines(log)
    assert len(found) == 1, f"only the executed annotation is one, got {found}"
    assert "Process completed with exit code 1." in found[0]


def test_ansi_codes_are_stripped_from_printed_lines(mod):
    """The stored log carries colour codes; a reader's terminal must not receive them."""
    assert mod._clean("\x1b[36;1m  echo hello\x1b[0m") == "  echo hello"


def test_an_unknown_step_conclusion_is_reported_not_dropped(mod):
    """A conclusion this version does not know is named, not read as success.

    Same reasoning as `check-merge-freshness.py`'s named status sets: a new GitHub
    conclusion silently treated as fine is a guard that rots without changing.
    """
    job = _job(steps=[{"number": 1, "name": "Mystery", "conclusion": "some_new_state"}])
    failed, unknown = mod._step_cause(job)
    assert failed == []
    assert unknown == ["1. Mystery [some_new_state]"], (
        f"an unknown conclusion must be reported, got failed={failed} unknown={unknown}"
    )


def test_every_call_names_gh_and_the_api(mod, monkeypatch):
    """The helper prepends `gh api`, so no call site can send the request elsewhere.

    Driven through the real `_gh_api` with only `subprocess.run` replaced, because the
    argv the helper builds is the claim under test - a router above it would test the
    router. `monkeypatch` restores the attribute even if the assertion fails.
    """
    calls: list[list[str]] = []

    def fake(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, json.dumps({"jobs": []}), "")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    mod._jobs(1, "owner/repo")
    assert calls and calls[0][:2] == ["gh", "api"], calls
    assert calls[0][2].startswith("repos/owner/repo/actions/runs/1/jobs")
    assert calls[0][2].endswith("jobs?per_page=100"), (
        "the listing is bounded and asks for the jobs endpoint"
    )


# --- the host has to be able to find it ------------------------------------
#
# A reading nobody can reach is not a reading. The duty this tool serves (a failed run's
# cause must be read) lives in the evolution prompt, which only the agent reads; the host
# whose release round just went red looks in the docs. So the tool is documented in
# `DEVELOPMENT.md`, and these two tests keep that document honest: one pins the *contract*
# (a presence check on a path would bless a paragraph whose prose has rotted), and the
# other pins the spelling - every flag the document shows is asked of argparse rather than
# transcribed, so a renamed option cannot leave a documented command that will not run.

_DEV = REPO_ROOT / "DEVELOPMENT.md"


def _documented_block(doc: str) -> str:
    """The lines that invoke this tool, located by its own name rather than by line number."""
    return "\n".join(line for line in doc.splitlines() if "read-run-failure.py" in line)


def _real_flags(mod, capsys) -> set[str]:
    """The tool's own option list, read from `--help` rather than retyped here."""
    with pytest.raises(SystemExit):
        mod.main(["--help"])
    return set(re.findall(r"--[a-z][a-z0-9-]*", capsys.readouterr().out))


def _contract_prose(doc: str) -> str:
    """The paragraph that follows the documented invocation, where the contract is stated.

    Scoped rather than document-wide because a document-wide `in` is not discriminating:
    this file's **first** version asserted `"never a pass" in text`, and a mutation arm that
    deleted the phrase from this tool's paragraph **survived** - the notarize paragraph above
    it carries the same four words for its own exit code. The region is located by the tool's
    own name, then carried to the end of the fenced block, then to the end of the prose
    paragraph after it: an edit elsewhere in the document cannot satisfy this check.
    """
    lines = doc.splitlines()
    naming = [i for i, line in enumerate(lines) if "read-run-failure.py" in line]
    if not naming:
        return ""
    rest = lines[max(naming) + 1:]
    for index, line in enumerate(rest):
        if line.strip() == "```":  # the fence closes here
            block: list[str] = []
            for following in rest[index + 1:]:
                if not following.strip():
                    if block:  # the paragraph ends at its blank line
                        break
                    continue  # the blank line the closing fence leaves behind
                block.append(following)
            return "\n".join(block)
    return ""


def test_development_md_documents_the_reading_and_its_contract() -> None:
    """DEVELOPMENT.md names the tool, the silent-empty command it replaces, and its contract.

    Measured 2026-10-04: `gh run view <id> --log-failed` returns 0 bytes with rc 0 for every
    run on this host, which is why a failed release build had to be investigated through the
    API. A reader who does not know the tool exists will run that command again, and the
    document must also carry the one thing a bare path cannot: exit `2` is "no verdict was
    reached", so a reader who folds it into a pass buys the wasted round the tool prevents.
    """
    text = _DEV.read_text(encoding="utf-8")
    assert "scripts/read-run-failure.py" in text, (
        "DEVELOPMENT.md does not name scripts/read-run-failure.py - a host reading a failed "
        "run's cause is back to `gh run view --log`, which answers nothing here"
    )
    assert "--log-failed" in text, (
        "the document names the tool without the command it replaces, so the reader cannot "
        "tell why the familiar one is not the answer"
    )
    prose = _contract_prose(text)
    assert prose.strip(), (
        "the invocation is documented with no prose after it - an exit-code contract stated "
        "nowhere is the half a bare command block cannot carry"
    )
    assert "never a pass" in prose, (
        "the paragraph documenting the tool states its exit codes without the contract - 2 "
        "means no verdict was reached, and folding that into a pass is the conflation this "
        "tool exists to prevent"
    )
    assert "empty" in prose, (
        "a failed job with an empty log is the shape that looks like a pass and is not; the "
        "document must say so beside the exit code"
    )


def test_every_flag_the_document_spells_is_a_flag_the_tool_has(mod, capsys) -> None:
    """The documented invocation must be runnable, not merely present.

    `--no-sync` is `uv`'s flag, so the cut is the tool's own path - reading the whole line
    would demand the tool accept its launcher's option.
    """
    block = _documented_block(_DEV.read_text(encoding="utf-8"))
    assert "read-run-failure.py" in block, "the document no longer invokes the tool"
    # Per line, and only the part after the tool's path: each documented invocation repeats
    # the launcher, and `uv run --no-sync python3` sits *before* the path on every line.
    arguments = "\n".join(
        line.split("read-run-failure.py", 1)[1]
        for line in block.splitlines()
        if "read-run-failure.py" in line
    )
    documented = set(re.findall(r"--[a-z][a-z0-9-]*", arguments))
    assert documented, "the documented invocation names no flag, so this measures nothing"
    unknown = documented - _real_flags(mod, capsys)
    assert not unknown, (
        f"DEVELOPMENT.md spells {sorted(unknown)}, which the tool does not accept - a renamed "
        "flag leaves the document showing a command that cannot run"
    )
    assert "--tail" in documented, "the documented invocation lost the flag that shows more output"
