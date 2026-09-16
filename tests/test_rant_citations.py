"""Tests for scripts/check-rant-citations.py - a citation must name a public record.

Background (issue #1252, cycle cyc20260916-120404)
--------------------------------------------------
A rant timestamp indexes `~/.emrg/rants.jsonl` **on the machine that wrote it**, so
instruction prose that cites one is unresolvable for every other reader - measured by
the issue's reporter: 0 of 24 resolved on a second host - and the store keeps only
the ten most recent completed rants, so it also ages out of its own host's reach.
A PR number stays resolvable forever. The rule is therefore not "do not cite a rant"
(the citation is the instruction's provenance) but "name the public record beside it".

Two defect classes are pinned here in **both** directions, never inferred from the
failure case alone (#455):

* a **bare `#N`** is not a record - `journal_prompt.md`'s numbered list matches `#12`,
  `#13`, `#2` on five lines, so the loose rule reports five resolved sites that name
  nothing (a guard whose instrument reports the healthy answer without measuring);
* a **minute-truncated** spelling (`2026-08-24T17:50`) must stay a site with readable
  timestamps - a seconds-only pattern gave it none, and `problems` then indexed an
  empty list and crashed. A guard that cannot survive its own input is not a guard.

The unit tests inject text; the integration tests run on the real tree and on the
real exit-code contract (`0` holds, `1` violated, `2` unmeasurable).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-rant-citations.py"
CANONICAL = "uv run --no-sync python3 scripts/check-rant-citations.py"

HOST_OWNED = "emrg/server/evolution_prompt.md"
OTHER = "emrg/server/journal_prompt.md"


def _load():
    spec = importlib.util.spec_from_file_location("check_rant_citations", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def snippet(mod, monkeypatch):
    """The module with an empty debt list, for the injected-text tests.

    `problems` is a whole-tree verdict - it also reports stale debt entries - so a
    snippet that does not carry the host-owned template's citations would be
    reported with eight "stale debt entry" lines that have nothing to do with the
    rule under test. A test that exercises the debt rules sets its own entries on
    this empty list, and monkeypatch restores the real one afterwards.
    """
    monkeypatch.setattr(mod, "DEBT", {})
    return mod


def sites_of(mod, text: str, path: str = OTHER):
    return mod.scan(text, path)


# --- the rule, both directions -------------------------------------------------


def test_a_record_beside_a_citation_satisfies_the_rule(snippet):
    text = "- do the thing (PR #123, rant 2026-08-01T10:00:00)\n"
    assert snippet.problems(sites_of(snippet, text)) == []


def test_a_citation_with_no_record_is_reported(snippet):
    text = "- do the thing (rant 2026-08-01T10:00:00)\n"
    found = snippet.problems(sites_of(snippet, text))
    assert len(found) == 1
    assert "2026-08-01T10:00:00" in found[0]
    assert "add a public record" in found[0]


def test_a_bare_hash_is_not_a_record(snippet):
    """The loose spelling is a measured trap, not a preference.

    `#12` here is a list ordinal. A rule that accepted any `#\\d+` would call this
    site resolved and report the healthy answer without measuring anything.
    """
    text = "12. **Spot-check citations** (rant 2026-09-10T11:28:43): see #12 and #13\n"
    words = [m.group(0) for m in snippet.PUBLIC_RECORD.finditer(text)]
    assert words == [], f"a bare hash was read as a record: {words}"
    found = snippet.problems(sites_of(snippet, text))
    assert len(found) == 1 and "add a public record" in found[0]


def test_issue_form_is_a_record_too(snippet):
    text = "- do the thing (issue #1252, rant 2026-08-01T10:00:00)\n"
    assert snippet.problems(sites_of(snippet, text)) == []


def test_each_citation_word_needs_its_own_record(snippet):
    """One record anchors only the first of two citations on a line.

    Measured case: `journal_prompt.md:461` carries two rants. The discriminating
    input is *one* record against two words - with no record, or with two, the
    counting rule and a naive `records > 0` agree, so neither pins the decision.
    """
    none = "- a (rant 2026-09-10T11:28:43) then b (rant 2026-09-02T20:24:48)\n"
    one = ("- a (PR #1116, rant 2026-09-10T11:28:43) then "
           "b (rant 2026-09-02T20:24:48)\n")
    two = ("- a (PR #1116, rant 2026-09-10T11:28:43) then "
           "b (PR #1111, rant 2026-09-02T20:24:48)\n")
    assert len(snippet.problems(sites_of(snippet, none))) == 1
    assert "2 public records" in snippet.problems(sites_of(snippet, none))[0]
    found = snippet.problems(sites_of(snippet, one))
    assert len(found) == 1, f"one record satisfied two citation words: {found}"
    assert "with 1 public record(s)" in found[0]
    assert snippet.problems(sites_of(snippet, two)) == []


def test_a_second_timestamp_under_one_word_is_a_run_not_a_second_word(snippet):
    """`rant ts1 + ts2` is one citation word; one record covers the block."""
    text = "- hygiene (PR #941, rant 2026-08-23T08:04:26 + 2026-08-28T22:12:16)\n"
    site = sites_of(snippet, text)[0]
    assert site.words == 1
    assert site.timestamps == ["2026-08-23T08:04:26", "2026-08-28T22:12:16"]
    assert snippet.problems([site]) == []


def test_the_word_is_a_named_group_so_a_caller_can_repeat_it(snippet):
    """`group(0)` is the whole match, timestamp included.

    A sweep that repeated `group(0)` beside a second timestamp duplicated the first
    one (measured 2026-09-16); the named group is what makes that unrepresentable.
    """
    m = snippet.CITATION.search("(rants 2026-08-01T10:00:00)")
    assert m.group("word") == "rants"
    assert m.group("ts") == "2026-08-01T10:00:00"
    assert m.group(0) != m.group("word")


# --- the spellings that are not sites of their own -----------------------------


def test_a_date_less_time_is_reported_and_does_not_crash(snippet):
    text = "- x (PR #957, Rants 2026-08-24T15:27:37 + 11:00:31)\n"
    found = snippet.problems(sites_of(snippet, text))
    assert len(found) == 1
    assert "11:00:31" in found[0] and "expand it" in found[0]


@pytest.mark.parametrize("text, expected", [
    # minute-truncated, no record: a site, reported, not a crash.
    ("- x (rant 2026-08-24T17:50)\n", 1),
    # minute-truncated, with a record: clean.
    ("- x (PR #959, rant 2026-08-24T17:50)\n", 0),
])
def test_a_minute_truncated_spelling_is_still_a_site(snippet, text, expected):
    site = sites_of(snippet, text)[0]
    assert site.timestamps == ["2026-08-24T17:50"]
    assert len(snippet.problems([site])) == expected


def test_a_timestamp_not_introduced_by_the_word_is_not_a_citation(snippet):
    """system.j2's memory-format example must stay out of scope."""
    text = "event_at: 2026-01-15T14:30:00\ntype: project\n"
    assert sites_of(snippet, text) == []
    assert snippet.problems(sites_of(snippet, text)) == []


