"""A path the caller names is not noise — the rule `glob` and `grep` walk by.

Measured 2026-10-02 on master `bc114ab9`, in this repository, where every path
below exists:

    glob('.gitignore')               -> No files matched pattern '.gitignore'
    glob('**/.gitignore')            -> No files matched pattern '**/.gitignore'
    glob('.github/workflows/*.yml')  -> No files matched pattern '...'
    read('.gitignore')               ->      1\t*.pyc          <- the same file

Both discovery tools keyed their skip on the path alone, so a glob that *named*
a hidden path was answered "there is none" — while `read` and `grep(path=…)`
returned it. Two readers of one fact, disagreeing, with the empty answer wearing
the shape of a real one.

The rule is now the shell's own, stated once in `emrg/tools/ignored_paths.py`: a
leading dot in a *pattern* is a request, a leading dot in a *name* is noise until
something asks for it. These tests pin the rule, then the promise to each tool,
and then the property that made the defect possible — that both tools read the
same rule rather than keeping a copy each.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool
from emrg.tools.ignored_paths import is_ignored
from emrg.tools.read_tool import ReadTool


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tree(tmp_path):
    """A tree where every hidden path the tests name really exists."""
    for rel, text in (
        (".gitignore", "*.pyc\nMARK\n"),     # hidden file at the root
        (".env", "PORT=1\n"),
        (".hidden/c.py", "MARK\n"),
        (".github/workflows/ci.yml", "name: ci\nMARK\n"),
        (".emrg/memory/MEMORY.md", "MARK\n"),   # EMRG's own state: never noise
        (".git/config", "[core]\n"),
        ("node_modules/pkg/index.js", "MARK\n"),
        ("sub/b.py", "MARK\n"),
        ("a.py", "MARK\n"),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _glob(workdir, pattern):
    out = _run(GlobTool().execute({
        "pattern": pattern, "workdir": str(workdir), "intent": "probe"}))
    return {ln.strip().rstrip("/") for ln in out.content.split("\n")[1:]
            if ln.strip() and not ln.startswith("...")}


def _grep(path, glob=None):
    args = {"pattern": "MARK", "path": str(path), "intent": "probe"}
    if glob is not None:
        args["glob"] = glob
    out = _run(GrepTool().execute(args)).content
    import re
    return set(re.findall(r"^([^\s:][^:]*):\d+:$", out, re.M))


# ── the rule, read directly ────────────────────────────────────────


class TestTheRule:
    """`is_ignored` is where the decision lives, so it is read here rather than inferred."""

    @pytest.mark.parametrize("parts,pattern", [
        ((".gitignore",), ".gitignore"),
        ((".gitignore",), "**/.gitignore"),
        ((".gitignore",), ".*"),
        ((".github",), ".github/*"),
        ((".github", "workflows", "ci.yml"), ".github/**/*.yml"),
    ])
    def test_a_hidden_name_the_pattern_asks_for_is_not_noise(self, parts, pattern):
        assert not is_ignored(parts, pattern), (parts, pattern)

    @pytest.mark.parametrize("pattern", ["**/*.py", "*", "*.py", "**/*"])
    def test_a_wildcard_does_not_ask_for_hidden_names(self, pattern):
        assert is_ignored((".hidden", "c.py"), pattern), pattern

    def test_a_wildcard_that_starts_with_the_dot_does_ask(self):
        """The shell's rule: `.*rc` is a request, `*.rc` is not."""
        assert not is_ignored((".eslintrc",), ".*rc")
        assert is_ignored((".eslintrc",), "*.rc")

    def test_a_nested_hidden_component_needs_its_own_request(self):
        """`.a` is named so the walk enters it; `.b` is not, so the walk stops there."""
        assert not is_ignored((".a", "c.py"), ".a/*/c.py")
        assert is_ignored((".a", ".b", "c.py"), ".a/*/c.py")

    def test_emrgs_own_state_is_never_noise(self):
        """`**/*.md` lists `.emrg/memory/*.md` today and must keep doing so."""
        assert not is_ignored((".emrg", "memory", "MEMORY.md"), "**/*.md")

    @pytest.mark.parametrize("pattern", ["**/*", ".git/config", "node_modules/**", "*"])
    def test_the_build_and_repository_trees_are_never_walked(self, pattern):
        """Naming one does not open it: these can be enormous, and they are not content."""
        assert is_ignored((".git", "config"), pattern), pattern
        assert is_ignored(("node_modules", "pkg", "index.js"), pattern), pattern

    def test_an_ordinary_path_is_never_noise(self):
        assert not is_ignored(("sub", "b.py"), "**/*.py")


# ── the promise to each tool ───────────────────────────────────────


class TestTheToolsFindWhatYouName:
    def test_glob_finds_a_hidden_file_at_the_root(self, tree):
        assert _glob(tree, ".gitignore") == {".gitignore"}

    def test_glob_finds_a_hidden_file_under_a_wildcard_that_names_it(self, tree):
        assert _glob(tree, "**/.gitignore") == {".gitignore"}

    def test_glob_finds_a_hidden_directory_by_name(self, tree):
        assert _glob(tree, ".github/workflows/*.yml") == {".github/workflows/ci.yml"}

    def test_grep_finds_a_hidden_file_its_glob_names(self, tree):
        assert _grep(tree, glob="**/.gitignore") == {".gitignore"}

    def test_grep_finds_a_hidden_directory_by_name(self, tree):
        assert _grep(tree, glob=".github/**/*.yml") == {".github/workflows/ci.yml"}

    def test_the_file_read_can_show_is_one_glob_can_find(self, tree):
        """The defect stated as the property it broke: the two agree, or neither is wrong."""
        shown = _run(ReadTool().execute({
            "file_path": str(tree / ".gitignore"), "intent": "probe"}))
        assert not shown.error and "*.pyc" in shown.content
        assert _glob(tree, ".gitignore") == {".gitignore"}


# ── what must not change ───────────────────────────────────────────


class TestTheOrdinaryReadingIsUnchanged:
    def test_a_wildcard_still_skips_hidden_paths(self, tree):
        found = _glob(tree, "**/*.py")
        assert "sub/b.py" in found and "a.py" in found
        assert ".hidden/c.py" not in found

    def test_a_wildcard_still_skips_the_build_trees(self, tree):
        assert "node_modules/pkg/index.js" not in _glob(tree, "**/*.js")

    def test_grep_still_skips_hidden_paths_a_wildcard_would_reach(self, tree):
        found = _grep(tree, glob="**/*.py")
        assert "sub/b.py" in found
        assert ".hidden/c.py" not in found

    def test_emrgs_own_state_is_still_reachable(self, tree):
        assert ".emrg/memory/MEMORY.md" in _glob(tree, "**/*.md")


# ── the shape that let it happen ───────────────────────────────────


class TestTheRuleHasOneHome:
    def test_neither_tool_keeps_its_own_copy(self):
        """Two copies of one rule are free to drift — which is what this cycle closed.

        Read as source, because the copy is exactly what a later edit would
        reintroduce: `glob` had a `_is_hidden_or_ignored` method and `grep` an
        inline `skip_dirs` set, and the two had already diverged from what a
        caller can name.
        """
        assert not hasattr(GlobTool, "_is_hidden_or_ignored")
        source = inspect.getsource(GrepTool._collect_files)
        assert "skip_dirs" not in source
        assert "ignored_paths.is_ignored" in source

    def test_glob_reads_the_same_rule(self):
        source = inspect.getsource(GlobTool.execute)
        assert "ignored_paths.is_ignored" in source
