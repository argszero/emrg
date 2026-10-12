"""Every opener in this package sits in a scope that asks its subject's kind.

Why this file exists
--------------------
`emrg/tools/base.py`'s `special_file_kind` states the rule: an **open is not a read**. On a
FIFO it blocks until a peer appears, on a socket until a connection does, and a character
device never ends. `daemon._run_tool_loop` awaits tools **on the event loop**, so the block
is the whole daemon and the only recovery is a restart, which belongs to the host.

The rule was applied site by site, and each site carried its own two-direction test:
`#2062` the file tools, `#2073` the daemon's readers, `#2076`, `#2081`, `#2112`, `#2117`,
`#2120`, `#2122`, `#2124`, `#2126`, `#2128`, `#2130`. **None of them stopped the next
site**, because the rule lived in a docstring and in per-site tests -- never in one place
that enumerates the tree. This is the history `tests/test_walk_skips_read_their_root.py`
records for the walk-skip family, and this file is that file's shape applied here (issue
#2136).

What is asserted
----------------
An opener's subject is what the call opens: the first argument of `open(...)` /
`os.open(...)` (or the `file=` keyword), and the **receiver** of `p.read_text()` /
`p.write_bytes(...)`. The enclosing scope must ask the subject's kind, or the site is
listed in `EXEMPT` with the reading that makes it safe. `EXEMPT` is asserted to be
**exactly** the flagged set, so a site that is fixed also fails here rather than rotting
into a licence for the next one.

"Asks the kind" is a read of a name carrying `non_regular` or `special_file_kind` **in that
scope**, and the fragments are matched rather than the names spelled out because the family
reaches one predicate through five spellings: `non_regular_kind`, the daemon's and
`git_utils`' `_non_regular_kind`, `config`'s and `scheduler`'s `_refuse_non_regular`, and
`session`'s `_refuse_a_non_regular_subject`. `memory.NotARegularFile` is deliberately **not**
one of them: it is the refusal the memory store raises *after* asking, so a scope that only
catches it has asked nothing, and the control below pins that direction. A sweep that
matched the tool layer's spelling alone reports a guarded function as uncovered -- measured
2026-10-12 (`cyc20261012-115759`) against the handover table in #2129, one row of which
(`emrg/server/__main__.py`'s `_redirect_std_streams`) is guarded by `_non_regular_kind` four
lines above the `open` it names; **38 of the 71 guarded functions in the tree use only a
wrapper spelling**.

The controls below drive the detector in both directions on synthetic sources, so a
detector that has silently stopped matching cannot pass this file by returning an empty
list -- the failure mode a sweep like this has by construction.

Named limits
------------
* "Asks" is a property of the **scope**, not of the subject: a function that guards one
  subject and opens another is accepted here. Every flagged site today is in a scope with
  no such read at all, so nothing is concealed, and the narrower reading (the guard's
  argument is this call's subject) is the next tightening rather than a claim this one
  makes;
* a scope reached through an **indirection** is out of reach: an opener hidden behind a
  helper called from a guarded scope reads as unguarded here, and a helper that guards is
  invisible to the scope that calls it -- the `EXEMPT` entries below are where the readings
  for those live;
* the sweep is the `emrg` package, not the whole first-party tree. `scripts/` and `tests/`
  are the host's own one-shots and construct their own subjects; the code that runs *inside
  the daemon's event loop* is this package, and it is the loop that a block takes down;
* `EXEMPT` keys on the **function and the spelling**, not on a line number: measured
  2026-10-12, #2136's own table named `emrg/client/app.py:2417` and `:3128`, which are the
  two `read_bytes` sites in the tree **before** #2131 landed -- the merge moved them 48
  lines, which is what any line-keyed exemption does on the next unrelated edit.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The package whose openers this file judges -- the code that runs inside the daemon's
#: event loop and inside the TUI process. Not the whole first-party tree: `scripts/` and
#: `tests/` are host-side and build their own subjects.
JUDGED = ("emrg",)

#: Directories that hold no first-party source. The test walks this checkout, so its own
#: skip reads the path relative to the root -- the rule
#: `tests/test_walk_skips_read_their_root.py` exists for, applied here.
SKIPPED = {".venv", "__pycache__", "node_modules"}

#: Calls that name their subject **positionally**: `open(p)`, `os.open(p)`.
FUNCTION_OPENERS = frozenset({"open", "os.open", "io.open"})

#: Calls that name their subject as the **receiver**: `p.read_text()`, `p.write_bytes(b)`.
METHOD_OPENERS = frozenset({"open", "read_text", "read_bytes", "write_text", "write_bytes"})

#: What marks a scope as asking the kind. One fragment covers every wrapper spelling the
#: family uses; see the module docstring for the five and the measurement behind them.
PROBE_FRAGMENTS = ("non_regular", "special_file_kind")

#: `(relative path, function, spelling) -> (how many sites, the reading that makes them safe)`.
#: The reading is the whole point of the entry: a bare exemption is a licence, and the next
#: cycle cannot tell it from a site nobody got to.
EXEMPT = {
    ("emrg/_stop_all.py", "stop_daemon", "read_text"): (
        1,
        "the stop path. The subject is the app's own `<config>/emrgd.token`, read by the "
        "`emrg stop` client process -- not the daemon's event loop, so a block here wedges "
        "the host's stop command rather than the server. Adding a guard means testing it, "
        "and the only test is one that reaches the stop path: MANIFESTO 第四条附则二, the "
        "host's red line, forbids it.",
    ),
    ("emrg/_stop_all.py", "_open_stop_log", "open"): (
        1,
        "the same path and the same reason. The subject is a name this function composes "
        "itself (`<stop log dir>/stop_all-<local timestamp>.log`) inside the app's own log "
        "directory, and the open is wrapped in `except OSError: return None`.",
    ),
    ("emrg/client/app.py", "handle_key", "read_bytes"): (
        2,
        "the clipboard read-back, reached only through `if _extract_clipboard_image(...)`, "
        "which asks the kind first (#2131): `_clipboard_extraction_verdict` returns False "
        "for any subject `non_regular_kind` names, and its own docstring records that the "
        "directory case used to answer True and crash the TUI on this very read. The "
        "subject is the app's own `images/_clipboard_tmp_<n>.png`, not a path the host "
        "names.",
    ),
    ("emrg/config.py", "ensure_config", "write_text"): (
        1,
        "unreachable for a non-regular subject, measured in #2103 and restated in the "
        "function's own docstring: its first act is `if cfg_path.exists(): return`, and "
        "`Path.exists()` is **true** for a FIFO, a socket and a directory alike, so the "
        "write below is never reached with one.",
    ),
    ("emrg/server/git_utils.py", "ensure_local_exclude", "read_text"): (
        1,
        "defended on both halves, measured in #2122. The read is "
        "`exclude.read_text(...) if exclude.is_file() else \"\"`, and `is_file()` is false "
        "for a FIFO, a socket and a directory; the write beside it goes through "
        "`atomic_write_bytes`' `mkstemp` name rather than the subject.",
    ),
    ("emrg/session.py", "save_image", "write_bytes"): (
        1,
        "**the family's one known gap, not a safe site.** The subject is "
        "`self.images_dir / <label>_<blake2b prefix>.png`, so no caller can name the file "
        "in advance and a FIFO cannot be placed there by pointing at it; the method has no "
        "caller in this checkout. It is listed so the sweep is exact and this file does not "
        "grow a licence it cannot defend -- the reading is 'unpredictable subject', which "
        "is weaker than the rule and is named as such.",
    ),
}

SCOPE_TYPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _subject(call: ast.Call) -> ast.AST | None:
    """The expression naming what this call opens, or `None` when it names no subject.

    A method spelling names its subject as the **receiver** -- `p.read_text()` reads `p` --
    and a function spelling names it as the first argument. Reading `call.args[0]` for every
    spelling leaves every `p.read_text()` in the tree invisible, because the commonest
    reading in this package is spelled with keywords only (`read_text(encoding="utf-8")`)
    and has no positional argument at all: measured 2026-10-12 on the probe this file was
    built from, that blind spot hid two of the seven sites below.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in METHOD_OPENERS:
        if func.attr != "open":
            return func.value
        return call.args[0] if call.args else None
    if isinstance(func, ast.Name) and func.id in FUNCTION_OPENERS:
        if call.args:
            return call.args[0]
        for keyword in call.keywords:
            if keyword.arg == "file":
                return keyword.value
    return None


