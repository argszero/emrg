"""One scan scope, several guards: read the pairing out of the files that declare it.

Why this file exists
--------------------
Every first-party guard that judges this tree -- the read-before-binding check, the
unbound-name check, the dead-string-statement check, the annotation-name check -- spells the
scope it walks as its own module-level literal. The spellings are identical today, and the
relation between two of them is asserted **in prose**:

    #: Directories the scan covers, relative to the tree root -- the same first-party
    #: set `check_unbound_reads.py` reads, so a file that guard judges is judged here.
    SCANNED_ROOTS = ("emrg", "scripts", "tests", "packaging")

That sentence is the whole reason the two guards can be read as one instrument, and nothing
held it: a change that adds a root to one file leaves the other on the old tuple, silences a
file one guard judges, and every existing test on either side stays green -- each is asserted
against its own spelling. The skip set below it is the same shape, spelled once per guard
again, and the tables below are what hold both: a guard that joins the family, or one whose
spelling drifts, is admitted here or fails here.

This is the reasoning `tests/test_log_dir_pairing.py` records for the log directory, applied
where no language boundary is in the way: both sides are Python literals in this checkout, so
they are read as values and compared rather than restated.

How the declarations are read
-----------------------------
By **parsing each file's own AST** -- the modules are never imported. Two reasons, and the
second is the pointed one: these are scripts with hyphens in their names, so they cannot be
imported under their real path at all; and a value read out of the file is a read, while a
value written into the test would be the test asserting itself.

Named limits
------------
This pins the scope the guards **declare**, not the files each one actually ends up judging: a
guard could declare the right roots and still skip a file for another reason (a parse error, a
missing directory). What it closes is the silent one-sided divergence, which is the failure the
prose claim could not survive. `tests/test_no_dead_string_statement.py`'s smaller skip set is
deliberately **not** pinned to the others -- issue #1999 records why that is a separate
decision rather than an oversight.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files that declare the **roots** they walk, and the module-level name each uses. The
#: names differ (`SCANNED_DIRS` / `SCANNED_ROOTS` / `SWEPT` / `FIRST_PARTY_TREE`), which is
#: exactly why the pairing has to be read out of the files rather than assumed from a shared
#: spelling. Two entries arrived by this file's own membership reading rather than by hand:
#: `tests/test_walk_skips_read_their_root.py` (found on `428cc330`) and this file's own
#: `FIRST_PARTY_TREE`, which the reading caught as soon as it was written.
ROOT_DECLARATIONS = {
    "scripts/check_unbound_reads.py": "SCANNED_DIRS",
    "scripts/check-undefined-names.py": "SCANNED_ROOTS",
    "tests/test_annotation_names_resolve.py": "SCANNED_ROOTS",
    "tests/test_guard_scan_scope_pairing.py": "FIRST_PARTY_TREE",
    "tests/test_no_dead_string_statement.py": "SCANNED_DIRS",
    "tests/test_walk_skips_read_their_root.py": "SWEPT",
}

#: Guards that declare the directories they **skip**, and the name each uses.
SKIP_DECLARATIONS = {
    "scripts/check-citation-resolves.py": "SKIP_DIRS",
    "scripts/check_unbound_reads.py": "SKIPPED_DIRS",
    "scripts/check-undefined-names.py": "SKIPPED_DIRS",
    "tests/test_annotation_names_resolve.py": "SKIP_DIRS",
}


#: The constructors whose result is exactly the elements they are handed, so `X = tuple(a)` and
#: `X = a` declare the same scope. **Named, not "any call"**: `X = make_roots(a)` is not a
#: declaration of `a`, and reading it as one attributes a value the file never wrote -- silently,
#: on the member side, where two tuples that need not be equal would then read as agreement
#: (measured 2026-10-10: the unwrap was `value.args[0]` for *any* call). A call outside this set is
#: not read even when it would preserve its argument (`sorted(a)`), because the reading decides by
#: the name a declaration spells, not by what a function does at runtime -- the same reason
#: `tests/test_no_dead_string_statement.py`'s smaller skip set is left unpinned rather than guessed.
_COLLECTION_BUILDERS = frozenset({"frozenset", "set", "tuple", "list"})


def _declared_names(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """`(name, value)` for every module-level declaration `node` makes, in source order.

    One place decides **what a declaration is**, so the two readers below cannot drift on it: the
    class is data a third reading can check, which is the same reason the scope tables are read out
    of the files rather than restated. A tuple target (`A, B = ...`) declares no single name.

    Both assignment kinds are read -- `Assign` and the annotated `AnnAssign` -- because a
    declaration's *kind* does not change what it declares: `TREE: tuple = (...)` binds the same
    value `TREE = (...)` does. A target that is not a plain name (an attribute or a subscript)
    declares no module-level constant of this class and is not read.

    The annotated spelling was once excluded, on the reading that a declaration this extractor
    cannot read at least fails **loudly** (the name comes back as "moved or renamed" and the
    scan-roots pairing refuses). Measured 2026-10-10 on `cde96007`: that is true of exactly **one**
    of the two readers. `_declared_literal` returns None for it, which the pairing reports as a
    missing member -- loud. But `_module_literals` **omits** it, so the membership sweep does not
    see the file at all and certifies silence: a guard that joins the family with
    `SCANNED_ROOTS: tuple = (...)` and is not in the table was found by nothing, which is the same
    defect #2030 added that sweep to catch, one spelling later. Both sides now read it.
    """
    if isinstance(node, ast.Assign):
        return [(target.id, node.value) for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return [(node.target.id, node.value)]
    return []


def _declared_collection(node: ast.expr):
    """The collection a declaration's value spells, or None when it spells none.

    The one normalisation both readers use: a call of a named collection builder is unwrapped to
    its argument, a tuple/list/set/frozenset literal is itself, and anything else -- a name, an
    f-string, a comprehension, a call to something else, a literal that is not a collection -- is
    None rather than a guess. Every one of those raises inside `ast.literal_eval`, so the `try` is
    what keeps a readable sentence where a traceback would otherwise be: before it, a declaration
    of `X = OTHER_NAME` reached the caller as an uncaught `ValueError`.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if not (isinstance(func, ast.Name) and func.id in _COLLECTION_BUILDERS and node.args):
            return None
        node = node.args[0]
    try:
        literal = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return literal if isinstance(literal, (tuple, list, set, frozenset)) else None


