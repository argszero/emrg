"""A glob pattern is relative to the directory it is searched in.

pathlib's glob engine takes relative patterns: it parses the pattern against the
directory being searched and raises ``NotImplementedError("Non-relative patterns are
unsupported")`` when the pattern carries its own root. That is a documented constraint
of both tools — `glob`'s schema says "Glob pattern **relative to the project root**" —
but neither tool answered for it. Measured 2026-10-11 (`cyc20261011-064130`) on master
`12bb54db`, one fixture, in this checkout:

    glob  pattern='/abs/*.py'            -> NotImplementedError (raised out of the tool)
    grep  path=<dir>  glob='/abs/*.py'   -> NotImplementedError (raised out of the tool)
    grep  path=<file> glob='/abs/*.py'   -> answered: "… offers only its own name to the
                                            filter, and '/abs/*.py' names a directory …
                                            pass the containing directory with this
                                            pattern"

The third line is the one that makes this urgent rather than tidy: the named-file
branch (added by #2064) answers with a **remedy that raises**. `_named_file_exclusion_note`
tells the caller to pass the containing directory instead — and that is exactly the walk
pathlib refuses, so following the advice produces
``Tool execution error: Non-relative patterns are unsupported``: the exception's own
string, from the daemon's generic handler, naming neither the tool nor what to write.

`glob` has a branch written for exactly this class — ``Error: invalid pattern`` — but it
catches ``(OSError, ValueError)``, and the shape those cover is ``'.'``
(``ValueError: Unacceptable pattern``). The case that branch is named for is the one that
escapes it; `grep`'s collector has no guard at all, and since #2064 the filter has two
branches that now answer the same input differently.

These tests pin three things: the rule itself, the rule against pathlib's real behaviour
(so the predicate cannot drift from the engine it describes), and both tools' promise to
answer rather than raise — on both of grep's branches, because the second one is where
the wrong remedy lived.
"""

import asyncio
import pathlib
import re

import pytest

from emrg.tools.base import relative_pattern_refusal
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool


def _run(coro):
    return asyncio.run(coro)


def _offered_spellings(content: str, refused: str) -> list[str]:
    """The spellings a refusal offers, minus the pattern it is refusing.

    Read out of the sentence rather than restated, so the test cannot pass while the
    message and the advice have drifted apart — the failure mode that produced the
    defect: a sentence offering the caller a walk the tool then refuses.
    """
    return [q for q in re.findall(r"'([^']+)'", content) if q != refused]


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a.py").write_text("MARK\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("MARK\n", encoding="utf-8")
    (tmp_path / "sub" / "c.md").write_text("MARK\n", encoding="utf-8")
    return tmp_path


#: Spellings a caller may hand either tool. The point of the sweep is that it is not
#: chosen to make one predicate look right — it holds both halves of every boundary
#: (a pattern with a root, one with a drive, and the relative spellings the schemas
#: document), so an assertion that only ever sees one side cannot pass it.
PATTERNS = (
    "/etc/passwd",              # absolute file
    "/tmp/*.py",                # absolute glob
    "//server/share/*.md",      # absolute, doubled root
    "C:/proj/*.py",             # drive: refused on Windows, a literal name on POSIX
    "*.py",                     # the documented spellings
    "**/*.py",
    "sub/*.md",
    "emrg/tools/*.py",
    "../sibling/*.py",
    "sub/../a.py",
    "..",
    ".",
    "",
)


class TestTheRule:
    """`relative_pattern_refusal` is a refusal or an absence, never a third thing."""

    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_it_refuses_or_stays_out_of_the_way(self, pattern):
        """An answer must carry the pattern and the remedy, never just "unsupported"."""
        refusal = relative_pattern_refusal("glob", pattern)
        if refusal is None:
            return
        assert refusal.error is True
        assert refusal.name == "glob"
        assert pattern in refusal.content, "the refused pattern must be named"
        assert "relative" in refusal.content, "the reason must be named"
        # The remedy, not the diagnosis: a caller that is told "unsupported" and
        # nothing else has to guess. One of these spellings must be offered.
        assert "**/*.py" in refusal.content or "*.py" in refusal.content

    def test_the_answer_is_the_tools_own_words(self):
        """It must not be an exception's string passed through."""
        content = relative_pattern_refusal("glob", "/etc/*.conf").content
        assert "Non-relative patterns are unsupported" not in content
        assert "Traceback" not in content

    def test_each_tool_names_itself(self):
        assert "grep" in relative_pattern_refusal("grep", "/etc/*.conf").content


class TestTheRuleMatchesTheEngine:
    """The predicate against pathlib — the leg that makes drift visible.

    `relative_pattern_refusal` decides with the predicate pathlib documents
    (`Path._parse_path` then `if drv or root`). If a future pathlib refuses a pattern
    this allows — or accepts one it refuses — the two disagree and this fails, instead
    of the tools quietly raising on an input nobody re-measured.
    """

    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_refusal_is_exactly_what_pathlib_rejects(self, tmp_path, pattern):
        try:
            list(tmp_path.glob(pattern))
        except NotImplementedError:
            pathlib_raises = True
        except ValueError:
            # `'.'` and `''` — the shape `glob`'s own branch does catch. Not this rule.
            pathlib_raises = False
        else:
            pathlib_raises = False

        assert (relative_pattern_refusal("glob", pattern) is not None) is pathlib_raises, (
            f"the rule and pathlib disagree about {pattern!r}"
        )


