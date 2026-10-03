"""Every `scripts/<name>` a tracked Markdown file names must exist on disk.

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

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A reference to a script in this repository. Deliberately anchored on the `scripts/`
#: directory rather than on `*.py`: a document naming `packaging/gen-assets.sh` or
#: `install.sh` names something outside this class, and a rule that flags them would be a
#: rule with exceptions.
#:
#: `/` is in the name class, and the reason is the direction this guard is allowed to be
#: wrong in. The first version used `[A-Za-z0-9_.-]+` - a **flat** name - so a document
#: naming `scripts/tools/foo.py` matched the anchor and then failed on the `/`, and the
#: reader returned **nothing** for it. That is not "this document names no script": it is a
#: recognized shape read as an absent one, which is the one failure a reader of this family
#: may not have (a name that does not exist is reported; a shape that was not classified is
#: invisible). No such reference exists in the tracked documents today (measured
#: 2026-10-03), so the fix changes no verdict - it removes a silence.
REFERENCE = re.compile(r"scripts/([A-Za-z0-9_./-]+\.(?:py|sh))")

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


class TestTheReaderClassifiesBeforeItReports:
    """A shape the reader recognises but cannot classify must be read, not dropped.

    The anchor is the `scripts/` directory, and everything after it up to the extension is
    the script's location. The first version of the name class held no `/`, so a reference
    to a script one directory down - `scripts/tools/foo.py` - matched the anchor and then
    failed on the `/`: the reader returned **nothing** for it, and nothing is what a document
    that names no script returns. Those two answers must not be the same answer.

    Both directions are pinned, and the second one is the point: this is not a change to a
    verdict (nothing in the tree is nested today) but the removal of a silence.
    """

    def test_a_nested_script_path_is_a_reference(self) -> None:
        assert referenced_scripts("see `scripts/tools/gone.py` for the rest") == ["tools/gone.py"]

    def test_a_nested_reference_is_reported_when_the_script_is_absent(self) -> None:
        """The consequence, in the rule's own vocabulary - and the half the silence hid."""
        text = "run `scripts/tools/gone.py` then `scripts/check-doc-count.py`\n"
        found = missing_scripts(text, lambda name: name == "check-doc-count.py")
        assert found == ["tools/gone.py"], (
            f"a nested reference the script does not exist for was not reported, so the "
            f"guard is silent exactly where a reader would lose the round: {found!r}"
        )

    def test_the_anchor_still_excludes_other_directories(self) -> None:
        """The class boundary is unchanged: only paths under `scripts/` are references."""
        text = "`packaging/gen-assets.sh`, `tests/test_check_doc_count.py`, `install.sh`\n"
        assert referenced_scripts(text) == [], referenced_scripts(text)

    def test_the_real_corpus_has_no_reference_the_reader_drops(self) -> None:
        """The control over the real documents: what the reader finds, the corpus contains.

        Written as a *comparison* rather than a count of captured names, because a count
        cannot see a dropped reference - the dropped one is precisely the one not counted.
        The broad pattern accepts any path characters, so anything it sees that the rule's
        reader does not is a shape this rule does not classify.
        """
        broad = re.compile(r"scripts/([A-Za-z0-9_./*?\[\]-]+\.(?:py|sh))")
        dropped: list[str] = []
        for name in _tracked_markdown():
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            for reference in sorted(set(broad.findall(text)) - set(referenced_scripts(text))):
                dropped.append(f"{name}: {reference}")
        assert not dropped, (
            "these references are in the documents and are not classified as references, so "
            "this guard reports nothing about them:\n  " + "\n  ".join(dropped)
        )
