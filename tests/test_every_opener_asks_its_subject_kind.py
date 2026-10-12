"""Every opener asks its subject's kind before it opens a path.

Why this file exists
--------------------
`special_file_kind` (`emrg/tools/base.py`) states the rule, and its docstring carries the
measurement that made it one: an **open is not a read**, so on a FIFO it blocks until a peer
appears, on a socket until a connection does, and a device never ends -- and
`daemon._run_tool_loop` awaits tools **on the event loop**, so the block is the whole daemon
rather than a slow tool. The rule was then applied to the tree one site at a time, and the
sites kept appearing: `#2062`, `#2073`, `#2076`, `#2081`, `#2120` (the memory file's writer),
`#2122` (the runtime's own files), `#2124` (the client's log sink), `#2126` (the upgrade
tick's version readers), `#2128` (the daemon's own version reader), `#2130` (the clipboard
extraction). **Six of those landed in the six merges before this file was written** --
`#2120` through `#2130`, in order -- which is what makes the shape a defect rather than a run
of bad luck.

Each fix carried its own two-direction test, and none of them stopped the next site, because
the rule lived in a docstring, in the review of the moment, and in a private test -- never in
one place that *enumerates the tree*. `tests/test_walk_skips_read_their_root.py` records the
same history for the walk-skip family ("fixed one file at a time, five times in two days") and
is the shape this file copies: a detector over the first-party sources, driven in both
directions on synthetic sources, plus one leg that runs it over this checkout.

What is asserted
----------------
An **opener** is a call whose name is one of `OPENERS` -- the methods and the builtin that
take a path and open it. A scope **asks** when its own body, or the body of a function that
encloses it, calls one of the `GUARDS` (the whitelist itself, or a per-layer wrapper that
reaches it). Every opener must either sit in a scope that asks, or be listed in `EXEMPT` with
the reading that makes it safe.

This is a **structural** check, not a dataflow one: it asks whether the scope that opens asks
the kind *at all*, which is the question every historical site above would have failed
(`_current_installed_version` and `_read_local_version` were existing functions that named no
kind, not new code). A named limit below records what that costs.

Measured 2026-10-12 (cycle `cyc20261012-150608`) on master `8454654`: **91** files judged,
**77** opener sites, **6** scopes with no asking scope -- 7 calls, because
`interactive.handle_key` opens twice -- each in `EXEMPT` with its reason. Re-measured 2026-10-12
(cycle `cyc20261012-183804`) after `stop_daemon`'s token read moved behind a kind check
(issue #2140): the same **91** files and **77** opener sites, **5** scopes and **6** calls, the row
that left being the one whose stated reason -- "no test for a fix here could reach its own code" --
turned out to be a fact about where the guard sat rather than about the site. The table is
asserted to be **exactly** the flagged set, so an exemption that is no longer needed -- a site
fixed, a function removed -- fails here too rather than rotting.

Named limits
------------
* the guard is read at the **scope**, so a scope that asks about one subject can open another
  without the detector noticing. The mode form gives this away: `special_file_kind(info.st_mode)`
  takes no subject argument at all, so a scope that asks in that form has nothing at the call
  site to key on. A site that *does* name a subject can still differ from the one it opens --
  `GrepTool.execute` asks about `root` (`root.stat()`) and opens `filepath`, a name it got from
  `_collect_files`, and it is `_collect_files`' own `path.is_file()` gate that makes that safe,
  not the ask in this scope. The rule the sweep enforces is "the scope asks", and no reading of
  the call site can widen it to "the scope asks about *this* subject";
* an opener reached through a **helper** is out of reach: `shutil.copyfile`, `os.read` and
  `os.write` open their subject inside another module, and no reading of the call site can see
  it. The repair belongs in that helper, and this file's tree leg is green on it;
* a call **named** `open` counts whatever it belongs to, so a non-file helper of that name
  (`webbrowser.open`) would be reported. There is none in `emrg/` today, and the repair is an
  exemption row with its reason, not a narrower predicate -- the same doubt this file resolves
  toward reporting everywhere else;
* an `EXEMPT` row exempts a **(scope, opener) pair, not a call site** -- the key carries no line,
  so a scope that opens the *same* opener twice is covered by the one row and a repeat is
  invisible. `interactive.handle_key` is that pair today (`emrg/client/app.py:2465` and `:3176`,
  both `read_bytes`), and it is a real rule rather than an oversight: it is what keeps an edit
  above a site from rotting the table, and a *different* opener added to the same scope is still
  reported (`ensure_config` + `read_text` fails, measured 2026-10-12). What it cannot see is a
  third unguarded call of the same opener in that scope, which the row's reading would not cover;
* the sweep covers `emrg/`, the package this checkout ships. `emrg/gui/` is JavaScript and
  TypeScript; the only Python under it is **vendored into an npm dependency** --
  `emrg/gui/node_modules/dmg-builder/vendor/mac_alias/*.py` is 12 files in a checkout that has
  run `npm ci` and 0 in a fresh one -- which is the reason `node_modules` is in `SKIPPED`, and
  the reason this file's sentence about it names the vendored copies rather than the directory.
  `scripts/` and `tests/` are not swept: the rule is about the daemon's own opens, and a guard
  script or a test that opens a FIFO it just created is exercising the rule, not obeying it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The tree whose opens the rule governs. `emrg/gui/` is JavaScript/TypeScript; its only Python
#: is vendored into an npm dependency under `node_modules` (which `SKIPPED` drops), so this is
#: the whole shipped-Python surface.
SWEPT = REPO_ROOT / "emrg"

#: Directories under `SWEPT` that hold no first-party source. The skip reads the path
#: **relative to the root it walks** -- the rule `tests/test_walk_skips_read_their_root.py`
#: exists for, applied to this file's own walk.
SKIPPED = {".venv", "__pycache__", "node_modules"}

#: The calls that open a path. `open` is the builtin and `path.open` alike; the rest are
#: `Path`'s readers and writers, which open their subject directly.
OPENERS = frozenset(
    {
        "open",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "readline",
        "readlines",
        "writelines",
    }
)

#: The calls that ask a subject's kind. Two shapes for one rule: the whitelist itself
#: (`special_file_kind`, mode form) and its path-taking twin (`non_regular_kind`), plus the
#: per-layer wrappers that reach one of them.
#:
#: Two names that *look* like they belong are deliberately absent, because a scope that says a
#: refusal has not necessarily asked: `special_file_refusal` renders the message **after** a
#: kind is in hand, and `NotARegularFile` is the exception raised **after** -- and one of the
#: two is a class whose body asks nothing at all. Both were measured free to remove (stripping
#: them leaves the flagged set unchanged), so keeping them would only widen the blind spot
#: the docstring's Named limits records: a scope whose *only* guard-named call was a refusal
#: would read as asking. `test_every_guard_asks_the_whitelist` is what holds the rest of this
#: list to its meaning.
GUARDS = frozenset(
    {
        "special_file_kind",
        "non_regular_kind",
        "_non_regular_kind",
        "_refuse_non_regular",
        "_refuse_non_regular_file",
        "_refuse_a_non_regular_subject",
    }
)

#: The name the whitelist itself is spelled by, and the mode-form read a wrapper may reach it
#: through (`special_file_kind(info.st_mode)`) -- the two things a guard's body may name to
#: count as asking. `test_every_guard_asks_the_whitelist` reads this.
WHITELIST_NAMES = frozenset({"special_file_kind", "non_regular_kind"})

#: Openers with no asking scope, keyed by `(path relative to REPO_ROOT, qualname, opener)`.
#: Keyed by qualname rather than by line so an edit above a site does not rot the table -- and
#: by the opener, so a **different** unguarded opener added to an already-exempt function still
#: fails here. A repeat of the *same* opener in the same scope does not: the key carries no line,
#: so two such calls collapse into one row (`interactive.handle_key` is that pair today), which
#: the docstring's Named limits states. Every row states the reading that makes the site safe
#: today; the tree leg asserts this is exactly the flagged set, so a row whose site is fixed is
#: reported too.
EXEMPT: dict[tuple[str, str, str], str] = {
    ("emrg/_stop_all.py", "_open_stop_log", "open"): (
        "the same stop path: `open(..., \"a\")` on `stop_all-<YYYYMMDD-HHMMSS>.log`, whose name "
        "carries a second-resolution timestamp, inside a `try/except OSError` that answers a "
        "raising open. Recorded beside `stop_daemon` for the same reason the file is not "
        "touched here -- the stop path, and no test can reach its own code."
    ),
    ("emrg/client/app.py", "interactive.handle_key", "read_bytes"): (
        "reads back the `_clipboard_tmp_<n>.png` that `_extract_clipboard_image` just wrote, "
        "under `if _extract_clipboard_image(str(tmp_path)):` -- and that function asks "
        "`target_path`'s kind before any arm runs and answers False for a non-regular one, so "
        "this line is reached only for a regular file (`#2130`)."
    ),
    ("emrg/config.py", "ensure_config", "write_text"): (
        "the one open in `config.py` with no guard, measured in issue #2103: the function's "
        "first act is `cfg_path.exists()`, and `exists()` is true for a FIFO, a socket and a "
        "directory alike, so a non-regular subject never reaches this `write_text`. A guard "
        "here would be code no reading can reach."
    ),
    ("emrg/server/git_utils.py", "ensure_local_exclude", "read_text"): (
        "defended on both halves: the read sits under `exclude if exclude.is_file() else \"\"`, "
        "and `is_file()` is false for a FIFO, a socket and a directory; the write half replaces "
        "the file by atomic rename rather than by opening it for writing."
    ),
    ("emrg/session.py", "Session.save_image", "write_bytes"): (
        "the `while filepath.exists()` above it exits only on a name `exists()` says is "
        "**absent**, and `exists()` is true for a FIFO, a socket and a directory alike -- the "
        "same reading this table carries for `ensure_config` -- so no non-regular subject "
        "reaches this `write_bytes`. Measured 2026-10-12 (`cyc20261012-142737`): a FIFO planted "
        "at the name the method computes (`shot_<hash>.png`, reproducible -- the label and the "
        "hash both come from the caller) is diverted, the call returning `shot_1_<hash>.png` "
        "with the FIFO still in place. An earlier revision of this row read the loop as an "
        "acknowledgement that something may be there; it is what removes the possibility."
    ),
}


def _called_name(node: ast.AST) -> str | None:
    """The name a call is made by -- `read_text` for `p.read_text(...)`, `open` for `open(p)`."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _own_nodes(scope: ast.AST):
    """Every node `scope` holds that is not inside a scope it nests.

    A guard inside a nested function is that function's, not this one's: `_read_age` asking a
    kind says nothing about the `read_text` its caller performs.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def opener_sites(tree: ast.AST) -> list[tuple[str, str, int, bool]]:
    """`(qualname, opener, line, asked)` for every opener in `tree`.

    `asked` is read at the scope chain: a scope asks when its own body calls a guard, or when a
    function enclosing it does. A **module** or **class** body's guard is that body's own -- it
    is a statement that ran once, not evidence about the subject a method opens -- so it is not
    inherited, which keeps one import-time guard from blessing every function below it.
    """
    found: list[tuple[str, str, int, bool]] = []

    def visit(scope: ast.AST, prefix: str, inherited: bool) -> None:
        owns = list(_own_nodes(scope))
        asked = inherited or any(_called_name(node) in GUARDS for node in owns)
        for node in owns:
            name = _called_name(node)
            if name in OPENERS:
                found.append((prefix or "<module>", name, node.lineno, asked))
        # The recursion is over `owns`, not over `ast.iter_child_nodes(scope)`: `owns` yields a
        # nested `def` wherever it sits (inside a `try`, an `if`, a `with`) and a `lambda`
        # wherever it appears (an assignment, a call argument), while `iter_child_nodes` yields
        # only this node's **direct** children. Recursing over the direct children instead is
        # measured 2026-10-12 to return `[]` for a lambda in an assignment, a lambda at module
        # level, a lambda as a call argument, and a `def` nested inside an `if` or a `try` --
        # four shapes of scope the sweep would not have looked into at all, which is the one
        # thing a file claiming to enumerate the rule may not carry.
        # `Lambda` is in this tuple for the same reason a `def` is: a lambda's body **is** a
        # function body, and a scope it encloses inherits an ask made there. Leaving it out sent
        # a nested scope the *grandparent's* flag instead, measured 2026-10-12: an opener inside
        # a lambda nested in an asking lambda was reported, while the identical shape nested in
        # an asking `def` was silent -- the false-red direction, since the inner call runs only
        # in the branch where the enclosing lambda already asked its subject's kind. `ClassDef`
        # stays out deliberately, and has its own reading in the docstring above.
        carried = (
            asked
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            else inherited
        )
        for node in owns:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node, f"{prefix}.{node.name}" if prefix else node.name, carried)
            elif isinstance(node, ast.Lambda):
                visit(node, f"{prefix}.<lambda>" if prefix else "<lambda>", carried)

    visit(tree, "", False)
    return sorted(set(found))


def unguarded(source: str) -> list[tuple[str, str, int]]:
    """`(qualname, opener, line)` for every opener in `source` with no asking scope."""
    return [
        (qualname, opener, line)
        for qualname, opener, line, asked in opener_sites(ast.parse(source))
        if not asked
    ]


def definitions_of(tree: ast.AST, names: frozenset[str]) -> dict[str, list[list[ast.AST]]]:
    """For each of `names` defined in `tree`, the nodes of every definition found."""
    found: dict[str, list[list[ast.AST]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in names:
                found.setdefault(node.name, []).append(list(ast.walk(node)))
    return found


def guards_that_ask(
    defined: dict[str, list[list[ast.AST]]], names: frozenset[str] = GUARDS
) -> set[str]:
    """Which of these definitions reach the whitelist -- by naming it, or by calling one that does.

    A name whose body neither reads a mode nor calls another guard asks nothing, whatever it is
    called. Reachability is read from the whitelist outward (a breadth-first walk over callers),
    so a chain of wrappers counts only while it ends at the whitelist -- two new names calling
    each other reach nothing and are reported. The whitelist's two names are roots **by name**,
    whether or not this tree defines them, because that is what they are.
    """
    roots: set[str] = set(WHITELIST_NAMES)
    callers: dict[str, set[str]] = {}
    for name, sites in defined.items():
        for nodes in sites:
            for node in nodes:
                if isinstance(node, ast.Attribute) and node.attr.startswith("S_IS"):
                    roots.add(name)  # reads a mode directly -- asking whatever its name
                called = _called_name(node)
                if called is not None and called in names:
                    callers.setdefault(called, set()).add(name)

    reached: set[str] = set()
    frontier = set(roots)
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        frontier |= callers.get(name, set()) - reached
    return {name for name in defined if name in reached}


class TestTheDetector:
    """Both directions, on sources small enough to read."""

    def test_an_opener_with_no_guard_is_reported(self) -> None:
        source = "def load(p):\n    return p.read_text()\n"

        assert unguarded(source) == [("load", "read_text", 2)]

    def test_an_opener_behind_a_guard_is_silent(self) -> None:
        source = (
            "def load(p):\n"
            "    if non_regular_kind(p) is not None:\n"
            "        raise OSError('not a regular file')\n"
            "    return p.read_text()\n"
        )

        assert unguarded(source) == []

    def test_a_guard_in_a_nested_scope_does_not_cover_the_caller(self) -> None:
        """The direction that keeps the rule about the scope, not about the module.

        A guard inside a helper is the helper's. Reading the containing module for the guard's
        name would bless the caller's open on the strength of a decision about a different
        subject -- which is how a real site would slip through a coarser sweep.
        """
        source = (
            "def _ask(p):\n"
            "    return non_regular_kind(p)\n"
            "\n"
            "def load(p):\n"
            "    return p.read_text()\n"
        )

        assert unguarded(source) == [("load", "read_text", 5)]

    def test_a_guard_in_an_enclosing_function_covers_the_nested_one(self) -> None:
        """The other direction: a closure is a real scope chain, not a separate module.

        Narrowing past this would report a site the guard genuinely covers, and a false red is
        the cost that erodes a guard.
        """
        source = (
            "def load(p):\n"
            "    if non_regular_kind(p) is not None:\n"
            "        raise OSError('not a regular file')\n"
            "    def read():\n"
            "        return p.read_text()\n"
            "    return read()\n"
        )

        assert unguarded(source) == []

    def test_a_module_level_guard_does_not_cover_every_function(self) -> None:
        """A guard that ran once at import is not evidence about a function's own subject.

        Inheriting a module body's guard would let one import-time call silence every opener in
        the file -- the shape that makes a sweep report a clean reading of nothing.
        """
        source = (
            "if non_regular_kind(SOME_PATH) is not None:\n"
            "    raise OSError('not a regular file')\n"
            "\n"
            "def load(p):\n"
            "    return p.read_text()\n"
        )

        assert unguarded(source) == [("load", "read_text", 5)]

    def test_every_opener_spelling_is_seen(self) -> None:
        """The enumeration is read, not the first spelling that comes to mind.

        `read_text` alone would miss the writer, the byte forms and the builtin -- and the
        builtin is the one `#2130` measured blocking (`open(target_path, "wb")` on the Linux
        clipboard arm).
        """
        spellings = (
            "p.read_text()",
            "p.write_text('x')",
            "p.read_bytes()",
            "p.write_bytes(b'x')",
            "open(p)",
            "p.open()",
        )
        for call in spellings:
            source = f"def f(p):\n    return {call}\n"
            assert unguarded(source) == [("f", _called_name(ast.parse(call).body[0].value), 2)]

    def test_a_call_named_like_an_opener_but_made_by_something_else_is_still_seen(self) -> None:
        """The named limit, asserted rather than left to be discovered in review.

        The predicate keys on the call's name, so `webbrowser.open(url)` reads as an opener.
        That over-report is deliberate -- the doubt is resolved toward reporting -- and the
        repair is an `EXEMPT` row with its reason, not a narrower predicate that would also
        stop seeing this file's control.
        """
        source = "import webbrowser\n\ndef show(url):\n    return webbrowser.open(url)\n"

        assert unguarded(source) == [("show", "open", 4)]

    def test_a_lambda_body_is_scanned(self) -> None:
        """A lambda is a scope, and its body is where an opener hides.

        A lambda is never a **direct** child of a scope node: it sits inside an assignment, a
        call argument, an expression. So a recursion over `ast.iter_child_nodes` never reaches
        it, and its body is never scanned. Measured 2026-10-12: the direct-children form
        returned `[]` for each shape below -- three openers the sweep would not have looked at.
        """
        shapes = (
            "def outer(p):\n    f = lambda q: q.read_text()\n    return f\n",
            "handler = lambda p: p.read_text()\n",
            "use(fn=lambda p: p.read_text())\n",
        )
        for source in shapes:
            assert unguarded(source), f"a lambda body went unscanned: {source!r}"

    def test_a_lambda_that_asks_is_silent(self) -> None:
        """The other direction: a lambda that asks is a scope that asked, like a nested def."""
        source = "handler = lambda p: p.read_text() if non_regular_kind(p) is None else None\n"

        assert unguarded(source) == []

    def test_a_lambda_is_covered_by_an_enclosing_guard(self) -> None:
        """And it inherits the way a nested function does, or a real site reads as a gap."""
        source = (
            "def load(p):\n"
            "    if non_regular_kind(p) is not None:\n"
            "        raise OSError('not a regular file')\n"
            "    return lambda: p.read_text()\n"
        )

        assert unguarded(source) == []

    def test_a_lambda_is_covered_by_an_asking_lambda(self) -> None:
        """A lambda is an enclosing **function**, so the chain carries the same way twice.

        The sibling above pins this for a `def` that encloses a lambda; this pins it for a lambda
        that encloses one. The two read differently until `Lambda` joined the tuple that decides
        which scopes carry their ask -- measured 2026-10-12, this source was **reported** while
        the `def`-shaped one was silent, which is the false red that erodes a guard: the inner
        call runs only in the branch where the enclosing lambda has already asked the subject's
        kind.
        """
        source = (
            "load = lambda p: (\n"
            "    (lambda: p.read_text()) if non_regular_kind(p) is None else None\n"
            ")\n"
        )

        assert unguarded(source) == []

    def test_a_class_body_still_does_not_cover_its_methods(self) -> None:
        """The other half of the same tuple, pinned so widening it for lambdas cannot take this.

        A class body's ask is a statement that ran once, not evidence about the subject a method
        opens -- the reading the docstring gives for keeping `ClassDef` out. A lambda body is
        different in kind: it is a function body, and it runs per call.
        """
        source = (
            "class C:\n"
            "    if non_regular_kind(X) is not None:\n"
            "        raise OSError('not a regular file')\n"
            "\n"
            "    def read(self, p):\n"
            "        return p.read_text()\n"
        )

        assert unguarded(source) == [("C.read", "read_text", 6)]

    def test_a_def_nested_in_a_control_flow_block_is_scanned(self) -> None:
        """A nested `def` is not always a direct child of the scope that holds it.

        Inside a `try`, an `if` or a `with`, the nested function hangs off the **statement**
        rather than off the enclosing scope, so a recursion over direct children never visits it
        as a scope and its body is never scanned. Measured 2026-10-12: each shape below returned
        `[]` under the direct-children recursion.
        """
        shapes = (
            "def outer(p):\n    if True:\n        def inner(q):\n            return q.read_text()\n",
            "def outer(p):\n    try:\n        def inner(q):\n            return q.read_text()\n    except OSError:\n        pass\n",
            "def outer(p):\n    with open('x'):\n        def inner(q):\n            return q.read_text()\n",
        )
        for source in shapes:
            assert unguarded(source), f"a nested def went unscanned: {source!r}"

    def test_a_def_nested_in_a_control_flow_block_keeps_its_own_name(self) -> None:
        """And it is reported under its own qualname, not the enclosing scope's."""
        source = "def outer(p):\n    if True:\n        def inner(q):\n            return q.read_text()\n"

        assert unguarded(source) == [("outer.inner", "read_text", 4)]


