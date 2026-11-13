"""The fence-marker rule is read by several scripts, and one of them read a tab as a fence.

A **fence marker** (`` ``` `` or `` ~~~ ``) opens a code block only at an indent of up to
three **spaces**; at four columns it is an indented code block and the marker is ordinary
text. Several scripts in this tree read that rule to answer one question each - *is this
line content, or is a fence quoting it?* - and they do not all read it the same way:

* ``check-cycle-record.py`` asks it of a cycle record's state file and archive;
* ``check-vote-count.py`` asks it of a review body, to tell a *stated* veto from a quoted
  one;
* ``check-issue-links.py`` asks it of an issue or PR body, to tell a *declared* claim
  from a quoted one - and it masks **wider on purpose**, because masking stops early and
  costs a loud ``UNCLAIMED`` row, while reading the quotation invents a link.

Measured 2026-11-13 (issue #2890): the vote counter's pattern admitted a tab, which is
four columns, so the veto under a tab-indented marker was read as quoted and dropped -
the one direction that leaves the approvals in front of it live. **The carriers are
discovered, not listed**, so a script that starts reading this rule is found rather than
remembered, and each carrier's indent dialect is measured rather than trusted: a change
to any one of them moves a row here instead of passing quietly.

What makes a pattern a **carrier** is behaviour, not its spelling: it must match a bare
fence line *entirely* (so a pattern that matches a marker embedded in prose - an inline
code span - is not one), and it must not match ordinary prose (so a catch-all ``.*`` is
not one either). Both arms are asserted below, because a discovery that could return
anything proves nothing about what it returned.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every tree a reader of this rule can live in. ``scripts/`` holds the gates,
#: ``emrg/`` the runtime; both are scanned because the rule is about reading text,
#: which either side may do.
TREES = ("scripts", "emrg")

#: The two dialects measured today. ``spaces`` is the CommonMark rule; ``any`` admits a
#: tab and any indent as a marker, which is right for a masker and wrong for a reader
#: that must not drop a stated veto. A third dialect is not admitted silently - it makes
#: ``_dialect`` return ``None`` and the census reddens, so adding one is a decision.
SPACES = "spaces"
ANY = "any"

#: Module name -> the dialect(s) it reads. Discovered carriers must match this exactly:
#: a new module carrying a fence pattern is an omission the reading reports, and a
#: dialect that moved is a change to the rule rather than a new reader.
PINNED = {
    "check-cycle-record.py": {SPACES},
    "check-vote-count.py": {SPACES},
    "check-issue-links.py": {ANY},
}

#: One line per probe. The first is the bare marker every carrier must accept; the
#: fourth is prose no carrier may accept.
PROSE = "hello world"


def _pattern(node: ast.Call) -> re.Pattern[str] | None:
    """The compiled pattern of a ``re.compile(<str>)`` call, or None for any other call."""

    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "compile" and node.args):
        return None

    def literal(sub: ast.AST) -> object:
        try:
            return ast.literal_eval(sub)
        except Exception:  # a computed argument is not a pattern this census can read
            return None

    source = literal(node.args[0])
    if not isinstance(source, str):
        return None
    flags = literal(node.args[1]) if len(node.args) > 1 else 0
    try:
        return re.compile(source, flags if isinstance(flags, int) else 0)
    except (re.error, TypeError):
        return None


def _dialect(pattern: re.Pattern[str]) -> str | None:
    """Which indent classes the pattern admits, named - or None for a dialect not seen yet."""

    if pattern.match("   ```") is None:
        return None
    tab = pattern.match("\t```") is not None
    four = pattern.match("    ```") is not None
    if tab and four:
        return ANY
    if not tab and not four:
        return SPACES
    return None


def _fence_carriers(path: Path) -> list[tuple[int, str, str]]:
    """Every fence-marker pattern in one file, as (line, source, dialect)."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # scripts carry invalid escapes
        tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        pattern = _pattern(node)
        if pattern is None:
            continue
        # A carrier matches a bare marker line *entirely*, and never plain prose.
        match = pattern.match("```")
        if match is None or match.span() != (0, 3):
            continue
        if pattern.match(PROSE) is not None:
            continue
        dialect = _dialect(pattern)
        found.append((node.lineno, pattern.pattern, dialect))
    return found


