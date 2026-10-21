"""A docstring that points at a section title must point at one the module carries.

Issue #2455. `scripts/feishu-gate.py` sends a reader to a titled section of its own module
docstring, and the title it names is not in the file — so the pointer resolves to nothing,
silently: the path opens, the file parses, and only a reader who goes looking discovers there
is nothing to find. Two other citations of the same shape exist in the tree and both resolve,
which is why the convention is real and the drift is what needs a guard.

Two subjects, both discovered
-----------------------------
A **title** is a line a docstring itself underlines with a row of dashes. A **citation** is
found by pattern anywhere in a module's source, `#` comments included — the one citation that
resolves in `scripts/check-doc-count.py` is a comment, so a docstring-only reader would miss
the half of the class that lives outside docstrings. Neither subject is a hand-written list of
files or titles: such a list admits every position it omits, and the state this guard reports
on has to be the tree's, not a roster's.

A citation resolves when the title it quotes is **contained in** one of the module's own
titles. Containment rather than equality is deliberate: `Scope` is a fair way to send a reader
to a section titled `Scope, stated with its measurement`. A citation that shares no wording
with any title is the defect.

What this does not measure
--------------------------
Whether the section a citation points at is the one the reader needed. That is a judgment
about the prose; this guard answers only whether the pointer lands anywhere.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The citation shape: a quoted title, then the claim that the module's own docstring carries
#: it. `\s+` rather than a space because the defect this guard was written for wraps the phrase
#: across a line break — an anchor pinned to one line would have read that site as no citation.
CITATION = re.compile(
    r'\bsee\s+"([^"]{2,200})"\s+in\s+(?:the\s+|this\s+)?module(?:\'s)?\s+docstring',
    re.IGNORECASE | re.DOTALL,
)

#: The directories whose Python sources carry this convention. Narrower than the tree on
#: purpose: these three are where a reader meets a module docstring, and a walk of the whole
#: repository would drag `.venv` and the GUI's own sources in for nothing.
SCANNED = ("emrg", "scripts", "tests")


def module_titles(docstring: str) -> list[str]:
    """The section titles a docstring carries: a line a row of dashes underlines.

    The underline is the whole rule. A short line above a paragraph is emphatic, not a title,
    and a reader sent to it would still find no heading — so a guard that treated every line
    as a title would call a broken citation resolved.
    """
    lines = docstring.splitlines()
    titles = []
    for index, line in enumerate(lines[:-1]):
        underline = lines[index + 1].strip()
        if len(underline) >= 3 and set(underline) == {"-"} and line.strip():
            titles.append(line.strip())
    return titles


def citations(text: str) -> list[tuple[int, str]]:
    """Every (line number, cited title) in a module's source text, in file order."""
    found = []
    for match in CITATION.finditer(text):
        line_number = text.count("\n", 0, match.start()) + 1
        found.append((line_number, match.group(1).strip()))
    return found


def scan(paths) -> list[str]:
    """One finding per citation that lands on no title the citing module carries."""
    findings = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(f"{path}: could not be read ({exc}); unmeasured, never a pass")
            continue
        cites = citations(text)
        if not cites:
            continue
        try:
            docstring = ast.get_docstring(ast.parse(text)) or ""
        except SyntaxError as exc:
            findings.append(f"{path}: does not parse ({exc}); unmeasured, never a pass")
            continue
        titles = module_titles(docstring)
        for line_number, cited in cites:
            if not any(cited.lower() in title.lower() for title in titles):
                findings.append(
                    f"{path}:{line_number}: cites {cited!r} as being in the module "
                    f"docstring, which carries {titles!r} — the reader is sent to a "
                    f"heading that is not there"
                )
    return findings


