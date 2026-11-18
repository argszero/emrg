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
  is, and a leftover class is the old page still shipping;
* the sheets the entry imports **are** the sheets the renderer carries — every one of
  them, in both directions, because a set that is only floored lets a stylesheet leave
  it without a word.
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


def _css_naming_lines(text: str) -> list[str]:
    """Entry lines that name a stylesheet in an `import`, in any spelling at all.

    Deliberately looser than `IMPORT_CSS`: this is the **independent** count the matcher is
    compared against, so it must not repeat the matcher's own assumptions. A line counts if
    it starts an `import` and mentions a `.css`.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("import") and ".css" in line
    ]


def _shipped_sheets() -> set[Path]:
    """Every stylesheet the renderer carries, as resolved paths; build output excluded.

    The subject of this guard is the renderer's stylesheets, not the entry's import list.
    The import list is how the bundle reaches them; a sheet that leaves it still ships in
    the tree, and only a comparison against what is on disk can name that. `dist/` and
    `node_modules/` are excluded because they are produced rather than written.
    """
    root = RENDERER.parent
    shipped: set[Path] = set()
    for path in root.rglob("*.css"):
        # The skip is decided on the path **relative to the root it was walked from**: a walk
        # rooted at an absolute path yields absolute paths, so the absolute path's parts would
        # make the machine's own ancestor directory names decide, and a checkout under a
        # directory called `node_modules` would silence the whole sweep. Same rule, and the
        # same reason, as `tests/test_walk_skips_read_their_root.py`.
        relative = path.relative_to(root)
        if "node_modules" in relative.parts or "dist" in relative.parts:
            continue
        shipped.add(path.resolve())
    return shipped


def _read_sheets() -> list[tuple[str, str]]:
    """Every stylesheet the bundle loads, found by reading the entry's own imports.

    A list written here would have been a second reading of the same fact, and it had
    already gone stale: the first version named only the two sheets the new shell owns
    and left `css/layout.css` — the vanilla page's sheet, still imported, still shipping
    its rules for the deleted results panel — outside every assertion. The entry is the
    subject; the sheets are whatever it imports.

    The set is held to the stylesheets the renderer carries, in **both** directions, and
    not by a floor: with seven imports and a floor of six, one import could be deleted and
    a deleted class written into the now-unscanned sheet while every assertion here stayed
    green (measured 2026-11-18 on head `5b0f0be4`). A sheet the entry no longer imports is
    named, and so is an import the renderer does not carry.
    """
    entry_text = ENTRY.read_text(encoding="utf-8")
    unreadable = _unreadable_css_imports(entry_text)
    assert unreadable == [], (
        f"{ENTRY.name} loads a stylesheet in a shape this guard cannot read, so it would "
        "fall outside every assertion below:\n  " + "\n  ".join(unreadable)
    )
    imports = _css_imports(entry_text)
    # 入口一个 CSS 都没 import 会把这个模块变成一次空比较
    # One fact, two readings: the matcher scans the whole text, the refusal checks line by
    # line, and a matcher that loses `re.M` still matches every single line while `findall`
    # over the whole entry anchors once and comes up short. The counts are compared here
    # because the refusal alone cannot see that shape.
    naming = _css_naming_lines(entry_text)
    assert len(imports) == len(naming), (
        f"{ENTRY.name} names a stylesheet on {len(naming)} line(s) but the matcher read "
        f"{len(imports)}, so at least one sheet is outside the set:\n  " + "\n  ".join(naming)
    )
    assert imports, f"{ENTRY.name} imports no stylesheet at all"
    paths = [(rel, (ENTRY.parent / rel).resolve()) for rel in imports]
    for rel, path in paths:
        assert path.is_file(), f"{rel} is imported by {ENTRY.name} but does not exist"
    imported = {path for _, path in paths}
    shipped = _shipped_sheets()
    unimported = sorted(shipped - imported)
    assert unimported == [], (
        "the renderer carries a stylesheet that nothing imports, so no assertion below "
        "would scan it:\n  " + "\n  ".join(str(p) for p in unimported)
    )
    foreign = sorted(imported - shipped)
    assert foreign == [], (
        f"{ENTRY.name} imports a stylesheet the renderer does not carry:\n  "
        + "\n  ".join(str(p) for p in foreign)
    )
    sheets: list[tuple[str, str]] = [
        (rel, path.read_text(encoding="utf-8")) for rel, path in paths
    ]
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


def test_a_stylesheet_the_entry_no_longer_imports_is_named(tmp_path, monkeypatch) -> None:
    """Membership, not a floor: a sheet still on disk but no longer imported is an escape.

    Measured on head `5b0f0be4`: deleting the `layout.css` import left six imports against a
    floor of six, and writing `artifact-row` into that sheet left all six tests green. The
    floor could not see the drop; this comparison names it.
    """
    module = sys.modules[__name__]
    renderer = tmp_path / "renderer"
    (renderer / "css").mkdir(parents=True, exist_ok=True)
    (renderer / "src").mkdir(exist_ok=True)
    for name in ("a.css", "b.css"):
        (renderer / "css" / name).write_text("x" * 600, encoding="utf-8")
    entry = renderer / "src" / "main.tsx"
    entry.write_text('import "../css/a.css";\n', encoding="utf-8")
    monkeypatch.setattr(module, "RENDERER", renderer / "src")
    monkeypatch.setattr(module, "ENTRY", entry)

    with pytest.raises(AssertionError) as refused:
        _read_sheets()
    assert "nothing imports" in str(refused.value)
    assert "b.css" in str(refused.value)


def test_a_matcher_that_reads_fewer_lines_than_name_a_sheet_is_caught(
    tmp_path, monkeypatch
) -> None:
    """The two readings are compared, so a matcher that under-reads cannot pass unnoticed.

    `_unreadable_css_imports` matches line by line, while the set comes from `findall` over
    the whole text. Drop `re.M` from the matcher and the two separate: `^` still matches at
    the start of each individual line, so the refusal stays empty, while `findall` over the
    whole entry anchors once and comes up short. Only the count comparison notices.
    """
    module = sys.modules[__name__]
    renderer = tmp_path / "renderer"
    (renderer / "css").mkdir(parents=True, exist_ok=True)
    (renderer / "src").mkdir(exist_ok=True)
    for name in ("a.css", "b.css"):
        (renderer / "css" / name).write_text("x" * 600, encoding="utf-8")
    entry = renderer / "src" / "main.tsx"
    entry.write_text('import "../css/a.css";\nimport "../css/b.css";\n', encoding="utf-8")
    monkeypatch.setattr(module, "RENDERER", renderer / "src")
    monkeypatch.setattr(module, "ENTRY", entry)
    # The same matcher without re.M: right per line, short over the whole entry.
    monkeypatch.setattr(
        module,
        "IMPORT_CSS",
        re.compile(r"""^\s*import\s+(?:"([^"]+\.css)"|'([^']+\.css)');?\s*$"""),
    )
    text = entry.read_text(encoding="utf-8")
    assert _unreadable_css_imports(text) == [], "per line, both imports still read"
    assert len(_css_imports(text)) < len(_css_naming_lines(text)), (
        "over the whole text the matcher must fall short of the lines that name a sheet, "
        f"else there is nothing here to notice: {len(_css_imports(text))} vs "
        f"{len(_css_naming_lines(text))}"
    )

    with pytest.raises(AssertionError) as refused:
        _read_sheets()
    assert "the matcher read" in str(refused.value)
    assert "b.css" in str(refused.value)


def test_the_renderer_carries_no_stylesheet_the_entry_does_not_import() -> None:
    """The live tree: the imported set and the sheets on disk are the same set, both ways."""
    imported = {
        (ENTRY.parent / rel).resolve() for rel in _css_imports(ENTRY.read_text(encoding="utf-8"))
    }
    shipped = _shipped_sheets()
    assert shipped - imported == set(), sorted(str(p) for p in shipped - imported)
    assert imported - shipped == set(), sorted(str(p) for p in imported - shipped)
    assert len(shipped) == 7, sorted(p.name for p in shipped)
