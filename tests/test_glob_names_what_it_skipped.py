"""`glob` names the entries its own filter dropped — measured on this repository.

The tool skips hidden names and four build-noise directories. Until now nothing in the
answer said so, which makes an empty answer indistinguishable from a true one. Measured
on master `84d1cca0`, 2026-10-02, in this checkout:

| pattern | on disk | the tool answered |
|---|---|---|
| `**/*.yml` | 2 | **`No files matched pattern '**/*.yml' in /Users/xiaokeai/.emrg/evolution/emrg`** |
| `**/*.md` | 81 | `Found 79 matches` |

The two `.yml` files are `build-release.yml` and `test.yml`, both under `.github/` — the
project's CI. An agent that acts on "No files matched" concludes there is no CI, and
neither the sentence nor the schema told it a filter had run. `**/*.md` is the quieter
half of the same defect: a count that is 79 where the tree holds 81, with nothing to say
which two are missing.

So the rule keeps its behaviour — the entries it always dropped, it still drops — and
gains a voice: both answer branches end with a line naming the count and the path parts
that hid the entries.

These live in their own file because two unlanded branches also rewrite this filter
(`a-named-hidden-path-is-found` moves it to `ignored_paths.py`, and
`the-documented-glob-examples-work` touches the same module).
"""

import asyncio
import re
from pathlib import Path

from emrg.tools.glob_tool import GlobTool


def _run(**kwargs):
    args = {"intent": "measure what the search skipped"}
    args.update(kwargs)
    return asyncio.run(GlobTool().execute(args))


def _listing(content: str) -> list[str]:
    """The lines the tool presents as matches (two-space indented)."""
    return [line.strip() for line in content.split("\n") if line.startswith("  ")]


NOTE = re.compile(
    r"\.\.\. \[(\d+) entr\w* that match\w* the pattern \w+ skipped as hidden or ignored: ([^\]]+)\]"
)


def _note(content: str) -> tuple[int, list[str]] | None:
    """The skip note as `(count, culprits)`, or `None` when there is no note."""
    match = NOTE.search(content)
    if not match:
        return None
    return int(match.group(1)), [c.strip() for c in match.group(2).split(",")]


class TestAnEmptyAnswerSaysWhy:
    """The sharpest case: every match was dropped, so the answer reads as a true zero."""

    def test_a_recursive_search_into_a_hidden_dir_is_not_a_silent_zero(self, tmp_path):
        """The repo's own shape: `**/*.yml` with the only matches under `.github/`."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "build-release.yml").write_text("")
        (tmp_path / ".github" / "workflows" / "test.yml").write_text("")

        result = _run(pattern="**/*.yml", workdir=str(tmp_path))

        assert result.content.startswith("No files matched pattern '**/*.yml'"), result.content
        note = _note(result.content)
        assert note is not None, "the answer dropped two matching files and said nothing"
        count, culprits = note
        assert count == 2, note
        assert culprits == [".github"], (
            f"the note names the files rather than the directory that hid them: {culprits}"
        )

    def test_the_culprit_is_the_hidden_part_not_the_whole_path(self, tmp_path):
        """A reader of a recursive search needs to know *which* subtree to search next."""
        (tmp_path / ".cache" / "deep" / "deeper").mkdir(parents=True)
        (tmp_path / ".cache" / "deep" / "deeper" / "x.json").write_text("")

        result = _run(pattern="**/*.json", workdir=str(tmp_path))

        assert _note(result.content) == (1, [".cache"]), result.content

    def test_a_hidden_file_is_named_by_its_own_name(self, tmp_path):
        (tmp_path / ".env").write_text("")
        (tmp_path / "visible.txt").write_text("")

        result = _run(pattern="*", workdir=str(tmp_path))

        assert _listing(result.content) == ["visible.txt"]
        assert _note(result.content) == (1, [".env"]), result.content

    def test_build_noise_is_named_by_its_directory(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "a.pyc").write_text("")
        (tmp_path / "__pycache__" / "b.pyc").write_text("")

        result = _run(pattern="**/*.pyc", workdir=str(tmp_path))

        assert _note(result.content) == (2, ["__pycache__"]), result.content


class TestTheFoundAnswerCarriesItToo:
    """The quieter defect: a count that is short of the tree, with nothing saying so."""

    def test_a_partial_result_says_what_is_missing(self, tmp_path):
        (tmp_path / "a.md").write_text("")
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "README.md").write_text("")

        result = _run(pattern="**/*.md", workdir=str(tmp_path))

        assert "Found 1 matches" in result.content, result.content
        assert _listing(result.content) == ["a.md"]
        assert _note(result.content) == (1, [".github"]), result.content

    def test_the_count_and_the_names_can_differ(self, tmp_path):
        """Three entries, one culprit: the count is the entries, the names are the causes."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "a.yml").write_text("")
        (tmp_path / ".github" / "workflows" / "b.yml").write_text("")
        (tmp_path / ".github" / "c.yml").write_text("")

        count, culprits = _note(_run(pattern="**/*.yml", workdir=str(tmp_path)).content)

        assert count == 3
        assert culprits == [".github"], "three entries, one cause — the note repeats it"

    def test_the_note_lists_every_culprit_and_then_counts_the_rest(self, tmp_path):
        for name in (".alpha", ".beta", ".gamma", ".delta", ".epsilon", ".zeta"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "x.md").write_text("")

        count, culprits = _note(_run(pattern="**/*.md", workdir=str(tmp_path)).content)

        assert count == 6
        assert len(culprits) == 4, culprits       # SKIP_NAMES_SHOWN
        assert ".alpha" in culprits
        assert any("more" in c for c in culprits), culprits