class TestTheGuardList:
    """`GUARDS` is hand-typed, and this family grows a wrapper name with nearly every merge.

    That is the erosion direction: the next wrapper is absent from the list, every site behind
    it becomes a **false positive**, and the remedy a later cycle reaches for is an `EXEMPT` row
    reading "guarded by the new wrapper". A table of irreducible gaps then becomes a list of
    excuses. The control below is what makes the list grow deliberately instead.
    """

    def test_a_guard_whose_body_asks_nothing_is_reported(self) -> None:
        """Both directions on a source small enough to read."""
        source = (
            "def special_file_kind(mode):\n"
            "    if stat.S_ISREG(mode):\n"
            "        return None\n"
            "    return 'a directory'\n"
            "\n"
            "def _refuse_regular_or_something(p):\n"
            "    return f'{p} is not a regular file'\n"
        )
        defined = definitions_of(ast.parse(source), frozenset({"_refuse_regular_or_something"}))

        assert guards_that_ask(defined) == set(), (
            "a name whose body names no mode and calls no guard asks nothing, whatever it is "
            "called -- the whitelist reached by a speculative name is the whole point"
        )

    def test_a_wrapper_that_calls_a_guard_is_accepted(self) -> None:
        source = (
            "def _refuse_non_regular(p):\n"
            "    kind = non_regular_kind(p)\n"
            "    if kind:\n"
            "        raise OSError(kind)\n"
        )

        assert guards_that_ask(definitions_of(ast.parse(source), GUARDS)) == {"_refuse_non_regular"}

    def test_two_names_calling_each_other_reach_nothing(self) -> None:
        """Reachability is read from the whitelist outward, so a cycle is not a chain."""
        names = frozenset({"_wrapper_a", "_wrapper_b"})
        source = (
            "def _wrapper_a(p):\n"
            "    return _wrapper_b(p)\n"
            "\n"
            "def _wrapper_b(p):\n"
            "    return _wrapper_a(p)\n"
        )

        assert guards_that_ask(definitions_of(ast.parse(source), names), names) == set()

    def test_the_whitelist_is_reached_through_a_chain(self) -> None:
        source = (
            "def special_file_kind(mode):\n"
            "    if stat.S_ISREG(mode):\n"
            "        return None\n"
            "    return 'a socket'\n"
            "\n"
            "def non_regular_kind(path):\n"
            "    return special_file_kind(os.stat(path).st_mode)\n"
            "\n"
            "def _non_regular_kind(path):\n"
            "    return non_regular_kind(path)\n"
            "\n"
            "def _refuse_non_regular(path):\n"
            "    return _non_regular_kind(path)\n"
        )

        assert guards_that_ask(definitions_of(ast.parse(source), GUARDS)) == {
            "special_file_kind", "non_regular_kind", "_non_regular_kind", "_refuse_non_regular",
        }

    def test_every_guard_in_this_tree_asks_the_whitelist(self) -> None:
        """The rule, over every `GUARDS` name this tree defines."""
        defined: dict[str, list[list[ast.AST]]] = {}
        for path in sorted(SWEPT.rglob("*.py")):
            if SKIPPED & set(path.relative_to(REPO_ROOT).parts):
                continue
            for name, sites in definitions_of(ast.parse(path.read_text(encoding="utf-8")), GUARDS).items():
                defined.setdefault(name, []).extend(sites)

        assert defined, (
            "no `GUARDS` name is defined anywhere in this tree: the control has stopped "
            "measuring, and a clean verdict over an empty set is not one"
        )

        asked = guards_that_ask(defined)
        silent = sorted(name for name in defined if name not in asked)
        assert not silent, (
            "a name in `GUARDS` is not a thing that asks: its definition reaches neither the "
            "whitelist (`special_file_kind` / `non_regular_kind`) nor a mode read. A name added "
            "speculatively makes every site behind it a false positive, and the remedy then "
            "becomes an EXEMPT row that says 'guarded by the new wrapper'. Either the wrapper "
            "asks, or it is not a guard: " + ", ".join(silent)
        )


