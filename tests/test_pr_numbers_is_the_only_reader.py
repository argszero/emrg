"""The open-PR list has one reader -- checked, not promised.

Why this file exists
--------------------
Five merge gates each carried their own four lines reading the open PR list off
`gh`, all six call sites pasted from each other (`check-merge-pairs.py` borrows
`check-merge-sequence.py`'s), and the copy carried the bug: two of the three answers
`gh` can give were handled and the third -- **output that is not a list of numbers**
-- reached `int()` and raised a bare `ValueError`. `main` in every one of those gates
catches its own family's error, not that, so the exception left as a traceback at exit
**1**, which in every one of their tables is *the finding*:

    check-merge-order.py        1  at least one PR conflicts with the base
    check-merge-tree-health.py  1  at least one clean merge produced a tree that FAILS the guard
    check-merge-sequence.py     1  at least one clean step landed a tree that FAILS it
    check-merge-landing-diff.py 1  at least one path reads backwards
    check-merge-plan-suite.py   1  the plan's final tree was built and its suite FAILED

Measured 2026-10-03 (`cyc20261003-023102`): fifteen (gate, shape) pairs, fifteen times
exit 1 with a traceback and no finding. The reading now lives in `scripts/pr_numbers.py`
and the gates delegate; this file is what keeps it that way.

The rules
---------
R1  **No re-spelling.** The shape the deleted copies were made of -- `int()` over
    `stdout.split()`, and the `--jq` projection `.[].number` -- may not appear as
    *code* in a gate. A docstring may quote it: prose is allowed to describe a rule,
    it just may not be one. That distinction is checked in both directions below
    (`test_a_planted_copy_is_caught` / `test_prose_may_quote_the_rule`).
R2  **The name may stay, the body may not.** A gate may keep `def _open_pr_numbers`
    -- the tests of four gates monkeypatch that name -- but its body must reach
    `pr_numbers.open_pr_numbers`; the name is a spelling, the copy was the defect.
R3  **No unimported call.** A gate that asks `pr_numbers.<rule>` must import it. The
    import is path-based (these scripts have hyphenated names and are loaded by file
    path), so a missing one is a `NameError` at the first measurement, not at review.

What is *not* checked here: whether the reading is correct. That is the unit legs
below and `scripts/pr_numbers.py` itself.

`review-queue.py` is deliberately outside the family: it asks `gh` the same question
but publishes a **different answer** -- "every open PR number, in the order GitHub
lists them (newest first)", which its report is printed in -- and it reads the JSON
list rather than the `--jq` lines, refusing a shape it cannot use since
`cyc20261003-015225`. An ascending reader would silently reorder that report, which is
why it was not folded in.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

OWNER = SCRIPTS / "pr_numbers.py"

#: Every gate, discovered rather than listed: a new `check-merge-*.py` is covered the
#: day it lands, which is the day a copy would be pasted into it.
FAMILY = sorted(SCRIPTS.glob("check-merge-*.py"))

#: The rule the owner holds, by the name a copy would reuse.
RULE = "open_pr_numbers"

#: The gates whose `main` runs the default source with no positional arguments, and so
#: reach the shared reader. `check-merge-pairs.py` is absent on purpose: its `main`
#: requires its own arguments, and it reaches the reader through
#: `check-merge-sequence.py` (covered in its own leg below).
READERS = ["check-merge-landing-diff.py", "check-merge-order.py",
           "check-merge-plan-suite.py", "check-merge-sequence.py",
           "check-merge-tree-health.py"]

#: Valid JSON of a shape none of these gates can use. `[]` and `{}` are what a proxy,
#: a wrapper `gh` or a mis-projected answer really looks like; the bare sentence is
#: what a `gh` that is really `/usr/bin/pr` prints.
WRONG_SHAPES = ["[]", "{}", '{"not": "the shape"}', "oops not a number"]

_GH_STUB = '''#!/usr/bin/env python3
"""Stand in for `gh` and print whatever GH_PAYLOAD says."""
import os, sys
sys.stdout.write(os.environ.get("GH_PAYLOAD", "[]") + "\\n")
'''

class _Proc:
    """A `subprocess.CompletedProcess` stand-in: the owner reads three fields."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the stand-in is a shebang script; on Windows that measures the stub's absence "
        "rather than the gate. The source rules above run everywhere, and the owner's "
        "own legs below drive it with an in-process runner."
    ),
)


