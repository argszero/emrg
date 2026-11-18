"""The page's skeleton, read from the files the bundle is built from (issue #2969).

Two readings live here rather than in the renderer's own suite, and the reason is a
measurement, not a preference: the renderer typechecks with `types: ["vite/client",
"@testing-library/jest-dom"]` and has no `@types/node` of its own, so a `node:fs` read
inside a renderer test compiles on a host whose sibling package hoists that library and
fails on a clean `npm ci` (`tests/test_renderer_node_types.py`). Its `?raw` substitute
answers an empty string for a stylesheet, and a scan over an empty string passes for
every needle — so the CSS half is read here, where a file is just a file.

What is pinned:

* the body has no width ceiling — the old page fixed it at 720px and centred it, which
  left a fifth of a 1920px window empty on each side;
* the classes the deleted left column and right panel were drawn with are gone from the
  stylesheets as well as from the components — a rule is not unmounted when its element
  is, and a leftover class is the old page still shipping.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER = REPO_ROOT / "emrg" / "gui" / "renderer" / "src"

#: The entry the bundle starts from, and the only file that knows which sheets ship.
ENTRY = RENDERER / "main.tsx"

#: A side-effect stylesheet import, in either quote spelling.
#:
#: TypeScript makes the trailing semicolon optional and accepts single or double quotes,
#: so a matcher pinned to one of those spellings does not report a smaller number — it
#: drops a whole stylesheet from every assertion below, and reports success while doing
#: it. Both are read here, and anything else is named by `_unreadable_css_imports`.
IMPORT_CSS = re.compile(r"""^\s*import\s+(?:"([^"]+\.css)"|'([^']+\.css)');?\s*$""", re.M)


def _css_imports(text: str) -> list[str]:
    """Every stylesheet path a side-effect import names, in either quote spelling."""
    return [double or single for double, single in IMPORT_CSS.findall(text)]


def _unreadable_css_imports(text: str) -> list[str]:
    """Entry lines that load a stylesheet in a shape the matcher does not read.

    The set built below is meant to be **every** stylesheet the bundle loads, so a line
    that loads one and is absent from the set is not a shorter measurement — it is a
    sheet that escaped. Naming it is what turns "six or more sheets" into the contract:
    a guard that silently reads a subset of its subject passes by not looking.
    """
    missed: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("import") or ".css" not in stripped:
            continue
        if not IMPORT_CSS.match(line):
            missed.append(stripped)
    return missed


def _read_sheets() -> list[tuple[str, str]]:
    """Every stylesheet the bundle loads, found by reading the entry's own imports.

    A list written here would have been a second reading of the same fact, and it had
    already gone stale: the first version named only the two sheets the new shell owns
    and left `css/layout.css` — the vanilla page's sheet, still imported, still shipping
    its rules for the deleted results panel — outside every assertion. The entry is the
    subject; the sheets are whatever it imports.
    """
    entry_text = ENTRY.read_text(encoding="utf-8")
    unreadable = _unreadable_css_imports(entry_text)
    assert unreadable == [], (
        f"{ENTRY.name} loads a stylesheet in a shape this guard cannot read, so it would "
        "fall outside every assertion below:\n  " + "\n  ".join(unreadable)
    )
    imports = _css_imports(entry_text)
    # 入口一个 CSS 都没 import 会把这个模块变成一次空比较
    assert len(imports) >= 6, f"{ENTRY.name} imports {len(imports)} stylesheet(s)"
    sheets: list[tuple[str, str]] = []
    for rel in imports:
        path = (ENTRY.parent / rel).resolve()
        assert path.is_file(), f"{rel} is imported by {ENTRY.name} but does not exist"
        sheets.append((rel, path.read_text(encoding="utf-8")))
    # 一个空的样式表会让下面每条断言都通过：先把主体读出来
    for name, text in sheets:
        assert len(text) > 500, f"{name} read as {len(text)} characters"
    return sheets


def _rule_blocks(text: str) -> list[str]:
    """Every `selector { ... }` block, as text, from one stylesheet."""
    return [block for block in text.split("}") if "{" in block]


def test_the_body_has_no_width_ceiling() -> None:
    """`.react-shell-main` may take the whole window; no `max-width` may cap it."""
    bodies: list[str] = []
    for name, text in _read_sheets():
        for block in _rule_blocks(text):
            selector = block.split("{", 1)[0]
            if not re.search(r"\.react-shell-main\b", selector):
                continue
            bodies.append(f"{name}: {selector.strip()}")
            assert "max-width" not in block, (
                f"{name}: the body is capped again — {selector.strip()}\n{block}"
            )
    # 一条正文规则都没读到，说明这次检查什么都没看
    assert bodies, "no .react-shell-main rule found in either stylesheet"


def test_the_old_pages_classes_are_gone() -> None:
    """The grouping and the results panel are deleted, stylesheets included."""
    gone = (
        # 旧的左栏两级分组
        "conv-group-label",
        "open-sessions-label",
        "sidebar-head",
        "react-sidebar",
        # 旧的右栏结果面板与它自己的拖拽面
        "result-panel",
        "result-resizer",
        "result-tabbar",
        "result-filetab",
        "artifact-row",
    )
    hits: list[str] = []
    for name, text in _read_sheets():
        for needle in gone:
            if needle in text:
                hits.append(f"{name}: {needle}")
    assert hits == [], "the old page's classes are still styled:\n  " + "\n  ".join(hits)


def test_a_stylesheet_import_is_read_in_either_quote_spelling() -> None:
    """TypeScript's semicolon is optional and either quote will do: one import, one read.

    The single-quoted and unterminated forms are the ones the previous matcher could not
    see, and a sheet it could not see was excluded from both assertions above while the
    guard still reported success.
    """
    text = 'import "../css/tokens.css";\nimport \'../css/base.css\';\nimport "./shell.css"\n'
    assert _css_imports(text) == ["../css/tokens.css", "../css/base.css", "./shell.css"]
    assert _unreadable_css_imports(text) == []


def test_a_stylesheet_the_matcher_cannot_read_is_named_not_dropped() -> None:
    """A sheet outside the set has escaped it, so the guard names the line.

    A dynamic import is a stylesheet the bundle really loads, and it is deliberately the
    example here: the point is not that this spelling is common, it is that a spelling
    outside the matcher must fail loudly rather than leave a smaller set looking green.
    """
    text = 'import "../css/tokens.css";\nimport("../css/late.css");\n'
    assert _css_imports(text) == ["../css/tokens.css"]
    assert _unreadable_css_imports(text) == ['import("../css/late.css");']


def test_the_live_entry_leaves_no_stylesheet_unread() -> None:
    """The entry in this tree: every line that loads a sheet is read by the matcher."""
    text = ENTRY.read_text(encoding="utf-8")
    assert _unreadable_css_imports(text) == []
    assert len(_css_imports(text)) >= 6


def test_read_sheets_refuses_an_entry_it_cannot_read(tmp_path, monkeypatch) -> None:
    """The wiring, not only the helper: `_read_sheets` is what both assertions call.

    Naming an unreadable line in a helper that nobody consults would be a correction
    with no effect, so this drives the function the two property tests actually use.
    """
    entry = tmp_path / "main.tsx"
    entry.write_text('import "../css/a.css";\nimport("../css/late.css");\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ENTRY", entry)

    with pytest.raises(AssertionError) as refused:
        _read_sheets()
    assert "cannot read" in str(refused.value)
    assert "../css/late.css" in str(refused.value)
