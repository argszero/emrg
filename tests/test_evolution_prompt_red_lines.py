"""The shipped evolution template must state the daemon stop/restart red line.

Why this file exists
--------------------
Issue #1324 measured the gap. The project's highest-priority rule — never write,
restore or introduce anything that stops or restarts the emrg server / `emrgd` —
was enforced in one place and stated in two: `tests/conftest.py` refuses the
in-process, `os.kill` and spawned-child routes, and `MANIFESTO.md` 第四条附则二 plus
this host's session prompt state it in prose. The template that *ships* in a release
and is installed for other instances said nothing about it, so an instance whose task
prompt is built from the shipped template never saw the rule at all.

The fix is a clause in the template's `### Forbidden` list — the section a reader
looks in for exactly this class of rule. This module pins that clause where it has to
sit, so a later prompt edit cannot drop it in silence.

Named limit
-----------
This pins the *presence* of the clause, not the behaviour it asks for. The behaviour
is guarded mechanically elsewhere (`_guard_stop_all_hermeticity` and
`_guard_no_live_daemon_is_signalled` in `tests/conftest.py`, whose docstrings carry
the routes they close). What no test can show is that an agent reading the template
obeys it — which is precisely why the clause exists beside those guards rather than
instead of them. The same reasoning leaves this file responsible for one question
only: is the rule stated where an instance's prompt is built from?
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: The clause's own opening words, used where the question is *where* it sits rather
#: than which routes it names.
CLAUSE = "stops or restarts the emrg server"

#: The load-bearing terms of the clause, one per route the red line names. Each is a
#: verbatim substring of the shipped wording, so this list cannot drift away from the
#: sentence it is checking without the test saying so.
REQUIRED_TERMS = (
    CLAUSE,                                # the rule itself
    "`stop_all()`",                        # the in-process route
    "`stop_daemon()`",                     # the other in-process route
    "`emrg server stop`",                  # the CLI route
    "`emrg server restart`",               # the other CLI route
    "not subject to any evolution mechanism",
)


def _forbidden_section(text: str) -> str:
    """The template's `### Forbidden` section, up to the next `###` heading."""
    mark = "### Forbidden"
    start = text.find(mark)
    assert start != -1, "the template has no `### Forbidden` section"
    rest = text[start + len(mark):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _missing_terms(section: str) -> list[str]:
    """The required terms this section does not carry. Empty means the rule is stated."""
    return [term for term in REQUIRED_TERMS if term not in section]


def test_the_forbidden_section_states_the_daemon_stop_restart_rule() -> None:
    section = _forbidden_section(TEMPLATE.read_text(encoding="utf-8"))
    missing = _missing_terms(section)
    assert not missing, (
        "emrg/server/evolution_prompt.md §Forbidden must state the permanent daemon "
        f"stop/restart red line (MANIFESTO.md 第四条附则二, PR #854); missing: {missing}"
    )


def test_the_rule_is_not_stated_a_second_time_outside_the_forbidden_section() -> None:
    """One clause, in the section a reader looks in.

    A duplicate elsewhere in the same template is a second copy of the rule that can
    drift away from this one — the two would then need reconciling rather than
    reading — so the count is asserted rather than left to whoever edits the prompt
    next.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert _missing_terms(_forbidden_section(text)) == []
    assert text.count(CLAUSE) == 1, (
        "the red line is stated once, in §Forbidden — a second copy is a copy that can "
        "drift"
    )


def test_the_checks_can_report_absence() -> None:
    """The instrument's control: a section without the clause must read as missing.

    A check that reports the healthy answer whatever it is given is not a check; this
    feeds it a template that has a `### Forbidden` section and none of the clause, and
    requires every term to come back missing.
    """
    assert _missing_terms("### Forbidden\n\n- Must push\n") == list(REQUIRED_TERMS)


def test_a_template_without_a_forbidden_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _forbidden_section("# A template with no Forbidden section")
