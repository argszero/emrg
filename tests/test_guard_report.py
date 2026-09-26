"""A guard's two output streams do not interleave in the order it printed them.

The rule
--------
Every guard in the `scripts/check-*.py` family names the tree it read before it
states a verdict, and the family says so in as many words: `check-citation-resolves.py`
records that it "prints `tree: <resolved root>` before any verdict", and
`check-merge-sequence.py` refuses another guard's report outright unless it names
the tree it read (`TREE_IN_REPORT`, matched against a captured report). The order is
the promise, so it has to hold for the text a reader actually gets.

Why it does not, by itself - measured 2026-09-26 (cycle `cyc20260926-050158`,
master `60cad23f`)
---------------------------------------------------------------------------------
`print()` to `sys.stdout` is block-buffered when stdout is **not a tty**, and
`sys.stderr` is not. A cycle reads a gate through exactly that shape (`2>&1 | tail`,
and CI's log capture is the same), so stdout's bytes are held until the process exits
while every stderr line goes out as written. Measured on the guard whose two outputs
are reachable with one call - the tree line at `main()`'s start and the refusal of
`--resolve-conflict` in a tree with no `Agent.md`:

    $ python3 scripts/check-doc-count.py --resolve-conflict 2>&1 | cat -n
         1  error: cannot read .../Agent.md: [Errno 2] No such file or directory
         2  tree: /private/tmp/gateprobe2.XpsY7K

The same command with `PYTHONUNBUFFERED=1` prints the tree line first, which is the
program's own order. So the verdict overtakes the identity line **only under a pipe**
- the reading mode the promise is made for - and only because of buffering.

The remedy, and why it is one line at each `main()`
---------------------------------------------------
`sys.stdout.reconfigure(line_buffering=True)` makes every line leave the process when
the program wrote it, so a merged reader gets program order. It is stated at each of
the four gates rather than in a shared module because those four are independent
tools that otherwise share nothing (`merge_tree.py` is the merge gates' module, and
three of these four are not merge gates); the *rule* - all four must have it - is
mechanised here instead of being left as prose.

This test is the rule, in three parts
-------------------------------------
* the mechanism itself, both ways round: with the remedy the tree line is first, and
  **without it the verdict is**, so the assertion below is about the remedy and not
  about something else that happens to be true;
* one real gate end-to-end, in a synthetic tree it cannot damage (the copy's
  `REPO_ROOT` is that tree, and `--resolve-conflict` refuses before writing anything);
* an AST sweep over `scripts/check-*.py`: every gate that prints a `tree: ` line to
  stdout *and* writes to stderr must carry the call - unconditionally, so a gate that
  gains the shape without the remedy is caught wherever it came from. The sweep also
  holds a baseline of the gates known to have the shape and asserts it has not *shrunk*,
  so a `tree: ` line that quietly disappears is visible; it deliberately allows the set
  to grow, because a gate that gains the shape *and* the remedy has followed the rule
  and should not be failed for it (`#1631` adds one).

What is deliberately out of scope
---------------------------------
A `tree:` line that is a *field of a nested report* rather than the identity line -
`check-extension-load.py` prints `  tree: <cwd>` indented under a heading - is not
swept: this rule is about the line a reader uses to know which checkout answered, and
only a line starting at column 0 is that. A gate that writes nothing to stderr cannot
invert anything and needs no remedy, which is why the sweep requires both halves.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# The remedy, spelled once: the pin below and the prose in the gates quote this
# call, and two spellings of it would be one spelling too many.
REMEDY = "sys.stdout.reconfigure(line_buffering=True)"

# Measured 2026-09-26 by the sweep in the last test of this file: the gates that
# print the family's identity line to stdout *and* write a verdict to stderr. Held as
# a baseline the sweep must still cover - a gate losing either half leaves this set
# and is caught there. It is not an upper bound: a gate that gains both halves and
# carries the remedy is correct and passes without editing this.
SWEPT = {
    "check-citation-resolves.py",
    "check-doc-count.py",
    "check-node-test-count.py",
    "check-rant-citations.py",
}


def _run_merged(argv: list[str], cwd: Path) -> str:
    """Run a command with stderr merged into a piped stdout, as a cycle reads it.

    `PYTHONUNBUFFERED` is removed from the child's environment on purpose: it is
    the variable that *hides* this defect, and a test that inherits it would pass
    by accident wherever the developer had exported it. The point is the default
    behaviour of a piped stdout, which is what a reader gets.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Pinned, not inherited: the locale codec would mojibake a guard's own
        # report on a non-UTF-8 host, which is the class
        # `test_script_decode_is_locale_independent.py` sweeps for.
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def _probe(with_remedy: bool) -> str:
    """A two-line program with the shape under test: an identity line, a verdict."""
    code = (
        "import sys; "
        + (REMEDY + "; " if with_remedy else "")
        + "print('tree: /probe'); "
        + "print('error: a refusal', file=sys.stderr)"
    )
    return _run_merged(["-c", code], SCRIPTS)


