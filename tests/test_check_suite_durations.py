"""`scripts/check-suite-durations.py` — the CI-leg duration guard (issue #2296 R4).

Every arm here is offline: the API is a stand-in, so the suite that runs this file
never makes a request, and the one arm that runs the real script does it with an empty
`PATH` so `gh` cannot be found at all (the same technique `tests/test_check_pr_base.py`
uses for the cannot-ask path — the Windows runner has no `gh`, and a guard whose tests
need the network would be red for a reason that is not the rule).

The three-state contract is what these arms are about, in the order the family means
it: a leg over budget is **1**, an unreadable leg is **2**, and only a complete, clean
reading is **0**. The arm that matters most is the middle one — a guard that reported
"no suite step found" as success is exactly the failure mode R4 exists to close.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-suite-durations.py"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load():
    spec = importlib.util.spec_from_file_location("check_suite_durations", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BASE = datetime(2026, 10, 15, 6, 0, 0)


def _stamp(minute: int, seconds: int) -> tuple[str, str]:
    """`(started_at, completed_at)` for a step starting `minute` minutes after the base.

    Built by adding a delta rather than by formatting arithmetic: the first version
    formatted the end from `minute * 60 + seconds` and so produced instants *before*
    their start, which `step_seconds` refuses (rightly) — the guard was correct and the
    fixture was wrong.
    """
    start = _BASE + timedelta(minutes=minute)
    end = start + timedelta(seconds=seconds)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    """`subprocess.run`, answering the three endpoints the guard calls.

    `runs` maps a workflow file to (run dict, jobs list). A workflow that is absent
    answers as GitHub does when there is no run to read: an empty `workflow_runs`.
    """

    def __init__(self, runs: dict, jobs: dict | None = None):
        self.runs = runs
        self.jobs = jobs or {}
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        joined = " ".join(args)
        match = re.search(r"workflows/([^/]+)/runs", joined)
        if match:
            workflow = match.group(1)
            if workflow not in self.runs:
                return _Completed(json.dumps({"workflow_runs": []}))
            return _Completed(json.dumps({"workflow_runs": [self.runs[workflow][0]]}))
        match = re.search(r"actions/runs/(\d+)/jobs", joined)
        if match:
            run_id = int(match.group(1))
            for run, jobs in self.runs.values():
                if run["id"] == run_id:
                    return _Completed(json.dumps({"jobs": jobs}))
            return _Completed(json.dumps({"jobs": self.jobs.get(run_id, [])}))
        match = re.search(r"actions/runs/(\d+)$", joined)
        if match:
            run_id = int(match.group(1))
            for run, _jobs in self.runs.values():
                if run["id"] == run_id:
                    return _Completed(json.dumps(run))
            return _Completed("{}", returncode=1, stderr="gh: Not Found (HTTP 404)")
        return _Completed("{}", returncode=1, stderr="gh: unexpected call")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _run_dict(run_id: int = 111, branch: str = "master", path: str = ".github/workflows/test.yml") -> dict:
    return {
        "id": run_id,
        "head_branch": branch,
        "created_at": "2026-10-15T06:00:00Z",
        "path": path,
        "name": "Test",
    }


def _step(name: str, seconds: int, minute: int = 0) -> dict:
    start, end = _stamp(minute, seconds)
    return {
        "name": name, "status": "completed", "conclusion": "success",
        "started_at": start, "completed_at": end,
    }


def _job(name: str, steps: list[dict], conclusion: str = "success") -> dict:
    return {"name": name, "conclusion": conclusion, "steps": steps}


def _two_legs(test_seconds=100, windows_seconds=200, release_seconds=150) -> dict:
    """Both declared workflows, each with the legs the guard has to read.

    Every declared workflow must contribute a readable run, or `main` refuses with
    rc 2 — which is the point of `test_a_workflow_with_no_run_at_all_is_rc_2`, and why
    this fixture cannot supply only `test.yml`.
    """
    return {
        "test.yml": (_run_dict(), [
            _job("test", [_step("uv run pytest tests/ -v -n auto --dist loadfile", test_seconds)]),
            _job("test-windows", [_step("Python tests (Windows)", windows_seconds)]),
        ]),
        "build-release.yml": (_run_dict(222, "v0.3.13"), [
            _job("build (windows-2025, x64)", [
                _step("Python tests (regression gate)", release_seconds),
            ]),
            _job("build (ubuntu-24.04, x86_64)", [
                _step("Python tests (regression gate)", release_seconds),
            ]),
        ]),
    }


# ── the three states ──────────────────────────────────────────────────────────────────


def test_a_leg_over_budget_is_rc_1(monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs()))

    rc = mod.main([])

    out = capsys.readouterr().out
    assert rc == 1, out
    assert "test-windows" in out
    assert "OVER" in out


def test_a_clean_reading_is_rc_0(monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs(test_seconds=90, windows_seconds=170)))

    rc = mod.main([])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ok" in out.lower()


def test_a_leg_with_no_readable_step_is_rc_2_not_0(monkeypatch, capsys) -> None:
    """The refusal that keeps this guard from blessing a workflow it never read.

    A run whose jobs carry no recognisable suite step is not a clean reading — it is a
    reading that did not happen. Reporting 0 here is the whole defect class.
    """
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh({
        "test.yml": (_run_dict(), [_job("test", [_step("Set up Python", 3)])]),
    }))

    rc = mod.main(["--workflow", "test.yml"])

    captured = capsys.readouterr()
    assert rc == 2, captured.out + captured.err
    assert "could not measure" in captured.err
    assert "no step this guard can recognise" in captured.err


def test_a_workflow_with_no_run_at_all_is_rc_2(monkeypatch, capsys) -> None:
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh({}))

    rc = mod.main([])

    captured = capsys.readouterr()
    assert rc == 2
    assert "no completed run to read" in captured.err


def test_a_step_with_no_instants_is_rc_2_not_0(monkeypatch, capsys) -> None:
    """`completed_at: null` is unknown, not zero.

    A step that has not finished (or was skipped) carries no instants. Reading that as
    0.0s would invent the number the guard exists to measure — and 0.0s passes.
    """
    mod = _load()
    unfinished = _step("Python tests (Windows)", 200)
    unfinished["completed_at"] = None
    monkeypatch.setattr(mod.subprocess, "run", FakeGh({
        "test.yml": (_run_dict(), [_job("test-windows", [unfinished])]),
    }))

    rc = mod.main(["--workflow", "test.yml"])

    captured = capsys.readouterr()
    assert rc == 2, captured.out + captured.err
    assert "no started_at/completed_at" in captured.err


# ── the number comes from the artefact ────────────────────────────────────────────────


def test_the_duration_is_read_from_the_stamps(monkeypatch, capsys) -> None:
    """Two payloads differing only in the instants give different seconds.

    Without this the arms above would pass against a guard that printed a constant.
    """
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs(test_seconds=10, windows_seconds=170)))
    assert mod.main([]) == 0
    first = capsys.readouterr().out

    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs(test_seconds=42, windows_seconds=170)))
    assert mod.main([]) == 0
    second = capsys.readouterr().out

    assert "10.0s" in first and "42.0s" in second
    assert first != second


def test_the_budget_flag_moves_the_verdict(monkeypatch, capsys) -> None:
    """The one duration written down is the rule, and it is overridable."""
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs(test_seconds=100, windows_seconds=200)))

    assert mod.main(["--budget", "250"]) == 0
    assert "250s" in capsys.readouterr().out

    assert mod.main(["--budget", "150"]) == 1
    assert capsys.readouterr().out


def test_a_run_id_is_read_directly_when_given(monkeypatch) -> None:
    mod = _load()
    fake = FakeGh(_two_legs())
    monkeypatch.setattr(mod.subprocess, "run", fake)

    assert mod.main(["--run", "111"]) == 1

    joined = [" ".join(call) for call in fake.calls]
    assert any("actions/runs/111" in call and "workflows" not in call for call in joined), joined


def test_the_report_names_the_run_it_read(monkeypatch, capsys) -> None:
    """The subject is named before the verdict — the remote reader's version of `tree:`."""
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run", FakeGh(_two_legs()))

    mod.main([])

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("repo: ")
    first_verdict = next(i for i, line in enumerate(lines) if "OVER" in line or "OK:" in line)
    assert any("run 111" in line for line in lines[:first_verdict]), out


