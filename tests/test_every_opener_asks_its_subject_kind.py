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
encloses it, calls one of the `GUARDS` (the whitelist itself, a per-layer wrapper for it, or
the refusal the family raises). Every opener must either sit in a scope that asks, or be
listed in `EXEMPT` with the reading that makes it safe.

This is a **structural** check, not a dataflow one: it asks whether the scope that opens asks
the kind *at all*, which is the question every historical site above would have failed
(`_current_installed_version` and `_read_local_version` were existing functions that named no
kind, not new code). A named limit below records what that costs.

Measured 2026-10-12 (cycle `cyc20261012-132122`) on master `be1dea5`: **91** files judged,
**77** opener sites, **7** with no asking scope, each of them in `EXEMPT` with its reason. The
table is asserted to be **exactly** the flagged set, so an exemption that is no longer needed
-- a site fixed, a function removed -- fails here too rather than rotting.

Named limits
------------
* the guard is read at the **scope**, so a scope that asks about one subject and opens
  another reads as asking. Measured on the same tree: of the 70 sites this detector calls
  asked, 7 name a subject other than the guard's argument, and all 7 are the tool layer's
  **mode form** (`special_file_kind(info.st_mode)`, after `path.stat()`) -- one shape for one
  rule, not one rule per spelling. Zero sites anywhere in this checkout ask about one subject
  and open another;
* an opener reached through a **helper** is out of reach: `shutil.copyfile`, `os.read` and
  `os.write` open their subject inside another module, and no reading of the call site can see
  it. The repair belongs in that helper, and this file's tree leg is green on it;
* a call **named** `open` counts whatever it belongs to, so a non-file helper of that name
  (`webbrowser.open`) would be reported. There is none in `emrg/` today, and the repair is an
  exemption row with its reason, not a narrower predicate -- the same doubt this file resolves
  toward reporting everywhere else;
* the sweep covers `emrg/`, the package this checkout ships. `emrg/gui/` holds no Python at
  all (`emrg/gui/**/*.py` matches nothing), so the exclusion conceals no site. `scripts/` and
  `tests/` are not swept: the rule is about the daemon's own opens, and a guard script or a
  test that opens a FIFO it just created is exercising the rule, not obeying it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The trees whose opens the rule governs. `emrg/gui/` holds no Python (measured: the glob
#: matches nothing), so this is the whole shipped-Python surface.
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

#: The calls that ask a subject's kind. Three shapes for one rule: the whitelist itself
#: (`special_file_kind`, mode form), its path-taking twin (`non_regular_kind`), the per-layer
#: wrappers that refuse on its answer, and the refusal the family raises.
GUARDS = frozenset(
    {
        "special_file_kind",
        "special_file_refusal",
        "non_regular_kind",
        "_non_regular_kind",
        "_refuse_non_regular",
        "_refuse_non_regular_file",
        "_refuse_a_non_regular_subject",
        "NotARegularFile",
    }
)

#: Openers with no asking scope, keyed by `(path relative to REPO_ROOT, qualname, opener)`.
#: Keyed by qualname rather than by line so an edit above a site does not rot the table -- and
#: by the opener, so a **second** unguarded open added to an already-exempt function still
#: fails here. Every row states the reading that makes the site safe today; the tree leg
#: asserts this is exactly the flagged set, so a row whose site is fixed is reported too.
EXEMPT: dict[tuple[str, str, str], str] = {
    ("emrg/_stop_all.py", "stop_daemon", "read_text"): (
        "the stop path, which MANIFESTO pin 4-2 (host-established) forbids a cycle to write, "
        "restore or introduce anything into -- a guard here is a change no cycle may make. "
        "Recorded as a known gap rather than repaired."
    ),
    ("emrg/_stop_all.py", "_open_stop_log", "open"): (
        "the same forbidden surface: `_open_stop_log` writes the stop log this family's red "
        "line protects. Recorded, not repaired."
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
        "the family's one known gap: the subject is `self.images_dir / <name derived from the "
        "image bytes>`, and a non-regular file could sit at that path -- the `while "
        "filepath.exists()` above it is the code's own acknowledgement that something may "
        "already be there. Left unguarded because the method has no caller in this checkout "
        "(measured: `save_image` occurs once, at its own definition). Recorded so the cycle "
        "that next touches `session.py` decides between guarding it and dropping it."
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
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                carried = asked if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else inherited
                visit(child, qualname, carried)

    visit(tree, "", False)
    return sorted(set(found))


def unguarded(source: str) -> list[tuple[str, str, int]]:
    """`(qualname, opener, line)` for every opener in `source` with no asking scope."""
    return [
        (qualname, opener, line)
        for qualname, opener, line, asked in opener_sites(ast.parse(source))
        if not asked
    ]


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