def tree_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every Python file under the directories this convention covers, in a stable order."""
    return sorted(
        path
        for name in SCANNED
        for path in (root / name).rglob("*.py")
        if path.is_file()
    )


# ── the tree ──────────────────────────────────────────────────────────────────
#
# Fixtures below build the citation phrase from parts on purpose. Written inline it would be a
# citation site in *this* file, and the tree reading above would report this guard's own
# fixtures back at it — a guard that cannot be run on the tree that holds it is no guard.
SEE = "see"
IN_MODULE = "in the module docstring"
IN_THIS_MODULE = "in this module's docstring"


def _fixture(cited: str, titles: list[str], connexion: str) -> str:
    docstring_lines = []
    for title in titles:
        docstring_lines.append(title)
        docstring_lines.append("-" * max(3, len(title)))
        docstring_lines.append("Body text.")
        docstring_lines.append("")
    body = "\n".join(docstring_lines)
    return f'"""{body}\n\nA pointer: {SEE} "{cited}" {connexion}.\n"""\n\n\nVALUE = 1\n'


def test_every_citation_in_the_tree_resolves():
    """The subject is this tree, and the reading names every site that does not land."""
    findings = scan(tree_files())
    assert not findings, (
        "a docstring points at a section title its module does not carry; the reader who "
        "follows it finds nothing, and nothing else in the suite asks:\n  "
        + "\n  ".join(findings)
    )


def test_the_defect_this_guard_was_written_for_is_reported(tmp_path):
    """Issue #2455's shape: a title assembled in prose, citing no section the file carries."""
    site = tmp_path / "citing.py"
    site.write_text(
        _fixture(
            "one approval, spent before it is sent",
            ["Spent before it is sent"],
            IN_MODULE,
        ),
        encoding="utf-8",
    )
    findings = scan([site])
    assert findings, (
        "a citation naming no section of its own module was read as resolved; this is the "
        "defect issue #2455 exists for and the arm the guard is worth nothing without"
    )
    assert "one approval, spent before it is sent" in findings[0], findings[0]
    assert "Spent before it is sent" in findings[0], (
        f"the finding does not say which titles the module does carry: {findings[0]}"
    )


def test_a_citation_across_a_line_break_is_still_a_citation(tmp_path):
    """The real site wraps the phrase, so a line-anchored reader would miss it entirely."""
    site = tmp_path / "wrapped.py"
    site.write_text(
        _fixture("a title that is absent", ["A title that is present"], IN_THIS_MODULE)
        .replace(IN_THIS_MODULE, "in this\nmodule's docstring"),
        encoding="utf-8",
    )
    assert citations(site.read_text(encoding="utf-8")), (
        "the wrapped citation was not found at all, so this guard cannot see the one site "
        "issue #2455 was filed for"
    )
    assert scan([site]), "a wrapped citation naming no section was read as resolved"


def test_a_partial_title_resolves(tmp_path):
    """`Scope` is a fair pointer to `Scope, stated with its measurement`."""
    site = tmp_path / "partial.py"
    site.write_text(
        _fixture("Scope", ["Scope, stated with its measurement"], IN_MODULE),
        encoding="utf-8",
    )
    assert scan([site]) == [], (
        "a citation naming the opening words of a real section was reported; a reader sent "
        "by a section's own opening words finds the section"
    )


def test_an_exact_title_resolves(tmp_path):
    """The convention's ordinary case, so the guard is not green only on the defect."""
    site = tmp_path / "exact.py"
    site.write_text(_fixture("The rule", ["The rule", "Another section"], IN_MODULE),
                    encoding="utf-8")
    assert scan([site]) == []


def test_a_section_that_underlines_nothing_is_not_a_title(tmp_path):
    """A bare emphatic line is not a heading, so citing it is the defect, not a resolution."""
    site = tmp_path / "ununderlined.py"
    site.write_text(
        '"""Intro.\n\nAn emphatic line\n\nBody text, with no underline beneath the line.\n"""\n'
        f'\n\n# A pointer: {SEE} "An emphatic line" {IN_MODULE}.\n\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    findings = scan([site])
    assert findings, (
        "a line with no underline was treated as a section title, so a citation to something "
        "that is not a heading read as resolved"
    )


def test_a_sentence_about_the_module_is_not_a_citation(tmp_path):
    """The lookalikes are the load-bearing half: a guard that reports prose gets deleted."""
    site = tmp_path / "lookalike.py"
    site.write_text(
        '"""A module.\n\nA section\n-----------\nBody.\n"""\n\n\n'
        "# The measurement is in the module docstring.\n"
        f"# A pointer: see \"A section\" for the detail.\n"
        f"# Another: {SEE} `A section` {IN_MODULE}.\n\n\nVALUE = 1\n",
        encoding="utf-8",
    )
    assert citations(site.read_text(encoding="utf-8")) == [], (
        "a sentence that merely mentions the module docstring, or backticks its subject, was "
        "read as a citation of a titled section"
    )
