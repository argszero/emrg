"""`scripts/check-release-published.py` -- the publish half of the release chain.

The readings this file pins are the ones a hand-run of `gh run list` plus `gh release
view` gets wrong or does not take at all: a run that has not concluded (which is *not* a
pass -- the release does not exist until the run's release job created it), a tag with no
run under the only workflow that publishes, a green run whose release is missing, and the
three unusable-release states (draft, prerelease, zero assets).

The fakes never touch the network: `_gh` is replaced by a routing table keyed on the
argument list, every call is recorded, and each state is built from the API payloads the
live endpoints return (measured 2026-09-29 against `argszero/emrg`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-release-published.py"

TAG = "v0.3.5"
REPO = "argszero/emrg"
WORKFLOW = "build-release.yml"
RUN_ID = 36505953376


def _load_module():
    spec = importlib.util.spec_from_file_location("check_release_published", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeGh:
    """`_gh` replaced by a routing table: prefix of argv -> payload or exception.

    A route may be a dict/list (returned as JSON), a string (returned verbatim, for the
    paths that parse it themselves), or an exception instance (raised). Unrouted calls
    raise, so a test that forgets a route fails loudly instead of reading a default.
    """

    def __init__(self, routes: dict[tuple[str, ...], object]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        for prefix, payload in self.routes.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(payload, BaseException):
                    raise payload
                if isinstance(payload, str):
                    return payload
                return json.dumps(payload)
        raise AssertionError(f"unrouted gh call: gh {' '.join(args)}")

    def called(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


def _gh_error(returncode: int | None, detail: str = "boom"):
    def factory(mod, args):
        return mod.GhError(returncode, args, detail)

    return factory


def _run(status="completed", conclusion="success", run_id=RUN_ID) -> dict:
    return {
        "databaseId": run_id,
        "headBranch": TAG,
        "status": status,
        "conclusion": conclusion,
        "displayTitle": "emrg: release v0.3.5 (#1720)",
        "url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }


def _release(draft=False, prerelease=False, assets=("EMRG-0.3.5-macos-arm64.pkg",)) -> dict:
    return {
        "tag_name": TAG,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": name} for name in assets],
    }


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh", fake)


def _routes(**overrides):
    routes = {
        ("run", "list"): overrides.get("runs", [_run()]),
        ("run", "view"): {
            "jobs": overrides.get("jobs", [{"name": "build / macos", "conclusion": "success"}])
        },
        ("api", f"repos/{REPO}/releases/tags/{TAG}"): overrides.get("release", _release()),
        ("api", f"repos/{REPO}/releases/latest"): overrides.get("latest", {"tag_name": TAG}),
    }
    routes.update(overrides.get("extra", {}))
    # `latest=None` deliberately removes the route, so "not readable" can be exercised.
    if overrides.get("latest") is None and "latest" in overrides:
        routes.pop(("api", f"repos/{REPO}/releases/latest"), None)
    return routes


def _install_missing_release(mod, monkeypatch, routes) -> None:
    """`gh` answers 404 for this tag's release, and the routing table for everything else.

    404 is the *definite* absence (`release_for_tag` returns None for it), which is the
    state "nothing was ever published for this tag" -- distinct from a call that failed.
    The error is built the way `_gh` builds it from a real call: process code 1, the
    status in the message (measured 2026-09-29; `gh api` exits 1 for 404 and for 500).
    """
    def _gh(args):
        if tuple(args[:2]) == ("api", f"repos/{REPO}/releases/tags/{TAG}"):
            raise mod.GhError(1, args, "gh: Not Found (HTTP 404)", http_status=404)
        return FakeGh({k: v for k, v in routes.items() if v is not None})(args)

    monkeypatch.setattr(mod, "_gh", _gh)


def _check(mod, monkeypatch, capsys, routes):
    fake = FakeGh(routes)
    _install(mod, monkeypatch, fake)
    code = mod.check(TAG, REPO)
    return code, capsys.readouterr().out, fake


# --------------------------------------------------------------------------- 0


def test_a_green_run_and_a_published_release_is_the_only_pass(mod, monkeypatch, capsys):
    code, out, _ = _check(mod, monkeypatch, capsys, _routes())
    assert code == 0
    assert out.splitlines()[0] == f"repo: {REPO}"
    assert out.splitlines()[1] == f"tag: {TAG}"
    assert "build run: 36505953376 completed/success" in out
    assert "draft=False prerelease=False assets=1" in out
    assert "OK: v0.3.5 built green and is published" in out


def test_the_asset_names_are_printed_so_the_reading_is_inspectable(mod, monkeypatch, capsys):
    """The count alone would hide which platform is missing (the v0.3.1 failure)."""
    release = _release(assets=("a.pkg", "b.AppImage", "c.exe"))
    code, out, _ = _check(mod, monkeypatch, capsys, _routes(release=release))
    assert code == 0
    assert "asset: a.pkg" in out and "asset: b.AppImage" in out and "asset: c.exe" in out


def test_latest_is_reported_not_asserted(mod, monkeypatch, capsys):
    """An older tag may legitimately be republished; the assertion lives in the run."""
    routes = _routes(latest={"tag_name": "v9.9.9"})
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 0
    assert "github Latest: v9.9.9" in out
    assert "(this tag)" not in out


# --------------------------------------------------------------------------- 2


def test_a_run_that_has_not_concluded_is_not_measurable_never_a_pass(mod, monkeypatch, capsys):
    routes = _routes(runs=[_run(status="in_progress", conclusion=None)])
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 2
    assert "still `in_progress`" in out
    assert "this is not a pass" in out
    assert "OK" not in out


def test_gh_missing_is_not_measurable(mod, monkeypatch, capsys):
    """`FileNotFoundError` on the `gh` binary is `GhError(returncode=None)`."""
    err = mod.GhError(None, ["run", "list"], "gh is not available on this machine")
    monkeypatch.setattr(mod, "runs_for_tag", lambda tag, repo: (_ for _ in ()).throw(err))
    code = mod.check(TAG, REPO)
    out = capsys.readouterr().out
    assert code == 2
    assert "not measurable" in out and "not a pass" in out


def test_an_unauthenticated_gh_is_not_measurable(mod, monkeypatch, capsys):
    def boom(args):
        raise mod.GhError(1, args, "gh: To get started with GitHub CLI, please run: gh auth login")

    monkeypatch.setattr(mod, "_gh", boom)
    code = mod.check(TAG, REPO)
    out = capsys.readouterr().out
    assert code == 2
    assert "gh auth status" in out


def test_a_non_json_answer_is_not_measurable(mod, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_gh", lambda args: "not json at all")
    code = mod.check(TAG, REPO)
    out = capsys.readouterr().out
    assert code == 2
    assert "not JSON" in out


# --------------------------------------------------------------------------- 1


def test_no_run_and_no_release_is_a_fault_naming_the_only_trigger(mod, monkeypatch, capsys):
    """Both halves absent: nothing was published, so the re-push remedy is the right one."""
    routes = _routes(runs=[])
    _install_missing_release(mod, monkeypatch, routes)
    code = mod.check(TAG, REPO)
    out = capsys.readouterr().out
    assert code == 1
    assert "no `build-release.yml` run exists" in out
    assert "only thing that creates a release" in out
    assert "git push origin" in out
    assert "no release exists for the tag either" in out


def test_no_run_but_a_published_release_is_not_measurable_never_a_fault(
    mod, monkeypatch, capsys
):
    """The state the 50-run window produced for v0.2.57 on 2026-09-29.

    The run exists; the reading simply did not see it. Reporting that as a fault handed
    out a remedy that deletes the tag of a live release, so a release that exists must
    make this unmeasurable instead -- and the verdict must not name the re-push.
    """
    code, out, _ = _check(mod, monkeypatch, capsys, _routes(runs=[]))
    assert code == 2
    assert "not measurable" in out
    assert "not a window limit" in out
    assert "a release for the tag DOES exist" in out
    assert "Do NOT re-push the tag" in out
    # The control: no re-push remedy, and never a pass.
    assert "git push origin" not in out
    assert "OK" not in out


def test_the_run_list_is_asked_for_by_the_tag_not_by_a_window(mod, monkeypatch, capsys):
    """A window over the workflow's runs answers about whichever tags are inside it."""
    _, _, fake = _check(mod, monkeypatch, capsys, _routes())
    assert fake.called("run", "list", "--repo", REPO, "--workflow", WORKFLOW, "--branch", TAG)


