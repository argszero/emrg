"""A read that failed must say what arrived — and must never become an ordinary state.

Two defects of one shape, both measured in `scripts/` on 2026-10-03
(`cyc20261003-015225`) by running the guards against a `gh` stand-in that answers with
valid JSON of the wrong shape:

**1. The empty message.** Ten asserts in `scripts/` were written `assert isinstance(x,
dict)` with no message. `main()` of each tool prints `f"error: {exc}"` and an
`AssertionError` carries no text, so a host whose `gh` answered with a list where an
object was expected got exactly this, and nothing else:

    error:

The code was right (`2`, could not measure) and the sentence was empty — the same
"a message that names nothing costs a cycle" defect this repo has fixed repeatedly, and
the family already carried the good spelling two files over: `check-pr-base.py` prints
`cannot determine PR bases: expected a list of PRs, got dict`.

**2. The fabricated ordinary state.** `check-merge-landed.py` read the PR payload with
`view.get("merged")` and branched on its truth. A payload without that key is falsy, so
the tool answered **PENDING — "nothing has landed yet"** and exited **0**, with the state
rendered as a hole (`#1818 is  and unmerged`). Its own docstring calls `PENDING` the
quieter hole of the two states it announces on stderr; nothing checked that the field
*was read*. The sibling states the rule and the reason:
`check-vote-count.py::_merge_state` refuses a payload whose `mergeable` went missing,
"since an absent conflict is indistinguishable from no conflict".

What is asserted here, and the control that keeps it honest
----------------------------------------------------------
* **the source rule, mechanically over `scripts/`** — no `assert` may be written without
  a message. This is the whole population, not a sample: there are 11 asserts in the
  directory and the rule is absolute, so there is nothing to exempt and no registry that
  can rot;
* **the control** — the scan must *find* asserts. A rule of the form "no X may lack Y"
  passes vacuously when X disappears (a refactor that turned every assert into a raise),
  and a vacuous pass is indistinguishable from a real one from the verdict alone;
* **the behaviour, executed** — the four guards that read `gh` payloads are run against a
  stand-in answering with a list, a bare object and `null`; no failure line may be a bare
  `error:`, and `check-merge-landed.py` may not report a state at all from a payload whose
  merge flag was not read;
* **the other direction** — the same stand-in with the *honest* payloads still produces
  `PENDING`/0 for a genuinely unmerged PR, so the refusal above cannot be satisfied by a
  tool that refuses everything.

Hermetic: the stand-in is a script in `tmp_path`, no network, no GitHub, no real `gh`.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

#: The guards whose failure line a host reads, and the arguments each needs to run.
GH_READERS = [
    ("review-queue.py", []),
    ("check-vote-count.py", ["1818"]),
    ("check-merge-freshness.py", ["1818"]),
    ("check-merge-landed.py", ["1818"]),
]

#: Payloads `gh` could really answer with: valid JSON, wrong shape for the caller.
WRONG_SHAPES = ["[]", "{}", "null", '[{"number": 1}]']

_GH_STUB = '''#!/usr/bin/env python3
"""Stand in for `gh` and print whatever GH_PAYLOAD says."""
import os, sys
sys.stdout.write(os.environ.get("GH_PAYLOAD", "[]") + "\\n")
'''

_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the stand-in is a shebang script; on Windows that measures the stub's "
        "absence rather than the guard. The guards themselves are cross-platform, "
        "and the source rule above runs everywhere."
    ),
)


def _message_is_empty(msg: ast.expr | None) -> bool:
    """True when this assert's message renders as nothing.

    `None` and an empty constant both render as `""` through `error: {exc}` — the
    second is the shape a hurried `assert x, ""` leaves behind, and it is the same
    defect wearing a message.
    """
    if msg is None:
        return True
    return isinstance(msg, ast.Constant) and not str(msg.value).strip()


def _asserts_without_a_message() -> list[str]:
    """`file:line` for every `assert` in `scripts/` whose message renders as nothing."""
    bare: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert) and _message_is_empty(node.msg):
                bare.append(f"{path.name}:{node.lineno} (assert {ast.unparse(node.test)})")
    return bare


def _assert_count() -> int:
    total = 0
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))
    return total


def test_every_assert_in_the_scripts_carries_a_message() -> None:
    """`assert` without a message is `error:` — the host learns nothing.

    The failure this pins is not stylistic: each of these asserts guards a read whose
    result decides a verdict, and every tool in the family renders the exception as
    `error: {exc}`. With no message that line is the word `error:` and a colon.
    Measured before the fix, ten sites: four in `check-vote-count.py`, four in
    `check-merge-freshness.py`, one in `check-merge-landed.py`, one in
    `review-queue.py`.
    """
    bare = _asserts_without_a_message()
    assert not bare, (
        "these asserts would render as a bare `error:` when they fire, because every "
        "tool in this family prints `error: {exc}` and an AssertionError has no text:\n  "
        + "\n  ".join(bare)
        + "\n\nGive each one a message that names what was expected and what arrived "
        "(the spelling `check-pr-base.py` already uses: `expected a list of PRs, got "
        "dict`), or raise the tool's own error type with that message."
    )


def test_the_rule_is_scanning_something() -> None:
    """The control: a rule of the form "no X may lack Y" must find its X.

    Without this, a refactor that turned every assert in `scripts/` into a `raise`
    would satisfy the rule above *and* read as a pass. The number is not pinned (it
    may legitimately change), only its non-emptiness.
    """
    assert _assert_count() > 0, (
        "no `assert` was found anywhere in scripts/ - either the directory moved (and "
        "the rule above is now scanning nothing) or the guards stopped asserting on the "
        "shapes they read"
    )


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return bindir


def _run_guard(script: str, args: list[str], payload: str, stub_bin: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GH_PAYLOAD"] = payload
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _says_nothing(line: str) -> bool:
    """True when this line is a failure report whose cause is blank.

    Two shapes, one defect. The tool prints `error: {exc}`, so an assert with no message
    gives `error:` exactly — and a wrapper that adds context around it gives
    `error: could not list open PRs in argszero/emrg:` with nothing after the colon,
    which is what `review-queue.py` printed on master for payloads `{}` and `null`.
    A line is blank-caused when everything after its last colon is empty; the messages
    that carry a reading ("... answered with list, not the object those fields arrive
    in") do not end on a colon.
    """
    stripped = line.strip()
    if not stripped.startswith("error"):
        return False
    if ":" not in stripped:
        return True
    return not stripped.rsplit(":", 1)[-1].strip()


@_posix_only
def test_no_guard_answers_a_wrong_shaped_payload_with_an_empty_message(
    stub_bin: Path,
) -> None:
    """The behaviour the assert rule is about, measured rather than inferred.

    Each guard is asked a question with a `gh` that answers a shape it cannot use. The
    exit code must be `2` (could not measure — never `1`, which is a verdict in this
    family) and no line of the report may be the tool's own message with nothing after
    it, which is what an `AssertionError` with no message renders as.
    """
    for script, args in GH_READERS:
        for payload in WRONG_SHAPES:
            proc = _run_guard(script, args, payload, stub_bin)
            combined = proc.stdout + proc.stderr
            assert proc.returncode == 2, (
                f"{script} answered {proc.returncode} for payload {payload!r} - anything "
                f"but 2 claims a reading it did not take.\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
            empty = [line for line in combined.splitlines() if _says_nothing(line)]
            assert not empty, (
                f"{script} reported payload {payload!r} with no cause in its message - the "
                f"host has nothing to act on.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
            assert "Traceback" not in proc.stderr, (
                f"{script} crashed on payload {payload!r} instead of reporting it:\n"
                f"{proc.stderr}"
            )


@_posix_only
def test_a_payload_that_does_not_say_whether_it_merged_is_refused(stub_bin: Path) -> None:
    """`check-merge-landed.py` may not answer `PENDING` from a flag it never read.

    Measured before the fix: payload `{}` and payload `{"a": 1}` both printed
    `#1818 is  and unmerged, so nothing has landed yet` and exited **0** — an unread
    merge flag read as an ordinary state, with the state itself rendered as a hole.
    """
    for payload in ["{}", '{"a": 1}', '{"state": "closed"}']:
        proc = _run_guard("check-merge-landed.py", ["1818"], payload, stub_bin)
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, (
            f"payload {payload!r} was not refused: rc={proc.returncode}\n{combined!r}"
        )
        assert "does not say whether it merged" in combined, (
            f"the refusal for payload {payload!r} does not name the fields it could not "
            f"read:\n{combined!r}"
        )
        assert "PENDING" not in combined, (
            f"payload {payload!r} was still reported as the ordinary PENDING state, "
            f"which is what the refusal exists to prevent:\n{combined!r}"
        )


@pytest.mark.skipif(
    not (REPO_ROOT / "scripts" / "merge_tree.py").exists(),
    reason="the sibling this tool borrows its reading from is missing",
)
def test_the_honest_unmerged_answer_still_reads_as_pending() -> None:
    """The other direction, through the module rather than the stand-in.

    A rule that refused *everything* would satisfy the test above, so this pins the
    honest case at the function the refusal lives in: a payload that really carries
    `merged: False` is still `PENDING`. (The end-to-end leg for it is
    `tests/test_check_merge_landed.py::test_an_unmerged_pr_is_pending_and_compares_nothing`.)
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_merge_landed_for_this_test", SCRIPTS / "check-merge-landed.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    honest = {"state": "open", "merged": False, "merge_commit_sha": None}
    original = mod._gh_json
    try:
        mod._gh_json = lambda args: honest  # type: ignore[assignment]
        verdict = mod.check_pr(1613, "argszero/emrg")
    finally:
        mod._gh_json = original  # type: ignore[assignment]
    assert verdict.reading == mod.PENDING, verdict
    assert "open" in verdict.reason, verdict.reason

    for broken in [{}, {"state": "closed"}, {"merged": "yes", "state": "closed"}]:
        try:
            mod._gh_json = lambda args, _b=broken: _b  # type: ignore[assignment]
            with pytest.raises(RuntimeError, match="does not say whether it merged"):
                mod.check_pr(1613, "argszero/emrg")
        finally:
            mod._gh_json = original  # type: ignore[assignment]
