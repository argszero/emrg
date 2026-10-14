"""A test that reaches a POSIX-only primitive carries the gate that skips it where there is none.

The rule, and why it is not a style preference
----------------------------------------------
``os.mkfifo`` does not exist on Windows -- ``hasattr(os, "mkfifo")`` is ``False`` there, and
the same is true of ``os.setsid``, ``os.killpg``, ``signal.SIGWINCH`` and the ``pty`` /
``termios`` / ``fcntl`` modules. So an arm that *reaches* one raises ``AttributeError`` (or
fails to import, which is worse: collection of the whole file dies) on the ``windows-2025``
leg while the ``ubuntu`` leg stays green. It is a red only one leg can see, which is exactly
the shape this repository mechanises rather than remembers.

It has already been paid for once. On 2026-10-11 (``cyc20261011-034647``, in ``#2074``)
``tests/test_daemon.py`` declared ``_needs_mkfifo = pytest.mark.skipif(not hasattr(os,
"mkfifo"), ...)`` and applied it to **no test**: all five FIFO tests called ``os.mkfifo``
unguarded, ``test-windows`` went red on ``AttributeError``, and the ``ubuntu`` leg was green
throughout. The reviewer's sentence names the defect exactly -- *"the marker is defined and
never applied"*.

Nothing holds that today. The convention is carried by **prose**: test files repeat the
reasoning in their own docstrings ("The FIFO arm carries an ``os.mkfifo`` gate, so it skips
where there are no FIFOs"), and every call site carries a hand-written gate. A new arm that
forgets one is silently red on one leg and green on the other, and no command in this repo
says so.

What is asserted
----------------
Every ``test*`` function under ``tests/`` that reaches a POSIX-only primitive -- **directly,
or through a same-file helper** -- must carry a gate. A gate is one of:

* a decorator written inline whose condition names the platform, e.g.
  ``@pytest.mark.skipif(sys.platform == "win32", reason=...)``;
* a decorator naming a module-level marker whose **definition** names the platform, in either
  of the two spellings this tree uses -- a value (``_needs_mkfifo = pytest.mark.skipif(not
  hasattr(os, "mkfifo"), ...)``, ``needs_a_shell``, ``_needs_posix_tty``) and a **decorator
  factory** (``test_memory.py``'s ``def _needs_mkfifo(func): return pytest.mark.skipif(not
  hasattr(os, "mkfifo"), ...)(func)``);
* an inline guard in the body -- ``if hasattr(os, "mkfifo"):`` -- the shape a test uses when
  it means to run on both legs and *drop only the FIFO half*;
* a module-level ``pytestmark`` that names the platform, which gates every test in the file.

The detector reads the **AST**, not the text. That is what makes a docstring explaining a gate
a non-reference (``test_scheduler.py``'s control says "Making one also needs ``os.mkfifo``,
which does not exist on Windows" and is *deliberately* ungated -- its subject is a regular
file), and what lets a helper be followed: ``self._fifo(path)`` is a reference if ``_fifo`` is.

The controls below drive the detector in both directions on synthetic sources, so a detector
that has silently stopped matching cannot pass this file by reporting nothing -- the failure
mode a sweep like this has by construction. The tree leg asserts the sweep judged a
non-trivial number of files **and** that two named real subjects were seen, for the same
reason: an empty reading must not read as a clean one.

Named limits
------------
* **A kind-selecting helper is not followed.** ``test_config.py``, ``test_client_log_sink.py``
  and ``test_skills_loader.py`` each make their FIFO from one branch of a helper chosen by an
  argument (``_plant(path, kind)``, ``_project(tmp_path, fifo=True)``). The graph is seeded
  only by an **unconditional** reference, because the guard cannot read which argument a caller
  passes -- so a *new* ungated caller of one of those helpers is not caught, and the tree's
  FIFO callers there are still gated by hand. The same cut is what keeps their FIFO-free
  controls (the arms the Windows leg *does* run) from being reported as reaching a FIFO.
* **Nested functions are not nodes.** ``test_ws_e2e.py`` defines an ``async def _test()``
  inside dozens of arms; keyed at module level they merged, and one FIFO arm made 63 unrelated
  tests "reach" a primitive they never call. A nested body is still walked as part of the
  function that holds it, so nothing it references is lost -- and, since PR #2279's review,
  nothing it *mentions* becomes the enclosing test's gate either: a gate is read off a
  condition (`TestAWordIsNotAGate`), which is the hole this same walk opened while the reader
  searched unparsed source.
* A test that **shells out** to a ``mkfifo`` binary is not seen: a string is not a reference.
  Every call site today is in-process ``os.mkfifo(...)`` (measured 2026-10-15), so the gap is
  empty rather than merely small.
* A helper **imported from another test module** is not followed -- the graph is same-file.
* A POSIX dependency reached through **product** code -- a tool that itself calls
  ``os.mkfifo`` -- is out of scope. The tree's rule there is the opposite one: the product
  refuses a FIFO *by kind*, on every platform, and its tests assert that by shape
  (``test_base_tool.py::test_non_regular_kind_judges_a_symlink_by_what_will_be_opened``).
* **The condition reader reads a condition, not a source.** It asks the two sites where a
  gate can sit -- the condition of a ``skipif(...)`` call, and the test of an ``if`` -- and
  matches a regex against that expression. It is not an evaluator, so a gate written in a
  spelling it does not know (``platform.system()``) is **reported**; admitting the spelling
  fixes the file. Both directions have been measured: a *word* naming the platform is not a
  gate (the five controls in `TestAWordIsNotAGate`, all of which the reader that searched the
  whole function called gated), and an unknown *spelling* of a real gate is reported rather
  than passed. The earlier version of this bullet said "nothing is missed", and that sentence
  was false in the second direction until PR #2279's review
  (`cyc20261015-043845`) measured it.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

TESTS_DIR = Path(__file__).resolve().parent

#: Attribute references that exist only where the platform is POSIX. A call to one raises
#: `AttributeError` on Windows; `signal.SIGWINCH` is an absent attribute too.
POSIX_ATTRIBUTES = frozenset(
    {
        ("os", "mkfifo"),
        ("os", "getuid"),
        ("os", "geteuid"),
        ("os", "getgid"),
        ("os", "getegid"),
        ("os", "setsid"),
        ("os", "killpg"),
        ("os", "fork"),
        ("os", "chown"),
        ("os", "lchown"),
        ("signal", "SIGWINCH"),
    }
)

#: Modules whose very import fails on Windows. A module-level import of one of these breaks
#: **collection** of the whole file, which no per-test gate placed below it can repair -- so
#: it is a finding for every test in that file, not for the import's own line.
POSIX_MODULES = frozenset({"pty", "termios", "fcntl", "tty", "resource", "pwd", "grp"})

#: What makes a skip a *platform* gate. A `skipif` about anything else -- a Python version, a
#: missing binary -- is not one, which is the #2074 lesson read from the other side: a marker
#: that is declared and *attached but about something else* protects nothing either.
#:
#: It is asked of a **condition**, never of a whole unparsed function: see `_gate_conditions`
#: for why (a word is not a decision, and searching the source made an ungated arm pass by
#: printing `os.name`).
PLATFORM_CONDITION = re.compile(r"sys\.platform|\bos\.name\b|hasattr\(\s*(os|sys|platform)\s*,")


class Reading(NamedTuple):
    """What one file's AST says: the subjects that need a gate, and the ones that are ungated."""

    path: Path
    #: ``(lineno, name, primitives)`` for every test that needs a gate and has none.
    ungated: list[tuple[int, str, str]]
    #: ``(lineno, name, primitives)`` for every test that needs a gate and carries one.
    gated: list[tuple[int, str, str]]



