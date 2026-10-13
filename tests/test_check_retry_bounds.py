"""`scripts/check-retry-bounds.py` - do the release's retry messages read their loop's bound?

What this file is about
-----------------------
`Agent.md`'s rule - *a derived number is never written where a guard can measure it* - was
unmet in the release workflow: each retry loop counted with one number and printed another,
and **nothing read the printed one**. Changing a loop bound is caught today (the staple
test's own `ATTEMPTS = 5` disagrees with the observed submit count and the suite reddens);
the person then updates that constant, the suite goes green, and the sentence an operator
triages a lost release on still states the old number. Silent exactly after the guard was
satisfied - issue #2210, where two instances wrote two carriers of this shape in one day.

The arms below are the instrument's two directions and its two ways of saying nothing:

* the **clean** direction, in both spellings the tree actually uses (a list loop with a
  literal, and a variable bound the message reads) - without it, a guard that faulted
  everything would pass the drift arm;
* the **drift** direction, on a synthetic file and again inside a *copy of the real
  workflow*, because an arm that only ever reads a toy proves the parser and not the
  subject: the real-tree arm inserts one stale sentence into the real file's first retry
  block and requires the fault to name that line;
* **could not measure** - a bound variable with no assignment above it, and a tree with no
  retry loop at all. Both are exit 2 and never 0: the defect this whole tool was written
  against is an answer that looks like an answer while measuring nothing.

Everything runs in-process through the module's `main`, except the two arms that assert on
a real subprocess (`tree:` first; the copy of the real tree), which need no fake and so
would catch a wiring the in-process call bypasses.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-retry-bounds.py"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_retry_bounds", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _yml(body: str) -> str:
    """A minimal workflow whose one step runs `body` - the shape the file really has."""
    lines = body.strip("\n").splitlines()
    head = "name: t\non: push\njobs:\n  g:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n"
    return head + "".join(f"          {line}\n" for line in lines)


def _tree(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "w.yml").write_text(_yml(body), encoding="utf-8")
    return tmp_path


def _run(mod, capsys, root: Path, *args: str) -> tuple[int, str, str]:
    code = mod.main(["--root", str(root), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- both directions on a synthetic loop --------------------------------------


def test_a_list_loop_whose_message_agrees_is_clean(tmp_path, mod, capsys) -> None:
    """The clean direction: a literal that agrees is not a fault, and is read at all."""
    root = _tree(
        tmp_path,
        """
for attempt in 1 2 3; do
  echo "try (attempt ${attempt}/3)"
done
echo "gave up on all 3 attempts"
""",
    )
    code, out, err = _run(mod, capsys, root)
    assert (code, err) == (0, ""), f"{code=} {out=} {err=}"
    assert "2 bound claim(s) read" in out, out


def test_a_loop_that_iterates_one_more_time_than_it_says_is_a_fault(
    tmp_path, mod, capsys
) -> None:
    """The drift direction - the shape that reaches a real operator.

    Both carriers are asserted: the per-attempt line *and* the summary after `done`, since
    the summary is the sentence a triaging reader actually lands on.
    """
    root = _tree(
        tmp_path,
        """
for attempt in 1 2 3 4; do
  echo "try (attempt ${attempt}/3)"
done
echo "gave up on all 3 attempts"
""",
    )
    code, out, err = _run(mod, capsys, root)
    assert code == 1, f"{code=} {out=} {err=}"
    assert err.count("iterates 4 time(s), but the message states 3") == 2, err
    # The lines the faults name are read back out of the file, not spelled here: a
    # hard-coded number would pin the YAML preamble's length, not the guard's reading.
    written = (root / ".github" / "workflows" / "w.yml").read_text(encoding="utf-8").splitlines()
    per_attempt = next(n for n, line in enumerate(written, 1) if "try (attempt" in line)
    summary = next(n for n, line in enumerate(written, 1) if "gave up on all" in line)
    assert f"w.yml:{per_attempt}" in err, err
    assert f"w.yml:{summary}" in err, (
        f"the summary printed after `done` was not read at all:\n{err}"
    )


def test_the_bound_is_read_from_the_nearest_assignment_above_the_loop(
    tmp_path, mod, capsys
) -> None:
    """A variable bound: resolved from above, and its summary read after `done`."""
    root = _tree(
        tmp_path,
        """