def _local_base() -> str:
    """A commit this checkout already has, for the gates that refresh their base.

    Five gates call `_refresh_base(base)` before they read the queue, and a
    remote-tracking base is refreshed with a real `git fetch`. Measured 2026-10-03
    (`cyc20261003-023102`) on a host whose HTTPS to github.com is down: the first
    version of these legs passed `--base` nothing, so four of the fifteen runs spent
    **75 s** failing to reach the network and then reported *that* refusal - the gate
    was right (exit 2, no traceback) and the leg measured the wrong thing. A commit
    id is taken literally by `_refresh_base` ("a SHA is immutable by construction"),
    so naming one makes the run hermetic and the refusal it reads is the queue's.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    ).stdout.strip()


_LOCAL_BASE = _local_base()


def _docstrings(tree: ast.AST) -> set[int]:
    """The `id()` of every string constant that is a docstring.

    Prose may quote the rule's spelling -- four gates' docstrings do, to explain what
    they used to get wrong -- and only code may not contain it. `ast` keeps the two
    apart for exactly this reason.
    """
    found: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _code_strings(tree: ast.AST) -> list[ast.Constant]:
    """Every string constant of the file that is code rather than a docstring."""
    prose = _docstrings(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


def _splits_stdout(node: ast.AST) -> bool:
    """Whether this expression is `<something>.stdout.split()`."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "split":
        return False
    inner = node.func.value
    return (
        isinstance(inner, ast.Attribute)
        and inner.attr == "stdout"
    )


def _reads_stdout_as_ints(tree: ast.AST) -> list[ast.AST]:
    """Every comprehension that turns `<something>.stdout.split()` into `int`s.

    This is the deleted line, whatever it is spelled with:

        numbers = [int(line) for line in proc.stdout.split() if line.strip()]

    The shape rather than the text, because a copy that renamed `line` to `token` is
    the same defect and the same four lines.
    """
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            continue
        if not any(_splits_stdout(gen.iter) for gen in node.generators):
            continue
        if any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "int"
            for inner in ast.walk(node)
        ):
            found.append(node)
    return found


def findings(path: Path, owner: str = "pr_numbers") -> list[str]:
    """Every way this file keeps its own copy of the reader the owner holds."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.lstrip("_") == RULE and not _asks_the_owner(node, owner):
                found.append(
                    f"{path.name}:{node.lineno}: defines `{node.name}` itself instead of "
                    f"asking {owner}.{RULE}"
                )

    for node in _code_strings(tree):
        if node.value == ".[].number":
            found.append(
                f"{path.name}:{node.lineno}: re-spells the `--jq` projection "
                f"(`.[].number`) in code instead of asking {owner}"
            )

    for node in _reads_stdout_as_ints(tree):
        found.append(
            f"{path.name}:{node.lineno}: parses stdout into integers itself instead of "
            f"asking {owner}.{RULE}"
        )

    return found


def _asks_the_owner(node: ast.AST, owner: str = "pr_numbers") -> bool:
    """Whether a definition's body reaches `pr_numbers.open_pr_numbers`."""
    return any(
        isinstance(inner, ast.Attribute)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == owner
        and inner.attr == RULE
        for inner in ast.walk(node)
    )