def _gate_conditions(node: ast.AST) -> Iterator[ast.AST]:
    """Every expression in `node` where a platform gate can sit.

    A gate is a **decision**, so its only sites are a decision's own test: the condition of a
    ``skipif(...)`` call, and the test of an ``if`` / an ``if``-expression. This is what the
    check must read, and PR #2279's review is why (cycle `cyc20261015-043845`): the reader
    used to search the **whole unparsed function**, so a platform word *anywhere* counted as
    the gate and an arm could pass ungated by

    * printing `os.name` beside the call (case 1),
    * comparing two string constants that spell it -- `assert "os.name" == "os.name"` (case 2),
    * mentioning it in a nested helper the test merely calls (case 3),
    * listing it in `@pytest.mark.parametrize("p", [os.name, "nt"])` (case 4), or
    * quoting it in some *other* `skipif`'s `reason=` (case 5, which is
      `test_a_skipif_about_something_else_is_not_a_gate` with one word changed).

    Each of the five is a control below, and each was measured reading `GATED` on the loose
    reader and `UNGATED` on this one -- a guard whose subject is "the arm that forgets its
    gate" cannot have an escape hatch that lets exactly that arm through.
    """
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "skipif"
        ):
            if sub.args:
                yield sub.args[0]
            for keyword in sub.keywords:
                if keyword.arg in ("condition", "cond"):
                    yield keyword.value
        elif isinstance(sub, (ast.If, ast.IfExp)):
            yield sub.test


