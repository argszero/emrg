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
REFERENCE = re.compile(r"scripts/([A-Za-z0-9_.-]+\.(?:py|sh))")

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


#: A script written **bare** - no directory part - as a document's own token. This is the
#: other half of the same question, and the anchored rule above cannot see it: `scripts/x.py`
#: carries the directory to search under, while `x.py` written alone carries no anchor at
#: all, so a reader that looks only for the first shape never reads the second.
#:
#: The class is **syntactic**, not defined by what the name currently resolves to. That
#: distinction is the whole point: a rule whose class is "bare names that resolve into
#: `scripts/`" would drop a name out of its own class the moment that script was renamed -
#: exactly when the rule is needed - and would then be silent forever (measured
#: 2026-10-03, `cyc20261003-132521`). Reading the token that is written, and requiring it to
#: resolve, is the direction that stays live under the rename it exists to catch.
#:
#: Deliberately narrow: the token has to be the *entire* backticked span. `` `x.py --flag` ``
#: and `` `python3 x.py` `` are prose about a command, and widening to them would need the
#: exemption list this file's other arm argues against.
BARE_REFERENCE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh))`")


def bare_referenced_scripts(text: str) -> list[str]:
    """The bare script names a document writes, deduplicated and sorted."""
    return sorted(set(BARE_REFERENCE.findall(text)))


def unresolvable_bare_scripts(text: str, resolve) -> list[str]:
    """The bare names that resolve to no tracked file, per the `resolve` predicate.

    `resolve` is injected for the same reason `exists` is above: the real corpus is clean,
    so only an arm that supplies its own index can show the rule discriminates.
    """
    return [name for name in bare_referenced_scripts(text) if not resolve(name)]


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


def _tracked_file_basenames() -> dict[str, list[str]]:
    """Tracked path basename -> the paths carrying it, over the whole tree.

    The bare-name rule cannot look under `scripts/`: a bare name carries no directory, so
    the paths to consider have to be *discovered* first, and the discovery has to stay
    inside this repository. `git ls-files` is that discovery, and it is the same source the
    document class comes from, so a host-local tree (`.emrg/`, `node_modules/`, a build
    output) is excluded by construction rather than by an ignore list.

    Discovery is **not** the whole question — see `_resolves_to_a_tracked_file`, which also
    requires the discovered file to be there. Fails loudly when `git` cannot answer, for the
    reason `_tracked_markdown` records: an empty index would make every bare name
    unresolvable, and reporting every document as a fault is the same class of error as
    reporting none.
    """
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"`git ls-files` failed (rc={proc.returncode}), so the tracked tree was never "
        f"measured: {proc.stderr.strip()!r}"
    )
    index: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            index.setdefault(Path(line).name, []).append(line)
    return index


#: `git ls-files` is the expensive part and the index is a property of the tree, not of a
#: document, so it is read once per process. The first version of this rule built it inside
#: the predicate, which the real-tree arm then called once per name.
_TRACKED_BASENAMES: dict[str, list[str]] | None = None


def _resolves_to_a_tracked_file(name: str) -> bool:
    """Whether a tracked file with that basename **is there**. The rule's live predicate.

    Two questions, and both have to be answered yes, because the failure this rule exists
    for is a reader typing a command that is not there:

    * is any file this repository tracks named that (`git ls-files`), and
    * does that file exist on disk.

    The second half is not decoration. `git ls-files` answers from the **index**, so a
    script renamed in the working tree — or deleted from it — is still listed, and an
    index-only reader would call the name resolvable while the reader following the
    document gets `No such file or directory`. Measured 2026-10-03 (`cyc20261003-161834`):
    with `scripts/classify-conflict.py` moved aside but still in the index, the index-only
    version of this predicate answered **True** and the rule stayed green. Asking the disk
    as well is also what makes this reader agree with the anchored one above, whose
    `_exists` has always asked the disk: two questions about the same file that disagree
    about what "there" means is how a guard reports a clean tree over a broken command.

    Presence, not uniqueness: `__main__.py` is carried by three tracked files
    (`emrg/__main__.py`, `emrg/client/__main__.py`, `emrg/server/__main__.py`) and a
    document naming it bare is **not** wrong - it is ambiguous about which one it means,
    which is a thing to read, not a fault to fail. Asserting uniqueness would report three
    correct documents as broken, and the exemptions that would follow are how a rule widens
    until it is ignored.

    Kept as a named function rather than an inline expression so it is the *one* place the
    real documents are judged by, and so a mutation can be aimed at it. The first version
    had this helper *and* a second, inline spelling in the arm, so mutating the helper
    changed nothing and the arm survived (measured 2026-10-03, `cyc20261003-132521`).
    """
    global _TRACKED_BASENAMES
    if _TRACKED_BASENAMES is None:
        _TRACKED_BASENAMES = _tracked_file_basenames()
    paths = _TRACKED_BASENAMES.get(name)
    if not paths:
        return False
    return any((REPO_ROOT / path).exists() for path in paths)


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


def test_every_bare_script_a_document_writes_resolves_to_a_tracked_file() -> None:
    """The second half of the rule: `x.py` written alone is also a command a reader types.

    The anchored rule above reads `scripts/x.py` and cannot see this shape at all, so a
    rename of `x.py` leaves every bare mention of it pointing at nothing and nothing in the
    tree notices. Measured on this tree when the arm was written (2026-10-03,
    `cyc20261003-132521`): 17 distinct bare names across the tracked documents, all of them
    resolving - a coverage gap, not a current fault, which is why it is a rule and not a fix.
    """
    index = _tracked_file_basenames()
    assert index, (
        "`git ls-files` answered with no tracked file at all, so every bare name below "
        "would be reported as unresolvable"
    )
    offenders: list[str] = []
    for name in _tracked_markdown():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for unresolved in unresolvable_bare_scripts(text, _resolves_to_a_tracked_file):
            offenders.append(f"{name} writes `{unresolved}`: {_absence_reason(unresolved)}")

    assert not offenders, (
        "a document writes a bare script name that this repository does not have, so the "
        "command it gives a reader fails at the shell:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_bare_name_class_is_not_empty_and_is_read_from_real_documents() -> None:
    """The premise: the bare-name reader finds something, or the rule above is vacuous.

    An arm that reported "no bare name is unresolvable" over a reader that matched nothing
    at all would look exactly like a clean tree. This is the same shape as the premise arm
    for the anchored rule, kept separate because the two readers can fail independently.
    """
    carrying = {
        name: bare_referenced_scripts((REPO_ROOT / name).read_text(encoding="utf-8"))
        for name in _tracked_markdown()
    }
    found = {name: names for name, names in carrying.items() if names}
    # Measured on this tree 2026-10-03 (`cyc20261003-132521`): **2** tracked documents carry
    # bare names (`Agent.md` 15, `DEVELOPMENT.md` 3). The floor is the measured population
    # rather than a round number - this arm exists to notice the reader going blind, and a
    # floor above what the corpus really has would fail for a reason that is not the reader.
    assert len(found) >= 2, (
        f"only {len(found)} tracked document(s) carry a bare script name - the reader is "
        f"finding almost nothing, so the rule above is not about anything: {found}"
    )
    assert len(carrying["Agent.md"] if "Agent.md" in carrying else []) >= 10, (
        f"Agent.md carries only {carrying.get('Agent.md')} bare names - it is the document "
        f"this reader was written for, and a reader that stopped reaching it would leave "
        f"the rule above passing over an empty set"
    )


def test_a_bare_name_that_is_not_a_tracked_file_is_reported() -> None:
    """The failing direction, on a synthetic document.

    The real corpus is clean, so this is the arm that shows the rule discriminates rather
    than merely being silent - the same reason the anchored rule has its own.
    """
    text = "Then run `bump-version.py 0.3.9` and `rename-me.py` when you are done.\n"
    found = unresolvable_bare_scripts(text, lambda name: name == "bump-version.py")
    # `bump-version.py 0.3.9` is not a bare token - the backticked span carries arguments -
    # so the only bare name here is `rename-me.py`, and it is the only thing reported.
    assert found == ["rename-me.py"], f"expected exactly the absent name, got {found!r}"


def test_a_command_written_in_backticks_is_not_a_bare_name() -> None:
    """The other direction of the reader, and the boundary this rule deliberately keeps.

    `` `x.py --flag` `` and `` `python3 x.py` `` are prose about a command, not a document's
    own token for a file. Widening to them would mean reading flags and interpreters as part
    of a filename, and the exemptions that would follow are the failure mode the anchored
    rule's own boundary arm argues against.

    The sample names **two different files** on purpose. The first draft used the same script
    in every form, so a reader widened to whole command spans returned the same single name
    and this arm passed anyway - a mutation of the reader survived it (measured 2026-10-03,
    `cyc20261003-161834`). A boundary arm has to give the widened reader something extra to
    find, or it is not testing the boundary.
    """
    text = (
        "Run `python3 other-tool.py` to regenerate, then the bare "
        "`check-doc-count.py --measure`; `check-doc-count.py` alone is the form this rule "
        "reads.\n"
    )
    assert bare_referenced_scripts(text) == ["check-doc-count.py"], (
        f"a command line was read as a bare file name: {bare_referenced_scripts(text)!r}"
    )


def _absence_reason(name: str) -> str:
    """Why the live predicate says no, in the words that state deserves.

    Two states reach `_resolves_to_a_tracked_file` and they are fixed differently: the
    repository tracks no file of that name (the document is wrong, or the script was
    renamed away and the document kept the old name), or it tracks one that is not on disk
    (the file was moved or deleted and the index has not caught up). A single message
    covering both would name neither, which is the defect the failure text exists to avoid.
    """
    global _TRACKED_BASENAMES
    if _TRACKED_BASENAMES is None:
        _TRACKED_BASENAMES = _tracked_file_basenames()
    if name not in _TRACKED_BASENAMES:
        return "no tracked file in this repository is named that"
    return "it is tracked, but no such file is present on disk"


def test_the_live_predicate_answers_both_ways_and_says_which_state() -> None:
    """The predicate the synthetic arms cannot reach, because they inject their own.

    Same gap the anchored rule records one screen up: `missing_scripts` takes an `exists`
    so the failing direction is testable, and the predicate the *real* documents are judged
    by then has no arm of its own. Without this, a predicate stuck at `True` - the shape a
    first draft had - would leave every arm green while the rule never reported anything.
    """
    assert _resolves_to_a_tracked_file("check-doc-count.py") is True, (
        "a script this repository tracks and has was not read as resolvable, so the rule "
        "would report correct documents as broken"
    )
    assert _resolves_to_a_tracked_file("definitely-not-a-script.py") is False, (
        "a name no tracked file carries was read as resolvable, so the rule over the real "
        "documents can never report anything"
    )
    assert "no tracked file" in _absence_reason("definitely-not-a-script.py")


def test_a_tracked_name_whose_file_is_gone_does_not_resolve(monkeypatch) -> None:
    """Discovery is the index; existence is the disk - and this is where they part.

    The first version asked the index alone, and measured 2026-10-03
    (`cyc20261003-161834`) that answers **True** for a script moved aside in the working
    tree: `git ls-files` still lists it, the document still names it, and a reader following
    the document gets `No such file or directory`. The tree below is injected rather than
    built on disk so the arm states the pair it is about - a tracked path that is there, and
    a tracked path that is not - without touching the repository.
    """
    monkeypatch.setattr(
        "tests.test_documented_scripts_exist._TRACKED_BASENAMES",
        {
            "there.py": ["scripts/check-doc-count.py"],
            "gone.py": ["scripts/removed-last-week.py"],
        },
        raising=True,
    )
    assert _resolves_to_a_tracked_file("there.py") is True, (
        "a tracked path that exists on disk must still resolve - the disk half must narrow "
        "the index, not replace it"
    )
    assert _resolves_to_a_tracked_file("gone.py") is False, (
        "a name that is tracked but whose file is not on disk does not resolve: the reader "
        "following the document runs a command that is not there, which is the failure this "
        "rule exists for. An index-only reader answers True here - measured"
    )
    assert "no such file is present on disk" in _absence_reason("gone.py"), (
        f"the two states must be named apart: {_absence_reason('gone.py')!r}"
    )