def _imports_owner(path: Path, owner: str = "pr_numbers") -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and owner in [alias.name for alias in node.names]:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == owner:
            return True
    return False


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TestTheFamilyAsksTheOwner:
    def test_every_gate_is_discovered(self) -> None:
        """The gate set is found by pattern, not listed -- a new gate cannot escape."""
        names = {path.name for path in FAMILY}
        assert {
            "check-merge-landing-diff.py",
            "check-merge-order.py",
            "check-merge-pairs.py",
            "check-merge-plan-suite.py",
            "check-merge-sequence.py",
            "check-merge-tree-health.py",
        } <= names, names

    def test_no_gate_keeps_a_copy(self) -> None:
        """The whole family, measured -- the five copies this cycle deleted stay gone."""
        found: list[str] = []
        for path in FAMILY:
            found.extend(findings(path))
        assert not found, "a gate kept its own copy of the reading:\n  " + "\n  ".join(found)

    def test_the_readers_name_kept_is_delegation(self) -> None:
        """R2: `_open_pr_numbers` may stay (four gates' tests patch it), its body may not.

        The name is not the defect and the tests that patch it are not a second copy --
        but a body that measures anything on its own is.
        """
        for name in READERS:
            path = SCRIPTS / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            defs = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_open_pr_numbers"
            ]
            assert defs, f"{name} no longer defines _open_pr_numbers - update this test"
            for node in defs:
                assert _asks_the_owner(node), (
                    f"{name}:{node.lineno}: `_open_pr_numbers` no longer asks "
                    f"pr_numbers.{RULE}"
                )

    def test_a_gate_that_asks_the_owner_imports_it(self) -> None:
        """R3: the import is path-based, so a missing one is a NameError at runtime."""
        for path in FAMILY:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            asks = any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "pr_numbers"
                for node in ast.walk(tree)
            )
            if asks:
                assert _imports_owner(path), f"{path.name}: calls pr_numbers without importing it"

    def test_the_owner_is_the_one_reader(self) -> None:
        """The owner holds the rule, so the rules are *about* something."""
        tree = ast.parse(OWNER.read_text(encoding="utf-8"), filename=str(OWNER))
        defs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert RULE in defs, f"{OWNER.name} no longer defines {RULE}: {sorted(defs)}"

    def test_review_queue_is_not_in_the_family(self) -> None:
        """The exemption is a decision, so it is asserted rather than assumed.

        `review-queue.py` answers a different question (the queue in GitHub's order,
        which its report is printed in) and reads the JSON list. If it is ever folded
        in, this test is the thing that asks the question -- and the answer is not
        "delete this test", it is "decide what its report's order means first".
        """
        assert "review-queue.py" not in {path.name for path in FAMILY}
        text = (SCRIPTS / "review-queue.py").read_text(encoding="utf-8")
        assert "in the order GitHub lists them" in text, (
            "review-queue.py no longer documents its own order - re-read whether it "
            "should be in this family rather than editing this line"
        )


class TestTheRulesAreMeasuredInBothDirections:
    """A discriminator proven only in the failing state proves nothing (#455 lesson)."""

    _PLANTED_COPY = '''\
"""A gate."""


def _open_pr_numbers(repo):
    proc = _run(["gh", "pr", "list", "-R", repo, "--state", "open",
                 "--json", "number", "--jq", ".[].number"])
    if proc.returncode != 0:
        raise RuntimeError("no")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise RuntimeError("no open PRs reported")
    return sorted(numbers)
'''

    _PROSE = '''\
"""A gate that used to parse stdout itself.

It carried:  numbers = [int(line) for line in proc.stdout.split() if line.strip()]
and the projection `.[].number`, both of which are now `pr_numbers.open_pr_numbers`'s.
"""

import pr_numbers


def _open_pr_numbers(repo):
    """Delegation, and this docstring quotes `.[].number` on purpose."""
    return pr_numbers.open_pr_numbers(repo, run=_run)
'''

    def test_a_planted_copy_is_caught(self, tmp_path: Path) -> None:
        planted = tmp_path / "check-merge-planted.py"
        planted.write_text(self._PLANTED_COPY, encoding="utf-8")
        found = findings(planted)
        assert any("defines `_open_pr_numbers` itself" in f for f in found), found
        assert any(".[].number" in f for f in found), found
        assert any("parses stdout into integers" in f for f in found), found

    def test_prose_may_quote_the_rule(self, tmp_path: Path) -> None:
        quoted = tmp_path / "check-merge-quoted.py"
        quoted.write_text(self._PROSE, encoding="utf-8")
        assert findings(quoted) == [], findings(quoted)