def test_a_word_far_from_a_timestamp_is_not_a_citation(snippet):
    """The word must *introduce* the timestamp, not merely share its line.

    Prose uses the word ("the rant about cadence") and prose also contains dates;
    a window wide enough to bridge them turns every such sentence into a citation
    demanding a record. Three characters is the measured window (`rant 2026-…`,
    `rants\\n  2026-…`).
    """
    text = "- the rant about cadence is long, and a date like 2026-01-15T14:30:00\n"
    assert sites_of(snippet, text) == []


def test_a_wrapped_parenthetical_is_one_site(snippet):
    """The block's parentheses keep it one site - in both directions.

    The class writes a record on the opening line to cover a timestamp on the
    closing one, so both halves must be measured: a **record on the last line** has
    to count (a line-at-a-time scan sees neither it nor the word), and a
    **timestamp on the last line** has to be seen (otherwise the site looks
    resolved while a citation in it was never read).
    """
    record_last = "- x (rant 2026-08-01T10:00:00 — the\n  reason, see PR #123)\n"
    ts_last = ("- x (PR #123, rant 2026-08-01T10:00:00 — the\n"
               "  and also 2026-08-02T00:00:00)\n")
    bare = "- x (rant 2026-08-01T10:00:00 — the\n  reason for it)\n"

    assert snippet.problems(sites_of(snippet, record_last)) == []
    site = sites_of(snippet, ts_last)[0]
    assert site.timestamps == ["2026-08-01T10:00:00", "2026-08-02T00:00:00"]
    found = snippet.problems(sites_of(snippet, bare))
    assert len(found) == 1 and "2026-08-01T10:00:00" in found[0]