def _names_the_platform(node: ast.AST) -> bool:
    """Whether one of `node`'s gate conditions names the platform."""
    for condition in _gate_conditions(node):
        try:
            source = ast.unparse(condition)
        except Exception:  # pragma: no cover - unparse only fails on exotic trees
            continue
        if PLATFORM_CONDITION.search(source):
            return True
    return False


def _primitive_refs(node: ast.AST, *, unconditional_only: bool = False) -> set[str]:
    """The POSIX-only names this subtree reaches, as rendered strings.

    ``unconditional_only`` skips the bodies of ``if`` / ``try`` / loop statements: a
    reference under one of those is *selected* -- by an argument the caller passes, or by a
    `try` that means to run everywhere -- so a caller reaching that function has not been
    shown to reach the primitive. The tree's kind-selecting helpers are that shape
    (``_plant(path, kind)`` makes a FIFO only for one ``kind``), and their FIFO callers
    carry the gate by hand.
    """
    hits: set[str] = set()

    def walk(node: ast.AST) -> None:
        for sub in ast.iter_child_nodes(node):
            if unconditional_only and isinstance(
                sub, (ast.If, ast.For, ast.While, ast.Try, ast.Match)
            ):
                continue
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                if (sub.value.id, sub.attr) in POSIX_ATTRIBUTES:
                    hits.add(f"{sub.value.id}.{sub.attr}")
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.name.split(".")[0] in POSIX_MODULES:
                        hits.add(f"import {alias.name}")
            elif isinstance(sub, ast.ImportFrom) and sub.module:
                if sub.module.split(".")[0] in POSIX_MODULES:
                    hits.add(f"from {sub.module} import ...")
            walk(sub)

    walk(node)
    return hits


