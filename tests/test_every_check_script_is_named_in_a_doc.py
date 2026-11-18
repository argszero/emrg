"""Every tracked `scripts/check-*.py` is named in some tracked Markdown document.

The reverse of `tests/test_documented_scripts_exist.py`. That file asks whether a script a
*document* names is on disk, and gives the reason it mechanised that direction: a document
naming a missing script "fails only for the reader who follows it, and only after they have
typed the command; nothing is watching". The same reasoning runs the other way and had no
arm: a guard no document names fails only for the host who never learns it exists, and
nothing was watching. Measured 2026-11-18 (`cyc20261118-130337`) on `d9073fcc`: of the
**36** tracked guards, **8** were named in no tracked document, and the family test's own
enumeration had the same eight missing — the lists agreed, so this was not a reading error
but a class with no reading at all.

`Agent.md` states the rule this enforces: *a new CI check needs a host-side counterpart — a
documented command or one-click tool — or the host cannot self-check before CI.* A guard
nobody can read the name of has no counterpart, whatever it can do.

Both classes are **discovered**, never listed, so a new document is covered the moment it
lands and a renamed guard cannot hide behind a stale row: `git ls-files 'scripts/check-*.py'`
and `git ls-files '*.md'`. That also keeps this off host-local state by construction —
`.emrg/` is untracked, so memory written about this machine is not a document this rule
reads, which is the point: a guard has to be findable by someone who cloned the repository.

The `check-` prefix is the boundary, and it is the same one `test_documented_scripts_exist.py`
draws: it is what this repository names a guard. `scripts/check_nonlocal.py` and
`scripts/check_unbound_reads.py` are underscore-named modules the suite imports rather than
commands a reader runs, and they are outside the class for the reason the other file gives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Floors, not exact counts: an honest addition must not have to come back and update a pin,
#: and a count written here would go stale silently. What each floor rules out is a `git`
#: that answered nothing — an empty class satisfies the rule while having measured nothing.
_MIN_GUARDS = 20
_MIN_DOCUMENTS = 10

#: Guards that are deliberately named in no document, with the reason. Empty today, and
#: every entry has to carry one: an exemption without a reason is the defect this rule
#: exists to catch, written in the rule's own file.
UNDOCUMENTED_ON_PURPOSE: dict[str, str] = {}


def _tracked(pattern: str) -> list[str]:
    """Every tracked path matching `pattern`, relative to the repository root.

    Fails loudly when `git` cannot answer: an empty list would make the rule below pass
    while measuring nothing, and "could not list the files" is not "every file is fine".
    """
    proc = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"`git ls-files {pattern!r}` failed (rc={proc.returncode}), so that class was never "
        f"measured: {proc.stderr.strip()!r}"
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _tracked_guards() -> list[str]:
    """The guards this repository ships, as repository-relative paths."""
    return sorted(_tracked("scripts/check-*.py"))


def _documented_text() -> str:
    """Every tracked Markdown document, concatenated.

    Concatenated rather than searched one at a time: the rule is "some document names it",
    and which one is the author's choice — a release guard belongs in the release section,
    not in a list at the end of a file.
    """
    documents = _tracked("*.md")
    assert len(documents) >= _MIN_DOCUMENTS, (
        f"only {len(documents)} tracked document(s) were found, so the document class this "
        f"rule reads is too small to mean anything: {documents}"
    )
    return "\n".join(
        (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace") for name in documents
    )


def undocumented_guards(guards: list[str], documents: str) -> list[str]:
    """The guards whose file name appears in no document, sorted.

    Split from the test so the arm below can drive it with a synthetic document instead of
    only with this tree: a predicate measured only on a passing tree is a predicate whose
    failing branch nobody has ever seen run.
    """
    return sorted(
        name
        for name in guards
        if Path(name).name not in documents and Path(name).name not in UNDOCUMENTED_ON_PURPOSE
    )


def test_the_guard_class_is_not_empty() -> None:
    """The premise, and the arm that keeps the rule from passing vacuously."""
    guards = _tracked_guards()
    assert len(guards) >= _MIN_GUARDS, (
        f"only {len(guards)} guard(s) named scripts/check-*.py are tracked, so the class is "
        f"too small to mean anything: {guards}"
    )


def test_every_guard_is_named_in_a_tracked_document() -> None:
    """The rule: a guard no reader can find the name of is a guard only its author runs."""
    missing = undocumented_guards(_tracked_guards(), _documented_text())
    assert not missing, (
        f"{len(missing)} guard(s) are named in no tracked document, so a host reading this "
        f"repository cannot learn they exist: {missing}. Name each one in the document that "
        f"owns its subject, with the command that runs it, and say what question it answers."
    )


def test_a_guard_in_no_document_is_reported_by_name() -> None:
    """The failing branch, driven by a synthetic document.

    Without this, the rule above is only ever observed passing, and a predicate that cannot
    report anything would pass it just as quietly. The document carries one of the two names
    and the assertion is that the *other* one comes back — so a predicate that returned the
    whole class, or nothing, fails here.
    """
    guards = ["scripts/check-present.py", "scripts/check-absent.py"]
    documents = "Run `scripts/check-present.py` to ask."
    assert undocumented_guards(guards, documents) == ["scripts/check-absent.py"]


def test_a_document_that_merely_mentions_the_prefix_names_nothing() -> None:
    """A name is a file name, not a prefix: prose about `check-` guards documents none.

    This tree's own documents talk about the guard family by prefix, so a matcher that
    accepted a partial name would report every guard as documented the moment one document
    said "guards named `check-`.py" — the silent pass this file exists to prevent.
    """
    guards = ["scripts/check-release-owed.py"]
    assert undocumented_guards(guards, "the `check-` prefix names a guard") == guards


def test_every_exemption_carries_a_reason() -> None:
    """An exemption with no reason is the defect written into the rule's own file."""
    empty = [name for name, reason in UNDOCUMENTED_ON_PURPOSE.items() if not reason.strip()]
    assert not empty, f"exemption(s) written down with no reason: {empty}"
