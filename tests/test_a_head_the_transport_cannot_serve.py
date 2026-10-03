"""A PR head the transport cannot serve is reached through the API (2026-10-03).

The family's gates read a PR's head with `git fetch origin +pull/<N>/head:<ref>`, and
on this host `origin` is **not** the remote the PR lives on: `.git/config` carries

    url.C:/Users/Administrator/.emrg/evolution/emrg/.insteadof https://github.com/argszero/emrg.git

(this host's offline fallback, deliberate and not to be removed), so the fetch asks a
local clone that has no `refs/pull/*`, and five of the six merge gates answered nothing
at all - every cycle - while the same template requires their readings to decide a
merge. Measured in `cyc20261003-194944`:

    #1836: could not measure: could not fetch PR #1836: fatal: couldn't find remote
    ref pull/1836/head

Both directions are asserted here, because "falls back" is also what a gate that has
stopped preferring the transport does: the fallback runs only after the fetch fails,
it is one API call plus the repository's own materialiser, and a fallback that cannot
deliver still has to hand the caller a refusal naming both attempts - on this host's
`origin` the fetch's own sentence is about the *checkout*, and alone it reads as a fact
about the PR.

The archive half is the step those gates reach *only* once a head is available, and it
hung here three times (two runs of `check-merge-tree-health.py`, one of
`check-merge-sequence.py`) inside `git archive`, with the process alive and idle and no
verdict. An unbounded wait is not a measurement, so it is bounded and reported.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

HEAD = "b" * 40


def load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: `dataclasses` resolves a field's annotation through
    # `sys.modules[cls.__module__]`, which is `None` for a module nobody registered.
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def completed(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


@pytest.fixture(scope="module")
def merge_tree():
    return load("merge_tree_for_test", "merge_tree.py")


class Recorder:
    """A runner that answers by rule and remembers what it was asked."""

    def __init__(self, *, api=None, git=None):
        self.calls: list[list[str]] = []
        self._api = api
        self._git = git

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["gh", "api"]:
            return self._api or completed(argv, 1)
        if argv[:2] == ["git", "cat-file"]:
            return self._git or completed(argv, 0)
        return completed(argv, 0)

    def named(self, *prefix) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


class TestTheApiFallback:
    def test_it_asks_the_api_then_the_repos_own_materialiser(self, merge_tree) -> None:
        """The three steps, in order, and the second one is the existing script."""
        run = Recorder(api=completed([], 0, HEAD + "\n"))
        sha, why = merge_tree.head_via_api("argszero/emrg", 1836, run=run)

        assert (sha, why) == (HEAD, "")
        assert run.calls[0][:3] == ["gh", "api", "repos/argszero/emrg/pulls/1836"]
        # The instrument already exists and is named, not reimplemented: a second
        # "rebuild a commit from the API" is the defect the shared module removed.
        script = run.calls[1]
        assert Path(script[1]).name == "sync-master-from-api.py"
        assert script[2:] == ["--repo", "argszero/emrg", "--ref", HEAD]
        assert run.calls[2][:2] == ["git", "cat-file"]

    def test_a_head_the_api_cannot_name_is_not_guessed(self, merge_tree) -> None:
        run = Recorder(api=completed([], 1, "", "HTTP 404: Not Found"))
        sha, why = merge_tree.head_via_api("argszero/emrg", 7, run=run)

        assert sha == ""
        assert "HTTP 404: Not Found" in why
        assert len(run.calls) == 1, "nothing is asked once the head is unknown"

    def test_an_answer_that_is_not_an_object_name_is_refused(self, merge_tree) -> None:
        """`spawned` is not a sha, and a sha is not checked by hoping it is one."""
        run = Recorder(api=completed([], 0, "  spawned  \n"))
        sha, why = merge_tree.head_via_api("argszero/emrg", 7, run=run)

        assert sha == ""
        assert "spawned" in why
        assert not run.named("git", "cat-file")

    def test_a_materialiser_that_fails_is_reported(self, merge_tree) -> None:
        calls: list[list[str]] = []

        def failing(argv, *args, **kwargs):
            calls.append(list(argv))
            if argv[:2] == ["gh", "api"]:
                return completed(argv, 0, HEAD + "\n")
            return completed(argv, 128, "", "fatal: unable to access the API")

        sha, why = merge_tree.head_via_api("argszero/emrg", 7, run=failing)

        assert sha == ""
        assert "unable to access the API" in why
        assert all(c[:2] != ["git", "cat-file"] for c in calls), "no commit is claimed"

    def test_a_named_commit_this_repository_does_not_have_is_reported(
        self, merge_tree
    ) -> None:
        """The materialiser's exit code is not the commit's presence - ask git."""
        run = Recorder(api=completed([], 0, HEAD + "\n"), git=completed([], 1))
        sha, why = merge_tree.head_via_api("argszero/emrg", 7, run=run)

        assert sha == ""
        assert "no such commit" in why


GATES = (
    # module, file, the function a caller reads a head through, then its arguments.
    # `check-merge-order.py` is the one gate whose `_fetch_head` already carries the
    # repository, so it composes the fallback where the others keep a sibling.
    ("check_merge_order", "check-merge-order.py", "_fetch_head", "argszero/emrg", 1),
    ("check_merge_landing_diff", "check-merge-landing-diff.py", "_head", 1, "argszero/emrg"),
    ("check_merge_plan_suite", "check-merge-plan-suite.py", "_head", 1, "argszero/emrg"),
    ("check_merge_sequence", "check-merge-sequence.py", "_head", 1, "argszero/emrg"),
    ("check_merge_tree_health", "check-merge-tree-health.py", "_head", 1, "argszero/emrg"),
)


class TestEachGateFallsBack:
    @pytest.mark.parametrize("module_name,filename,entry,first,second", GATES)
    def test_a_failed_fetch_is_not_yet_unmeasurable(
        self, module_name, filename, entry, first, second, monkeypatch
    ) -> None:
        """Every gate that parks a ref reaches for the API before it refuses."""
        mod = load(module_name + "_fallback", filename)
        asked: list[list[str]] = []

        def failing_run(argv, *args, **kwargs):
            asked.append(list(argv))
            return completed(argv, 1, "", "fatal: couldn't find remote ref pull/1/head")

        monkeypatch.setattr(mod, "_run", failing_run)
        with pytest.raises(Exception) as caught:
            getattr(mod, entry)(first, second)

        message = str(caught.value)
        assert "could not fetch PR #1" in message, "git's own sentence survives"
        assert "couldn't find remote ref" in message
        assert "the API could not name" in message, "and so does the fallback's"
        assert any(c[:2] == ["gh", "api"] for c in asked), "the API was really asked"

    @pytest.mark.parametrize("module_name,filename,entry,first,second", GATES)
    def test_a_transport_that_works_is_asked_first(
        self, module_name, filename, entry, first, second, monkeypatch
    ) -> None:
        """The API is the fallback, never the first move: it is a network round trip."""
        mod = load(module_name + "_first", filename)
        asked: list[list[str]] = []

        def working_run(argv, *args, **kwargs):
            asked.append(list(argv))
            return completed(argv, 0, HEAD + "\n")

        monkeypatch.setattr(mod, "_run", working_run)
        monkeypatch.setattr(mod, "_rev_parse", lambda ref: HEAD)
        assert getattr(mod, entry)(first, second) == HEAD

        assert asked[0][:2] == ["git", "fetch"], "the transport is still the fast path"
        assert all(c[:2] != ["gh", "api"] for c in asked)


def an_empty_tar() -> bytes:
    """A tarfile whose archive is empty: the extraction half has nothing to do."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        pass
    return buffer.getvalue()


