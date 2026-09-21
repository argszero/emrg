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

The structural half is keyed on the **ref**, not on a function's name (issue #1330,
2026-09-17). The property is "nothing parks `refs/<gate>/…` without releasing it",
and `_fetch_head` is only what four of the five gates happen to call their parking
site: `check-merge-plan-suite.py` parked `refs/emrg-plan-suite/tip` inside
`_suite_verdict` at the time. A name-keyed guard cannot see that shape at all —
measured on the tree that introduced this file, adding a second, unreleased
`_park_head_for_forecast` to `scripts/check-merge-order.py` left the guard green
(11 passed) while the run counted one more resident ref (105 → 106). So the walker
below finds *every* site that writes into the gate's own namespace and asks each
one whether it releases; the walker's two directions are themselves fixture-tested.

That fifth shape has since moved twice, and both moves are why the walker resolves
*names* rather than literals. The tip ref was removed on 2026-09-21 (it was one
fixed name two overlapping runs of the same tool could swap, so each reported a
tree the other was measuring — `cyc20260921-172459`), and the gate's remaining ref
is written as `f"{PLAN_REF_PREFIX}{number}"`, a module constant interpolated into
the ref's text. A walker that reads only string *literals* sees neither the
constant nor the site, which is the blind spot its own "no parking site found"
assertion exists to announce — so it resolves module-level constants, whole
(`TIP_REF = "…"`) and interpolated (`f"{PLAN_REF_PREFIX}{number}"`).
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