def _declared_literal(path: Path, name: str):
    """The literal a module-level `name = ...` binds in `path`, or None if it binds none.

    Handles the spellings these files use: a tuple literal, `frozenset({...})` / `set({...})`, and
    the same wrapped in a collection builder (`_COLLECTION_BUILDERS`).
    Returning None -- for a name that is not there, and for a declaration this reading cannot
    reduce to a collection -- is what lets the callers below tell "the declaration moved or was
    renamed" from "the declaration changed": a file that stops declaring the name, or declares it
    in a form this cannot read, must not read as agreement.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        for declared, value in _declared_names(node):
            if declared == name:
                literal = _declared_collection(value)
                if literal is None:
                    return None
                return frozenset(literal) if isinstance(literal, (set, frozenset)) else tuple(literal)
    return None


def _read_all(declarations: dict[str, str]):
    """`{file: literal}` for a declaration table, with the files that bind nothing."""
    read: dict[str, object] = {}
    for relative, name in declarations.items():
        read[relative] = _declared_literal(REPO_ROOT / relative, name)
    return read


def _one_value(read: dict[str, object]):
    """The single value every entry agrees on, or None when they do not all agree."""
    values = list(read.values())
    if not values or any(value is None for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


#: Where first-party Python lives, for the two membership readings below. **Deliberately not
#: read from `ROOT_DECLARATIONS`**: the sweep scope is the input to the question "is every
#: declaration of the family's value a member?", so deriving it from the members would make the
#: reading an echo -- a member dropping `packaging` would shrink the sweep with it and the
#: reading would stay green. It coincides with the family's roots today, and #2028's measurement
#: (2026-10-10, `428cc330`) is why it can be stated independently: every top-level directory this
#: checkout holds Python in is one of these four.
FIRST_PARTY_TREE = ("emrg", "scripts", "tests", "packaging")

#: What a search of that tree must not descend into. A search-scope detail, **not** the family's
#: skip set: the three that can appear inside those roots. Held by the vendored case in
#: `test_the_membership_reading_is_driven_in_both_directions` -- until that case existed, blanking
#: this set left every target green (measured 2026-10-10) while the tree really does hold twelve
#: vendored `.py` files under the roots (`emrg/gui/node_modules/...`), each of them a file the
#: sweep would otherwise read as a declaration.
SEARCH_SKIP = {".venv", "__pycache__", "node_modules"}


def _module_literals(path: Path) -> dict[str, object]:
    """Every module-level `name = <collection of strings>` in `path`, by name.

    The same two helpers `_declared_literal` uses -- the statement kinds in `_declared_names` and
    the normalisation in `_declared_collection` -- so the two readings compare equal by
    construction rather than by two copies happening to agree. A value that is not a collection of
    strings is left out rather than guessed at.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    out: dict[str, object] = {}
    for node in tree.body:
        for declared, value in _declared_names(node):
            literal = _declared_collection(value)
            if literal is None or not all(isinstance(element, str) for element in literal):
                continue
            out[declared] = literal
    return out


