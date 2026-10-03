"""A guard must not drop a filesystem subject and still call its verdict clean.

The defect class (measured 2026-10-03, cycle cyc20261003-083317)
---------------------------------------------------------------
`scripts/check-citation-resolves.py` walked a tree with

    try:
        entries = sorted(current.iterdir())
    except OSError:
        continue

and read its sites with

    try:
        text = site.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue

Both are the same mistake: the subject the loop was given is dropped, the loop
moves on, and the sentence that comes out is about the whole tree. Measured on a
tree that really holds the defect (`chmod 000` on the directory / on the site
file), each answered `0` - "every citation names a node id pytest collects" -
where the same tree with the mode restored answered `1`. A third reading was
missing too: a root with no `tests/` at all answered `0` over an empty module
set, though that file's own exit table had promised `2` for exactly it.

Why a rule and not just the fix
-------------------------------
The fix is local; the shape is not. It is the same shape as "the root is not the
tree the caller stands in" (`check-doc-count.py`, `check_nonlocal.py`) and as
"every parse-skipped body is a clean answer" - a guard whose *coverage* is
narrower than its *claim*. Prose does not survive that, so the rule is read off
the source instead: **a `Continue` that skips a filesystem subject**, and every
instance of it either reports the subject or is registered below with a reason.

The rule is deliberately narrow - the `try` body must touch the filesystem
(`iterdir` / `read_text` / `open` / ...), the handler must be a bare `continue`,
and it must sit inside a loop. A `continue` after `json.loads` on a ledger row
is a different question (a corrupt *line*, not a skipped subject) and is not
this rule's to answer; widening it to cover those would trade a zero-false-
positive rule for a noisy one, which is how this family's rules get deleted.

Census at the time of writing: `scripts/check-doc-count.py::offenders` is the
only site left, and it is justified in the registry. On `master` the same scan
reports two more (`check-citation-resolves.py`), which is the reading this file
exists to keep at zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

#: Names whose call in a `try` body means the body touched the filesystem.
FS_CALLS = frozenset(
    {"iterdir", "rglob", "glob", "read_text", "read_bytes", "open", "walk", "scandir", "listdir"}
)

#: Every site allowed to drop a filesystem subject, keyed by
#: `(path relative to the repo, enclosing function, the except type as written)`.
#: Keyed by function rather than line number so ordinary edits above the handler
#: do not invalidate the registry - a registry that breaks on unrelated edits is
#: a registry somebody deletes.
JUSTIFIED: dict[tuple[str, str, str], str] = {
    (
        "scripts/check-doc-count.py",
        "offenders",
        "(OSError, UnicodeDecodeError)",
    ): (
        "Binary or unreadable: a claim cannot live in a file this rule cannot read, "
        "and failing the whole scan over, say, a PNG would make the guard unusable "
        "rather than strict. Stated in the code at the handler."
    ),
}


def _enclosing(tree: ast.AST, target: ast.AST) -> str:
    """The function a node sits in, or ``<module>``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if inner is target:
                    return node.name
    return "<module>"


def _in_a_loop(tree: ast.AST, target: ast.AST) -> bool:
    """Whether `target` sits inside a `for`/`while` somewhere in the tree."""
    found: list[bool] = []

    def walk(node: ast.AST, loop: bool) -> None:
        if node is target:
            found.append(loop)
        for child in ast.iter_child_nodes(node):
            walk(child, loop or isinstance(node, (ast.For, ast.While)))

    walk(tree, False)
    return found[0] if found else False


def _touches_the_filesystem(node: ast.AST) -> str | None:
    """The filesystem call a `try` body makes, if any."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in FS_CALLS:
            return name
    return None


def _name(path: Path) -> str:
    """The path as the registry keys it: repo-relative when it is inside the repo."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return path.name


def skipped_subjects() -> list[tuple[str, str, str]]:
    """Every `(path, function, except type)` that drops a filesystem subject."""
    out: list[tuple[str, str, str]] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not _touches_the_filesystem(node):
                continue
            for handler in node.handlers:
                if not (len(handler.body) == 1 and isinstance(handler.body[0], ast.Continue)):
                    continue
                if not _in_a_loop(tree, handler):
                    continue
                out.append(
                    (
                        _name(path),
                        _enclosing(tree, handler),
                        ast.unparse(handler.type) if handler.type else "bare",
                    )
                )
    return out


def test_no_guard_drops_a_filesystem_subject_without_a_reason():
    """The rule: every such `continue` is either reported or registered."""
    unregistered = [key for key in skipped_subjects() if key not in JUSTIFIED]
    assert unregistered == [], (
        "these guards skip a filesystem subject with a bare `continue`, so their "
        "verdict is about a subset of what it claims - report the subject (naming "
        "it and its reason) instead, or register the site here with the reason "
        "that makes a skip honest:\n  "
        + "\n  ".join(f"{p}::{fn}  except {exc}" for p, fn, exc in unregistered)
    )


def test_the_registry_names_no_site_that_is_gone():
    """The reverse leg: a registry entry for a fixed site is a stale permission."""
    present = set(skipped_subjects())
    stale = [key for key in JUSTIFIED if key not in present]
    assert stale == [], (
        "these sites no longer skip a filesystem subject, so their justification "
        "is now permission for nothing:\n  "
        + "\n  ".join(f"{p}::{fn}  except {exc}" for p, fn, exc in stale)
    )


def test_the_rule_fires_on_the_shape_it_was_written_for(tmp_path):
    """The control leg: the scan must be able to see the defect at all.

    A scanner that reports nothing on every tree would pass both legs above, so
    it is pointed at a synthetic module carrying the exact shape and required to
    name it.
    """
    module = tmp_path / "synthetic_guard.py"
    module.write_text(
        "from pathlib import Path\n"
        "\n"
        "def walk(root):\n"
        "    out = []\n"
        "    for path in sorted(root.iterdir()):\n"
        "        try:\n"
        "            out.append(path.read_text())\n"
        "        except OSError:\n"
        "            continue\n"
        "    return out\n",
        encoding="utf-8",
    )

    global SCRIPTS
    original = SCRIPTS
    try:
        SCRIPTS = tmp_path
        assert skipped_subjects() == [("synthetic_guard.py", "walk", "OSError")]
    finally:
        SCRIPTS = original


def test_the_citation_guard_is_no_longer_a_site():
    """The fix this rule was derived from, pinned by name.

    The rule above would accept a registered justification; this one refuses
    that escape for the file whose two holes were the measurement, so the fix
    cannot be undone by adding a registry entry.
    """
    offenders = {key[0] for key in skipped_subjects()}
    assert "scripts/check-citation-resolves.py" not in offenders