# --- the frozen debt -----------------------------------------------------------


def test_debt_is_only_for_the_host_owned_file(snippet):
    """Debt is "the caller must fix this", so it may only name the caller's file."""
    text = "- x (rant 2026-08-01T10:00:00)\n"
    snippet.DEBT[(OTHER, "2026-08-01T10:00:00")] = "not host-owned"
    found = snippet.problems(sites_of(snippet, text))
    assert any("is not in the host-owned file" in line for line in found), found
    assert not any("add a public record" in line for line in found), \
        "an entry outside the host-owned file silenced a real violation"


def test_a_site_is_exempt_only_if_every_timestamp_is_debt(snippet):
    """`any` instead of `all` would exempt a section by citing one frozen rant."""
    frozen, live = "2026-08-07T10:17:27", "2026-01-01T00:00:00"
    snippet.DEBT[(HOST_OWNED, frozen)] = "invented for this test"

    mixed = sites_of(snippet, f"- x (rant {frozen} + {live})\n", HOST_OWNED)[0]
    assert mixed.exempt() is False, "one frozen timestamp exempted the whole site"
    assert any("add a public record" in line
               for line in snippet.problems([mixed]))

    # The other direction: with *every* timestamp frozen the site is exempt, so the
    # rule does not report the one file routine evolution must not edit.
    snippet.DEBT[(HOST_OWNED, live)] = "invented for this test"
    whole = sites_of(snippet, f"- x (rant {frozen} + {live})\n", HOST_OWNED)[0]
    assert whole.exempt() is True
    assert snippet.problems([whole]) == []


def test_a_stale_debt_entry_is_reported(snippet):
    """A debt list that cannot shrink grows until it means "everything"."""
    snippet.DEBT[(HOST_OWNED, "2026-01-01T00:00:00")] = "invented for this test"
    found = snippet.problems(sites_of(snippet, "- nothing cited here\n", HOST_OWNED))
    assert any("stale debt entry" in line for line in found), found


def test_the_host_owned_file_is_in_the_scanned_class(mod):
    """The debt scope check only means something if the file is scanned at all."""
    assert HOST_OWNED in mod.INSTRUCTION_FILES


# --- the tree, and the exit-code contract --------------------------------------


def test_the_real_tree_has_no_unresolved_citation(mod):
    sites, missing = mod.scan_tree(REPO_ROOT)
    assert missing == [], f"unmeasurable: {missing}"
    assert mod.problems(sites) == []
    # This assertion was the opposite until 2026-09-16: it required the real tree to
    # carry *some* frozen-debt site (`any(s.exempt())`). The host's ruling on issue
    # #1252 narrowed the red line to the running copy of the template, the repository
    # copy was swept, and the debt list was emptied - so the claim that can now fail
    # is the one worth asserting: nothing in this tree is exempt. An entry quietly
    # added back would exempt its site and make this red, while `problems` alone
    # would stay green (that is exactly what an entry is for).
    exempt = [s.key for s in sites if s.exempt()]
    assert exempt == [], f"a site is exempt from naming a record: {exempt}"
    assert mod.DEBT == {}, "the debt list is empty; a new entry needs the host's call"


def test_a_missing_file_is_unmeasurable_not_a_pass(mod, tmp_path, capsys):
    """`2`, never `0`: a guard that cannot measure must not report health."""
    mod.REPO_ROOT = tmp_path
    mod.INSTRUCTION_FILES = ("not-here.md",)
    assert mod.main([]) == 2
    assert "unmeasurable" in capsys.readouterr().err


def test_the_script_reports_ok_on_the_real_tree_in_a_subprocess():
    """The CI-visible contract: the canonical command exits 0 on this tree."""
    out = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT,
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith("tree: "), out.stdout
    assert "OK:" in out.stdout
