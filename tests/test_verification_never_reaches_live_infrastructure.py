"""The open-source template bounds what a verification may touch, not only what it concludes.

§3.1.6 tells a cycle how to read a check's *result* — the suite, a test that must fail without
the change, a red check answered in the code, a suite that could not run recorded as a skip.
None of those bounds the check's *subject*: a round honouring every one of them could still
verify a change against the target project's live database, send a real message, or paste a
token into a pull request, and the suite would be green while the damage sat outside the
repository.

So the template carries the rule, and this file is what makes it a requirement rather than
prose: each part of the rule is named here, the paragraph is required to sit inside the
section that owns verification, and an extractor control proves the patterns can see absence.

Wording is read as parts, never as one paste-locked sentence — the paragraph may be reflowed,
and a guard that pins an exact string is a guard against editing rather than against losing
the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS = Path(__file__).resolve().parent.parent / "emrg" / "server"
TEMPLATE = PROMPTS / "open_source_prompt.md"

#: Both anchors are single phrases, so reflowing the paragraph's prose moves neither.
CLAUSE_START = "**A verification never reaches the project's live infrastructure.**"
CLAUSE_END = "be taken at all."

#: The section the rule belongs to, and the one after it — the rule is measured in between.
SECTION_HEAD = "##### 3.1.6 Test"
NEXT_SECTION_HEAD = "##### 3.1.7 Commit and open the pull request"

#: Every part is one requirement a reader can point at; the labels say what losing it admits.
RULE = (
    (
        "the project's own doubles are what a check drives",
        re.compile(r"own doubles\b.*?\bdrive (?:those|them)\b"),
    ),
    ("a production database or a signed-in account is out of reach", re.compile(r"production database")),
    ("a message, mail or payment that actually leaves is out of reach", re.compile(r"actually leaves")),
    ("a listener on somebody else's service is out of reach", re.compile(r"\blistener\b")),
    ("a resource created or deleted for real is out of reach", re.compile(r"deleted for real")),
    ("a secret or token does not leave with the work", re.compile(r"\bsecret\b")),
    ("a database and a run's output do not leave with the work", re.compile(r"command's output")),
    ("an unmeasured direction is named in the pull request", re.compile(r"could not be measured")),
)


def _flat(text: str) -> str:
    """Collapse the template's hard wrapping so a pattern spans a line break."""
    return re.sub(r"\s+", " ", text)


def _bound(text: str, start: str, end: str) -> str:
    flat = _flat(text)
    left = flat.find(start)
    assert left != -1, f"the clause's opening anchor is gone: {start!r}"
    right = flat.find(end, left)
    assert right != -1, f"the clause's closing anchor is gone: {end!r}"
    return flat[left : right + len(end)]


def _clause() -> str:
    return _bound(TEMPLATE.read_text(encoding="utf-8"), CLAUSE_START, CLAUSE_END)


def _parts(clause: str) -> set[str]:
    return {label for label, pattern in RULE if pattern.search(clause)}


def test_the_rule_lives_in_the_section_that_owns_verification():
    """A rule filed under another subject is a rule the round it governs never reads."""
    flat = _flat(TEMPLATE.read_text(encoding="utf-8"))
    head = flat.find(SECTION_HEAD)
    next_head = flat.find(NEXT_SECTION_HEAD)
    start = flat.find(CLAUSE_START)
    assert head != -1 and next_head != -1 and start != -1, (
        "the template lost one of its anchors: "
        f"section {head}, next section {next_head}, clause {start}"
    )
    assert head < start < next_head, (
        "the verification-bounds clause is no longer inside §3.1.6 — it sits "
        f"at offset {start}, outside the section {head}..{next_head}"
    )


@pytest.mark.parametrize("label,pattern", RULE, ids=[label for label, _ in RULE])
def test_the_clause_states_every_part_of_the_rule(label, pattern):
    clause = _clause()
    assert pattern.search(clause), (
        f"the clause no longer states that {label}. It reads:\n\n{clause}\n\n"
        "A round that loses this part can spend a third party's live system, or carry "
        "its secrets out, while every check it runs is green."
    )


def test_the_clause_is_not_silently_emptied():
    """The bound is a paragraph, not a phrase: a one-line replacement must not pass."""
    clause = _clause()
    assert len(clause) > 600, f"the clause has shrunk to {len(clause)} characters: {clause!r}"
    assert _parts(clause) == {label for label, _ in RULE}, (
        "the clause no longer carries every part: "
        f"missing {sorted({label for label, _ in RULE} - _parts(clause))}"
    )


def test_the_parts_patterns_can_see_absence():
    """An extractor that matches everything would report every part as present.

    The edit removes the *instruction* — the doubles are still named, they are simply no
    longer what the round is told to drive — so a pattern that matches on the noun alone
    would keep reporting the part present. That is the failure this control exists to catch,
    and it caught exactly that on the first draft of these patterns.
    """
    clause = _clause()
    doctored = clause.replace("drive those", "drive the real thing")
    assert doctored != clause, "the control's edit did not apply to the clause"

    gone = {label for label, pattern in RULE if not pattern.search(doctored)}
    assert gone == {"the project's own doubles are what a check drives"}, (
        "removing the drive instruction should drop exactly that part and no other; "
        f"the patterns report {sorted(gone)} as absent. A pattern that still matched the "
        "noun alone would report the part present on a clause that no longer requires it."
    )


def test_the_rule_does_not_swallow_the_publish_credential_rule():
    """The new bounds add to the credential rule; they do not replace where it lives."""
    flat = _flat(TEMPLATE.read_text(encoding="utf-8"))
    assert "spent rather than stored" in flat, (
        "the publish-credential rule is gone — a credential is still spent in one command "
        "and written to no file, and that sentence is where the template says so"
    )