# Every gate that writes a ref of its own, for the structural half — by namespace,
# not by function name, so the fifth one (whose parking site is `_suite_verdict`)
# is in scope too. Its cleanup landed with #1325, so the exclusion this list used to
# carry (a live PR owning those lines) no longer applies.
PARKING_GATES = (
    ("check-merge-order.py", "refs/emrg-forecast"),
    ("check-merge-sequence.py", "refs/emrg-merge-seq"),
    ("check-merge-tree-health.py", "refs/emrg-tree-health"),
    ("check-merge-landing-diff.py", "refs/emrg-landing-diff"),
    ("check-merge-plan-suite.py", "refs/emrg-plan-suite"),
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


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """`NAME = "literal"` at module level, so a ref that lives in a constant resolves."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = node.value.value
    return out


def _static_text(node: ast.AST, constants: dict[str, str]) -> str | None:
    """The static text of a string expression, or None when it is not static.

    An f-string contributes its literal parts (`f"refs/x/pr{n}"` → `"refs/x/pr"`);
    a name contributes its bound literal, if any. Anything else is not static — and
    "not static" must not be read as "does not park", which is why the sites this
    walker *cannot* resolve are reported rather than dropped (`_bound_refs` keeps
    only what it resolved, so an unresolvable refspec yields no site, and the
    per-gate "at least one site" assertion below is what keeps that honest).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                # A module-level constant interpolated into the ref's text - the shape
                # `check-merge-plan-suite.py::_fetch_head` uses, `f"{PLAN_REF_PREFIX}{number}"`.
                # The interpolated number contributes nothing, so what comes back is a
                # *prefix* of the real ref, which is all the caller asks of it (it matches
                # by `startswith`). Without this the walker saw no site at all in that
                # gate once the plan tip stopped being a ref, and said so through the
                # "the walker is blind" assertion rather than passing quietly - the reason
                # that assertion exists (measured 2026-09-21, `cyc20260921-172459`).
                parts.append(constants.get(value.value.id, ""))
        return "".join(parts) or None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _bound_refs(fn: ast.AST, constants: dict[str, str]) -> dict[str, str]:
    """`ref = <string expression>` bindings inside one function body."""
    out: dict[str, str] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        text = _static_text(node.value, constants)
        if text is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = text
    return out


def _git_argv(call: ast.Call) -> list[str] | None:
    """The literal words at the head of a call's argv, if that argv starts with `git`.

    Only the literal words: `["git", "fetch", "--quiet", "origin", f"+pull/…:{ref}"]`
    yields the first four, and the refspec — the part that says *where* — is left to
    `_names_the_namespace`, which reads the whole argument rather than the words.
    """
    for arg in call.args:
        if not isinstance(arg, (ast.List, ast.Tuple)):
            continue
        words = [
            elt.value
            for elt in arg.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        if words and words[0] == "git":
            return words
    return None


def _names_the_namespace(node: ast.AST, bound: dict[str, str], constants: dict[str, str],
                         namespace: str) -> bool:
    """Does this argument *name* the gate's own ref — literally, or by a bound name?

    Scoped to the call's arguments on purpose. These files discuss their namespace
    in docstrings, so a function-wide (let alone module-wide) text search is not a
    parking signal; the argv is.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value.startswith(namespace):
                return True
        if isinstance(sub, ast.Name):
            if bound.get(sub.id, "").startswith(namespace):
                return True
            if constants.get(sub.id, "").startswith(namespace):
                return True
    return False


def _parking_sites(source: str, namespace: str) -> list[tuple[str, int, bool]]:
    """[(function, line, releases)] — every function that writes into `namespace`.

    A *park* is a git invocation that puts a ref there: `fetch` (the forced-refspec
    form), or `update-ref <ref>` (which `-d` makes a release, not a park). A
    *release* is either the family's `merge_tree.drop_ref(ref, …)` or the gate's own
    `git update-ref -d <ref>` — both are how the five gates let one go — and it has
    to sit in the same function, since the whole point is that an early return, a
    raise or a kill cannot skip it.
    """
    tree = ast.parse(source)
    constants = _module_strings(tree)
    sites: list[tuple[str, int, bool]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = _bound_refs(fn, constants)

        def parks(call: ast.Call) -> bool:
            return any(
                _names_the_namespace(arg, bound, constants, namespace) for arg in call.args
            )

        parked_at: list[int] = []
        released = False
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            argv = _git_argv(call)
            writes = argv is not None and argv[1:2] == ["fetch"]
            writes = writes or (argv is not None and argv[1:3] == ["update-ref"]
                                and "-d" not in argv[2:])
            if writes and parks(call):
                parked_at.append(call.lineno)
            if isinstance(call.func, ast.Attribute) and \
                    ast.unparse(call.func) == "merge_tree.drop_ref" and parks(call):
                released = True
            if argv is not None and argv[1:3] == ["update-ref", "-d"] and parks(call):
                released = True
        if parked_at:
            sites.append((fn.name, min(parked_at), released))
    return sorted(sites, key=lambda row: row[1])


@pytest.mark.parametrize("script, namespace", PARKING_GATES)
def test_a_gate_that_parks_a_ref_releases_it(script: str, namespace: str) -> None:
    """Structural half: a future edit cannot silently drop the release again."""
    source = (SCRIPTS / script).read_text(encoding="utf-8")
    assert f'"{namespace}/pr{{number}}"' in source or f"{namespace}/" in source, (
        f"{script}: the namespace this test asserts on must be the one it uses"
    )

    sites = _parking_sites(source, namespace)
    # An instrument that finds nothing would pass every leak test ever written.
    assert sites, f"{script}: no parking site found — the walker is blind, not the gate clean"

    leaks = [f"{name}() line {line}" for name, line, released in sites if not released]
    assert not leaks, (
        f"{script}: parks {namespace}/… and never releases it: {leaks}"
    )


# ── the walker's own two directions ────────────────────────────────────────

INJECTED = '''
def _park_head_for_forecast(number):
    ref = f"refs/emrg-forecast/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    return _rev_parse(ref)
'''

INJECTED_WITH_RELEASE = INJECTED.replace(
    "    return _rev_parse(ref)",
    "    sha = _rev_parse(ref)\n    merge_tree.drop_ref(ref, run=_run)\n    return sha",
)


def test_the_walker_sees_a_parking_site_that_is_not_named_fetch_head() -> None:
    """The positive control: #1330's exact shape, and it must be caught."""
    assert _parking_sites(INJECTED, "refs/emrg-forecast") == [
        ("_park_head_for_forecast", 4, False)
    ]


def test_the_walker_credits_a_release_and_only_for_that_ref() -> None:
    """The other direction: a site that does release must not be reported as a leak."""
    assert _parking_sites(INJECTED_WITH_RELEASE, "refs/emrg-forecast") == [
        ("_park_head_for_forecast", 4, True)
    ]
    # …and a release of *some other* ref is not a release of this one.
    elsewhere = INJECTED_WITH_RELEASE.replace(
        "drop_ref(ref, run=_run)", "drop_ref(refs_emrg_merge_seq, run=_run)"
    )
    assert _parking_sites(elsewhere, "refs/emrg-forecast") == [
        ("_park_head_for_forecast", 4, False)
    ]


def test_the_walker_reads_a_ref_that_lives_in_a_module_constant() -> None:
    """A bare module constant as the whole ref — `check-merge-plan-suite.py`'s tip shape.

    The tip is gone (2026-09-21), but the resolution it needed is not: a gate may name
    the ref through a constant, and a walker that reads only literals would report the
    module as having no parking site at all.
    """
    source = (
        'TIP_REF = "refs/emrg-plan-suite/tip"\n'
        "\n"
        "\n"
        "def _suite_verdict(tip):\n"
        '    updated = _run(["git", "update-ref", TIP_REF, tip])\n'
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        '        _run(["git", "update-ref", "-d", TIP_REF])\n'
    )
    assert _parking_sites(source, "refs/emrg-plan-suite") == [("_suite_verdict", 5, True)]
    # …and the same site with the `-d` taken away: it parks and never releases.
    leaking = source.replace('["git", "update-ref", "-d", TIP_REF]', '["git", "update-ref", TIP_REF]')
    assert _parking_sites(leaking, "refs/emrg-plan-suite") == [("_suite_verdict", 5, False)]


def test_the_walker_reads_a_prefix_that_lives_in_a_module_constant() -> None:
    """The shape plan-suite uses now: `f"{PLAN_REF_PREFIX}{number}"`.

    Built from a constant plus a *loop variable*, so neither half is a literal of the
    ref: the constant contributes `refs/emrg-plan-suite/pr` and the number contributes
    nothing, which is enough because the caller only asks whether the text starts with
    the namespace. Without this the walker found no site in that gate at all and failed
    through its "the walker is blind" assertion — measured 2026-09-21
    (`cyc20260921-172459`), on the revision that removed the tip ref.
    """
    source = (
        'PLAN_REF_PREFIX = "refs/emrg-plan-suite/pr"\n'
        "\n"
        "\n"
        "def _fetch_head(number):\n"
        "    ref = f'{PLAN_REF_PREFIX}{number}'\n"
        '    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])\n'
        "    commit = _rev_parse(ref)\n"
        "    merge_tree.drop_ref(ref, run=_run)\n"
        "    return commit\n"
    )
    assert _parking_sites(source, "refs/emrg-plan-suite") == [("_fetch_head", 6, True)]
    # The other direction, and the one that matters: take the release away and the site
    # must be reported as a leak rather than silently resolved to something else.
    leaking = source.replace("    merge_tree.drop_ref(ref, run=_run)\n", "")
    assert _parking_sites(leaking, "refs/emrg-plan-suite") == [("_fetch_head", 6, False)]
    # …and a constant that does *not* name this namespace is not a site in it, so the
    # resolution above cannot manufacture one.
    elsewhere = source.replace(
        '"refs/emrg-plan-suite/pr"', '"refs/emrg-merge-seq/pr"'
    )
    assert _parking_sites(elsewhere, "refs/emrg-plan-suite") == []