class TestGlobAnswers:
    """The promise: an absolute pattern is answered, not raised."""

    def test_an_absolute_pattern_is_answered(self, tree):
        result = _run(GlobTool().execute({
            "pattern": str(tree / "*.py"), "workdir": str(tree), "intent": "probe",
        }))
        assert result.error is True
        assert str(tree) in result.content, "the refused pattern must be named"
        assert "relative" in result.content

    def test_an_absolute_file_path_is_answered(self, tree):
        result = _run(GlobTool().execute({
            "pattern": str(tree / "a.py"), "workdir": str(tree), "intent": "probe",
        }))
        assert result.error is True

    def test_the_relative_spelling_still_finds_the_files(self, tree):
        """No regression: the documented spelling keeps the reading it always had."""
        out = _run(GlobTool().execute({
            "pattern": "**/*.py", "workdir": str(tree), "intent": "probe",
        })).content
        assert "a.py" in out and "sub/b.py" in out
        assert not out.startswith("Error")

    def test_the_branch_that_caught_dot_still_catches_dot(self, tree):
        """`'.'` raised ValueError, which the existing branch already handled.

        Pinned beside the new one so widening this tool's answers later cannot
        quietly drop the shape that already worked.
        """
        result = _run(GlobTool().execute({
            "pattern": ".", "workdir": str(tree), "intent": "probe",
        }))
        assert result.error is True
        assert "invalid pattern" in result.content

    def test_the_spelling_it_offers_is_not_itself_refused(self, tree):
        """The remedy has to be a call that works, not the one that raised.

        This is the whole defect one level up, and it is what the named-file branch got
        wrong: its remedy was "pass the containing directory", which is the walk pathlib
        refuses. A refusal whose advice the same tool then refuses sends the caller in a
        circle, so the advice is run rather than read.
        """
        refusal = _run(GlobTool().execute({
            "pattern": str(tree / "*.py"), "workdir": str(tree), "intent": "probe",
        }))
        offered = _offered_spellings(refusal.content, str(tree / "*.py"))
        assert offered, f"the refusal offers no spelling to write instead: {refusal.content}"
        for spelling in offered:
            answered = _run(GlobTool().execute({
                "pattern": spelling, "workdir": str(tree), "intent": "probe",
            }))
            assert not answered.content.startswith("Error"), (
                f"the refusal tells the caller to write {spelling!r}, and this tool "
                f"refuses that too: {answered.content}"
            )


class TestGrepAnswers:
    """The same promise for the other home — its collector had no guard at all."""

    def test_an_absolute_glob_filter_is_answered(self, tree):
        result = _run(GrepTool().execute({
            "pattern": "MARK", "path": str(tree), "glob": str(tree / "*.py"),
            "intent": "probe",
        }))
        assert result.error is True
        assert "relative" in result.content

    def test_the_named_file_branch_answers_the_same_way(self, tree):
        """Both branches of one filter, one answer — the leg the wrong remedy hid in.

        Before this, the directory branch raised out of the tool while the named-file
        branch answered "pass the containing directory with this pattern", a remedy that
        raises. The filter is one parameter, so the two branches give one answer to it.
        """
        named = tree / "a.py"
        result = _run(GrepTool().execute({
            "pattern": "MARK", "path": str(named), "glob": str(named),
            "intent": "probe",
        }))
        assert result.error is True
        assert "relative" in result.content, result.content
        assert "offers only its own name" not in result.content, (
            "the named-file branch answered with its exclusion note, whose remedy is "
            f"the walk pathlib refuses: {result.content}"
        )

    def test_a_relative_glob_filter_still_searches(self, tree):
        out = _run(GrepTool().execute({
            "pattern": "MARK", "path": str(tree), "glob": "**/*.py", "intent": "probe",
        })).content
        assert "a.py" in out and "sub/b.py" in out
        assert "c.md" not in out, f"the filter stopped filtering: {out!r}"

    def test_the_spelling_it_offers_is_not_itself_refused(self, tree):
        """Same leg as `glob`'s: the advice is run, not read."""
        refusal = _run(GrepTool().execute({
            "pattern": "MARK", "path": str(tree), "glob": str(tree / "*.py"),
            "intent": "probe",
        }))
        offered = _offered_spellings(refusal.content, str(tree / "*.py"))
        assert offered, f"the refusal offers no spelling to write instead: {refusal.content}"
        for spelling in offered:
            answered = _run(GrepTool().execute({
                "pattern": "MARK", "path": str(tree), "glob": spelling, "intent": "probe",
            }))
            assert not answered.content.startswith("Error"), (
                f"the refusal tells the caller to write {spelling!r}, and this tool "
                f"refuses that too: {answered.content}"
            )

    def test_an_absent_glob_filter_is_untouched(self, tree):
        """`glob=None` is the common call and must not acquire a refusal."""
        out = _run(GrepTool().execute({
            "pattern": "MARK", "path": str(tree), "intent": "probe",
        })).content
        assert "a.py" in out and "sub/c.md" in out
        assert not out.startswith("Error")


class TestOneHome:
    """Both tools ask the same function, and neither re-implements the rule."""

    def test_the_rule_is_not_copied_into_the_tools(self):
        for path in ("emrg/tools/glob_tool.py", "emrg/tools/grep_tool.py"):
            source = pathlib.Path(path).read_text(encoding="utf-8")
            assert "relative_pattern_refusal" in source, path
            assert "is_absolute()" not in source, f"{path} re-implements the rule"
            assert ".drive" not in source, f"{path} re-implements the rule"

    def test_base_holds_the_only_definition(self):
        base = pathlib.Path("emrg/tools/base.py").read_text(encoding="utf-8")
        assert base.count("def relative_pattern_refusal(") == 1