def _declaring_the_family_value(root: Path, value) -> list[str]:
    """`file:name` for every module-level declaration under `root` equal to `value`.

    The reading the membership tests below are: a guard declares its scope as one module-level
    literal, so the declarations are compared **by value** rather than by the name they use --
    which is the whole reason the tables are read out of the files. Element-wise comparison, so
    a tuple, list, set or frozenset spelling all count as the same declaration.

    A function of its root, so the same predicate can be driven on a tree a test builds.
    """
    wanted = set(value)
    found: list[str] = []
    for name in FIRST_PARTY_TREE:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SEARCH_SKIP & set(path.relative_to(root).parts):
                continue
            relative = path.relative_to(root).as_posix()
            for declared, literal in _module_literals(path).items():
                if set(literal) == wanted:
                    found.append(f"{relative}:{declared}")
    return sorted(found)


def test_the_extractor_reads_the_files_own_value(tmp_path):
    """The control: an instrument that is a parser has to be shown reading, not agreeing.

    Each case is a value this test was not written against, so a helper that returned a
    constant -- or that read the table instead of the file -- would disagree here.
    """
    sample = tmp_path / "sample.py"
    sample.write_text('SCANNED_DIRS = ("alpha", "beta")\n', encoding="utf-8")
    assert _declared_literal(sample, "SCANNED_DIRS") == ("alpha", "beta")

    sample.write_text('SKIPPED_DIRS = frozenset({"gamma", "delta"})\n', encoding="utf-8")
    assert _declared_literal(sample, "SKIPPED_DIRS") == frozenset({"gamma", "delta"})

    # ... and the same file re-declared, for the discrimination the pins depend on: a
    # different literal must come back different, or "they agree" measures nothing.
    sample.write_text('SCANNED_DIRS = ("alpha", "beta", "epsilon")\n', encoding="utf-8")
    assert _declared_literal(sample, "SCANNED_DIRS") == ("alpha", "beta", "epsilon")

    # a name the file does not bind is None, never an empty reading that compares equal
    assert _declared_literal(sample, "NOT_DECLARED_HERE") is None

    # ... and the annotated spelling declares the same value: the annotation is a type on the
    # binding, not part of it. Read by neither reader before this, which made the membership
    # sweep miss such a file entirely (measured 2026-10-10 on `cde96007`).
    sample.write_text('SCANNED_ROOTS: tuple = ("emrg", "scripts")\n', encoding="utf-8")
    assert _declared_literal(sample, "SCANNED_ROOTS") == ("emrg", "scripts")


def test_the_extractor_refuses_what_it_cannot_reduce_to_a_collection(tmp_path):
    """The soundness control: the reading is a reduction of the declaration, not a guess at it.

    Each case below is a value the extractor must **refuse** rather than resolve, because a value
    it guesses is a value it certifies -- the defect this test was added with: the unwrap was
    `value.args[0]` for *any* call, so `TREE = make_roots((...))` came back as the literal inside
    it, exactly as if the file had written that literal, and the member side of the pairing
    silently agreed with a scope nobody declared (measured 2026-10-10, `0600c1aa`).
    """
    sample = tmp_path / "sample.py"

    # `make_roots` may return anything, so its argument is not this declaration's value
    sample.write_text('TREE = make_roots(("alpha", "beta"))\n', encoding="utf-8")
    assert _declared_literal(sample, "TREE") is None

    # a value that is a name is not a literal at all -- and must come back None, not raise:
    # `ast.literal_eval` raised an uncaught `ValueError` here before this test existed
    sample.write_text("TREE = SOME_OTHER_NAME\n", encoding="utf-8")
    assert _declared_literal(sample, "TREE") is None

    # a literal that is not a collection is not the family's scope, so it is not read as one
    sample.write_text("TREE = 3\n", encoding="utf-8")
    assert _declared_literal(sample, "TREE") is None

    # ... and the other direction, so the refusals above are not a reading that returns None for
    # everything: the constructors whose result *is* their argument stay readable
    for spelling, expected in (
        ('TREE = set({"alpha", "beta"})\n', frozenset({"alpha", "beta"})),
        ('TREE = tuple(["alpha", "beta"])\n', ("alpha", "beta")),
        ('TREE = ("alpha", "beta")\n', ("alpha", "beta")),
    ):
        sample.write_text(spelling, encoding="utf-8")
        assert _declared_literal(sample, "TREE") == expected


