"""Both `os.kill` guards in this suite reach **call-time readers** only.

Issue #1364. `tests/signal_probe_guard.py` (PR #1363) replaces `os.kill` for the
duration of the suite, so a real `os.kill(pid, 0)` on Windows is refused before
the call no matter which module makes it. Two other instruments work the same
way: `tests/test_stop_all.py`'s AST scan reads one file by name, and
`tests/test_daemon_manager.py` drives the restart logic by patching
`emrg.client.daemon_manager.os.kill` — a **module-path** patch.

All three share one silent dependency: the name must still be read from the `os`
module object when the call happens. A module that captures it **while it is
being imported** holds the original function object, and from then on that
module's probe is invisible to every one of them — the guard, the corpus and the
controls all stay green, and only the *coverage* shrinks, in exactly the module
that was edited.

**The rule is an invariant, not a list of spellings.** At import time the kill
function may appear only as the **callee** of a call — `os.kill(pid, sig)` reads
the attribute when the call runs, and that read sees the patch. Read anywhere
else the object is *stored*, and a stored function outlives the patch. So the
scan reports a kill function that is read as a **value** at import time.

That form was chosen after the enumeration it replaces was measured: a rule that
matched `kill = os.kill`, `kill = getattr(os, "kill")` and `from os import kill`
caught **1 of 7** import-time binding shapes — a walrus (`(kill := os.kill)`), a
tuple target (`kill, other = os.kill, ...`), a `for` target, and storage in a
container (`HANDLERS = {"probe": os.kill}`) all bound the same object and were all
missed. Enumerating spellings can only ever catch the spellings someone thought
of; the invariant catches the shape none of them were written for. The other half
of that lesson is `_definition_time_parts` below: a `def` was skipped whole, so a
**default argument** — `def probe(pid, kill=os.kill)`, the most natural way to
write this capture — was invisible.

```
from os import kill          # captured at import — the original function
kill = os.kill               # same, spelled as an assignment
(kill := os.kill)            # same, through a walrus
kill, other = os.kill, 2     # same, through a tuple target
HANDLERS = {"probe": os.kill}  # same, stored in a container
def probe(pid, kill=os.kill):  # same, in a default argument
```
The same statement *inside* a function body is not a defect: it executes when the
function runs, and `from os import kill` then reads `os.kill` off the module
object — the patched attribute. The recursion therefore descends through `if` /
`try` / `with` / `for` and class bodies (all of which run at import) and stops at
function and lambda **bodies** — but not at their signatures, which run at import
too. Flagging call-time reads would be wrong, and they are pinned as negatives.

**Measured on master** (cycle `cyc20260918-083832`): 61 files under `emrg/` and
115 under `tests/`, **0** captures, with the widened rule in place. The companion
fact — that a patch installed at `os` still lets the suite stop its own children —
was measured with a real child: `Popen.terminate()` reaches `os.kill` through the
stdlib, and the child still exited `rc=-15` while the guard was installed.

**A vendored tree is not the product, and it is not always here.** `emrg/gui/`
carries twelve `*.py` files under `node_modules/dmg-builder/vendor/`, which exist
only after an `npm install` — so including them would make this scan's coverage
differ between a developer's machine and CI (`KeyError`-free, but a vendored file
that binds `kill` would fail a guard about *our* modules). The scan skips any
path with a `node_modules` component; the count assertion below is what keeps
that exclusion honest.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Nodes whose **body** runs at call time — their signatures do not; see
#: `_definition_time_parts`, which is what gets walked instead.
_DEFERRED_BODIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: Path components that are not this repository's source — vendored copies whose
#: presence depends on an installer having run.
_SKIPPED_COMPONENTS = frozenset({"node_modules"})


def _definition_time_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a `def` / `lambda` that run when the module is imported.

    A function body runs at call time, so a read inside it is answered by the
    patched module object. Its **defaults, decorators and annotations** run at
    definition time — i.e. at import — so `def probe(pid, kill=os.kill)` captures
    the original function object before anything can patch it, exactly like a
    module-level assignment. Only the body is deferred; the signature is not.
    """
    args = node.args
    parts: list[ast.AST] = [
        *getattr(node, "decorator_list", []),
        *args.defaults,
        *[d for d in args.kw_defaults if d is not None],
        *[
            a.annotation
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            if a.annotation is not None
        ],
    ]
    returns = getattr(node, "returns", None)
    if returns is not None:
        parts.append(returns)
    return parts