class TestTheArchiveIsBounded:
    """The step a gate reaches only once it has a head, and the one that hung."""

    @pytest.mark.parametrize(
        "module_name,filename,function",
        (
            ("check_merge_tree_health", "check-merge-tree-health.py", "_guard_verdict"),
            ("check_merge_sequence", "check-merge-sequence.py", "_extract_tree"),
        ),
    )
    def test_an_archive_that_never_finishes_is_unmeasurable(
        self, module_name, filename, function, tmp_path, monkeypatch
    ) -> None:
        mod = load(module_name + "_timeout", filename)

        class NeverFinishes:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(an_empty_tar())
                self.returncode = None
                self.killed = False

            def wait(self, timeout=None):
                assert timeout == mod.ARCHIVE_TIMEOUT, "the bound is a real one"
                raise subprocess.TimeoutExpired("git", timeout)

            def poll(self):
                return 1 if self.killed else None

            def kill(self):
                self.killed = True

        stub = NeverFinishes()
        monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: stub)
        workdir = tmp_path / "tree"
        with pytest.raises(mod.MeasurementError, match="did not finish"):
            getattr(mod, function)("c" * 40, workdir)
        assert stub.killed, "the orphan is reclaimed, not left writing into a pipe"

    def test_an_archive_that_finishes_is_not_reported(self, tmp_path, monkeypatch) -> None:
        """The control arm: the bound is not a refusal that fires on every run."""
        mod = load("check_merge_tree_health_control", "check-merge-tree-health.py")

        class Finishes:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(an_empty_tar())
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def kill(self):  # pragma: no cover - the control arm never kills
                raise AssertionError("nothing to reclaim")

        monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: Finishes())
        workdir = tmp_path / "tree"
        workdir.mkdir()
        # No guard script in the (empty) tree, so the *next* step refuses - what this
        # asserts is only that the archive itself was not reported as a failure.
        with pytest.raises(mod.MeasurementError, match="is not present in the merged tree"):
            mod._guard_verdict("c" * 40, workdir)
