"""Every `which`-answer compared with `None` in this suite has to say why.

`shutil.which(name)` answers **is this name on PATH**. Read as **is this tool
available**, it is wrong on any host whose `name` resolves to something that cannot
start, and the reading is not merely imprecise - it decides a *skip*, so the suite runs
the test instead and fails for a reason that is not a defect in what it tests.

Measured 2026-10-03 (`cyc20261003-150951`) on the evolution host, whose `npm`, `node` and
`npx` asdf shims point at a removed interpreter:

    $ which npm        -> /Users/.../.asdf/shims/npm
    $ npm --version    -> rc=126: .../libexec/bin/asdf: No such file or directory

Seven gates in this suite read that first line as "the tool is here". The discriminator is
`tests/tool_preflight.py::starts`, which runs the program; this guard is what keeps a new
gate from being written the old way.

**What it flags, and what it deliberately does not.** Only a `which(...)` answer
compared with `None` - the shape that makes presence decide something. It is not a ban on
`which`:
`starts(shutil.which("bash"))` passes the answer into the discriminator, and
`shutil.which("git") == str(shim)` is an assertion *about* the pair rather than a gate, so
neither is reported. A `which(...)` in a docstring is text, and the AST does not see it -
which is why this reads the AST rather than the file, and why the test below pins that.

An entry in `ALLOWED` is a claim with a reason, not an exemption: each one says which two
states the `None` answer separates, and what measures the second.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: `(relative path, the program the gate asks about)` -> why presence is the right
#: question there. Every entry names the state the `None` answer stands for and what
#: measures the other one.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "tests/test_check_node_test_count.py",
        "npm",
    ): (
        "the `None` answer is `no npm on PATH`, which is a *different* fact from `on "
        "PATH and cannot start`, and the caller prints which one it is; the second state "
        "is measured by `_runner_that_cannot_start`, which runs `npm` through the tool's "
        "own resolver (`mod._run`) because that resolution is the code under test"
    ),
    (
        "tests/test_bash_v2_boundary.py",
        "linux_provider.BWRAP_BIN",
    ): (
        "the `None` answer is `bwrap is not installed`, named apart from `installed and "
        "cannot create a namespace here`, and the second state is probed on the lines "
        "immediately after with the same profile the provider builds"
    ),
}


def _assigned_from_a_which_call(tree: ast.Module) -> dict[str, tuple[int, str]]:
    """Local name -> `(line, program)` for every name bound to a `which(...)` result."""
    bound: dict[str, tuple[int, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not _is_a_which_call(call):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = (node.lineno, _program_text(call))
    return bound


def _is_a_which_call(node: ast.AST) -> bool:
    """Is this node a call to `shutil.which` (however `shutil` is spelled)?"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
        and len(node.args) == 1
    )


def _program_text(call: ast.Call) -> str:
    """The program name as a plain string, so `ALLOWED` reads like the source.

    A string literal is unquoted - `npm`, not `'npm'` - because the allow-list is read
    by a person deciding whether a gate still deserves its entry, and a quote is one more
    thing to get wrong when matching it.
    """
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    try:
        return ast.unparse(argument)
    except Exception:  # pragma: no cover - unparse covers every expression
        return "<unreadable>"