class TestTheNoteIsAbsentWhenNothingWasSkipped:
    """The control: a note on every answer is noise, and noise hides the real one."""

    def test_a_clean_search_carries_no_note(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("")

        result = _run(pattern="**/*.py", workdir=str(tmp_path))

        assert "skipped" not in result.content, result.content
        assert _listing(result.content) == ["src/main.py"]

    def test_an_empty_tree_carries_no_note(self, tmp_path):
        """Nothing matched and nothing was hidden: the zero is a true zero."""
        result = _run(pattern="**/*.rs", workdir=str(tmp_path))

        assert result.content.startswith("No files matched")
        assert "skipped" not in result.content, result.content

    def test_the_pycache_fixture_is_clean_here(self, tmp_path):
        (tmp_path / "keep.py").write_text("")

        result = _run(pattern="**/*.py", workdir=str(tmp_path))

        assert _note(result.content) is None, result.content


class TestTheExemptionAndTheFilterAreUnchanged:
    """The change is what the answer says, not which entries it returns."""

    def test_dot_emrg_is_still_searched(self, tmp_path):
        """This agent's own memory is the file an evolution cycle looks for."""
        (tmp_path / ".emrg" / "memory").mkdir(parents=True)
        (tmp_path / ".emrg" / "memory" / "MEMORY.md").write_text("")

        result = _run(pattern="**/*.md", workdir=str(tmp_path))

        assert _listing(result.content) == [".emrg/memory/MEMORY.md"], result.content
        assert _note(result.content) is None, "the exempt directory was reported as a skip"

    def test_the_returned_set_is_the_one_the_filter_always_left(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / ".hidden.py").write_text("")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "a.pyc").write_text("")
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "b.py").write_text("")

        result = _run(pattern="**/*.py", workdir=str(tmp_path))

        assert sorted(_listing(result.content)) == ["a.py", "pkg/b.py"]

    def test_the_cap_note_still_comes_first_and_still_counts(self, tmp_path):
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text("")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "x.py").write_text("")

        result = _run(pattern="**/*.py", workdir=str(tmp_path))

        assert "more matches not shown" not in result.content, "8 is under the cap"
        assert _note(result.content) == (1, [".hidden"]), result.content

    def test_the_schema_states_the_filter(self):
        """A model cannot honour a filter the tool never mentions."""
        text = GlobTool().definition().description

        assert "hidden" in text.lower(), text
        assert "__pycache__" in text, text
        assert len(text) < 900, f"the schema is not a place for an essay ({len(text)} chars)"
