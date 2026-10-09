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
#: names differ (`SCANNED_DIRS` / `SCANNED_ROOTS`), which is exactly why the pairing has to
#: be read out of the files rather than assumed from a shared spelling.
ROOT_DECLARATIONS = {
    "scripts/check_unbound_reads.py": "SCANNED_DIRS",
    "scripts/check-undefined-names.py": "SCANNED_ROOTS",
    "tests/test_annotation_names_resolve.py": "SCANNED_ROOTS",
    "tests/test_no_dead_string_statement.py": "SCANNED_DIRS",
}

#: Guards that declare the directories they **skip**, and the name each uses.
SKIP_DECLARATIONS = {
    "scripts/check-citation-resolves.py": "SKIP_DIRS",
    "scripts/check_unbound_reads.py": "SKIPPED_DIRS",
    "scripts/check-undefined-names.py": "SKIPPED_DIRS",
    "tests/test_annotation_names_resolve.py": "SKIP_DIRS",
}


def _declared_literal(path: Path, name: str):
    """The literal a module-level `name = ...` binds in `path`, or None if it binds none.

    Handles the two spellings these files use: a tuple literal, and `frozenset({...})`, whose
    literal is the call's argument. Returning None for a name that is not there is what lets
    the callers below tell "the declaration moved or was renamed" from "the declaration
    changed" -- a file that stops declaring the name must not read as agreement.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Call) and value.args:
                    value = value.args[0]  # `frozenset({...})` / `set({...})`
                literal = ast.literal_eval(value)
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
