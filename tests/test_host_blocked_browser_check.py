"""One rule, two carriers: "this step is the host's" needs a browser check first.

Why this file exists
--------------------
A task prompt decides what a round can do for itself, and two of the shipped
templates can hand a step back to the host for a **capability** reason: the
open-source template (``open_source_prompt.md``, whose subject is platform account
state — a CLA record, an agreement signed years ago, a repository setting) and the
competition template (``competition_prompt.md``, whose ``blocked`` row is the shape
that records a wall). Neither used to require that the state be looked at before
the conclusion was written down, and the failure that permits has already happened
here: a round concluded a blocked CLA meant *only the host could sign it*, the host
asked it to look in the browser instead, and the real cause was an empty author
email on the commits — fixable from the CLI. The conclusion was wrong in the one
direction that spends the host's time.

Two carriers, one requirement — so one arm on one carrier proves nothing about the
other. The tests below therefore do two things and the second is the point:
``test_each_carrier_states_every_part_of_the_rule`` asserts each carrier on its own
(a deletion reddens that carrier's case), and
``test_the_two_carriers_state_the_same_rule`` compares the two **both ways**, so an
edit that lands in one template and not the other is reported as the asymmetry it
is rather than as one missing phrase.

What the clause region is, and the level it is read at
------------------------------------------------------
Each carrier is read through a **marker-bounded extractor** rather than a whole-file
substring search, so "the rule is in the file" cannot be satisfied by a sentence
that wandered into an unrelated section. A region bound is no bound if it can widen
to EOF (a truncated end marker did exactly that once), so each clause is also held
to a fraction of its file.

File-level reading is one level short of a round's prompt: a Jinja comment
(``{# … #}``) removes text from the render while the file keeps every character.
That shape was measured on this repo before (#1904: five load-bearing phrases still
in the file, every file-level assertion green, the render 2,301 characters shorter).
So every clause is extracted **twice** — from the file, and from the file with its
Jinja comments removed — and must survive both. Named limit: this is the comment
shape only; a ``{% if false %}`` block is not simulated here, and nothing in this
file shows that a round *follows* the rule, only that it is stated.

The permission boundary is guarded in both directions too. The rule is about what
automation can do, never about what it may do: a step that spends money or changes
the host's account settings stays a stop-and-ask, and
``test_the_capability_rule_does_not_swallow_the_permission_stops`` fails if the
clause is rewritten into a licence — or if the iOS template's own explicit
exceptions are removed to "generalise" it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS = Path(__file__).resolve().parent.parent / "emrg" / "server"

OPEN_SOURCE = PROMPTS / "open_source_prompt.md"
COMPETITION = PROMPTS / "competition_prompt.md"
IOS_APP = PROMPTS / "ios_app_prompt.md"

#: Where the clause starts and ends in `open_source_prompt.md` §3.1.6. Both are
#: single-phrase anchors so a reflow of the surrounding prose does not move them.
OPEN_SOURCE_START = "**The same rule covers the host's side of the ledger.**"
OPEN_SOURCE_END = "whatever the browser shows."

#: The `blocked` shape in `competition_prompt.md`'s error-handling table.
COMPETITION_ROW_PREFIX = "| Needs a human action (real-name, SMS, card authorization)"

#: The rule as parts a reader can point at. Each pattern accepts the wording either
#: carrier uses: the carriers state one requirement, not one sentence, and pinning a
#: single exact string per carrier would be a paste-lock rather than a guard.
RULE = (
    (
        "the account's real state is read in the real browser",
        re.compile(r"\breal browser\b"),
    ),
    (
        "the probe without the login is described as logged-out",
        re.compile(r"logged-out"),
    ),
    (
        "such a probe is stated not to be evidence about the account",
        re.compile(r"not evidence about the account"),
    ),
    (
        "the fallback names the step that blocks",
        re.compile(r"(?:names that step|the step that blocks)"),
    ),
)

#: The hand-back shape the competition row used to state, retired by this change.
RETIRED_HAND_BACK = "`blocked` with what the host must do"

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _normalised(text: str) -> str:
    """Collapse whitespace, so a reflowed paragraph and a table row read alike."""
    return re.sub(r"\s+", " ", text).strip()


def _strip_jinja_comments(text: str) -> str:
    return JINJA_COMMENT.sub(" ", text)


def _bound_clause(clause: str, source: str, whole: str) -> str:
    """Hold an extracted clause inside its file, so a broken end marker cannot pass.

    A missing end marker would otherwise widen the region to EOF and let any later
    sentence satisfy the rule.
    """
    assert clause, f"{source}: the clause region came out empty"
    assert len(clause) < len(_normalised(whole)) / 3, (
        f"{source}: the extracted clause is {len(clause)} chars of a "
        f"{len(_normalised(whole))}-char file — the end marker did not bound it, so "
        "this region is not the clause and proves nothing"
    )
    return clause


def _open_source_clause(text: str) -> str:
    flat = _normalised(text)
    start = flat.find(OPEN_SOURCE_START)
    assert start != -1, (
        f"open_source_prompt.md: the clause does not start with {OPEN_SOURCE_START!r}"
    )
    end = flat.find(OPEN_SOURCE_END, start)
    assert end != -1, (
        f"open_source_prompt.md: the clause no longer ends with {OPEN_SOURCE_END!r}"
    )
    return _bound_clause(flat[start : end + len(OPEN_SOURCE_END)], "open_source_prompt.md", text)


def _competition_row(text: str) -> str:
    for line in text.splitlines():
        if line.startswith(COMPETITION_ROW_PREFIX):
            return _bound_clause(
                _normalised(line), "competition_prompt.md (blocked row)", text
            )
    raise AssertionError(
        f"competition_prompt.md: no row starts with {COMPETITION_ROW_PREFIX!r} — the "
        "host-blocked shape this rule is attached to is gone, so the rule is not "
        "reached by the round that writes the row"
    )


#: One entry per carrier: the file, and the extractor that reads *the clause*.
CARRIERS = {
    "open_source_prompt.md": (OPEN_SOURCE, _open_source_clause),
    "competition_prompt.md": (COMPETITION, _competition_row),
}


def _clause(name: str) -> str:
    """The clause, read at both levels: the committed file, and the render's text.

    Extraction is run on the file and again on the file with its Jinja comments
    removed — a sentence inside ``{# … #}`` is in the file and not in the prompt, so
    an extractor that only ever sees the raw file cannot tell those apart.
    """
    path, extractor = CARRIERS[name]
    assert path.is_file(), f"{path} is missing — a carrier cannot state the rule"
    raw = path.read_text(encoding="utf-8")
    clause = extractor(raw)
    extractor(_strip_jinja_comments(raw))
    return clause


def _parts(name: str) -> set[str]:
    clause = _clause(name)
    return {label for label, pattern in RULE if pattern.search(clause)}


# ── the carriers are the two the issue names, and nothing silently drops one ──


def test_the_carriers_are_the_two_templates_the_rule_belongs_to():
    assert set(CARRIERS) == {"open_source_prompt.md", "competition_prompt.md"}
    for name, (path, _extractor) in CARRIERS.items():
        assert path.is_file(), f"{name}: declared carrier missing at {path}"


# ── each carrier, on its own ────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(CARRIERS))
@pytest.mark.parametrize("label,pattern", RULE, ids=[label for label, _ in RULE])
def test_each_carrier_states_every_part_of_the_rule(name, label, pattern):
    clause = _clause(name)
    assert pattern.search(clause), (
        f"{name}: the clause no longer states that {label}. The clause reads:\n\n"
        f"{clause}\n\n"
        "A carrier that drops one part lets a round conclude a step is the host's "
        "without checking it — which is the failure this rule exists to stop."
    )


# ── the two carriers, compared both ways ────────────────────────────────────


def test_the_two_carriers_state_the_same_rule():
    open_source = _parts("open_source_prompt.md")
    competition = _parts("competition_prompt.md")

    only_in_open_source = open_source - competition
    only_in_competition = competition - open_source

    assert not only_in_open_source and not only_in_competition, (
        "the two carriers no longer state the same requirement.\n"
        f"  only in open_source_prompt.md: {sorted(only_in_open_source) or 'none'}\n"
        f"  only in competition_prompt.md: {sorted(only_in_competition) or 'none'}\n\n"
        "Both templates hand a step back to the host, so both must carry the same "
        "obligation: whichever side lost a part is the edit to finish."
    )


def test_the_competition_row_no_longer_states_a_bare_hand_back():
    clause = _clause("competition_prompt.md")
    assert RETIRED_HAND_BACK not in clause, (
        "competition_prompt.md's blocked row is back to the bare hand-back shape "
        f"({RETIRED_HAND_BACK!r}): it tells the round to record a wall without "
        "looking at the account's actual state first."
    )


# ── the rule is about capability, never permission ──────────────────────────


def test_the_capability_rule_does_not_swallow_the_permission_stops():
    clause = _clause("open_source_prompt.md").lower()
    for phrase in ("spend money", "account setting", "stop-and-ask"):
        assert phrase in clause, (
            "open_source_prompt.md: the clause no longer names "
            f"{phrase!r} as staying a stop-and-ask — without it the browser check "
            "reads as a licence to act on the host's behalf."
        )


def test_the_ios_templates_own_exceptions_are_untouched():
    """The iOS template keeps its own two exceptions; this rule does not rewrite them.

    It is listed here because the cheapest way to "generalise" the rule is to delete
    the exception it is not about.
    """
    text = " ".join(IOS_APP.read_text(encoding="utf-8").split())
    # Read the exceptions as the two things they name, not as one exact sentence:
    # the template bolds the word between them, so a sentence-level literal is a
    # markup lock rather than a statement about the rule.
    for phrase in ("money is spent", "account settings change"):
        assert phrase in text, (
            f"ios_app_prompt.md: its stop-and-ask exception for {phrase!r} is gone. "
            "Money and account settings are authorization boundaries, not capability "
            "claims, and the browser rule does not touch them."
        )


# ── the reading itself is self-tested ───────────────────────────────────────


def test_a_jinja_comment_over_the_clause_is_caught():
    """A comment hides the rule from the render while the file keeps every character.

    The device that catches it must be shown to fire, or a stripped-text reading
    that silently stopped stripping would pass for ever.
    """
    for name, (path, extractor) in CARRIERS.items():
        raw = path.read_text(encoding="utf-8")
        assert extractor(raw), f"{name}: the extractor found nothing on the real file"

        clause = extractor(raw)
        # Wrap the clause's own first words in a comment, the shape #1904 measured.
        head = clause.split()[0]
        index = raw.find(head)
        assert index != -1
        hidden = raw[:index] + "{# " + raw[index:] + " #}"
        # The file still carries the text …
        extractor(hidden)
        # … and the render does not, which is what the second reading catches.
        with pytest.raises(AssertionError):
            extractor(_strip_jinja_comments(hidden))


def test_the_retired_hand_back_shape_is_absent_and_its_absence_is_meaningful():
    """Guard against a vacuous pass: an absence check needs the phrase to be real.

    A phrase that was never in the file is absent from it for ever, so "the retired
    wording is gone" would be green on a template that never carried it. The subject
    is named here instead: the competition template still speaks of what the host
    must do, so the row this rule attaches to is the one being read.
    """
    text = COMPETITION.read_text(encoding="utf-8")
    assert "what the host must do" in text, (
        "competition_prompt.md no longer mentions what the host must do at all — the "
        "blocked row this rule attaches to has been renamed or removed."
    )
