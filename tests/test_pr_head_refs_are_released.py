"""A gate's parked PR-head ref is released before it returns (2026-09-17).

Background (cycle `cyc20260917-155538`)
---------------------------------------
Five gates fetch a PR's real head into a fixed `refs/<tool>/pr<N>`, and nothing
removed it again. Measured on the main tree before this change — one run of each
gate on a single PR each, `git for-each-ref` counted either side:

    refs/emrg-forecast/      104 -> 105   (check-merge-order.py)
    refs/emrg-merge-seq/      82 ->  83   (check-merge-sequence.py)
    refs/emrg-tree-health/    39 ->  40   (check-merge-tree-health.py)
    refs/emrg-landing-diff/   24 ->  25   (check-merge-landing-diff.py)

One ref per PR per run, kept for the life of the clone, and each one pins that
head's commits and trees. These gates are run *every cycle* as merge gates, so the
growth is monotonic and unbounded.

The fix is deliberately not "clean the refs up at the end of `main()`". That shape
is what the sibling `check-merge-plan-suite.py` had to repair in PR #1325, where
only the paths that reached the verdict ran the cleanup they had: a run that died
fetching leaked everything it had already parked. Dropping the ref the moment the
commit has been read cannot be skipped by an early return, a raise, or a kill.

Both directions are asserted, because "releases more" is also what a broken gate
does: the ref really exists before the drop (otherwise "it is gone" is evidence of
nothing), a drop of something absent is not an error, and a *failed fetch* leaves
nothing dropped — there is no ref to drop and the caller must still get its error.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

SHA = "a" * 40

# tool module, its ref namespace, and how to call `_fetch_head`
GATES = (
    ("check_merge_order", "check-merge-order", "refs/emrg-forecast", True),
    ("check_merge_sequence", "check-merge-sequence", "refs/emrg-merge-seq", False),
    ("check_merge_tree_health", "check-merge-tree-health", "refs/emrg-tree-health", False),
    ("check_merge_landing_diff", "check-merge-landing-diff", "refs/emrg-landing-diff", False),
)


def _load(name: str, script: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_run(calls: list[list[str]], fetch_rc: int = 0, stderr: str = ""):
    """A subprocess runner that records argv and answers fetch/rev-parse/update-ref."""

    def run(argv, *args, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["git", "fetch"] and fetch_rc != 0:
            return subprocess.CompletedProcess(argv, fetch_rc, "", stderr)
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, SHA + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


# ── the shared helper, on real git ─────────────────────────────────────────

def _repo_with_a_ref(tmp_path: Path, ref: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
        )

    assert git("init", "-q", "-b", "main").returncode == 0
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    assert git("update-ref", ref, "HEAD").returncode == 0
    return repo


def _merge_tree():
    """The family's shared module, imported the way the gates import it."""
    import sys

    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge_tree
    finally:
        sys.path.pop(0)
    return merge_tree


def test_drop_ref_removes_a_ref_that_is_there(tmp_path: Path) -> None:
    merge_tree = _merge_tree()

    ref = "refs/emrg-forecast/pr1322"
    repo = _repo_with_a_ref(tmp_path, ref)

    def git(*args: str):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
        )

    # Positive control for the instrument: the ref must be there to be dropped, or
    # "it is gone afterwards" measures nothing.
    assert git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0

    def run(argv):
        return subprocess.run(
            argv, cwd=repo, capture_output=True, text=True, encoding="utf-8"
        )

    proc = merge_tree.drop_ref(ref, run=run)
    assert proc.returncode == 0, proc.stderr
    assert git("rev-parse", "--verify", ref).returncode != 0, "the ref survived the drop"


def test_drop_ref_of_a_ref_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    """A failed fetch parks nothing, so the drop must be tolerant of absence."""
    merge_tree = _merge_tree()
    repo = _repo_with_a_ref(tmp_path, "refs/emrg-forecast/pr1")

    def run(argv):
        return subprocess.run(
            argv, cwd=repo, capture_output=True, text=True, encoding="utf-8"
        )

    proc = merge_tree.drop_ref("refs/emrg-forecast/pr999", run=run)
    assert proc.returncode == 0, proc.stderr


# ── the wiring, per gate ───────────────────────────────────────────────────

@pytest.mark.parametrize("name, script, namespace, needs_repo", GATES)
def test_a_gate_returns_a_sha_and_drops_its_ref(
    name: str, script: str, namespace: str, needs_repo: bool, monkeypatch
) -> None:
    mod = _load(name, script)
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", _fake_run(calls))

    head = mod._fetch_head("argszero/emrg", 7) if needs_repo else mod._fetch_head(7)

    assert head == SHA, "the caller must get the commit, not a mutable ref name"
    ref = f"{namespace}/pr7"
    fetches = [c for c in calls if c[:2] == ["git", "fetch"]]
    drops = [c for c in calls if c[:3] == ["git", "update-ref", "-d"]]
    assert fetches == [["git", "fetch", "--quiet", "origin", f"+pull/7/head:{ref}"]]
    assert drops == [["git", "update-ref", "-d", ref]], (
        "the parked ref must be released by the call that parked it"
    )
    # Order matters: the delete is after the commit is read, never before.
    assert calls.index(drops[0]) > calls.index(
        next(c for c in calls if c[:2] == ["git", "rev-parse"])
    )


@pytest.mark.parametrize("name, script, namespace, needs_repo", GATES)
def test_a_failed_fetch_drops_nothing_and_still_raises(
    name: str, script: str, namespace: str, needs_repo: bool, monkeypatch
) -> None:
    """Nothing was parked, so there is nothing to release — and the error is the answer."""
    mod = _load(name, script)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        mod, "_run", _fake_run(calls, fetch_rc=1, stderr="fatal: couldn't find remote ref")
    )

    with pytest.raises(Exception, match="couldn't find remote ref"):
        mod._fetch_head("argszero/emrg", 7) if needs_repo else mod._fetch_head(7)

    assert [c for c in calls if c[:3] == ["git", "update-ref", "-d"]] == []
    assert [c for c in calls if c[:2] == ["git", "rev-parse"]] == []


def test_every_gate_that_parks_a_ref_releases_it() -> None:
    """Structural half: a future edit cannot silently drop the release again.

    `check-merge-plan-suite.py` is deliberately **not** in this list: open PR #1325
    owns that function's cleanup, and touching the same lines here would guarantee a
    conflict with it. It is expected to adopt `merge_tree.drop_ref` once #1325 lands.
    """
    for _name, script, namespace, _needs_repo in GATES:
        source = (SCRIPTS / f"{script}.py").read_text(encoding="utf-8")
        assert f'"{namespace}/pr{{number}}"' in source or f"{namespace}/pr" in source, (
            f"{script}: the namespace this test asserts on must be the one it uses"
        )
        tree = ast.parse(source)
        fetch_heads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_fetch_head"
        ]
        assert len(fetch_heads) == 1, f"{script}: expected exactly one _fetch_head"
        called = {
            ast.unparse(node.func)
            for node in ast.walk(fetch_heads[0])
            if isinstance(node, ast.Call)
        }
        assert "merge_tree.drop_ref" in called, (
            f"{script}: _fetch_head parks {namespace}/pr<N> and must release it"
        )
