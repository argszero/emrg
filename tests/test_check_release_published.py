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


def test_no_run_for_the_tag_is_a_fault_naming_the_only_trigger(mod, monkeypatch, capsys):
    routes = _routes(runs=[])
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "no `build-release.yml` run exists" in out
    assert "only thing that creates a release" in out
    assert "git push origin" in out


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


def test_a_cancelled_run_is_a_fault_not_a_pass(mod, monkeypatch, capsys):
    routes = _routes(runs=[_run(conclusion="cancelled")], jobs=[])
    code, out, _ = _check(mod, monkeypatch, capsys, routes)
    assert code == 1
    assert "concluded `cancelled`" in out


def test_a_green_run_with_no_release_is_a_fault(mod, monkeypatch, capsys):
    """The state the workflow's own final step exists to report, read from outside."""
    routes = _routes()
    routes.pop(("api", f"repos/{REPO}/releases/tags/{TAG}"))
    routes[("api", f"repos/{REPO}/releases/tags/{TAG}")] = None

    def _gh(args):
        if tuple(args[:2]) == ("api", f"repos/{REPO}/releases/tags/{TAG}"):
            raise mod.GhError(404, args, "gh: Not Found (HTTP 404)")
        return FakeGh({k: v for k, v in routes.items() if v is not None})(args)

    monkeypatch.setattr(mod, "_gh", _gh)
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