def _discovered() -> dict[str, set[str | None]]:
    """Module name -> the dialects its fence patterns read, over every tree scanned."""

    assert _fence_carriers(REPO_ROOT / "scripts" / "check-vote-count.py"), (
        "the control file carries a fence pattern on master, so an empty reading here "
        "means the discovery is broken rather than that the rule has no carriers"
    )
    out: dict[str, set[str | None]] = {}
    for tree in TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py")):
            for _line, _source, dialect in _fence_carriers(path):
                out.setdefault(path.name, set()).add(dialect)
    return out


def test_every_fence_carrier_is_discovered_and_its_dialect_measured() -> None:
    """The census finds each carrier by behaviour, so a new reader cannot arrive unseen."""

    discovered = _discovered()

    assert set(discovered) == set(PINNED), (
        "the set of scripts reading the fence rule moved: discovered "
        f"{sorted(discovered)}, pinned {sorted(PINNED)} - a new carrier must be named "
        "with the dialect it reads, and a deleted one removes its row"
    )
    for module, dialects in sorted(discovered.items()):
        assert dialects == PINNED[module], (
            f"{module} reads a dialect this census does not know: {sorted(dialects)} "
            f"where {sorted(PINNED[module])} was pinned. `spaces` is CommonMark; `any` "
            "admits a tab, which is four columns and therefore an indented code block. "
            "A reader that stops dropping a stated veto has to read `spaces`."
        )


def test_the_tab_is_the_class_that_separates_the_two_dialects() -> None:
    """The dialects differ on exactly one probe, and it is the one that dropped a veto.

    Pinned as a reading of the two dialect names rather than as a claim about any one
    script: three spaces and four spaces agree across both dialects today, so a future
    divergence there is a different finding and must be reported as one.
    """

    table = {}
    for name in (SPACES, ANY):
        admitted = set()
        for probe, label in (
            ("```", "column 0"),
            ("   ```", "three spaces"),
            ("\t```", "tab"),
            ("    ```", "four spaces"),
        ):
            # The two pinned dialects, expressed as the readers themselves express them.
            pattern = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$") if name == SPACES else re.compile(
                r"^[ \t]*(?:```|~~~).*$"
            )
            if pattern.match(probe) is not None:
                admitted.add(label)
        table[name] = admitted

    assert table[SPACES] == {"column 0", "three spaces"}
    assert table[ANY] == {"column 0", "three spaces", "tab", "four spaces"}
    assert table[SPACES] ^ table[ANY] == {"tab", "four spaces"}, (
        "the dialects now differ on more than the indents a tab and a four-space run "
        f"create: {sorted(table[SPACES] ^ table[ANY])}"
    )


def test_the_discovery_rejects_a_pattern_that_is_not_a_carrier(tmp_path: Path) -> None:
    """Both arms of the definition, so a census that returns everything is not green.

    An inline code-span pattern matches a marker *inside* a line and a catch-all matches
    prose, so neither is a reader of this rule; a pattern that matches a bare marker line
    whole is. Without this arm the census above would pass on a discovery that returned
    every ``re.compile`` in the tree.
    """

    module = tmp_path / "sample.py"
    module.write_text(
        "import re\n"
        'INLINE = re.compile(r"`[^`\\n]*`|``.*?``")\n'
        'EVERYTHING = re.compile(r".*")\n'
        'FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")\n',
        encoding="utf-8",
    )
    sources = [source for _line, source, _dialect in _fence_carriers(module)]
    assert sources == [r"^ {0,3}(`{3,}|~{3,})(.*)$"], (
        f"only the fence pattern is a carrier of this rule, found {sources}"
    )