class TestThisTree:
    def test_every_opener_asks_its_subject_kind(self) -> None:
        """The rule, over this checkout: every opener asks, or is exempt with its reason."""
        flagged: dict[tuple[str, str, str], str] = {}
        judged = 0
        seen = 0
        for path in sorted(SWEPT.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if SKIPPED & set(path.relative_to(REPO_ROOT).parts):
                continue
            judged += 1
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:  # a file this sweep cannot read is not a pass
                raise AssertionError(f"{rel} does not parse: {exc}") from exc
            for qualname, opener, line, asked in opener_sites(tree):
                seen += 1
                if not asked:
                    flagged[(rel, qualname, opener)] = f"{rel}:{line}: {qualname}() {opener}()"

        assert judged > 60, (
            f"only {judged} file(s) were judged: a sweep over an empty set is a clean verdict "
            "about nothing, and that is the one reading this file must not give"
        )
        assert seen > 50, (
            f"the detector matched {seen} opener site(s): fewer than this checkout holds means "
            "the predicate has stopped seeing, not that the tree is clean"
        )

        unlisted = sorted(key for key in flagged if key not in EXEMPT)
        assert not unlisted, (
            "an opener that never asked its subject's kind: an open is not a read, and on a "
            "FIFO it blocks until a peer appears -- on the daemon's event loop that is the "
            "whole daemon, and the only recovery is a restart, which belongs to the host "
            "alone. Ask the kind before opening, or add an EXEMPT row with the reading that "
            "makes the site safe: " + ", ".join(flagged[key] for key in unlisted)
        )

        stale = sorted(key for key in EXEMPT if key not in flagged)
        assert not stale, (
            "these EXEMPT rows are no longer needed -- the site is guarded, or the function is "
            "gone. Remove the row so the table stays exactly the flagged set: "
            + ", ".join("/".join(key) for key in stale)
        )
