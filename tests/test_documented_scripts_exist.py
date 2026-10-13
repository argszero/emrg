"""Every `scripts/<name>` a tracked document names must exist on disk.

Measured 2026-10-03 (`cyc20261003-112240`): this survey was done **by hand in three
consecutive cycles** (`cyc20261003-065523`, `-090905`, `-112240`) before it was mechanised
here. That is the "same trivial check repeated" shape the evolution prompt's Discovery step
names, and the same reason the project mechanises any rule it can: prose decays, and the
survey lived in three cycle records nobody re-reads.

**Why Markdown and not everything.** `.github/workflows/*.yml` names scripts too, but a
workflow naming a missing script fails on its next run - loudly, to the person who broke it,
with the run in front of them. A *document* naming a missing script fails only for the reader
who follows it, and only after they have typed the command; nothing is watching. The two
failure modes are not the same, so the guard covers the quiet one.

**A second class, added 2026-10-14** (`cyc20261014-051251`): a *guard* named in the **prose of a
tracked Python file**. It is the same quiet failure mode - a docstring saying "the rule is
mechanised by the guard in `scripts/<a guard>.py`" is read by whoever opens the file next, and
the name is a pointer they will follow - and this tree had a live instance: `tests/test_review_queue.py`
cited a guard that exists only on an unmerged branch, so `git log master -- <that path>` was empty.
The class is discovered (`git ls-files '*.py'`) with two restrictions, each measured rather than
guessed:

* **prose only** - docstrings and comments, never a string literal. Those same names appear
  inside literals 11 times over 12 files (`check-some-other-guard.py`, `check-elsewhere.py`, ...),
  every one of them a fixture fed to a tool, and a fixture's data is not a claim about this tree -
  the line `check-citation-resolves.py` draws for the same reason.
* **the `check-` prefix**, which is what this repository names its guards. Over the 360 tracked
  Python files, the wider prose class names 319 `scripts/...` references and 3 of them do not
  resolve: the one above, a placeholder (`scripts/x.py`, five correct sentences about why a
  printed command needs its runner) and a retired tool (`scripts/archive-memory-index.py`, whose
  removal `tests/test_evolution_prompt_index_rule.py` asserts). Neither of those is a claim that
  a file is there, and a rule that flagged them would need an exemption list - the shape this
  file's own arms warn against.

The boundary is a *spelling*, so it cuts both ways: a placeholder written `scripts/check-<dots>.py`
is guard-shaped and this rule reports it as a missing file, which is how this paragraph's first
draft was caught by its own arm. That is the direction the rule is allowed to be wrong in -
a named file that is not there, never a silent pass - and the cure is to spell a placeholder so
it cannot be read as a claim.

**The class is every tracked `*.md`**, discovered rather than listed, so a new document is
covered the moment it lands and a renamed one cannot leave a stale exemption behind. It is
`git ls-files`, which also keeps this off host-local state by construction: `.emrg/` (cycle
records, session memory) is untracked, so a document there - written by this agent, about
this machine - is not a document this rule reads.

`emrg/server/evolution_prompt.md` is in the class and carries the most references of any
file: it is the instruction set every cycle runs, so a stale name there is a broken command
given to the agent, not merely a misleading line in a README.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A reference to a script in this repository. Deliberately anchored on the `scripts/`
#: directory rather than on `*.py`: a document naming `packaging/gen-assets.sh` or
#: `install.sh` names something outside this class, and a rule that flags them would be a
#: rule with exceptions.
REFERENCE = re.compile(r"scripts/([A-Za-z0-9_.-]+\.(?:py|sh))")

#: A *guard* named in prose. Anchored on the `check-` prefix this repository gives every
#: guard (`check-doc-count.py`, `check-citation-resolves.py`, ...), which is what keeps
#: the rule free of the exemption list its wider form would need: a placeholder such as
#: `scripts/x.py` and a retired tool such as `scripts/archive-memory-index.py` are prose
#: about a script rather than a claim that one is on disk, and neither is spelled this way.
GUARD_REFERENCE = re.compile(r"scripts/(check[-_][A-Za-z0-9_.-]+\.py)")

#: Stated limit: a URL to *another* repository's `scripts/...` path would be read as a
#: reference to ours. No such reference exists in the tracked documents today (measured
#: 2026-10-03), and if one lands the failure is a named missing file rather than a silent
#: pass - which is the direction this guard is allowed to be wrong in.


def referenced_scripts(text: str) -> list[str]:
    """The script names a document refers to, deduplicated and sorted."""
    return sorted(set(REFERENCE.findall(text)))


def missing_scripts(text: str, exists) -> list[str]:
    """The referenced names that do not exist, per the `exists` predicate.

    Takes the check as an argument so both directions are testable against a synthetic
    document: a guard whose only input is the real repository can be exercised in the
    direction where it passes and never in the direction where it fails.
    """
    return [name for name in referenced_scripts(text) if not exists(name)]


def prose_references(text: str) -> list[str]:
    """The guard names a Python source file's **prose** refers to, deduplicated and sorted.

    Docstrings and comments, and nothing else: a name inside a string literal is data - a
    fixture's input, or a message this tree prints - and data is not a claim about this tree.
    Reading with `ast` and `tokenize` rather than a substring search is the same choice
    `tests/test_review_queue.py` makes beside it, and for the same reason: a text search
    cannot tell a rule's explanation from a rule's violation, and this file's own docstring
    quotes both a correct name and an incorrect one.

    Raises `SyntaxError` on a file that does not parse, deliberately: "could not read the
    file" must never be answered as "the file names nothing missing".
    """
    names: list[str] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                names.extend(GUARD_REFERENCE.findall(docstring))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            names.extend(GUARD_REFERENCE.findall(token.string))
    return sorted(set(names))


def missing_guards(text: str, exists) -> list[str]:
    """The guard names a source file's prose names that do not exist, per `exists`.

    Takes the check as an argument for the same reason `missing_scripts` does: the arms
    below must be able to exercise the direction where it fails, and a guard whose only
    input is the real repository can only ever be silent.
    """
    return [name for name in prose_references(text) if not exists(name)]


def _tracked_markdown() -> list[str]:
    """Every tracked Markdown file, relative to the repository root.

    Fails loudly when `git` cannot answer: an empty list would make the arms below pass
    while measuring nothing, and "could not list the documents" is not "every document is
    fine".
    """
    proc = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"`git ls-files '*.md'` failed (rc={proc.returncode}), so the document class was "
        f"never measured: {proc.stderr.strip()!r}"
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _tracked_python() -> list[str]:
    """Every tracked Python source file, relative to the repository root.

    Fails loudly when `git` cannot answer, for the reason `_tracked_markdown` gives: an
    empty list would let the arms below pass while reading no file at all.
    """
    proc = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"`git ls-files '*.py'` failed (rc={proc.returncode}), so the source class was "
        f"never measured: {proc.stderr.strip()!r}"
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _exists(name: str) -> bool:
    return (REPO_ROOT / "scripts" / name).exists()


def test_the_document_class_is_not_empty() -> None:
    """The premise, and the arm that keeps every other arm from passing vacuously.

    A corpus that came back empty - a moved checkout, a `git` that answered nothing - would
    satisfy "no document names a missing script" while having read no document at all.
    """
    names = _tracked_markdown()
    assert len(names) >= 10, (
        f"only {len(names)} tracked Markdown file(s) were listed, which is too few for this "
        f"repository - the rule would pass by reading almost nothing: {names}"
    )

    carrying = [
        name
        for name in names
        if referenced_scripts((REPO_ROOT / name).read_text(encoding="utf-8"))
    ]
    assert len(carrying) >= 3, (
        f"only {len(carrying)} document(s) name a script at all - the reader is finding "
        f"almost nothing, so no arm below is about anything: {carrying}"
    )


def test_every_script_a_document_names_exists() -> None:
    """The rule: a documented command has to be a command that exists.

    A reader who follows a document into a renamed or deleted script loses the round they
    were in the middle of, and nothing in the tree told them - which is what this closes.
    """
    offenders: list[str] = []
    for name in _tracked_markdown():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for missing in missing_scripts(text, _exists):
            offenders.append(f"{name} names scripts/{missing}, which does not exist")

    assert not offenders, (
        "a document names a script that is not in the tree, so the command it gives a "
        "reader fails at the shell:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_predicate_the_real_rule_uses_answers_both_ways() -> None:
    """The predicate the synthetic arms cannot reach, because they inject their own.

    Written after this file's own mutation arm exposed the gap: `_exists` returned the bound
    method object rather than calling `.exists()`, and a truthy object makes every name
    "present" - so the rule over the real documents reported nothing, in every mutation,
    while the injected predicates below stayed honest. The arms that inject a predicate can
    therefore only ever test the *shape* of the rule, and this is the pair that tests the
    one an actual document is judged by.
    """
    assert _exists("check-doc-count.py") is True, (
        "a script that is in the tree was not read as present - the real rule would report "
        "a fault about a document that is correct"
    )
    assert _exists("definitely-not-a-script.py") is False, (
        "a name that is not in the tree was read as present, so the rule over the real "
        "documents can never report anything: check that the predicate CALLS `.exists()` "
        "rather than returning the bound method, which is always truthy"
    )


def test_a_missing_script_is_reported_by_name() -> None:
    """The failing direction, on a synthetic document.

    The real corpus is currently clean, so this is the only arm that can show the rule
    discriminates rather than merely being silent on today's tree.
    """
    text = "Run `python3 scripts/check-doc-count.py` then `scripts/gone-away.py`.\n"
    found = missing_scripts(text, lambda name: name == "check-doc-count.py")
    assert found == ["gone-away.py"], (
        f"expected the absent script to be named exactly once, got {found!r}"
    )


def test_a_path_outside_scripts_is_not_a_reference() -> None:
    """The other direction of the reader: only `scripts/` references are in the class.

    `packaging/gen-assets.sh` and `install.sh` are named all over the install documents;
    a reader that flagged them would report faults about files this rule never claimed,
    and the exemptions that would follow are how a rule widens until it is ignored.
    """
    text = (
        "`bash packaging/gen-assets.sh` and `curl ... | bash` run `install.sh`;\n"
        "see Agent.md and tests/test_check_doc_count.py for the rest.\n"
    )
    assert referenced_scripts(text) == [], (
        f"a non-script path was read as a script reference: {referenced_scripts(text)!r}"
    )


def test_the_references_the_documents_carry_are_found_individually() -> None:
    """A known document, read end to end, so the two arms above are not the only reading.

    Asserted as a floor rather than an exact count: a document may legitimately gain a
    reference, and a pin that has to be updated for every honest addition is a pin that
    gets updated without being read.
    """
    agent = referenced_scripts((REPO_ROOT / "Agent.md").read_text(encoding="utf-8"))
    prompt = referenced_scripts(
        (REPO_ROOT / "emrg" / "server" / "evolution_prompt.md").read_text(encoding="utf-8")
    )
    assert len(agent) >= 15, f"Agent.md names only {len(agent)} scripts: {agent}"
    assert len(prompt) >= 5, (
        f"evolution_prompt.md names only {len(prompt)} scripts: {prompt} - it is the "
        f"instruction set every cycle runs, and a reference there is a command"
    )


def test_the_python_prose_class_is_not_empty() -> None:
    """The premise of the second class, and the arm that keeps its rule from passing vacuously.

    Mirrors `test_the_document_class_is_not_empty`: a `git` that answered nothing, or a
    reader that stopped recognising prose, would satisfy "no source file names a missing
    guard" while having read nothing. The floor is asserted rather than the exact count, so
    an honest addition does not have to come back and update a pin.
    """
    names = _tracked_python()
    assert len(names) >= 200, (
        f"only {len(names)} tracked Python file(s) were listed, which is too few for this "
        f"repository - the rule would pass by reading almost nothing"
    )

    carrying = [
        name for name in names if prose_references((REPO_ROOT / name).read_text(encoding="utf-8"))
    ]
    assert len(carrying) >= 50, (
        f"only {len(carrying)} source file(s) name a guard in prose - the reader is finding "
        f"almost nothing, so the rule below is about nothing: {sorted(carrying)[:10]}"
    )


def test_every_guard_a_source_file_names_in_prose_exists() -> None:
    """The rule: a guard a docstring points at has to be a guard in this tree.

    A reader who follows such a pointer - a person, or the next agent to open the file and
    take the rule from it - finds nothing and has to re-derive the target, which is the cost
    the citation existed to remove. The instance this rule was written for had run on an
    unmerged branch for eleven days, and no arm in the tree could see it.
    """
    offenders: list[str] = []
    for name in _tracked_python():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for missing in missing_guards(text, _exists):
            offenders.append(f"{name} names scripts/{missing}, which does not exist")

    assert not offenders, (
        "a source file's prose names a guard that is not in the tree, so the pointer it "
        "gives a reader leads nowhere:\n  " + "\n  ".join(sorted(offenders))
    )


def test_a_missing_guard_is_reported_by_name() -> None:
    """The failing direction on synthetic source, since the real corpus is clean.

    Both carriers of "prose" are exercised together, and separately asserted: one rule with
    two carriers is pinned by an arm on one of them only until the other drifts.
    """
    docstring_only = '"""The rule lives in `scripts/check-gone-away.py`."""\n'
    comment_only = "# `scripts/check-gone-away.py` decides this.\n"
    for carrier, text in (("docstring", docstring_only), ("comment", comment_only)):
        found = missing_guards(text, lambda name: name == "check-doc-count.py")
        assert found == ["check-gone-away.py"], (
            f"in a {carrier}, an absent guard was not named exactly once: got {found!r}"
        )

    # And a name that *is* there is not reported, in either carrier.
    present = '"""Mechanised by `scripts/check-doc-count.py`."""\n# see scripts/check-doc-count.py\n'
    assert missing_guards(present, _exists) == [], (
        "a guard that exists was reported as missing - the rule would be red on a correct file"
    )


def test_a_guard_named_in_a_string_literal_is_not_a_claim() -> None:
    """The boundary that keeps this rule exempt from an exemption list.

    A literal is where a fixture's data and this tree's printed messages live. Measured
    2026-10-14: the same references appear inside literals 11 times over 12 files
    (`check-some-other-guard.py`, `check-elsewhere.py`, ...), all of them fixtures, and a
    whole-file search would report every one - and then need a list of names to forgive.
    """
    text = (
        'GONE = "scripts/check-some-other-guard.py"\n'
        '"""A real guard, named in prose."""  # noqa: D404 scripts/check-doc-count.py\n'
        "def _stub():\n"
        '    return "a printed command: scripts/check-elsewhere.py --flag"\n'
    )
    assert prose_references(text) == ["check-doc-count.py"], (
        f"a literal was read as a claim about this tree: {prose_references(text)!r}"
    )