def _descriptor(subject: ast.AST) -> bool:
    """A descriptor number is not a path: `open(fd)` blocks on nothing a kind can name."""
    if isinstance(subject, ast.Constant) and isinstance(subject.value, int):
        return True
    if isinstance(subject, ast.Name) and subject.id.lower() in {"fd", "fileno"}:
        return True
    if isinstance(subject, ast.Call) and _callee_name(subject.func) == "fileno":
        return True
    return False


def _opened_by(call: ast.Call) -> str | None:
    """The spelling this call opens with, or `None` when it is not an opener."""
    helper = call.func
    if isinstance(helper, ast.Attribute) and helper.attr in METHOD_OPENERS:
        return helper.attr
    if isinstance(helper, ast.Name) and helper.id in FUNCTION_OPENERS:
        return helper.id
    return None


def _own_nodes(scope: ast.AST):
    """Every node `scope` owns -- the subtree, stopping at a nested scope's own body.

    The stop is the whole point: `self._non_regular_kind(q)` inside a helper nested in
    `load` is a guard on `q`, not on the `open(p)` that follows `load`'s call to it, and
    a reading that descended would accept a site nobody guarded.
    """
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(child, SCOPE_TYPES):
            yield from _own_nodes(child)


class _Detector:
    """One parsed source, read for opener sites and the scopes that ask."""

    def __init__(self, source: str) -> None:
        self.tree = ast.parse(source)
        self.parent: dict[int, ast.AST] = {}
        self.scopes: list[ast.AST] = []
        self._index(self.tree, None)
        self.asks: set[int] = set()
        for scope in self.scopes:
            for node in _own_nodes(scope):
                if isinstance(node, ast.Name) and any(
                    fragment in node.id for fragment in PROBE_FRAGMENTS
                ):
                    self.asks.add(id(scope))
                    break
                if isinstance(node, ast.Attribute) and any(
                    fragment in node.attr for fragment in PROBE_FRAGMENTS
                ):
                    self.asks.add(id(scope))
                    break

    def _index(self, node: ast.AST, parent: ast.AST | None) -> None:
        if parent is not None:
            self.parent[id(node)] = parent
        if isinstance(node, SCOPE_TYPES):
            self.scopes.append(node)
        for child in ast.iter_child_nodes(node):
            self._index(child, node)

    def enclosing(self, node: ast.AST) -> ast.AST:
        current = self.parent.get(id(node))
        while current is not None and not isinstance(current, SCOPE_TYPES):
            current = self.parent.get(id(current))
        return current if current is not None else self.tree

    def scope_name(self, scope: ast.AST) -> str:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return scope.name
        return "<module>" if isinstance(scope, ast.Module) else "<lambda>"

    def sites(self) -> list[tuple[int, str, str, bool]]:
        """`(line, spelling, enclosing scope, that scope asks)` for every opener."""
        found = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            spelling = _opened_by(node)
            if spelling is None:
                continue
            subject = _subject(node)
            if subject is None or _descriptor(subject):
                continue
            scope = self.enclosing(node)
            found.append(
                (node.lineno, spelling, self.scope_name(scope), id(scope) in self.asks)
            )
        return sorted(found)