def test_a_red_run_names_the_jobs_that_failed(mod, monkeypatch, capsys):
    routes = _routes(
        runs=[_run(conclusion="failure")],
        jobs=[
            {"name": "build (macos-arm64)", "conclusion": "skipped"},
            {"name": "build (linux-x86_64)", "conclusion": "success"},
            {"name": "build (windows-x64)", "conclusion": "failure"},
            {"name": "release", "conclusion": "success"},
        ],
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "failed jobs: build (windows-x64)" in out
    # The control: a skipped platform leg is not a failure.
    assert "macos-arm64" not in out
    assert "gh run rerun" in out


def test_the_remedy_names_a_reading_that_answers(mod, monkeypatch, capsys):
    """The command a FAULT hands the reader has to be one that answers on a real host.

    Measured 2026-10-04 on v0.3.8's red run (`36956685533`), while this tool's own FAULT
    was being read: the remedy used to name `gh run view <id> --log-failed`, and that
    command returns **0 bytes with exit 0** on this host - for this run, for a green one
    and for a recent `Test` run, so it is the host's log path rather than one run's
    problem. An empty answer that exits 0 reads as "nothing to see", which arrives at the
    worst possible moment: the reader has just been told the release is missing. The
    remedy therefore names `scripts/read-run-failure.py`, which asks the API and keeps
    "no failed job" (rc 1) apart from "the log could not be read" (rc 2).

    Both halves are asserted on the **printed output** rather than on the source: the
    reading that matters is what a host sees, and a source-level `in` would pass for a
    string that never reaches a terminal. The negative half is the one that would have
    caught the defect - it pins that the broken command is not offered as an alternative
    beside the working one, because a remedy that lists both hands the reader the broken
    one at the moment they are least able to tell the difference.
    """
    routes = _routes(
        runs=[_run(conclusion="failure")],
        jobs=[{"name": "build (macos-15, arm64)", "conclusion": "failure"}],
    )
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert f"scripts/read-run-failure.py {RUN_ID}" in out, (
        "the remedy must name the reading that answers, with this run's id so the command "
        "is runnable as printed"
    )
    assert "--log-failed" not in out, (
        "the remedy offers `gh run view --log-failed`, which answers 0 bytes with exit 0 "
        "on a host this was measured on - the silent-empty shape that reading exists to "
        "replace"
    )
    # The other half of the remedy is untouched: a re-run is still the next step.
    assert f"gh run rerun {RUN_ID} --failed" in out


def test_a_cancelled_run_is_a_fault_not_a_pass(mod, monkeypatch, capsys):
    routes = _routes(runs=[_run(conclusion="cancelled")], jobs=[])
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "concluded `cancelled`" in out


def test_a_green_run_with_no_release_is_a_fault(mod, monkeypatch, capsys):
    """The state the workflow's own final step exists to report, read from outside."""
    routes = _routes()
    _install_missing_release(mod, monkeypatch, routes)
    code = mod.check(TAG, REPO)
    out = capsys.readouterr().out
    assert code == 1
    assert "no release exists for tag" in out
    assert "although its build run is green" in out


def test_a_draft_release_is_a_fault_with_its_remedy(mod, monkeypatch, capsys):
    routes = _routes(release=_release(draft=True))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "DRAFT" in out
    assert "--draft=false" in out
    assert "OK" not in out


def test_a_prerelease_is_a_fault_with_its_remedy(mod, monkeypatch, capsys):
    routes = _routes(release=_release(prerelease=True))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "PRERELEASE" in out
    assert "--prerelease=false" in out


def test_an_empty_asset_set_is_a_fault(mod, monkeypatch, capsys):
    routes = _routes(release=_release(assets=()))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "carries no asset at all" in out


def test_every_unusable_state_is_reported_at_once(mod, monkeypatch, capsys):
    """Draft + zero assets in one answer: the reader fixes both in one pass."""
    routes = _routes(release=_release(draft=True, assets=()))
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "DRAFT" in out and "carries no asset at all" in out


# ------------------------------------------------------------- the reader's own rules


def test_the_release_is_looked_up_for_the_tag_and_not_the_latest_one(mod, monkeypatch, capsys):
    """A guard that read `releases/latest` would answer about a different release."""
    _, _, fake = _check(mod, monkeypatch, capsys, _routes())
    assert fake.called("api", f"repos/{REPO}/releases/tags/{TAG}")


def test_the_run_is_matched_on_the_head_branch_of_the_tag(mod, monkeypatch, capsys):
    """The tag is compared against the run's own record, not against a title."""
    other = dict(_run(), headBranch="v0.3.4", databaseId=1)
    routes = _routes(runs=[other, _run()])
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 0
    assert f"build run: {RUN_ID} completed/success" in out


def test_only_the_publishing_workflow_is_asked(mod, monkeypatch, capsys):
    _, _, fake = _check(mod, monkeypatch, capsys, _routes())
    assert fake.called("run", "list", "--repo", REPO, "--workflow", "build-release.yml")


class _Proc:
    """Enough of `subprocess.CompletedProcess` for `_gh`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gh_carries_the_http_status_out_of_ghs_own_message(mod, monkeypatch):
    """`gh api` exits 1 for 404 exactly as it does for 500, so the code cannot ask.

    Measured 2026-09-29: `gh api repos/argszero/emrg/releases/tags/v9.9.9` prints
    `gh: Not Found (HTTP 404)` and exits 1. Without reading the status out of that
    message the absent-release branch is unreachable in production, and a tag that was
    never published reads as *not measurable* instead of as the fault it is.
    """
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: _Proc(1, stderr="gh: Not Found (HTTP 404)\n"),
    )
    with pytest.raises(mod.GhError) as excinfo:
        mod._gh(["api", f"repos/{REPO}/releases/tags/v9.9.9"])
    assert excinfo.value.returncode == 1
    assert excinfo.value.http_status == 404


def test_a_failure_that_is_not_http_shaped_carries_no_status(mod, monkeypatch):
    """`run list` refusing a workflow name is not an HTTP answer: nothing is inferred."""
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: _Proc(1, stderr="could not find any workflows named nope.yml\n"),
    )
    with pytest.raises(mod.GhError) as excinfo:
        mod._gh(["run", "list"])
    assert excinfo.value.http_status is None


def test_only_a_404_is_the_definite_absence(mod, monkeypatch):
    """A 500 is a question that could not be asked, and must propagate as one."""
    def not_found(args):
        raise mod.GhError(1, args, "gh: Not Found (HTTP 404)", http_status=404)

    monkeypatch.setattr(mod, "_gh", not_found)
    assert mod.release_for_tag(TAG, REPO) is None

    def server_error(args):
        raise mod.GhError(1, args, "gh: Server Error (HTTP 500)", http_status=500)

    monkeypatch.setattr(mod, "_gh", server_error)
    with pytest.raises(mod.GhError):
        mod.release_for_tag(TAG, REPO)