def _index_of(out: str, prefix: str) -> int:
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    raise AssertionError(f"no line starts with {prefix!r} in:\n{out}")


def test_without_the_remedy_the_verdict_overtakes_the_tree_line() -> None:
    """The defect, pinned as a fact: this is what a piped reader gets today.

    Without this half the test below would be an assertion about *some* otherwise
    true property. Here it is about the remedy: remove it and the order inverts.
    """
    out = _probe(with_remedy=False)
    assert _index_of(out, "error:") < _index_of(out, "tree: ")


def test_the_remedy_keeps_the_tree_line_before_the_verdict() -> None:
    out = _probe(with_remedy=True)
    assert _index_of(out, "tree: ") < _index_of(out, "error:")


def test_a_real_gate_names_its_tree_before_it_refuses(tmp_path: Path) -> None:
    """The same order, from a real gate, in a tree the run cannot damage.

    The copy is loaded from `<tmp>/scripts/`, so the guard's `REPO_ROOT` is that
    directory and every path it touches is inside it. `--resolve-conflict` refuses
    because the tree carries no `Agent.md` at all - before it would write - so the
    probe leaves nothing behind, and the tree line it prints is the tree it read,
    which is the property the refusal is about.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "check-doc-count.py", tmp_path / "scripts" / "check-doc-count.py")

    out = _run_merged([str(tmp_path / "scripts" / "check-doc-count.py"), "--resolve-conflict"],
                      tmp_path)

    assert _index_of(out, "tree: ") < _index_of(out, "error:")
    # The tree line is the identity line, and it names *this* tree: a guard that
    # answered about its own checkout would satisfy the order and fail this.
    assert str(tmp_path) in out.splitlines()[_index_of(out, "tree: ")]


def _sweep() -> tuple[set[str], set[str]]:
    """(gates needing the remedy, gates that have it) - read from the AST.

    The AST, not a text search: the request is about a `print` that *emits* the
    line, and a comment or a regex in a consumer's source mentions `tree: ` too
    (`check-merge-sequence.py` has both) without emitting one.
    """

    def is_stderr_write(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Attribute) and node.func.attr == "write":
            return (isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "stderr")
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            return False
        for kw in node.keywords:
            if kw.arg == "file":
                return (isinstance(kw.value, ast.Attribute)
                        and kw.value.attr == "stderr")
        return False

    def emits_tree_line(node: ast.AST) -> bool:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print" and node.args):
            return False
        for kw in node.keywords:
            if kw.arg == "file":
                return False  # a verdict line, not the identity line
        first = node.args[0]
        if isinstance(first, ast.JoinedStr):
            forms = [p.value for p in first.values
                     if isinstance(p, ast.Constant) and isinstance(p.value, str)]
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            forms = [first.value]
        else:
            forms = []
        # Column 0 only: an indented `tree:` is a field of a nested report, not
        # the line that says which checkout answered (see this file's docstring).
        return any(f.startswith("tree: ") for f in forms)

    def has_remedy(tree: ast.AST) -> bool:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reconfigure"):
                continue
            if any(kw.arg == "line_buffering" for kw in node.keywords):
                return True
        return False

    needs: set[str] = set()
    have: set[str] = set()
    for path in sorted(SCRIPTS.glob("check-*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not any(emits_tree_line(n) for n in ast.walk(tree)):
            continue
        if not any(is_stderr_write(n) for n in ast.walk(tree)):
            continue
        needs.add(path.name)
        if has_remedy(tree):
            have.add(path.name)
    return needs, have


def test_every_gate_with_a_tree_line_and_a_verdict_buffers_its_stdout() -> None:
    """The rule, over the whole family, with the scope pinned rather than assumed."""
    needs, have = _sweep()

    missing = sorted(needs - have)
    assert not missing, (
        f"{missing} print a `tree:` line and also write to stderr, so a piped reader "
        f"can see the verdict first; add `{REMEDY}` at the top of `main()`"
    )
    # The rule above is unconditional; this half is about *scope*, and it is a
    # one-way check on purpose. A gate that loses its tree line (or its stderr
    # write) drops out of the swept set silently, so the baseline must not shrink
    # without someone noticing - that is the drift worth catching.
    #
    # Growth is deliberately not a failure. A new gate that prints a tree line and
    # writes a verdict is a *good* change, and if it carries the remedy it has
    # followed the rule; failing it here would send CI red for whoever made it,
    # over a test they had no reason to know about. A new gate that skips the
    # remedy is already caught by the assertion above, with a message that says
    # what to add. An equality here would also have failed on this PR's own
    # reviewer feedback: #1631 adds a tree line to `check-merge-landed.py`, and
    # with the remedy its arrival is correct rather than a red master.
    assert SWEPT <= needs, (
        f"a gate in the baseline no longer prints a `tree: ` line together with a "
        f"verdict: baseline {sorted(SWEPT)}, measured {sorted(needs)}; if that was "
        "deliberate, say so in this test's docstring and drop it from the baseline"
    )
