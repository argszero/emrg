"""Both `os.kill` guards in this suite reach **call-time readers** only.

Issue #1364. `tests/signal_probe_guard.py` (PR #1363) replaces `os.kill` for the
duration of the suite, so a real `os.kill(pid, 0)` on Windows is refused before
the call no matter which module makes it. Two other instruments work the same
way: `tests/test_stop_all.py`'s AST scan reads one file by name, and
`tests/test_daemon_manager.py` drives the restart logic by patching
`emrg.client.daemon_manager.os.kill` — a **module-path** patch.

All three share one silent dependency: the name must still be read from the `os`
module object when the call happens. A module that binds it **while it is being
imported** captures the original function object, and from then on that module's
probe is invisible to every one of them — the guard, the corpus and the controls
all stay green, and only the *coverage* shrinks, in exactly the module that was
edited.

```
from os import kill               # bound at import time — the original function
kill = os.kill                    # same, spelled as an assignment
kill = getattr(os, "kill")        # same, spelled as a lookup
kill = __import__("os").kill      # same, with the module reached indirectly
```

**The assignment rule is deliberately wider than `os`.** A mutation arm run this
cycle appended `_probe = __import__("os").kill` to `emrg/_stop_all.py` — an
import-time binding, and invisible to a rule that requires the object to be
spelled `os` (measured: narrow rule `0` hits, widened rule `1`). The wider rule
flags **any** module-level `<name> = <something>.kill`; that means a future
`self.kill = …` or `Popen.kill` in a class body would also be reported. That is
the safe side of the error: the shape is never used on this tree (0 hits in every
reading above), a false report is a one-line fix, and a missed binding silently
removes a module from the guard's reach — the failure this file exists to
prevent. Widening a rule to the side whose errors are visible is how a scan stays
honest; narrowing it to the side whose errors are not is how a guard dies quiet.

**Why the scan is limited to import-time bindings.** The same statement *inside*
a function is not a defect: it executes at call time, and `from os import kill`
then reads `os.kill` off the module object — which is the patched attribute. So
the binding is only stale when it happens once, at import. The recursion below
therefore descends through a module's `if` / `try` / `with` / `for` and class
bodies (all of which run at import) and stops at function and lambda bodies.
Flagging those would be wrong, and they are pinned as negatives.

**Measured on master** (this cycle): 61 files under `emrg/` and 115 under
`tests/`, **0** import-time bindings in the four shapes. The companion fact —
that a patch installed at `os` still lets the suite stop its own children — was
measured with a real child: `Popen.terminate()` reaches `os.kill` through the
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

#: A module's import-time statements, skipping bodies that run later.
_CALL_TIME_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: Path components that are not this repository's source — vendored copies whose
#: presence depends on an installer having run.
_SKIPPED_COMPONENTS = frozenset({"node_modules"})


def _import_time_kill_bindings(root: Path) -> list[str]:
    """`file:line: shape` for every import-time binding of the kill function.

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
        stack: list[ast.stmt] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _CALL_TIME_NODES):
                    continue  # runs at call time, where `os.kill` is read fresh
                if isinstance(child, ast.ImportFrom) and child.module == "os":
                    for alias in child.names:
                        if alias.name == "kill":
                            found.append(
                                f"{path}:{child.lineno}: "
                                f"from os import kill as {alias.asname or 'kill'}"
                            )
                        elif alias.name == "*":
                            found.append(f"{path}:{child.lineno}: from os import *")
                if isinstance(child, ast.Assign):
                    value = child.value
                    if isinstance(value, ast.Attribute) and value.attr == "kill":
                        found.append(
                            f"{path}:{child.lineno}: <name> = <something>.kill"
                        )
                    if (
                        isinstance(value, ast.Call)
                        and getattr(value.func, "id", None) == "getattr"
                        and value.args
                        and getattr(value.args[-1], "value", None) == "kill"
                    ):
                        found.append(f"{path}:{child.lineno}: <name> = getattr(<...>, 'kill')")
                stack.append(child)
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
    """Positive control: every shape the scan claims to catch, caught.

    Without this, `== []` above cannot be told apart from an instrument that
    parses nothing — the same requirement `test_the_respelling_scan_is_not_blind`
    puts on the other scan in this suite.
    """
    shapes = {
        "import_kill.py": "from os import kill\n",
        "import_aliased.py": "from os import kill as k\n",
        "star.py": "from os import *\n",
        "assigned.py": "import os\nkill = os.kill\n",
        "looked_up.py": 'import os\nthe_probe = getattr(os, "kill")\n',
        "reached_indirectly.py": 'the_probe = __import__("os").kill\n',
        # import-time statements that are not the module body line: all of these
        # execute when the module is imported, so all of them are bindings
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
    """The negative half, and the reason the scan stops at function bodies.

    A `from os import kill` or a `kill = os.kill` **inside** a function reads the
    module attribute when it runs, so it sees the guard's patched object; the
    ordinary `import os` idiom and an unrelated `from os import …` are not
    bindings at all. Report any of these and the guard would be red on a correct
    tree, which is how a guard gets deleted instead of fixed.
    """
    allowed = {
        "plain.py": "import os\n\ndef probe(pid):\n    os.kill(pid, 0)\n",
        "local_import.py": "def probe(pid):\n    from os import kill\n    kill(pid, 0)\n",
        "local_assign.py": "import os\n\ndef probe(pid):\n    kill = os.kill\n    return kill(pid, 0)\n",
        "other_names.py": "from os import getpid, killpg\n\nkillpg(1, 15)\n",
        "string_and_comment.py": (
            '# from os import kill is what a module must NOT do\n'
            'DOC = "from os import kill"\n'
        ),
    }
    for name, source in allowed.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    assert _import_time_kill_bindings(tmp_path) == []