def offenders(source: str) -> list[tuple[int, str, str]]:
    """`(line, spelling, scope)` for every opener whose scope does not ask the kind."""
    return [
        (line, spelling, scope)
        for line, spelling, scope, asks in _Detector(source).sites()
        if not asks
    ]


def flagged_here(root: Path = REPO_ROOT) -> tuple[Counter, int]:
    """`((path, function, spelling) -> sites, files judged)` over the swept package."""
    flagged: Counter = Counter()
    judged = 0
    for directory in JUDGED:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIPPED & set(path.relative_to(root).parts):
                continue
            judged += 1
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = path.relative_to(root).as_posix()
            for _line, spelling, scope, asks in _Detector(source).sites():
                if not asks:
                    flagged[(relative, scope, spelling)] += 1
    return flagged, judged


def _table(flagged: Counter) -> str:
    return "\n".join(
        f'    ("{path}", "{scope}", "{spelling}"): ({count}, "<why this one is safe>"),'
        for (path, scope, spelling), count in sorted(flagged.items())
    )


class TestTheDetector:
    """Both directions, on sources small enough to read."""

    def test_an_unguarded_open_is_reported(self) -> None:
        assert offenders("def load(p):\n    return open(p).read()\n") == [(2, "open", "load")]

    def test_the_receiver_is_the_subject_of_a_method_spelling(self) -> None:
        """`p.read_text()` reads `p`, and the spelling usually carries no argument at all."""
        assert offenders("def load(p):\n    return p.read_text(encoding='utf-8')\n") == [
            (2, "read_text", "load")
        ]
        assert offenders("def save(p):\n    p.write_bytes(b'')\n") == [(2, "write_bytes", "save")]
        assert offenders("def load(p):\n    return p.open('r')\n") == [(2, "open", "load")]

    def test_the_keyword_subject_is_read(self) -> None:
        assert offenders("def load(p):\n    return open(file=p)\n") == [(2, "open", "load")]

    def test_a_scope_that_asks_the_kind_is_silent(self) -> None:
        assert offenders(
            "def load(p):\n"
            "    if non_regular_kind(p) is not None:\n"
            "        return None\n"
            "    return open(p).read()\n"
        ) == []

    def test_every_wrapper_spelling_counts_as_asking(self) -> None:
        """The false positive #2129's handover table carried, turned into a control.

        A sweep that matched the tool layer's name alone reported
        `emrg/server/__main__.py`'s `_redirect_std_streams` as uncovered while
        `_non_regular_kind(crash_log)` sat four lines above the `open` it named. Each
        spelling below is a name the tree really reaches the predicate by.
        """
        for spelling in (
            "_non_regular_kind(p)",
            "self._non_regular_kind(p)",
            "special_file_kind(os.stat(p).st_mode)",
            "if _refuse_non_regular_file(p) is None:\n            pass",
            "_refuse_a_non_regular_subject(p)",
        ):
            assert offenders(f"def load(p):\n    {spelling}\n    return open(p).read()\n") == [], spelling
        assert offenders(
            "def load(p):\n"
            "    kind = non_regular_kind(p)\n"
            "    if kind is not None:\n"
            "        raise NotARegularFile(p, kind)\n"
            "    return open(p).read()\n"
        ) == []
        # ... and the other direction: `NotARegularFile` is the *refusal* the memory store
        # raises **after** asking, so a scope that only catches it has asked nothing.
        assert offenders(
            "def load(p):\n"
            "    try:\n"
            "        return open(p).read()\n"
            "    except NotARegularFile:\n"
            "        return None\n"
        ) == [(3, "open", "load")]

    def test_a_guard_in_a_nested_function_is_not_a_guard_on_this_call(self) -> None:
        """The control that keeps "asks" about the scope rather than the whole file."""
        assert offenders(
            "def load(p):\n"
            "    def check(q):\n"
            "        return non_regular_kind(q)\n"
            "    check(p)\n"
            "    return open(p).read()\n"
        ) == [(5, "open", "load")]

    def test_a_guard_in_a_sibling_function_is_not_a_guard_on_this_call(self) -> None:
        assert offenders(
            "def check(p):\n"
            "    return non_regular_kind(p)\n"
            "\n"
            "def load(p):\n"
            "    return open(p).read()\n"
        ) == [(5, "open", "load")]

    def test_a_descriptor_is_not_a_path(self) -> None:
        """`open(fd)` blocks on nothing a kind can name, and it is not this rule's subject."""
        assert offenders("def load(fd):\n    return open(fd).read()\n") == []
        assert offenders("def load(p):\n    return open(3).read()\n") == []
        assert offenders("def load(p):\n    return os.fdopen(p.fileno())\n") == []

    def test_a_source_with_no_opener_is_silent(self) -> None:
        """The control on the detector itself: an empty answer must not be the only answer."""
        assert offenders("def double(x):\n    return x * 2\n") == []
        assert _Detector("def load(p):\n    return open(p)\n").sites()


class TestThisTree:
    def test_no_opener_is_unasked(self) -> None:
        """The rule, over this checkout -- and `EXEMPT` is exactly the set it finds."""
        flagged, judged = flagged_here()

        assert judged > 50, (
            f"only {judged} file(s) were judged: a sweep over an empty set is a clean verdict "
            "about nothing, and that is the one reading this file must not give"
        )
        assert flagged == Counter(
            {(path, scope, spelling): count for (path, scope, spelling), (count, _why) in EXEMPT.items()}
        ), (
            "the openers that do not ask their subject's kind are no longer the set `EXEMPT` "
            "describes. An **open is not a read**: on a FIFO it blocks until a peer appears, "
            "and `daemon._run_tool_loop` awaits tools on the event loop, so the block is the "
            "whole daemon and only the host can restart it. Ask the kind in the scope that "
            "opens (`non_regular_kind`, or the wrapper this layer already uses), or add the "
            "site to `EXEMPT` with the reading that makes it safe.\n"
            "measured now:\n" + _table(flagged)
        )