def test_one_value_refuses_a_disagreement(tmp_path):
    """Both directions of the comparison the two pins below use, on values made up here."""
    assert _one_value({"a": ("x",), "b": ("x",)}) == ("x",)
    assert _one_value({"a": ("x",), "b": ("y",)}) is None
    assert _one_value({"a": ("x",), "b": None}) is None
    assert _one_value({"a": (), "b": ()}) == ()


def test_the_tree_reading_guards_declare_the_same_scan_roots():
    """The prose claim in `check-undefined-names.py`, held by a reading instead of a sentence."""
    read = _read_all(ROOT_DECLARATIONS)
    missing = [name for name, value in read.items() if value is None]
    assert not missing, (
        f"these file(s) no longer declare the roots they walk: {missing}. The pairing is about "
        "the declarations, so a renamed or moved constant has to be reflected here rather than "
        "read as agreement."
    )
    assert all(read.values()), f"a scan-root declaration is empty: {read}"
    assert _one_value(read) is not None, (
        "the guards no longer walk the same first-party roots: "
        + "; ".join(f"{name} -> {value}" for name, value in read.items())
        + ". A file one of these guards judges is no longer judged by the others, and each "
        "file's own tests stay green because each is asserted against its own spelling."
    )


def test_the_tree_reading_guards_declare_the_same_skipped_directories():
    """The same shape again: one skip set, declared once per guard, no pin until now."""
    read = _read_all(SKIP_DECLARATIONS)
    missing = [name for name, value in read.items() if value is None]
    assert not missing, f"these script(s) no longer declare a skip set: {missing}"
    assert all(read.values()), f"a skip-set declaration is empty: {read}"
    assert _one_value(read) is not None, (
        "the scripts no longer skip the same directories: "
        + "; ".join(f"{name} -> {sorted(value)}" for name, value in read.items())
    )


def _membership_delta(table: dict[str, str], found: list[str]) -> tuple[list[str], list[str]]:
    """`(joined unread, listed but not declaring it)` for a table against a tree reading."""
    expected = {f"{relative}:{name}" for relative, name in table.items()}
    present = set(found)
    return sorted(present - expected), sorted(expected - present)


def test_every_declaration_of_the_family_roots_is_a_member():
    """The tree is read back, so a guard that joins the family cannot do it unread.

    The tables above are hand-written, and the premise they encode -- a root added to one guard
    and missed by another silences a file every existing test still passes -- holds one level up
    if nothing reads the tree: a guard declaring the family's roots without being listed is
    exactly that divergence, one guard later. Measured 2026-10-10 (`428cc330`), five modules
    declared the family's roots and the table named four; the fifth was
    `tests/test_walk_skips_read_their_root.py`, landed the day before.

    The family's value is read out of the members rather than restated, so there is no second
    copy of it free to disagree.
    """
    family = _one_value(_read_all(ROOT_DECLARATIONS))
    assert family is not None, "the members disagree on their roots, so this reading has no subject"

    unread, absent = _membership_delta(
        ROOT_DECLARATIONS, _declaring_the_family_value(REPO_ROOT, family)
    )
    assert not unread, (
        f"these module-level declaration(s) hold the family's own roots {family} but are not in "
        f"ROOT_DECLARATIONS: {unread}. A guard that declares the family's scope is a member of "
        "the pairing, or the first root added to one of them diverges here in silence -- admit "
        "it to the table (the constant's name is part of the entry, so a differently-named "
        "declaration is listed by its own name)."
    )
    assert not absent, (
        f"ROOT_DECLARATIONS lists declaration(s) that no longer hold the family's roots: {absent}. "
        "Either the constant moved or it was renamed: the reading follows the value."
    )


