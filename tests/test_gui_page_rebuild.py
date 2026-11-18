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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER = REPO_ROOT / "emrg" / "gui" / "renderer" / "src"

#: The entry the bundle starts from, and the only file that knows which sheets ship.
ENTRY = RENDERER / "main.tsx"
IMPORT_CSS = re.compile(r'^import\s+"([^"]+\.css)";', re.M)


def _read_sheets() -> list[tuple[str, str]]:
    """Every stylesheet the bundle loads, found by reading the entry's own imports.

    A list written here would have been a second reading of the same fact, and it had
    already gone stale: the first version named only the two sheets the new shell owns
    and left `css/layout.css` — the vanilla page's sheet, still imported, still shipping
    its rules for the deleted results panel — outside every assertion. The entry is the
    subject; the sheets are whatever it imports.
    """
    imports = IMPORT_CSS.findall(ENTRY.read_text(encoding="utf-8"))
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