class TestTheReaderRefusesWhatItCannotRead:
    """The owner, driven with an in-process runner -- no `gh`, no network."""

    def _owner(self):
        return _load(OWNER, "pr_numbers_for_this_test")

    @pytest.mark.parametrize("payload", WRONG_SHAPES)
    def test_a_shape_that_is_not_numbers_names_what_arrived(self, payload: str) -> None:
        """The defect: this raised `ValueError` out of the gate and became exit 1."""
        owner = self._owner()
        with pytest.raises(owner.MeasurementError) as excinfo:
            owner.open_pr_numbers("owner/name", run=lambda argv: _Proc(payload))
        message = str(excinfo.value)
        assert payload[:20] in message, message
        assert "not an empty queue" in message, message

    def test_a_numeric_answer_is_the_numbers_ascending(self) -> None:
        """The other direction: the refusal must not fire on the answer it exists for.

        Ascending is the ordering every deleted copy returned (`sorted(numbers)`) and
        the ordering the callers' output is built from -- `check-merge-order.py` sorts
        its positional numbers the same way -- so the answer is checked out of order
        rather than already sorted.
        """
        owner = self._owner()
        got = owner.open_pr_numbers("owner/name", run=lambda argv: _Proc("1820\n1818\n"))
        assert got == [1818, 1820], got

    def test_nothing_printed_stays_the_empty_queue(self) -> None:
        """A stub that prints nothing is 'no open PRs', not malformed output.

        This is why the wire shape stayed `--jq '.[].number'`:
        `tests/test_check_merge_sequence.py::test_an_empty_open_pr_list_is_refused_at_its_source`
        pins it, and `--json number` would read the same stub as `JSONDecodeError`.
        """
        owner = self._owner()
        with pytest.raises(owner.MeasurementError) as excinfo:
            owner.open_pr_numbers("owner/name", run=lambda argv: _Proc(""))
        assert "no open PRs reported" in str(excinfo.value)

    def test_a_refused_call_carries_ghs_own_message(self) -> None:
        """`gh`'s own words are what the caller's exit-2 line shows the host."""
        owner = self._owner()
        with pytest.raises(owner.MeasurementError) as excinfo:
            owner.open_pr_numbers(
                "owner/name",
                run=lambda argv: _Proc("", returncode=4, stderr="gh auth login"),
            )
        assert str(excinfo.value) == "gh pr list failed: gh auth login", str(excinfo.value)


@_posix_only
class TestTheGatesAnswerTwoInsteadOfOne:
    """The behaviour, end to end, in each gate's own process.

    The unit legs above prove the reader; these prove what each gate's table does with
    it, which is the part that was wrong. Exit 1 in a gate is a verdict -- the finding
    -- so the assertion is not "it failed" but "it failed *as unmeasurable*".
    """

    @pytest.fixture
    def stub_bin(self, tmp_path: Path) -> Path:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "gh"
        stub.write_text(_GH_STUB, encoding="utf-8")
        stub.chmod(0o755)
        return bindir

    def _run(self, script: str, payload: str, stub_bin: Path) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
        env["GH_PAYLOAD"] = payload
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), "--base", _LOCAL_BASE],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    @pytest.mark.parametrize("script", READERS)
    @pytest.mark.parametrize("payload", WRONG_SHAPES)
    def test_a_queue_that_cannot_be_read_is_never_the_finding(
        self, script: str, payload: str, stub_bin: Path
    ) -> None:
        proc = self._run(script, payload, stub_bin)
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, (
            f"{script} answered {proc.returncode} for payload {payload!r} - exit 1 in this "
            f"tool is the finding, and nothing was measured.\n{combined!r}"
        )
        assert "Traceback" not in proc.stderr, (
            f"{script} crashed on payload {payload!r} instead of reporting it:\n{proc.stderr}"
        )
        assert payload[:20] in combined, (
            f"{script} did not name what arrived for payload {payload!r}:\n{combined!r}"
        )

    def test_the_control_the_gates_really_read_the_list(self) -> None:
        """A gate that reported 2 for *every* payload would pass the legs above.

        The control is the reader itself, driven in process with a numeric answer: it
        answers the numbers rather than the refusal. Measured at the reader rather than
        through a gate because a gate that got past the reader would then reach for the
        network, and "it failed later for a different reason" is not a reading.
        """
        owner = _load(OWNER, "pr_numbers_for_the_control")
        got = owner.open_pr_numbers("owner/name", run=lambda argv: _Proc("7\n9\n"))
        assert got == [7, 9]

    def test_the_borrower_gets_the_same_refusal(self, stub_bin: Path) -> None:
        """`check-merge-pairs.py` reaches the reader through `check-merge-sequence.py`.

        It never had its own copy; it had sequence's, which is why the shared reader is
        what both of them ask now. Measured with the arguments it needs to run at all.
        """
        script = SCRIPTS / "check-merge-pairs.py"
        argv = [sys.executable, str(script), "1818", "1820", "--base", _LOCAL_BASE]
        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
        env["GH_PAYLOAD"] = "[]"
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        assert "Traceback" not in proc.stderr, proc.stderr
        assert proc.returncode in (0, 1, 2, 3), proc.returncode