f() {
  local attempts=7
  for (( attempt=1; attempt<=attempts; attempt++ )); do
    echo "try ${attempt}/${attempts}"
  done
  echo "gave up on all ${attempts} attempts"
}
f
""",
    )
    code, out, err = _run(mod, capsys, root)
    assert (code, err) == (0, ""), f"{code=} {out=} {err=}"
    assert "bound=7 claim=7" in out, out


def test_a_variable_bound_with_a_stale_literal_beside_it_is_a_fault(
    tmp_path, mod, capsys
) -> None:
    """The control on the arm above: resolution must not swallow a disagreeing literality."""
    root = _tree(
        tmp_path,
        """
f() {
  local attempts=7
  for (( attempt=1; attempt<=attempts; attempt++ )); do
    echo "try ${attempt}/9"
  done
}
f
""",
    )
    code, out, err = _run(mod, capsys, root)
    assert code == 1, f"{code=} {out=} {err=}"
    assert "iterates 7 time(s), but the message states 9" in err, err


def test_an_unresolvable_bound_is_not_measured(tmp_path, mod, capsys) -> None:
    """A bound nobody can read is exit 2 with the reason, never a silent pass."""
    root = _tree(
        tmp_path,
        """
f() {
  for (( attempt=1; attempt<=attempts; attempt++ )); do
    echo "try ${attempt}/${attempts}"
  done
}
f
""",
    )
    code, out, err = _run(mod, capsys, root)
    assert code == 2, f"{code=} {out=} {err=}"
    assert "`$attempts`" in err and "no assignment above it" in err, err


def test_a_tree_with_no_retry_loop_is_not_a_pass(tmp_path, mod, capsys) -> None:
    """The subject must be non-empty: nothing to read is not the same as nothing wrong."""
    root = _tree(tmp_path, "echo hi\n")
    code, out, err = _run(mod, capsys, root)
    assert code == 2, f"{code=} {out=} {err=}"
    assert "0 retry loop(s)" in out, out


# --- the real subject ---------------------------------------------------------


def test_the_real_tree_is_clean_and_the_census_is_not_empty(mod, capsys) -> None:
    """The tree this ships in: clean, and with a census a reader can check."""
    code, out, err = _run(mod, capsys, REPO_ROOT, "--json")
    assert code == 0, f"{code=} {out=} {err=}"
    reading = json.loads(out)
    assert reading["measured"] is True, out
    assert reading["loops"] == 0 or reading["claims"] > 0, out
    assert reading["claims"] > 0, (
        "the guard read no bound claim at all in this tree - it would pass anything"
    )


def _first_retry_block(lines: list[str]) -> tuple[int, int]:
    """(header, done) of the first retry loop in a workflow, found by its own message."""
    for index, line in enumerate(lines):
        if "${attempt}/" in line:
            for scan in range(index, len(lines)):
                if lines[scan].strip() == "done":
                    return index, scan
    raise AssertionError("no retry loop to mutate was found in the workflow")


def test_a_stale_sentence_inside_the_real_workflow_is_a_fault(tmp_path) -> None:
    """The subject is the real file, not a toy: one inserted stale sentence must be named.

    Run as a subprocess over a *copy* of `.github/workflows/`, so the arm proves the tool's
    wiring end to end and leaves the checkout untouched.
    """
    tree = tmp_path / "tree"
    target = tree / ".github" / "workflows"
    target.mkdir(parents=True)
    for source in sorted(WORKFLOW_DIR.glob("*.yml")):
        shutil.copyfile(source, target / source.name)

    name = "build-release.yml"
    path = target / name
    lines = path.read_text(encoding="utf-8").splitlines()
    header, done = _first_retry_block(lines)
    indent = " " * (len(lines[header]) - len(lines[header].lstrip()))
    lines.insert(done + 1, f'{indent}echo "gave up after all 42 attempts"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    inserted = done + 2  # 1-based, after the insertion

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tree)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 1, f"{proc.returncode=}\n{proc.stdout}\n{proc.stderr}"
    assert f"{name}:{inserted}" in proc.stderr, proc.stderr
    assert "states 42" in proc.stderr, proc.stderr


def test_the_tree_it_read_is_named_first(tmp_path) -> None:
    """The family's convention: no verdict without naming the tree it is about."""
    tree = _tree(tmp_path, "echo hi\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tree)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    first = (proc.stdout or "").splitlines()[:1]
    assert first == [f"tree: {tree.resolve()}"], (first, proc.stdout, proc.stderr)
