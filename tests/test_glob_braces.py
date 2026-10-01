"""The `{a,b}` alternation both tools' schemas offer as an example.

`grep`'s description ends with ``Examples: '*.py', '*.{py,rs}', 'src/**/*.ts'``, and
pathlib's glob engine has no brace alternation: it looks for a file literally named
``something.{py,rs}``. Measured on master `256400f4`, 2026-10-02, on a tree holding
`a.py`, `b.rs`, `src/c.py`, `src/nested/d.ts`:

    glob('*.{py,rs}')        -> No files matched pattern '*.{py,rs}'
    grep(glob='*.{py,rs}')   -> No matches for 'MARK' ...
    glob('*.py') / '*.rs'    -> one match each

So the documented example answered **empty**, in the shape of a real answer: a caller
following the tool's own description was told there were none, never that the pattern
could not work. These tests pin the expansion itself, and then the promise to the
caller: the example in the schema has to find the files the schema says it finds.
"""

import asyncio
import re

import pytest

from emrg.tools.base import expand_braces
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mixed_tree(tmp_path):
    """Two languages at the top, one nested, one TypeScript, one other."""
    for rel in ("a.py", "b.rs", "src/c.py", "src/nested/d.ts", "docs/e.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("MARK\n", encoding="utf-8")
    return tmp_path


def _glob_names(root, pattern):
    out = _run(GlobTool().execute({
        "pattern": pattern, "workdir": str(root), "intent": "probe"})).content
    return {ln.strip().rstrip("/") for ln in out.split("\n")[1:]
            if ln.strip() and not ln.startswith("...")}


def _grep_names(root, pattern):
    out = _run(GrepTool().execute({
        "pattern": "MARK", "path": str(root), "glob": pattern,
        "intent": "probe"})).content
    return set(re.findall(r"^([^\s:][^:]*):\d+:$", out, re.M))


class TestTheAlternationIsExpanded:
    """The unit: what `expand_braces` says a pattern stands for."""

    def test_the_schemas_own_example(self):
        assert expand_braces("*.{py,rs}") == ["*.py", "*.rs"]

    @pytest.mark.parametrize("pattern,expected", [
        ("*.py", ["*.py"]),
        ("src/**/*.{ts,tsx}", ["src/**/*.ts", "src/**/*.tsx"]),
        ("{a,b}{c,d}", ["ac", "ad", "bc", "bd"]),
        ("{a,{b,c}}", ["a", "b", "c"]),
        ("x{a,}", ["xa", "x"]),
    ])
    def test_the_expansion(self, pattern, expected):
        assert expand_braces(pattern) == expected

    @pytest.mark.parametrize("pattern", ["foo{bar", "{}", "a}b", "", "*"])
    def test_a_pattern_without_a_usable_group_is_left_alone(self, pattern):
        """An unmatched `{` is a literal, exactly as the shell treats it."""
        assert expand_braces(pattern) == [pattern]

    def test_no_pattern_is_expanded_twice(self):
        """`{a,a}` names one pattern, not two copies of it."""
        assert expand_braces("{a,a}") == ["a"]


class TestTheDocumentedExampleWorks:
    """The promise: the example in the schema finds what the schema says it does."""

    def test_the_example_is_still_the_schema_s_own(self):
        """If the description is reworded, this pins the promise to the behaviour."""
        description = GrepTool().definition().parameters["properties"]["glob"]["description"]
        assert "*.{py,rs}" in description

    def test_the_alternation_is_the_union_of_its_alternatives(self, mixed_tree):
        """The strongest reading: same files, not merely 'not empty'."""
        for name, call in (("glob", _glob_names), ("grep", _grep_names)):
            both = call(mixed_tree, "*.{py,rs}")
            py = call(mixed_tree, "*.py")
            rs = call(mixed_tree, "*.rs")
            assert both == py | rs, (name, both, py, rs)
            assert both, f"{name}: the documented example found nothing"

    def test_a_nested_alternation_reaches_the_nested_file(self, mixed_tree):
        for call in (_glob_names, _grep_names):
            assert call(mixed_tree, "src/**/*.{ts,tsx}") == {"src/nested/d.ts"}

    def test_a_file_matching_two_alternatives_is_named_once(self, mixed_tree):
        for call in (_glob_names, _grep_names):
            assert call(mixed_tree, "*.{py,py}") == call(mixed_tree, "*.py")

    def test_a_pattern_without_braces_is_unchanged(self, mixed_tree):
        """No regression: the ordinary spelling keeps the reading it always had."""
        for call in (_glob_names, _grep_names):
            assert call(mixed_tree, "*.py")
            assert call(mixed_tree, "docs/*.md") == {"docs/e.md"}