def _is_the_callee(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Is this read the function being called — `os.kill(pid, sig)`?

    The one position where reading is not capturing: the attribute is fetched as
    the call is made, so it fetches whatever is installed at `os` by then.
    """
    parent = parents.get(id(node))
    return isinstance(parent, ast.Call) and parent.func is node


def _is_a_kill_lookup(node: ast.AST) -> bool:
    """`getattr(<something>, "kill")` — the same read, spelled as a string."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and bool(node.args)
        and isinstance(node.args[-1], ast.Constant)
        and node.args[-1].value == "kill"
    )


def _spelling(node: ast.AST) -> str:
    """How the source spelled it, trimmed — the report names the actual shape."""
    text = ast.unparse(node)
    return text if len(text) <= 72 else text[:69] + "..."


def _import_time_kill_bindings(root: Path) -> list[str]:
    """`file:line: spelling` for every import-time **capture** of the kill function.

    A local instrument rather than a grep so it can be pointed at a tree this
    test builds — see the spoofed-tree controls below. Sources that cannot be
    parsed are reported rather than skipped: a file the instrument could not read
    is not a file it cleared.
    """
    found: list[str] = []
    for path in sorted(Path(root).rglob("*.py")):
        if _SKIPPED_COMPONENTS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken tree is its own report
            found.append(f"{path}: unreadable ({exc})")
            continue

        parents: dict[int, ast.AST] = {}
        candidates: list[ast.AST] = []
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
                if isinstance(child, _DEFERRED_BODIES):
                    for part in _definition_time_parts(child):
                        parents[id(part)] = child
                        stack.append(part)
                        candidates.append(part)
                    continue  # the body runs at call time, where `os.kill` is fresh
                stack.append(child)
                candidates.append(child)

        for node in candidates:
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "kill":
                        found.append(
                            f"{path}:{node.lineno}: "
                            f"from os import kill as {alias.asname or 'kill'}"
                        )
                    elif alias.name == "*":
                        found.append(f"{path}:{node.lineno}: from os import *")
                continue
            if isinstance(node, ast.Attribute) and node.attr == "kill":
                if not _is_the_callee(node, parents):
                    found.append(f"{path}:{node.lineno}: {_spelling(node)}")
                continue
            if _is_a_kill_lookup(node) and not _is_the_callee(node, parents):
                found.append(f"{path}:{node.lineno}: {_spelling(node)}")
    return found


def test_no_module_binds_the_kill_function_at_import_time():
    """The product, where the guard's reach is about real probes."""
    assert _import_time_kill_bindings(REPO_ROOT / "emrg") == []


def test_the_suite_does_not_defeat_its_own_guard():
    """And the suite itself — a test-side alias would hide that test's probe
    from the very fixture that is supposed to refuse it."""
    assert _import_time_kill_bindings(REPO_ROOT / "tests") == []


def test_the_scan_reads_the_tree_it_clears():
    """A zero-hit reading is evidence only if the instrument looked.

    Counted rather than assumed: the scan walks files, and a root with none would
    otherwise report the same empty list as a clean product. This is also what
    keeps the `node_modules` exclusion bounded — it may not grow into "the scan
    skipped most of the tree", because the counts above it are the product's real
    size (61 modules under `emrg/` excluding the vendored tree, 115 under
    `tests/`, this file included).
    """
    def scanned(root: Path) -> list[Path]:
        return [
            p
            for p in root.rglob("*.py")
            if not _SKIPPED_COMPONENTS & set(p.parts)
        ]

    assert len(scanned(REPO_ROOT / "emrg")) >= 55, (
        f"the product scan found only {len(scanned(REPO_ROOT / 'emrg'))} module(s)"
    )
    assert len(scanned(REPO_ROOT / "tests")) >= 100, (
        f"the suite scan found only {len(scanned(REPO_ROOT / 'tests'))} module(s)"
    )


