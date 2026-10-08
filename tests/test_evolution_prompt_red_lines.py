"""The shipped evolution template must state its red lines where a reader looks.

Why this file exists
--------------------
Issue #1324 measured the first gap. The project's highest-priority rule — never write,
restore or introduce anything that stops or restarts the emrg server / `emrgd` —
was enforced in one place and stated in two: `tests/conftest.py` refuses the
in-process, `os.kill` and spawned-child routes, and `MANIFESTO.md` 第四条附则二 plus
this host's session prompt state it in prose. The template that *ships* in a release
and is installed for other instances said nothing about it, so an instance whose task
prompt is built from the shipped template never saw the rule at all.

The second clause was measured the same way, on 2026-09-28 by cycle `cyc20260928-070538`,
and it is the same failure with a wider blast radius. The host's rule *never start a
background process* was in force in **one** project's memory and in **no** template,
so every other evolution instance — a different project's cycle, on a different machine
— carried none of it; two more isomorphic incidents happened on this host the morning
it was measured. Memory is per-project and per-session; the template is the one carrier
that reaches every instance, which is why the rule belongs here.

The fix, both times, is a clause in the template's `### Forbidden` list — the section a
reader looks in for exactly this class of rule. This module pins each clause where it
has to sit, so a later prompt edit cannot drop one in silence.

Named limit
-----------
This pins the *presence* of the clauses, not the behaviour they ask for. The behaviour
is guarded mechanically elsewhere (`_guard_stop_all_hermeticity` and
`_guard_no_live_daemon_is_signalled` in `tests/conftest.py`, whose docstrings carry
the routes they close). What no test can show is that an agent reading the template
obeys it — which is precisely why the clauses exist beside those guards rather than
instead of them. The same reasoning leaves this file responsible for one question
only: is the rule stated where an instance's prompt is built from?

Two levels, two questions
-------------------------
Being stated in the file and reaching the round are **not** the same question:
`evolution_prompt.md` is a Jinja2 template and `TaskHandler._build_evolution_prompt`
sends its **render**. So the section assertions below answer "does the shipped source
carry the clause?", and the render-level ones answer "is this what a cycle is
actually sent?" — neither implies the other. A Jinja construct (`{# … #}`,
`{% if false %}`) removes text from every round's prompt while the file keeps every
character, and a file read cannot see that at all: measured 2026-10-08
(`cyc20261008-084957`), wrapping the two `### Forbidden` red-line clauses in
`{# … #}` left all nine file-level assertions green and dropped the clauses from the
round's prompt entirely — `scripts/run-mutation-arm.py` reported `SURVIVED`. This is
the same shape fixed for `emrg/server/prompts/vibe_check.j2` (issue #1902) and
`emrg/server/competition_prompt.md` (issue #1904); `emrg/server/prompts/system.j2`
already carries its render-level leg in `test_no_background_process_reach.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from tests.task_handler_factory import make_handler

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: The first clause's own opening words, used where the question is *where* it sits
#: rather than which routes it names.
CLAUSE = "stops or restarts the emrg server"

#: The load-bearing terms of the first clause, one per route the red line names. Each
#: is a verbatim substring of the shipped wording, so this list cannot drift away from
#: the sentence it is checking without the test saying so.
REQUIRED_TERMS = (
    CLAUSE,                                # the rule itself
    "`stop_all()`",                        # the in-process route
    "`stop_daemon()`",                     # the other in-process route
    "`emrg server stop`",                  # the CLI route
    "`emrg server restart`",               # the other CLI route
    "not subject to any evolution mechanism",
)

#: The second clause's opening words, same role as `CLAUSE` above.
BACKGROUND_CLAUSE = "Never start a background process"

#: The load-bearing terms of the second clause: one per shape the prohibition names
#: (so the clause cannot be softened to a general warning), plus the sequencing it
#: prescribes and the permanence it claims. Each is a verbatim substring of the
#: shipped wording.
BACKGROUND_REQUIRED_TERMS = (
    BACKGROUND_CLAUSE,                     # the rule itself
    "No `&`, no `nohup`, no `disown`, no `setsid`",  # every shell shape, in one run
    "detached `subprocess`",               # the launcher shape
    "foreground, one at a time",           # what to do instead
    "no cycle may gate, skip or trade it away",
)

#: Every clause this file pins, as `(id, clause, terms)`. The clause string is the
#: one-token probe for *duplication*; the terms are the probe for *presence*.
RED_LINES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("daemon-stop-restart", CLAUSE, REQUIRED_TERMS),
    ("no-background-process", BACKGROUND_CLAUSE, BACKGROUND_REQUIRED_TERMS),
)


def _forbidden_section(text: str) -> str:
    """The template's `### Forbidden` section, up to the next `###` heading."""
    mark = "### Forbidden"
    start = text.find(mark)
    assert start != -1, "the template has no `### Forbidden` section"
    rest = text[start + len(mark):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _missing_terms(
    section: str, terms: tuple[str, ...] = REQUIRED_TERMS
) -> list[str]:
    """The required terms this section does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in section]


@pytest.mark.parametrize(
    "name, terms",
    [(name, terms) for name, _clause, terms in RED_LINES],
    ids=[name for name, _clause, _terms in RED_LINES],
)
def test_the_forbidden_section_states_each_red_line(
    name: str, terms: tuple[str, ...]
) -> None:
    section = _forbidden_section(TEMPLATE.read_text(encoding="utf-8"))
    missing = _missing_terms(section, terms)
    assert not missing, (
        f"emrg/server/evolution_prompt.md §Forbidden must state the {name} red line "
        f"(missing: {missing}) — an instance whose task prompt is built from this "
        "template sees nothing else"
    )


@pytest.mark.parametrize(
    "name, clause, terms",
    RED_LINES,
    ids=[name for name, _clause, _terms in RED_LINES],
)
def test_each_rule_is_not_stated_a_second_time_outside_the_forbidden_section(
    name: str, clause: str, terms: tuple[str, ...]
) -> None:
    """One clause, in the section a reader looks in.

    A duplicate elsewhere in the same template is a second copy of the rule that can
    drift away from this one — the two would then need reconciling rather than
    reading — so the count is asserted rather than left to whoever edits the prompt
    next.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert _missing_terms(_forbidden_section(text), terms) == []
    assert text.count(clause) == 1, (
        f"the {name} red line is stated once, in §Forbidden — a second copy is a copy "
        "that can drift"
    )


def test_the_checks_can_report_absence() -> None:
    """The instrument's control: a section without the clauses must read as missing.

    A check that reports the healthy answer whatever it is given is not a check; this
    feeds it a template that has a `### Forbidden` section and none of the clauses, and
    requires every term of every clause to come back missing.
    """
    for _name, _clause, terms in RED_LINES:
        assert _missing_terms("### Forbidden\n\n- Must push\n", terms) == list(terms)


def test_a_template_without_a_forbidden_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _forbidden_section("# A template with no Forbidden section")


# --- The second level: the prompt a cycle is actually sent -----------------------------------
#
# The assertions above read the file. `evolution_prompt.md` is rendered before it reaches a
# round, so a Jinja construct can take a clause out of every round while the file keeps it
# whole — the same defect as issues #1902 and #1904, in a third carrier.


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """The evolution prompt as a round receives it, rendered by the real builder.

    `TaskHandler._build_evolution_prompt` is the only thing that turns this template into
    a prompt, so it is what these tests render through: a fresh `jinja2.Environment` here
    could differ in `trim_blocks`, `lstrip_blocks`, `undefined` or the loader, and the
    artifact under test would then be a prompt no cycle is ever sent.

    The context is the one a real records-driven call produces (a project and nothing
    else); `config_dir` is redirected at a tmp tree — the shape
    `tests/test_evolution_prompt_index_rule.py` uses — so the `projects.yml` read lands on
    a file this test made, never on the host's `~/.emrg`.
    """
    tmp_path = tmp_path_factory.mktemp("red-lines")
    project_dir = tmp_path / "demoproj"
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "demoproj", "path": str(project_dir)}]), encoding="utf-8"
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name="demo-task",
            config={"project": "demoproj"},
            interval=300,
            identity=InstanceIdentity(),
            template_path=TEMPLATE,
        )
        return handler._build_evolution_prompt()
    finally:
        mod.config_dir = original