def presence_gates(
    root: Path, relative_to: Path | None = None
) -> list[tuple[str, int, str]]:
    """`(relative path, line, program)` for each `which` answer compared with `None`.

    Two spellings reach a comparison, and both are read: the direct
    `if shutil.which("x") is None:` and the two-statement form the release tests use
    (`shell = shutil.which("bash")` … `if shell is None:`), whose second half is found
    through `_assigned_from_a_which_call`.

    Paths are relative to `root`'s parent by default, which is what makes the real
    reading read `tests/…` - the form `ALLOWED` is keyed by. A caller scanning a
    temporary directory says so with `relative_to`, so its sample names stay the
    sample names.
    """
    base = root.parent if relative_to is None else relative_to
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = str(path.relative_to(base))
        bound = _assigned_from_a_which_call(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(
                isinstance(op, (ast.Is, ast.IsNot))
                and isinstance(c, ast.Constant)
                and c.value is None
                for op, c in zip(node.ops, node.comparators)
            ):
                continue
            for operand in [node.left, *node.comparators]:
                if _is_a_which_call(operand):
                    found.append((relative, node.lineno, _program_text(operand)))
                elif isinstance(operand, ast.Name) and operand.id in bound:
                    _, program = bound[operand.id]
                    found.append((relative, node.lineno, program))
    return sorted(set(found))


def test_no_gate_decides_on_presence_alone_without_saying_why() -> None:
    """The rule. A new presence gate fails here until it is given a reason."""
    unexplained = [
        (path, line, program)
        for path, line, program in presence_gates(TESTS_DIR)
        if (path, program) not in ALLOWED
    ]
    assert not unexplained, (
        "these gates compare a `shutil.which(...)` answer with None, so presence decides "
        "what they do - and a name that resolves is not a program that runs. Ask "
        "`tests.tool_preflight.starts` instead (or, if presence really is the question, "
        "add the pair to ALLOWED with the reason):\n  "
        + "\n  ".join(f"{p}:{n} asks about {prog}" for p, n, prog in unexplained)
    )


def test_the_allow_list_is_not_a_shadow_of_the_rule() -> None:
    """Every allowed pair must still exist, or the entry is hiding a gate that moved.

    An allow-list that outlives its subject stops being a reason and becomes an
    exemption: the next `which`-gate written at that path would be waved through by a
    stale entry. So each pair is required to be present in the scan.
    """
    observed = {(path, program) for path, _, program in presence_gates(TESTS_DIR)}
    stale = sorted(pair for pair in ALLOWED if pair not in observed)
    assert not stale, (
        f"these ALLOWED entries name gates that are no longer there: {stale} - delete "
        f"them, or the list is blessing a path rather than a reading"
    )


def _scan_a_directory_of_samples(tmp_path: Path, sources: dict[str, str]) -> set[tuple[str, str]]:
    """Run the shipped scanner over sample files this test writes.

    The files go through `presence_gates` itself rather than through a second copy of its
    logic: the first draft of these two tests re-implemented the scan inline, and a
    mutation arm that deleted the shipped two-statement branch **survived** - the arms
    were pinning the copy in the test, not the scanner the suite runs
    (`cyc20261003-150951`).
    """
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    return {
        (path, program) for path, _, program in presence_gates(tmp_path, relative_to=tmp_path)
    }


def test_the_scan_finds_the_shape_it_is_about(tmp_path) -> None:
    """The positive direction, in both spellings, on text this test owns."""
    found = _scan_a_directory_of_samples(
        tmp_path,
        {
            "direct.py": (
                'import shutil\nif shutil.which("tool") is None:\n    raise SystemExit\n'
            ),
            "assigned.py": (
                'import shutil\nshell = shutil.which("bash")\n'
                "if shell is None:\n    raise SystemExit\n"
            ),
            "inverted.py": (
                'import shutil\nif shutil.which("tool") is not None:\n    raise SystemExit\n'
            ),
        },
    )
    assert ("direct.py", "tool") in found, "the direct spelling was not read"
    assert ("assigned.py", "bash") in found, (
        "the two-statement spelling was not read, and it is the one the release tests use"
    )
    assert ("inverted.py", "tool") in found, (
        "`is not None` decides on presence exactly as `is None` does - a gate written that "
        "way must not slip through"
    )


def test_the_scan_leaves_the_legitimate_uses_alone(tmp_path) -> None:
    """The other direction: the shapes that must **not** be reported.

    A guard that flagged every `which` would be turned off within a cycle, and the
    reading it exists to give would go with it. So the boundary is pinned explicitly:
    the answer handed to `starts` is not a gate, an equality against a path is an
    assertion about the pair, and a `which` inside a docstring is not code at all.
    """
    found = _scan_a_directory_of_samples(
        tmp_path,
        {
            "discriminated.py": (
                "import shutil\nfrom tests.tool_preflight import starts\n"
                'if not starts(shutil.which("bash")):\n    raise SystemExit\n'
            ),
            "compared_with_a_path.py": (
                "import shutil, sys\n"
                'assert shutil.which("git") == sys.argv[1]\n'
            ),
            "in_a_docstring.py": (
                '"""The old shape was\nif shutil.which("npm") is None:\n    skip()\n"""\n'
            ),
            "counted.py": (
                'import shutil\nreport = shutil.which("git") is None\n'
            ),
        },
    )
    assert found == {("counted.py", "git")}, (
        f"the scan reported shapes it must not: {sorted(found)} - the discriminator call, "
        f"the equality against a path and the docstring are all fine (and the bare count "
        f"is a gate with no reason, so it is the one entry expected here)"
    )