def test_the_import_time_binding_scan_is_not_blind(tmp_path):
    """Positive control: every shape of capture, caught.

    Without this, `== []` above cannot be told apart from an instrument that
    parses nothing. The shapes are the ones the enumeration this rule replaced
    was measured against — it caught one of them — so this list is the regression
    lock on the *invariant*: each row binds the same function object through a
    different syntax, and a rule that only knows assignments fails the middle of
    the list.
    """
    shapes = {
        # the direct spellings
        "import_kill.py": "from os import kill\n",
        "import_aliased.py": "from os import kill as k\n",
        "star.py": "from os import *\n",
        "assigned.py": "import os\nkill = os.kill\n",
        "looked_up.py": 'import os\nthe_probe = getattr(os, "kill")\n',
        "reached_indirectly.py": 'the_probe = __import__("os").kill\n',
        # the same object, reached by syntax an enumeration forgets
        "walrus.py": "import os\n(kill := os.kill)\n",
        "tuple_target.py": "import os\nkill, other = os.kill, os.getpid\n",
        "starred_target.py": "import os\nfirst, *rest = os.kill, os.getpid\n",
        "for_target.py": "import os\nfor kill in (os.kill,):\n    pass\n",
        "in_a_dict.py": 'import os\nHANDLERS = {"probe": os.kill}\n',
        "in_a_list.py": "import os\nPROBES = [os.kill]\n",
        "as_a_call_argument.py": "import os\nregister(os.kill)\n",
        # runs at import because a definition is executed, not merely declared
        "default_argument.py": (
            "import os\n\ndef probe(pid, kill=os.kill):\n    return kill(pid, 0)\n"
        ),
        "keyword_default.py": (
            "import os\n\ndef probe(pid, *, kill=os.kill):\n    return kill(pid, 0)\n"
        ),
        "in_a_decorator.py": (
            "import os\n\n@register(os.kill)\ndef probe(pid):\n    return pid\n"
        ),
        # import-time statements that are not the module body line
        "in_a_try.py": "try:\n    from os import kill\nexcept ImportError:\n    pass\n",
        "in_a_class.py": "import os\n\n\nclass Client:\n    probe = os.kill\n",
        "behind_type_checking.py": (
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from os import kill\n"
        ),
    }
    for name, source in shapes.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    reported = _import_time_kill_bindings(tmp_path)
    for name in shapes:
        assert any(name in row for row in reported), f"the scan missed {name}: {reported}"


def test_the_scan_leaves_call_time_reads_alone(tmp_path):
    """The negative half, and the reason the boundary is "value" not "mention".

    Reading the attribute as the callee of a call is what the guards rely on: the
    fetch happens when the call runs, so it sees the patched object. A
    `from os import kill` or a `kill = os.kill` **inside** a function body, an
    ordinary `import os`, an unrelated `from os import …`, a bound method of some
    other object, and a docstring that merely spells the line are all not
    captures. Report any of these and the guard would be red on a correct tree,
    which is how a guard gets deleted instead of fixed.
    """
    allowed = {
        "plain.py": "import os\n\ndef probe(pid):\n    os.kill(pid, 0)\n",
        "called_at_module_level.py": "import os\n\nos.kill(4242, 15)\n",
        "lookup_called.py": 'import os\n\ngetattr(os, "kill")(4242, 15)\n',
        "method_call.py": "import subprocess\n\nproc = subprocess.Popen(['true'])\nproc.kill()\n",
        "local_import.py": "def probe(pid):\n    from os import kill\n    kill(pid, 0)\n",
        "local_assign.py": "import os\n\ndef probe(pid):\n    kill = os.kill\n    return kill(pid, 0)\n",
        "a_none_default.py": "import os\n\ndef probe(pid, kill=None):\n    return pid\n",
        "other_names.py": "from os import getpid, killpg\n\nkillpg(1, 15)\n",
        "a_signal_constant.py": "import signal\n\nFORCE = signal.SIGKILL\n",
        "string_and_comment.py": (
            '# from os import kill is what a module must NOT do\n'
            'DOC = "from os import kill"\n'
        ),
    }
    for name, source in allowed.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    assert _import_time_kill_bindings(tmp_path) == []
