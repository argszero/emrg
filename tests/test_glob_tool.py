"""Tests for the glob tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.glob_tool import GlobTool


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("")
        (root / "src" / "utils.py").write_text("")
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text("")
        (root / "__init__.py").write_text("")
        (root / "README.md").write_text("")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "compiled.pyc").write_text("")
        (root / ".hidden.py").write_text("")
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "package.json").write_text("")
        (root / "node_modules" / "pkg" / "index.js").write_text("")
        yield root


def _run(coro):
    return asyncio.run(coro)


def test_glob_all_python(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    assert not result.error
    assert "main.py" in result.content
    assert "utils.py" in result.content
    assert "test_main.py" in result.content
    assert "__init__.py" in result.content
    assert ".hidden.py" not in result.content
    assert "compiled.pyc" not in result.content


def test_glob_markdown(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.md", "workdir": str(temp_cwd)}))
    assert "README.md" in result.content
    assert "main.py" not in result.content


def test_glob_test_files(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/test*", "workdir": str(temp_cwd)}))
    assert "test_main.py" in result.content


def test_glob_no_match(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.rs", "workdir": str(temp_cwd)}))
    assert "No files matched" in result.content


def test_glob_no_match_says_nothing_about_skipping_when_nothing_was_skipped(temp_cwd):
    """The control for the two legs below: the skip clause is a measurement, not boilerplate.

    `*.rs` matches no path at all, so `skipped` is 0 and there is nothing to declare. A
    clause printed unconditionally would pass both legs below while telling a reader
    about a skip that did not happen.
    """
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.rs", "workdir": str(temp_cwd)}))
    assert "skipped" not in result.content


def test_a_pattern_whose_every_match_was_skipped_does_not_deny_them(temp_cwd):
    """The defect: `No files matched` for a pattern that matched 352 paths in this checkout.

    Measured 2026-10-06 (`cyc20261006-192020`) in `emrg/gui`:
    `node_modules/**/package.json` matched **352** paths, the skip policy dropped every
    one, and the tool answered `No files matched pattern ...` — a false statement about
    the tree, and the reading an agent answers "is this dependency installed?" with.
    """
    tool = GlobTool()
    result = _run(
        tool.execute({"pattern": "node_modules/**/package.json", "workdir": str(temp_cwd)})
    )
    assert not result.error
    assert "No files matched" in result.content
    assert "1 path(s) matched it but were skipped" in result.content


def test_the_count_in_a_hit_line_names_what_the_skip_dropped(temp_cwd):
    """"Found N matches" is the number left after the skip, and has to say so.

    The sibling `grep` names its subject in the same line ("searched N files"); without
    the same qualifier a reader cannot tell this count from the number the tree holds.
    """
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    assert "Found 4 matches for '**/*.py'" in result.content
    assert "1 path(s) also matched but were skipped" in result.content


def test_pointing_workdir_at_a_skipped_directory_reads_it(temp_cwd):
    """The remedy the refusal names is the measured one, not a plausible sentence.

    The skip compares each path relative to the root the caller passed, so a `workdir`
    inside the skipped directory finds the files — verified here because a remedy that
    does not work is worse than none.
    """
    tool = GlobTool()
    result = _run(
        tool.execute(
            {"pattern": "**/package.json", "workdir": str(temp_cwd / "node_modules")}
        )
    )
    assert "Found 1 matches" in result.content
    assert "package.json" in result.content


def test_the_definition_declares_the_skip_the_way_grep_does(temp_cwd):
    """The tool description is the contract the agent reads, and it did not mention the skip.

    `grep` states its skipping in its description ("automatic binary/hidden file
    skipping"); `glob` skipped the same way and said nothing, so "Found 5 matches" read
    as a fact about the tree.
    """
    tool = GlobTool()
    description = tool.definition().description
    assert "Skips hidden entries" in description
    assert "node_modules" in description
    assert "relative to it" in tool.definition().parameters["properties"]["workdir"]["description"]


def test_the_definition_names_the_one_hidden_entry_that_is_not_skipped():
    """The skip declaration is a claim, and as first written it was a universal one.

    "Skips hidden entries and the noise directories …" is refuted by `.emrg`, which is a
    hidden entry `_is_hidden_or_ignored` deliberately keeps — measured 2026-10-07
    (`cyc20261007-000240`, and independently by review on 2026-10-06): from the repo root
    `.emrg/memory/MEMORY.md` is read while `.git/config` and `.github/workflows/test.yml`
    are skipped. A caller who believes the universal sentence does not expect
    `**/MEMORY.md` to answer from `.emrg`, which is the same overclaim the rest of that
    change is about.
    """
    description = GlobTool().definition().description
    assert ".emrg" in description, (
        "the skip declaration has to carry its exception, or it claims to drop a "
        f"directory that is read: {description}"
    )


def test_the_emrg_directory_it_declares_as_read_really_is_read(temp_cwd):
    """The exception in the description, measured rather than asserted in prose.

    `_is_hidden_or_ignored` keeps any path whose parts include `.emrg` (and skips every
    other dot-part), so a search from the root does return the agent's own state. Pinned
    because the description promises it: a promise in a tool description that no test holds
    is how the universal claim above survived review.
    """
    tool = GlobTool()
    memory = temp_cwd / ".emrg" / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# index\n")

    result = _run(tool.execute({"pattern": "**/MEMORY.md", "workdir": str(temp_cwd)}))

    assert not result.error
    assert "Found 1 matches" in result.content, result.content
    # `.as_posix()`, because the tool now renders `path.relative_to(root).as_posix()`: a
    # report's path is `/`-separated on every platform, which is what makes it assertable
    # at all. This line used to be `str(Path(".emrg") / "memory" / "MEMORY.md")` — the
    # platform's own separator — which was portable *only* while the tool was not, and it
    # went red on `test-windows` the moment the tool became deterministic (run
    # 37990298947, 2026-10-10). The earlier history is in the note it replaces: run
    # 37495221347 (2026-10-06), where the POSIX literal failed the same leg.
    assert (Path(".emrg") / "memory" / "MEMORY.md").as_posix() in result.content
    assert "skipped" not in result.content, (
        "the .emrg path was dropped by the skip policy the description says reads it"
    )


def test_glob_no_pattern():
    tool = GlobTool()
    result = _run(tool.execute({"pattern": ""}))
    assert result.error
    assert "no pattern" in result.content.lower()


def test_definition():
    tool = GlobTool()
    d = tool.definition()
    assert d.name == "glob"
    assert "pattern" in d.parameters.get("properties", {})
    assert d.parameters.get("required") == ["pattern", "intent"]


def test_glob_invalid_workdir():
    """Non-existent workdir should return error."""
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": "/nonexistent"}))
    assert result.error
    assert "not found" in result.content.lower()


def test_a_brace_pattern_is_refused_rather_than_answered_with_no_files_matched(temp_cwd):
    """`**/*.{py,md}` is not a pattern this walk can read, and it did not say so.

    `Path.glob` has no brace expansion, so the whole pattern is one literal string that
    matches nothing — and the answer was `No files matched pattern '**/*.{py,md}'`, the
    same sentence a genuinely empty tree produces. Measured on this checkout 2026-10-10
    (`cyc20261010-220909`): `**/*.py` -> `Found N matches`, `**/*.{py,md}` -> `No files
    matched`, and the tree plainly holds both kinds of file.

    `glob`'s own description does not advertise braces, so this is not a broken promise —
    it is the other half: an answer that cannot be told apart from a true one. Refused
    with the reason instead, through the same rule `grep` fails to apply
    (`emrg/tools/base.py::brace_alternation`), so the two tools give one answer.
    """
    tool = GlobTool()

    brace = _run(tool.execute({"pattern": "**/*.{py,md}", "workdir": str(temp_cwd)}))
    assert brace.error, brace.content
    assert brace.content.startswith("Error:"), brace.content
    assert "brace" in brace.content, brace.content
    assert "does not expand" in brace.content, brace.content

    # The control: the two patterns the brace form meant, each still answered.
    for pattern, expected in (("**/*.py", "main.py"), ("**/*.md", "README.md")):
        control = _run(tool.execute({"pattern": pattern, "workdir": str(temp_cwd)}))
        assert not control.error, control.content
        assert expected in control.content, (
            f"{pattern} has to keep matching {expected}, or 'refused' is "
            f"indistinguishable from 'handled': {control.content}"
        )


def test_a_literal_brace_name_is_searched_not_refused(temp_cwd):
    """A brace is a literal character to this walk, so a name holding one is a real name.

    The refusal added for `**/*.{py,md}` first fired on `"{" in pattern or "}" in pattern`,
    and that refused patterns `Path.glob` reads correctly: on a tree holding a file named
    `a{b}.py`, master answered `Found 1 matches … a{b}.py` and the branch refused it, with a
    message asserting `{a,b}` alternation the input does not carry. Measured 2026-10-10
    (`cyc20261011-001130`) in this checkout. This repository named that class once already
    (`emrg/tools/command_scan.py`, the #1513 lesson: `echo sh "patch …"` was a bug, not a
    safe over-block).

    Alternation needs two alternatives, so the trigger is a comma inside the braces — and
    the tool additionally requires that the pattern selected nothing, which is what makes
    the refusal a reading rather than a prediction. Both halves are asserted here, because
    each alone is satisfied by a wrong rule: dropping the refusal entirely passes the first
    half, and the over-block passed the second.
    """
    tool = GlobTool()
    literal = temp_cwd / "a{b}.py"
    literal.write_text("")
    comma = temp_cwd / "a{b,c}.py"
    comma.write_text("")

    named = _run(tool.execute({"pattern": "a{b}.py", "workdir": str(temp_cwd)}))
    assert not named.error, (
        "`Path.glob` gives `{` no special meaning, so this pattern is the exact name of a "
        f"file that exists — refusing it blocks a pattern that works: {named.content}"
    )
    assert "a{b}.py" in named.content, named.content
    assert "Found 1 matches" in named.content, named.content

    # The literal name reached through a wildcard is the same reading.
    wildcard = _run(tool.execute({"pattern": "a{b}*", "workdir": str(temp_cwd)}))
    assert not wildcard.error, wildcard.content
    assert "a{b}.py" in wildcard.content, wildcard.content

    # The other half of the discriminator: a comma between the braces is alternation, it
    # selected nothing, and it is still refused rather than answered `No files matched`.
    alternation = _run(tool.execute({"pattern": "*.{py,rs}", "workdir": str(temp_cwd)}))
    assert alternation.error, (
        "`*.{py,rs}` is alternation this walk cannot expand, and it selected nothing — the "
        f"answer has to be the refusal, not an empty reading: {alternation.content}"
    )
    assert "brace" in alternation.content, alternation.content

    # And a comma-bearing pattern that **selects** the file whose name carries the comma is
    # answered: `Path.glob` reads that name literally, so this is the reading that pins the
    # "selected nothing" half of the rule — without it, gating on the pattern's shape alone
    # passes every call above.
    comma_named = _run(tool.execute({"pattern": "a{b,c}.py", "workdir": str(temp_cwd)}))
    assert not comma_named.error, (
        "a file is named `a{b,c}.py` and `Path.glob` matches that literal name, so the "
        f"pattern selects it and must be answered: {comma_named.content}"
    )
    assert "Found 1 matches" in comma_named.content, comma_named.content

    # And a literal brace name that is simply *absent* gets the empty answer, not the
    # refusal: `{b}` alternates between nothing, so refusing it asserts alternation the
    # pattern does not carry. This is the reading that tells the comma predicate apart from
    # "any brace" — with the wide predicate the gate alone still refuses this call whenever
    # the file is missing, and the file's presence above cannot separate the two.
    missing = _run(tool.execute({"pattern": "zz{b}.py", "workdir": str(temp_cwd)}))
    assert not missing.error, (
        "no file is named `zz{b}.py`, and that is the true answer — `{b}` alternates "
        f"between nothing, so this is not alternation: {missing.content}"
    )
    assert "No files matched" in missing.content, missing.content


def test_the_pattern_description_states_what_a_brace_pattern_gets(temp_cwd):
    """`glob`'s description now describes two behaviors, so both are read from it.

    A description is the behaviour's second home, and the pair drifts in both directions: a
    description that promises a refusal an unread pattern never gets, and a refusal whose
    description does not mention it, are one defect seen from two sides. Both clauses are
    asserted here — the refusal, and the literal name that keeps being searched — because
    each alone is satisfied by a description that is wrong about the other.
    """
    param = GlobTool().definition().parameters["properties"]["pattern"]["description"]
    assert "refused with that reason" in param, (
        f"the description no longer says what an unreadable pattern gets: {param}"
    )
    assert "still searched" in param, (
        f"the description no longer says a literal brace name is a real name: {param}"
    )

    refused = _run(GlobTool().execute({"pattern": "*.{py,rs}", "workdir": str(temp_cwd)}))
    assert refused.error, refused.content
    assert refused.content.startswith("Error:"), refused.content
    assert not refused.content.startswith("No files matched"), refused.content
    # "with that reason" is a clause of the description, so the reason is read too: a
    # refusal that does not name the pattern would leave the caller with an error and no
    # remedy, which is the promise the rest of the sentence makes.
    assert "brace" in refused.content, refused.content

    literal = temp_cwd / "a{b}.py"
    literal.write_text("")
    answered = _run(GlobTool().execute({"pattern": "a{b}.py", "workdir": str(temp_cwd)}))
    assert not answered.error, (
        f"the description says this is a literal name and is searched: {answered.content}"
    )
    assert "a{b}.py" in answered.content, answered.content
def test_the_siblings_spelling_of_workdir_selects_the_same_tree(temp_cwd):
    """`grep` names this parameter `path`; `glob` reads that spelling too (issue #2071).

    Measured 2026-10-11 (`cyc20261011-015723`) on master `63ee3a54`: `glob
    path=<a two-file tempdir>` fell back to the cwd and answered `No files matched
    pattern '*.py' in <this checkout>` — the tree the caller named was never searched.

    The fixture's own file is asserted **before** the comparison, because two readings
    that both fell back to the cwd would compare equal as well; an equality that holds
    for the wrong reason is not evidence that the alias works.
    """
    tool = GlobTool()
    declared = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    sibling = _run(tool.execute({"pattern": "**/*.py", "path": str(temp_cwd)}))

    assert "test_main.py" in declared.content, declared.content
    assert not sibling.error
    assert sibling.content == declared.content


def test_the_workdir_description_names_the_spelling_the_reader_accepts():
    """A description is read as the domain its reader enforces, so it names the alias."""
    description = GlobTool().definition().parameters["properties"]["workdir"]["description"]
    assert "`path`" in description, description