@pytest.mark.parametrize(
    "name, terms",
    [(name, terms) for name, _clause, terms in RED_LINES],
    ids=[name for name, _clause, _terms in RED_LINES],
)
def test_each_red_line_reaches_the_round_and_not_merely_the_file(
    rendered: str, name: str, terms: tuple[str, ...]
) -> None:
    """Every load-bearing term of each clause, asserted on the prompt a cycle is sent.

    The file-level test above cannot see this failure: a Jinja construct around the
    clause leaves every term in `evolution_prompt.md` and drops all of them from the
    render, so the rule the host asked to be stated *where an instance's prompt is built
    from* stops reaching the instance while the guard stays green. Measured 2026-10-08
    (`cyc20261008-084957`): wrapping the two clauses in `{# … #}` left the file-level
    assertions green and `run-mutation-arm.py` reported `SURVIVED` for exactly that arm.
    """
    missing = _missing_terms(rendered, terms)
    assert not missing, (
        f"the rendered evolution prompt does not carry the {name} red line (missing: "
        f"{missing}): the rule is in the file and in no render, which is the one failure "
        "a file-level guard cannot see — a Jinja construct (`{# … #}`, `{% if false %}`) "
        "removes it from every round's prompt while the file keeps every term"
    )


def test_the_render_substitutes_values_and_is_not_a_file_read(rendered: str) -> None:
    """The converse: a render assertion must not be a file read wearing a render's name.

    Two ways the test above would go vacuous if this were unchecked, both closed here.
    Were the fixture to hand back the file's text — or the terms read with their Jinja
    syntax intact, as `{% raw %}` or a copied-out literal would leave them — the round
    would be sent `{{ task.project }}` and told nothing, and the presence assertions
    above would still pass on the file's own characters. So the render must carry no
    template syntax **and** must carry the value a placeholder resolved to, which is a
    reading neither the file nor an unresolved render can produce.
    """
    assert "{{" not in rendered and "{%" not in rendered, (
        "the rendered evolution prompt still carries Jinja syntax — the round is sent "
        "template source rather than the prompt it names"
    )
    assert "demoproj" in rendered, (
        "the render does not carry the project name the record configured: "
        "`{{ task.project }}` resolved to nothing, so the round is told its project is "
        "blank and this test is reading a file rather than a render"
    )