def test_a_missing_gh_is_rc_2_offline(tmp_path) -> None:
    """The real script, no network: `gh` is not on an empty PATH.

    The in-process arms above stub `subprocess.run`; this one proves the *shipped*
    entry point refuses too, and does it without a round trip.
    """
    empty = tmp_path / "empty-path"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PATH=str(empty)),
    )

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "gh is not available" in proc.stderr


# ── the reading is not this file's arithmetic ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-15T06:00:00Z", "2026-10-15 06:00:00+00:00"),
        ("2026-10-15T06:00:00+00:00", "2026-10-15 06:00:00+00:00"),
    ],
)
def test_both_stamp_spellings_parse(value, expected) -> None:
    mod = _load()
    assert str(mod.parse_stamp(value)) == expected


@pytest.mark.parametrize("value", [None, "", "not a date", 12345])
def test_an_unreadable_stamp_is_none_not_an_exception(value) -> None:
    mod = _load()
    assert mod.parse_stamp(value) is None


def test_a_negative_duration_is_unmeasurable() -> None:
    """Clock skew between two instants is not a duration to report."""
    mod = _load()
    step = {
        "name": "Python tests (Windows)",
        "started_at": "2026-10-15T06:10:00Z",
        "completed_at": "2026-10-15T06:00:00Z",
    }
    assert mod.step_seconds(step) is None


def test_a_skipped_step_is_unmeasurable() -> None:
    mod = _load()
    assert mod.step_seconds({"name": "Python tests (Windows)", "conclusion": "skipped"}) is None


# ── the closure at the tree level ─────────────────────────────────────────────────────


def test_every_declared_workflow_carries_a_step_this_guard_can_see() -> None:
    """A rename that hid the suite from the markers fails here, not in CI.

    The guard recognises a suite step by marker, and a marker list is a hand-written
    claim about what the workflows spell. This asks the workflows themselves, so the
    claim is measured: every workflow the guard declares must carry at least one step
    whose API name (its `name:`, or its run command when unnamed) would match.
    """
    mod = _load()
    missing = []
    for workflow in mod.WORKFLOWS:
        text = (WORKFLOWS_DIR / workflow).read_text(encoding="utf-8")
        lowered = text.lower()
        if not any(marker in lowered for marker in mod.SUITE_STEP_MARKERS):
            missing.append(workflow)

    assert not missing, (
        f"{missing} carry no spelling this guard recognises, so their suite steps would "
        f"read as 'contributed nothing' (markers: {mod.SUITE_STEP_MARKERS})"
    )


def test_the_declared_workflows_are_the_ones_that_run_pytest() -> None:
    """The tuple is a claim too: a third workflow running pytest would go unread."""
    mod = _load()
    runners = sorted(
        path.name for path in WORKFLOWS_DIR.glob("*.yml")
        if "pytest" in path.read_text(encoding="utf-8")
    )
    assert runners == sorted(mod.WORKFLOWS), (
        "the workflows running pytest and the ones this guard reads differ: "
        f"{runners} vs {sorted(mod.WORKFLOWS)}"
    )