def test_every_declaration_of_the_family_skip_set_is_a_member():
    """The same reading for the skip sets, which legitimately differ between guards.

    This is *not* "every scan skips these directories" -- `test_no_dead_string_statement.py`'s
    smaller set is deliberately unpinned (issue #1999) and is not asked to join. It is the weaker
    but decidable claim: a module that skips exactly what the family skips, and declares it, is a
    member. Consistent on this checkout today (measured 2026-10-10: four declarations, four
    members), and the control below drives the predicate so it is not decoration.
    """
    family = _one_value(_read_all(SKIP_DECLARATIONS))
    assert family is not None, "the members disagree on their skip set, so this has no subject"

    unread, absent = _membership_delta(
        SKIP_DECLARATIONS, _declaring_the_family_value(REPO_ROOT, family)
    )
    assert not unread, (
        f"these module-level declaration(s) hold the family's own skip set but are not in "
        f"SKIP_DECLARATIONS: {unread}"
    )
    assert not absent, (
        f"SKIP_DECLARATIONS lists declaration(s) that no longer hold the family's skip set: "
        f"{absent}"
    )


def test_the_membership_reading_is_driven_in_both_directions(tmp_path):
    """The controls: the predicate is shown finding, and shown staying silent.

    The tree is built here, so the reading is a function of the root it is pointed at rather than
    of this checkout -- a sweep that returned the table back would pass the two tests above and
    fail every case below.
    """
    family = ("emrg", "scripts", "tests", "packaging")
    package = tmp_path / "tests"
    package.mkdir()
    (package / "declares_it_freshly.py").write_text(
        f'TREE = {family!r}\n', encoding="utf-8"
    )
    (package / "declares_another_set.py").write_text(
        'TREE = ("emrg", "scripts")\n', encoding="utf-8"
    )
    (package / "declares_it_inside_a_function.py").write_text(
        f"def build():\n    TREE = {family!r}\n    return TREE\n", encoding="utf-8"
    )
    (package / "declares_a_non_literal.py").write_text(
        "TREE = tuple(sorted({'emrg', 'scripts'}))\n", encoding="utf-8"
    )
    # ... and the same declaration written with its type annotated. This is the shape the sweep
    # used to miss in silence: `_declared_names` read `ast.Assign` only, and the membership side
    # omits what it cannot read rather than failing, so a guard joining the family this way was
    # found by nothing (measured 2026-10-10 on `cde96007`, where this file was absent from
    # `found`). It is asserted by name below so the failure is this case rather than the equality.
    (package / "declares_it_annotated.py").write_text(
        f"TREE: tuple = {family!r}\n", encoding="utf-8"
    )
    # ... and its twin, which the old extractor *did* resolve: an arbitrary callee handed the
    # family's own value. Unwrapping `make_roots(...)` would put this file in `found`, so the
    # equality below is what holds the "named builders only" rule on the sweep's own side
    # (measured 2026-10-10: `value.args[0]` was read for *any* call).
    (package / "declares_it_through_a_callee.py").write_text(
        f"TREE = make_roots({family!r})\n", encoding="utf-8"
    )
    # ... and a declaration of the same value the sweep must not reach, because it sits under a
    # skipped directory. This case is the only reading that holds `SEARCH_SKIP`: without it,
    # blanking that set left every target green (measured 2026-10-10) while the vendored `.py`
    # files already under the roots were being read.
    vendored = package / "node_modules" / "vendored.py"
    vendored.parent.mkdir()
    vendored.write_text(f"TREE = {family!r}\n", encoding="utf-8")

    found = _declaring_the_family_value(tmp_path, family)
    # asked before the equality below, so the case that blanks `SEARCH_SKIP` fails on its own line
    # rather than on the general one -- two arms, two assertions, each naming its own defect
    skipped = [entry for entry in found if entry.startswith("tests/node_modules/")]
    assert not skipped, f"the sweep read a file under a skipped directory: {skipped}"
    # the annotated spelling is a declaration like any other, and it is asked in its own assertion
    # so an extractor that stops reading it fails here by name rather than on the general equality
    annotated = [entry for entry in found if entry == "tests/declares_it_annotated.py:TREE"]
    assert annotated, (
        f"the sweep missed a declaration written with its type annotated: {found}. The two "
        "readers share `_declared_names`, so a declaration kind it does not read is silently "
        "absent from the membership reading -- a guard joining the family this way is found by "
        "nothing."
    )
    assert found == ["tests/declares_it_annotated.py:TREE", "tests/declares_it_freshly.py:TREE"], found

    # ... and the other direction: a different value is not this value, so nothing is found.
    assert _declaring_the_family_value(tmp_path, ("emrg", "scripts")) == [
        "tests/declares_another_set.py:TREE"
    ]

    # the skip side of the same predicate, on the same tree
    assert _declaring_the_family_value(tmp_path, family) != _declaring_the_family_value(
        tmp_path, ("emrg",)
    )