def _call_targets(node: ast.AST, owner: str | None) -> set[tuple]:
    """Where this subtree calls, as keys the caller can resolve without guessing.

    A bare ``f(...)`` is a module-level function; ``self.f(...)`` is a method of the
    enclosing class. Nothing else is followed -- the first cut keyed on the simple name
    alone, and in ``test_ws_e2e.py`` a method and a module-level helper of the same name
    merged, so one FIFO arm made 40 unrelated tests "reach" it.
    """
    targets: set[tuple] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            targets.add(("module", func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in ("self", "cls") and owner is not None:
                targets.add(("method", owner, func.attr))
    return targets


def _platform_markers(tree: ast.Module) -> set[str]:
    """Module-level names bound to a platform gate, in either of the tree's two spellings.

    A value -- ``_needs_mkfifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), ...)`` -- and a
    **decorator factory**, which is how ``test_memory.py`` writes the same thing:

        def _needs_mkfifo(func):
            return pytest.mark.skipif(not hasattr(os, "mkfifo"), ...)(func)
    """
    markers: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets = [node]
            value = node
        else:
            continue
        if not _names_the_platform(value):
            continue
        for target in targets:
            # An assignment binds a Name; a decorator factory is the function's own name.
            name = target.id if isinstance(target, ast.Name) else getattr(target, "name", None)
            if name and name != "pytestmark":
                markers.add(name)
    return markers


def _module_gated(tree: ast.Module) -> bool:
    """Whether a module-level `pytestmark` names the platform (gating every test in the file)."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "pytestmark" for t in node.targets):
            continue
        if _names_the_platform(node.value):
            return True
    return False


def _body_statements(node: ast.AST) -> list[ast.stmt]:
    """The function's own statements, with its docstring dropped.

    The docstring has to go: it is where this tree *explains* a gate, so a mention of the
    primitive inside it must not read as the gate itself.
    """
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


def _body_source(node: ast.AST) -> str:
    """The function's body as source, for the one shape that is not a condition."""
    return "\n".join(ast.unparse(stmt) for stmt in _body_statements(node))


def _is_gated(node: ast.AST, markers: set[str], module_gated: bool) -> bool:
    """Whether this function carries a gate of any admitted spelling."""
    if module_gated:
        return True
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id in markers:
            return True
        if _names_the_platform(decorator):
            return True
    body = _body_statements(node)
    if _names_the_platform(ast.Module(body=body, type_ignores=[])):
        return True
    # `pytest.skip(...)` is kept as a spelling of its own: the condition it is *under* is an
    # `if` this reader already asks about, and a skip under no platform condition at all is a
    # red on both legs rather than the one-leg red this file exists for.
    return "pytest.skip(" in _body_source(node)


def _index(path: Path) -> Reading:
    """Read one test module: which of its tests reach a POSIX primitive, and which are gated."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:  # pragma: no cover - a syntax error is someone else's red
        raise AssertionError(f"{path} does not parse, so its arms cannot be read: {exc}") from exc

    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    # Only the functions a caller can *name*: module-level and class-level. A nested
    # function is not one -- `test_ws_e2e.py` defines an `async def _test()` inside dozens
    # of arms, and keying those at module level merged them all, so one FIFO arm made 63
    # unrelated tests "reach" a primitive they never call. Their bodies are already inside
    # the walk of the function that holds them, so their refs are not lost this way.
    funcs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(parents[id(node)], (ast.Module, ast.ClassDef))
    ]
    # A module-level import of a POSIX module kills collection before any test runs.
    file_level = _primitive_refs(ast.Module(body=[
        stmt for stmt in tree.body if isinstance(stmt, (ast.Import, ast.ImportFrom))
    ], type_ignores=[]))
    if file_level:
        file_level = {f"module-level {name}" for name in file_level}

    markers = _platform_markers(tree)
    module_gated = _module_gated(tree)

    owner: dict[int, str | None] = {}
    for node in funcs:
        parent = parents[id(node)]
        owner[id(node)] = parent.name if isinstance(parent, ast.ClassDef) else None

    def key_of(node: ast.AST) -> tuple:
        cls = owner[id(node)]
        return ("method", cls, node.name) if cls else ("module", node.name)

    by_key: dict[tuple, list[ast.AST]] = {}
    for node in funcs:
        by_key.setdefault(key_of(node), []).append(node)

    refs = {id(node): _primitive_refs(node) for node in funcs}
    unconditional = {id(node): _primitive_refs(node, unconditional_only=True) for node in funcs}
    edges = {id(node): _call_targets(node, owner[id(node)]) for node in funcs}

    # A direct reference makes a function a *subject* whatever its shape -- a test that
    # guards the call inline is a subject with a gate, not a non-subject. The **chain**,
    # however, travels only out of a function that reaches the primitive unconditionally.
    frontier = {id(node) for node in funcs if unconditional[id(node)]} | (
        {id(node) for node in funcs} if file_level else set()
    )
    needs = set(frontier) | {id(node) for node in funcs if refs[id(node)]}
    changed = True
    while changed:
        changed = False
        for node in funcs:
            if id(node) in needs:
                continue
            for target in edges[id(node)]:
                if any(id(other) in frontier for other in by_key.get(target, ())):
                    needs.add(id(node))
                    frontier.add(id(node))
                    changed = True
                    break

    ungated: list[tuple[int, str, str]] = []
    gated: list[tuple[int, str, str]] = []
    for node in funcs:
        if id(node) not in needs or not node.name.startswith("test"):
            continue
        primitives = ", ".join(sorted(refs[id(node)] | file_level)) or "a same-file call"
        row = (node.lineno, node.name, primitives)
        (gated if _is_gated(node, markers, module_gated) else ungated).append(row)
    return Reading(path=path, ungated=ungated, gated=gated)


def _sweep() -> list[Reading]:
    # The skip is read off the path *relative to the root it was walked from*: `rglob` on an
    # absolute root yields absolute paths, so `path.parts` would ask about the machine's
    # directory layout rather than about this tree (`tests/test_walk_skips_read_their_root.py`).
    return [
        _index(path)
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if "__pycache__" not in path.relative_to(TESTS_DIR).parts
    ]


def _index_source(tmp_path: Path, source: str) -> Reading:
    path = tmp_path / "test_synthetic.py"
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return _index(path)


class TestTheDetectorReadsBothDirections:
    """Synthetic sources, so a detector that stopped matching cannot pass by reporting nothing."""

    def test_an_ungated_arm_is_reported(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os

            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading
        assert reading.ungated[0][2] == "os.mkfifo", reading

    def test_the_marker_spelling_is_silent(self, tmp_path: Path) -> None:
        """The tree's own spelling: a module-level marker whose definition names the platform."""
        reading = _index_source(tmp_path, '''
            import os

            import pytest

            _needs_mkfifo = pytest.mark.skipif(
                not hasattr(os, "mkfifo"), reason="this platform has no FIFOs"
            )

            @_needs_mkfifo
            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading
        assert [row[1] for row in reading.gated] == ["test_a_fifo_is_made"], reading

    def test_the_inline_guard_is_silent(self, tmp_path: Path) -> None:
        """The control shape: the test runs on both legs and drops only the FIFO half."""
        reading = _index_source(tmp_path, '''
            import os

            def test_a_fifo_is_made(tmp_path):
                if hasattr(os, "mkfifo"):
                    os.mkfifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading

    def test_a_skipif_written_inline_is_silent(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os
            import sys

            import pytest

            @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading

    def test_a_module_level_pytestmark_gates_every_test_in_the_file(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os
            import sys

            import pytest

            pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal")

            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading

    def test_a_skipif_about_something_else_is_not_a_gate(self, tmp_path: Path) -> None:
        """Declared, attached -- and about another question, so it protects nothing (#2074)."""
        reading = _index_source(tmp_path, '''
            import os
            import sys

            import pytest

            @pytest.mark.skipif(sys.version_info < (3, 11), reason="needs tomllib")
            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_a_helper_reached_from_an_ungated_test_is_reported(self, tmp_path: Path) -> None:
        """The shape most of this tree's arms use: the FIFO is made by a method."""
        reading = _index_source(tmp_path, '''
            import os

            class TestThing:
                def _fifo(self, path):
                    os.mkfifo(path)

                def test_a_fifo_is_made(self, tmp_path):
                    self._fifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_the_same_helper_behind_a_gated_test_is_silent(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os

            import pytest

            _needs_mkfifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")

            class TestThing:
                def _fifo(self, path):
                    os.mkfifo(path)

                @_needs_mkfifo
                def test_a_fifo_is_made(self, tmp_path):
                    self._fifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading

    def test_the_decorator_factory_spelling_is_silent(self, tmp_path: Path) -> None:
        """`test_memory.py`'s spelling: a function that returns the marker applied to its arg."""
        reading = _index_source(tmp_path, '''
            import os

            import pytest

            def _needs_mkfifo(func):
                return pytest.mark.skipif(
                    not hasattr(os, "mkfifo"), reason="this platform has no FIFOs"
                )(func)

            @_needs_mkfifo
            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert reading.ungated == [], reading
        assert [row[1] for row in reading.gated] == ["test_a_fifo_is_made"], reading

    def test_a_kind_selecting_helper_is_not_pulled_into_its_callers(self, tmp_path: Path) -> None:
        """The named limit, pinned: the FIFO lives in one branch the *caller* chooses.

        `test_config.py`, `test_client_log_sink.py` and `test_skills_loader.py` all make their
        FIFO this way, and their FIFO-free callers are the controls the Windows leg runs -- so
        a sweep that pulled every caller in would redden three files' worth of arms that never
        make a pipe.
        """
        reading = _index_source(tmp_path, '''
            import os

            def _plant(path, kind):
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "a FIFO":
                    os.mkfifo(path)
                elif kind == "a directory":
                    path.mkdir()

            def test_a_directory_is_refused(tmp_path):
                _plant(tmp_path / "sink", "a directory")

            def test_an_absent_subject_is_created(tmp_path):
                _plant(tmp_path / "sink", "nothing yet")
        ''')
        assert reading.ungated == [], reading
        assert reading.gated == [], reading

    def test_a_nested_helper_is_walked_with_its_enclosing_test(self, tmp_path: Path) -> None:
        """The other named limit: a nested body is inside its test, so it is not lost.

        `test_ws_e2e.py` writes its FIFO arm this way, and keying nested functions as
        module-level names merged dozens of unrelated arms into one finding.
        """
        reading = _index_source(tmp_path, '''
            import os

            def test_a_named_pipe_is_refused(tmp_path):
                async def _run():
                    os.mkfifo(tmp_path / "pipe")
                return _run
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_named_pipe_is_refused"], reading
        assert reading.ungated[0][2] == "os.mkfifo", reading

    def test_prose_is_not_a_reference(self, tmp_path: Path) -> None:
        """A docstring or a comment naming the primitive is not a call to it."""
        reading = _index_source(tmp_path, '''
            import os

            def test_a_regular_file_is_read(tmp_path):
                """Making one also needs `os.mkfifo`, which does not exist on Windows."""
                # os.mkfifo(tmp_path / "pipe") would also need os.mkfifo's own gate
                (tmp_path / "one.txt").write_text("x", encoding="utf-8")
        ''')
        assert reading.ungated == [], reading
        assert reading.gated == [], reading

    def test_a_module_level_posix_import_is_a_finding_for_every_test(self, tmp_path: Path) -> None:
        """`import pty` fails at *collection*, so a gate below it never runs."""
        reading = _index_source(tmp_path, '''
            import pty

            def test_nothing_to_do_with_a_tty():
                assert pty is not None
        ''')
        assert [row[1] for row in reading.ungated] == ["test_nothing_to_do_with_a_tty"], reading
        assert "module-level import pty" in reading.ungated[0][2], reading


class TestAWordIsNotAGate:
    """The quiet direction, one control per class PR #2279's review measured.

    Each source below is an **ungated** arm: it calls `os.mkfifo` and nothing skips it on a
    platform without FIFOs. Every one of them read `GATED` from the reader that searched the
    whole unparsed function, so the suite was green while the guard's stated contract -- "fails
    when a `test*` function reaches a POSIX-only primitive without one of those gates" -- was
    false for a *class* of arms, not a site. These five are the classes; a reader that widens
    again fails them.
    """

    def test_an_unrelated_statement_naming_the_platform_is_not_a_gate(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os

            def test_a_fifo_is_made(tmp_path):
                print(os.name)
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_a_string_constant_naming_the_platform_is_not_a_gate(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os

            def test_a_fifo_is_made(tmp_path):
                assert "os.name" == "os.name"
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_a_nested_helper_naming_the_platform_is_not_a_gate(self, tmp_path: Path) -> None:
        """The other side of the walk the docstring credits: a nested body is *in* its test.

        Its references must count -- and its mentions must not become the test's gate.
        """
        reading = _index_source(tmp_path, '''
            import os
            import sys

            def test_a_fifo_is_made(tmp_path):
                def note():
                    return sys.platform
                note()
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_a_parametrize_argument_naming_the_platform_is_not_a_gate(self, tmp_path: Path) -> None:
        reading = _index_source(tmp_path, '''
            import os

            import pytest

            @pytest.mark.parametrize("p", [os.name, "nt"])
            def test_a_fifo_is_made(tmp_path, p):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading

    def test_a_reason_quoting_the_platform_is_not_a_condition(self, tmp_path: Path) -> None:
        """`test_a_skipif_about_something_else_is_not_a_gate`, with one word moved into `reason=`.

        The marker is still about the Python version; the platform word is prose inside the
        message. Reading it as the condition re-opens #2074 from the other side.
        """
        reading = _index_source(tmp_path, '''
            import os
            import sys

            import pytest

            _needs_tomllib = pytest.mark.skipif(
                sys.version_info < (3, 11), reason="needs os.name on py3.11"
            )

            @_needs_tomllib
            def test_a_fifo_is_made(tmp_path):
                os.mkfifo(tmp_path / "pipe")
        ''')
        assert [row[1] for row in reading.ungated] == ["test_a_fifo_is_made"], reading


class TestTheSweptTree:
    """The real subject: every POSIX arm in `tests/` carries the gate that skips it."""

    def test_the_sweep_read_the_subject_it_claims_to_have_read(self) -> None:
        """An empty reading must not read as a clean one -- the baseline half."""
        readings = _sweep()
        assert len(readings) > 30, f"the sweep judged only {len(readings)} module(s)"
        seen = {r.path.name: r for r in readings}
        for name in ("test_grep_tool.py", "test_session.py", "test_daemon.py"):
            assert name in seen, f"{name} was not read at all"
            assert seen[name].gated, (
                f"{name} reaches a POSIX primitive and shows no gated subject, so the sweep "
                f"has stopped seeing the sites this file exists for: {seen[name]}"
            )

    def test_the_control_that_runs_on_both_legs_is_seen_as_gated_inline(self) -> None:
        """The one arm that deliberately runs everywhere is the inline-guard path, live."""
        readings = {r.path.name: r for r in _sweep()}
        gated = {row[1] for row in readings["test_grep_tool.py"].gated}
        assert "test_a_directory_holding_a_fifo_is_still_searched" in gated, gated

    def test_every_posix_arm_in_the_suite_carries_its_gate(self) -> None:
        readings = [reading for reading in _sweep() if reading.ungated]
        assert readings == [], (
            "a test that reaches a POSIX-only primitive must carry the gate that skips it where "
            "the platform has none, or the `windows-2025` leg is red while `ubuntu` is green "
            "(#2074: the marker was declared and applied to no test). Gate it with "
            '`pytest.mark.skipif(not hasattr(os, "<the primitive>"), reason=...)`, or guard the '
            'call inline with `if hasattr(os, "<the primitive>"):` when the test is meant to run '
            "on both legs:\n  "
            + "\n  ".join(
                f"{reading.path.relative_to(TESTS_DIR.parent)}:{lineno} {name} [{primitives}]"
                for reading in readings
                for lineno, name, primitives in reading.ungated
            )
        )
