"""Both prompt carriers must state the permanent auto-upgrade red line.

Why this file exists
--------------------
An outside review of PR #1403 measured this: the project has **two** permanent red
lines, and only one of them is stated where the agents that must obey them read.

`MANIFESTO.md` 第四条附则三 — never write, restore or introduce anything that triggers
the real auto-upgrade chain (a real GitHub releases request, a real read or write of
`~/.emrg/install/version.txt`, a real `emrg-upgrade` session write, `UpgradeManager.tick()`
reached unisolated) — is enforced mechanically in `tests/conftest.py` by the autouse
fixture `_guard_upgrade_hermeticity`, whose docstring carries the incident. It is stated
in `MANIFESTO.md`. It was stated in neither of the two prompts that drive an agent:

* `emrg/server/prompts/system.j2` — the **host session** prompt, rendered by the daemon.
  It carries the sibling rule (附则二, the daemon stop/restart red line) as a ⛔ block and
  nothing for 附则三, so the instance executing a cycle was told about one permanent red
  line in its own prompt and the other only if the project it happened to be working on
  carries `MANIFESTO.md`. A task whose working directory is another project does not.
* `emrg/server/evolution_prompt.md` — the template that ships in a release and is
  installed for other instances. Its `### Forbidden` list is where a reader looks for
  exactly this class of rule.

That asymmetry is the defect: a rule that governs every actor, stated only in a carrier
no other actor is guaranteed to open. `MANIFESTO.md` is a project-context file, read out
of the **session cwd** — it is present for this repo's tasks and absent for every other
project's.

A third class: the six task templates, not only the evolution one
---------------------------------------------------------------
Both carriers named above are one template and one session prompt. The scheduler ships
**six** templates — `competition`, `evolution`, `journal`, `open_source`, `paper`,
`promote` — and each is the instruction half of its own kind of session. A task cycle
reads its own template, and the template's `### Forbidden` list is where a reader looks
for a rule of exactly this class, which is why the templates restate the shared rules:
the sibling `~/.emrg/config.toml` prohibition is in all six. Measured at master
`1716a630` (`cyc20260919-105421`): both permanent red lines were in **one** of the six
(`evolution_prompt.md`); the other five matched neither `附则二` nor `附则三`. So a
journal, paper, promote, competition or open_source cycle read the two permanent rules
only where its session prompt happened to carry them — the original asymmetry of this
file, five carriers over. Duplication is forced here: `TaskHandler._build_evolution_prompt`
renders with `jinja2.Environment(undefined=Undefined).from_string(...)`, which has no
loader, so no `{% include %}` can share one copy. That makes drift the live risk — five
hand-copied clauses are five places to reword three of them — so this file pins one term
set for all six carriers and one wording for the five task templates.

Named limit
-----------
This pins the *presence* of both statements, not the behaviour they ask for. The
behaviour is guarded mechanically by `_guard_upgrade_hermeticity` (and, for the sibling
rule, `_guard_stop_all_hermeticity` / `_guard_no_live_daemon_is_signalled`). What no test
can show is that an agent reading a prompt obeys it — which is why these clauses exist
beside those guards rather than instead of them. One question is left to this file: is
each rule stated where its readers build from?
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"
EVOLUTION_TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: Every task template the scheduler can be configured with — the class, not the instance.
#: Taken from the directory rather than a literal list so a new task type is a new carrier
#: the moment its template lands (the deficit this file exists for was "five carriers were
#: never asked"); the count is asserted below so the glob cannot silently go empty.
TASK_TEMPLATES = sorted((REPO_ROOT / "emrg" / "server").glob("*_prompt.md"))

#: The heading of the 附则三 block in the host session prompt. The block runs to EOF.
UPGRADE_HEADING = "## ⛔ 最高原则·永久（宿主 2026-08-21 10:35 确立）"

#: The heading of the sibling block, which must survive any edit here.
STOP_HEADING = "## ⛔ 最高原则·永久（宿主 2026-08-18 22:58 确立）"

#: Load-bearing terms of the 附则三 statement in `system.j2`. Each is a verbatim
#: substring of the shipped wording, so this list cannot drift away from the sentence it
#: checks without the test saying so.
UPGRADE_TERMS = (
    "自动升级触发链",                    # the rule itself
    "UpgradeManager.tick()",            # the in-process route
    "install/version.txt",              # the file the chain must not touch
    "emrg-upgrade",                     # the session it must not write
    "_guard_upgrade_hermeticity",       # the mechanical half, named
    "不适用任何演化机制",                # permanent: no evolution mechanism reaches it
    "第四条附则三",                      # provenance
)

#: The verdict of "this is not a choice the system may revise", spelled as the host did.
PERMANENCE = "not subject to any evolution mechanism"

#: Terms of the 附则三 clause every template carrier must state. Written against
#: `evolution_prompt.md`'s wording, which the five task templates now carry too — so this
#: one list answers for all six, and a reword that drops a route from any of them fails
#: here.
SHARED_UPGRADE_TERMS = (
    "triggers the real auto-upgrade chain",   # the rule itself
    "UpgradeManager.tick()",                  # the in-process route
    "install/version.txt",                    # the file the chain must not touch
    "emrg-upgrade",                           # the session it must not write
    "_guard_upgrade_hermeticity",             # the mechanical half, named
    "第四条附则三",                            # provenance
    PERMANENCE,
)

#: Terms of the sibling 附则二 clause every template carrier must state. Narrower than
#: `SHARED_UPGRADE_TERMS` on purpose: `evolution_prompt.md`'s clause names the teardown-as-
#: mocked-with-`test_shutdown_all_*` shape while the five task templates name the two
#: autouse fixtures, and both are truthful — so the shared set is the rule, the entry
#: points, the provenance and the permanence, which every carrier states.
SHARED_STOP_TERMS = (
    "stops or restarts the emrg server",      # the rule itself
    "stop_all()",                             # the in-process route
    "stop_daemon()",                          # the other in-process route
    "emrg server stop",                       # the CLI route (also covers stop/restart)
    "第四条附则二",                            # provenance
    PERMANENCE,
)

#: The mark that identifies each clause's bullet inside a §Forbidden list, used where the
#: question is *which bullets state the rule* rather than which routes they name.
STOP_CLAUSE_MARK = "anything that stops or restarts the emrg server"
UPGRADE_CLAUSE_MARK = "anything that triggers the real auto-upgrade chain"

#: The five carriers that are not `evolution_prompt.md`. They are the ones that were silent
#: before this cycle, and the ones that must therefore agree word for word.
TASK_ONLY_TEMPLATES = tuple(t for t in TASK_TEMPLATES if t.name != "evolution_prompt.md")


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` heading or EOF.

    Returns ``""`` when the heading is absent — the caller asserts, so a missing block
    reads as a failure to measure rather than as a pass.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _forbidden_section(text: str) -> str:
    """The template's §Forbidden section, up to the next `###` heading.

    Found by "the last `##`/`###` heading whose title *ends* with `Forbidden`" rather than
    by the literal `### Forbidden`: `journal_prompt.md` states the section as
    `### 5. Error Handling + Forbidden`, so the literal marker reads that carrier as
    sectionless — a failure to measure, which is the one answer this helper must not give
    for a carrier that does state the rules.

    "Ends with" rather than "contains", so that prose *about* the section is not mistaken
    for it: the control below feeds `# A template with no Forbidden section`, whose heading
    mentions the word mid-sentence and must still read as absent. Level `##` is accepted
    because a carrier is free to promote the section without changing its meaning.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped.startswith(("## ", "### ")) and stripped.endswith("Forbidden"):
            start = index
    assert start is not None, "the template has no `### Forbidden` section"
    rest = "".join(lines[start + 1:])
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _bullet_lines(section: str) -> list[str]:
    """The section's list items — the lines a reader scans for a rule of this class."""
    return [line for line in section.splitlines() if line.startswith("- ")]


def _clause_bullets(section: str, mark: str) -> list[str]:
    """The §Forbidden bullets stating one red line, identified by its opening phrase.

    The mark is the clause's own opening words, so this answers "how many times is the
    rule stated here?" — the count the one-copy property is taken on.
    """
    return [line for line in _bullet_lines(section) if mark in line]


def _missing_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this text does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in text]


def test_the_host_session_prompt_states_the_auto_upgrade_red_line() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), UPGRADE_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the 附则三 block (MANIFESTO.md "
        "第四条附则三, host 2026-08-21T10:35:57) — it is the prompt a host session "
        "actually runs under"
    )
    missing = _missing_terms(block, UPGRADE_TERMS)
    assert not missing, (
        "the 附则三 block in system.j2 must name the rule and the routes it closes; "
        f"missing: {missing}"
    )


def test_the_host_session_prompt_keeps_the_sibling_block_it_had() -> None:
    """The 附则二 block is not to be traded for the new one.

    Nothing pinned this block before — an edit that rewrote the prompt's tail could have
    dropped the daemon stop/restart red line with the suite still green, which is the
    same class of gap this file exists for, one red line over.
    """
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), STOP_HEADING)
    assert block, "system.j2 must keep its 附则二 (daemon stop/restart) block"
    missing = _missing_terms(
        block, ("stop_all()", "emrg server stop/restart", "不适用任何演化机制", "第四条附则二"),
    )
    assert not missing, f"the 附则二 block in system.j2 lost terms it had: {missing}"


#: The clause's opening words in the shipped template, used where the question is
#: *where* it sits rather than which routes it names.
TEMPLATE_CLAUSE_OPEN = "- **Never write, restore or introduce anything that triggers"


def test_the_shipped_evolution_template_states_the_auto_upgrade_red_line() -> None:
    section = _forbidden_section(EVOLUTION_TEMPLATE.read_text(encoding="utf-8"))
    missing = _missing_terms(section, SHARED_UPGRADE_TERMS)
    assert not missing, (
        "emrg/server/evolution_prompt.md §Forbidden must state the permanent auto-upgrade "
        f"red line (MANIFESTO.md 第四条附则三, host 2026-08-21T10:35:57); missing: {missing}"
    )


def test_the_template_clause_is_a_list_item_of_the_forbidden_list() -> None:
    """Stated *as a list item*, not merely present somewhere after its heading.

    `### Forbidden` is the template's last section, so the extractor's window runs to EOF
    and a paragraph appended below the list is inside it — which is why the presence check
    above cannot answer this question on its own. The mutation arm this check was written
    to earn: reflow the clause into a paragraph (drop the leading `- `, leave the text in
    place) → **1 failed, 6 passed**, this test being the single failure.

    Named limit, measured rather than assumed: a bullet *appended* after `- Must push`
    stays a `- ` line inside the same section, and no check here objects. That arm reported
    the healthy answer, so it is recorded as a limit rather than as evidence — the property
    this file cares about is that a reader scanning §Forbidden sees the rule as a rule, and
    a trailing bullet in that section satisfies it.
    """
    section = _forbidden_section(EVOLUTION_TEMPLATE.read_text(encoding="utf-8"))
    bullets = _bullet_lines(section)
    assert bullets, "the §Forbidden section has no list items"
    assert any(line.startswith(TEMPLATE_CLAUSE_OPEN) for line in bullets), (
        "the auto-upgrade clause must be a `- ` item of the §Forbidden list; found it "
        f"only outside the list. First bullet: {bullets[0][:60]!r}"
    )


def test_each_carrier_states_the_rule_in_its_own_language() -> None:
    """Both carriers carry the rule; neither is left to point at the other.

    A cross-reference ("see MANIFESTO.md") is not a statement: the file it points at is a
    project-context file, read from the session cwd, and absent for a task run against
    another project. So the check is that each carrier names the routes itself.
    """
    system_text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    template_text = EVOLUTION_TEMPLATE.read_text(encoding="utf-8")
    for name, text, terms in (
        ("system.j2", system_text, UPGRADE_TERMS),
        ("evolution_prompt.md", template_text, SHARED_UPGRADE_TERMS),
    ):
        for route in ("UpgradeManager.tick()", "install/version.txt", "emrg-upgrade"):
            assert route in text, (
                f"{name} must name the route {route!r} itself, not only point at "
                "MANIFESTO.md"
            )
        assert not _missing_terms(text, terms), (
            f"{name} must state the 附则三 red line in full"
        )


def test_neither_carrier_states_the_rule_twice() -> None:
    """One statement per carrier — the sibling pin's property, carried to this clause.

    `tests/test_evolution_prompt_red_lines.py` (PR #1403, master `d041a948`) pins
    uniqueness for *its* clause — `text.count(CLAUSE) == 1`; this file pinned presence
    only, in both carriers. Measured 2026-09-19 (`cyc20260919-065231`) on the tree where
    both files sit side by side: appending the 附则三 block to `system.j2` a second time
    left **12 passed**, and appending the template clause as a second `- ` bullet after
    `- Must push` likewise left **12 passed**. So the auto-upgrade rule could be stated
    twice in the host's session prompt — prompt cost, and two copies free to drift apart,
    which is the thing #1403's uniqueness test exists to prevent one clause over. The
    heading is a block's identity in `system.j2`, so the count is taken on it; in the
    template, on the clause's opening words.

    Both headings are counted, not just the new one: this file is where the 附则二 block
    in `system.j2` is pinned at all (nothing pinned it before), so its one-copy property
    is pinned here too rather than left to #1403's file, which reads the template only.

    Named limit: this counts statements, not content — a reworded second copy that keeps
    a different opening reads as one statement.
    """
    system_text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for heading, rule in ((UPGRADE_HEADING, "附则三"), (STOP_HEADING, "附则二")):
        found = system_text.count(heading)
        assert found == 1, (
            f"emrg/server/prompts/system.j2 must state the {rule} rule once; found "
            f"{found} copies of its heading"
        )
    section = _forbidden_section(EVOLUTION_TEMPLATE.read_text(encoding="utf-8"))
    found = section.count(TEMPLATE_CLAUSE_OPEN)
    assert found == 1, (
        "the shipped template must state the auto-upgrade clause once in its §Forbidden "
        f"list; found {found}"
    )


def test_the_checks_can_report_absence() -> None:
    """The instrument's control: text without the block must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This
    feeds both extractors a template that has neither the heading nor the clause and
    requires the absence to be visible.
    """
    sample = "# A prompt with no red-line block\n\n- Must push\n"
    assert _block_after(sample, UPGRADE_HEADING) == ""
    assert _missing_terms(sample, UPGRADE_TERMS) == list(UPGRADE_TERMS)
    assert _missing_terms(_forbidden_section(sample + "\n### Forbidden\n\n- Must push\n"),
                          SHARED_UPGRADE_TERMS) == list(SHARED_UPGRADE_TERMS)


def test_a_prompt_without_a_forbidden_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _forbidden_section("# A template with no Forbidden section")


def test_the_rendered_host_prompt_carries_the_rule_not_merely_the_template() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Every check above reads `system.j2` as text, which answers "is the rule in the
    file?" — one level short of this file's own question, "is it in the prompt a host
    session runs under?". A block can be in the file and in no render: wrap it in
    `{% if false %}` and the file still carries every term while the prompt carries
    none. Measured 2026-09-19 (`cyc20260919-060712`) on this branch: with the 附则三
    block wrapped that way, the seven checks above stay green (**7 passed**) and the
    daemon's own environment renders a prompt with the heading, `UpgradeManager.tick()`
    and `install/version.txt` all absent — the 附则二 block, unwrapped, still present.
    The tail of the template is unconditional today (the last `{%` construct sits at
    line 110, both ⛔ blocks at 168 and 172), so that gap is dormant rather than live;
    this check is what keeps it dormant, by asking the renderer instead of the file.

    Rendered through the daemon's own environment (`_get_jinja_env`, which is what
    `_build_system_prompt` uses) rather than a fresh `jinja2.Environment`: a second
    environment could differ in `trim_blocks` / `lstrip_blocks` or in the loader path,
    and the prompt under test would then be a prompt nobody receives. The context is
    deliberately minimal — the blocks sit outside every conditional, so no context key
    can remove them, and a render that loses them because of a *context* difference is
    exactly what this check is for.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    rendered = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )
    block = _block_after(rendered, UPGRADE_HEADING)
    assert block, (
        "the rendered host prompt must carry the 附则三 block: it is present in "
        "system.j2 but no render that a host session receives contains it"
    )
    missing = _missing_terms(block, UPGRADE_TERMS)
    assert not missing, f"the rendered 附则三 block is missing terms: {missing}"
    assert _block_after(rendered, STOP_HEADING), (
        "the rendered host prompt must keep the 附则二 block"
    )
    # The artifact-level half of the one-copy property: a duplication can arrive from a
    # `{% for %}` rather than from a repeated block, in which case the file is written once
    # and every reader still receives it twice.
    assert rendered.count(UPGRADE_HEADING) == 1, (
        "the rendered host prompt must state the 附则三 rule once; found "
        f"{rendered.count(UPGRADE_HEADING)} copies"
    )


def test_every_task_template_states_both_permanent_red_lines() -> None:
    """All six carriers state both rules — the check the five silent ones lacked.

    Every earlier check in this file reads `evolution_prompt.md` or `system.j2`. The five
    other templates are as much a carrier of the rules as those two are, and until this
    cycle nothing asked them: a journal, paper, promote, competition or open_source cycle
    received the two permanent red lines only through `system.j2`, which is a *different
    artifact* — the installed package's session prompt, not the template the source tree
    hands the cycle. That asymmetry is the defect this file was written for, so the
    question is asked of the class, not of one member of it.

    The glob is asserted non-empty first: a `TASK_TEMPLATES` that resolved to no files
    would make the loop below pass over nothing, which is the failure mode this file's
    own control test exists to prevent.
    """
    assert len(TASK_TEMPLATES) == 6, (
        "the six shipped task templates must all be found; found "
        f"{[carrier.name for carrier in TASK_TEMPLATES]}"
    )
    for carrier in TASK_TEMPLATES:
        section = _forbidden_section(carrier.read_text(encoding="utf-8"))
        for rule, terms in (("附则二", SHARED_STOP_TERMS), ("附则三", SHARED_UPGRADE_TERMS)):
            missing = _missing_terms(section, terms)
            assert not missing, (
                f"emrg/server/{carrier.name} §Forbidden must state the permanent {rule} "
                f"red line (MANIFESTO.md 第四条{rule}); missing: {missing}"
            )


def test_every_task_template_states_each_red_line_once() -> None:
    """One statement per carrier, taken over *all six* rather than two.

    `test_neither_carrier_states_the_rule_twice` pins this for `system.j2`'s two blocks and
    for `evolution_prompt.md`'s upgrade clause. The five task templates were pinned for
    neither rule, and they are the carriers most likely to grow a second copy: their
    §Forbidden lists already restate a shared rule (`~/.emrg/config.toml`), so appending
    "the red lines too" a second time is an ordinary-looking edit.
    """
    for carrier in TASK_TEMPLATES:
        section = _forbidden_section(carrier.read_text(encoding="utf-8"))
        for rule, mark in (("附则二", STOP_CLAUSE_MARK), ("附则三", UPGRADE_CLAUSE_MARK)):
            found = len(_clause_bullets(section, mark))
            assert found == 1, (
                f"emrg/server/{carrier.name} §Forbidden must state the {rule} rule once; "
                f"found {found} bullets stating it"
            )


def test_the_five_task_templates_agree_word_for_word() -> None:
    """The duplication that is forced must not become the drift that is optional.

    `TaskHandler._build_evolution_prompt` renders with
    `jinja2.Environment(undefined=Undefined).from_string(...)`, which has no loader — so
    there is no `{% include %}` with which the five task templates could share one copy of
    the clause, and the text is copied five times by construction. The presence check above
    cannot see the consequence: reword any one copy as long as it keeps every load-bearing
    term and that check stays green, while the five carriers now state five variants of the
    same permanent rule — the "two copies free to drift apart" that
    `test_neither_carrier_states_the_rule_twice` refuses one level up. So the wording
    itself is the property: outside `evolution_prompt.md`, whose wording predates this and
    is host-owned, the five must be identical.

    Named limit: this compares the five *to each other*, so a reword applied to all five in
    one edit passes here — correctly, since one wording across the carriers is what is
    asked; the term check above is what keeps such a reword from dropping a route.
    """
    by_wording: dict[str, dict[str, list[str]]] = {}
    for carrier in TASK_ONLY_TEMPLATES:
        section = _forbidden_section(carrier.read_text(encoding="utf-8"))
        for mark in (STOP_CLAUSE_MARK, UPGRADE_CLAUSE_MARK):
            bullets = _clause_bullets(section, mark)
            assert len(bullets) == 1, (
                f"emrg/server/{carrier.name} must state the clause once before its wording "
                f"can be compared; found {len(bullets)}"
            )
            by_wording.setdefault(mark, {}).setdefault(bullets[0], []).append(carrier.name)
    for mark, wordings in by_wording.items():
        assert len(wordings) == 1, (
            f"the five task templates must state the {mark!r} clause in one wording; found "
            f"{len(wordings)}: "
            + "; ".join(f"{names} carry {text[:70]!r}" for text, names in wordings.items())
        )


def test_the_task_template_checks_can_report_absence() -> None:
    """The control for the three checks above, on a section of their own shape.

    The carriers' §Forbidden sections restate the `~/.emrg/config.toml` rule, so a control
    that feeds them "a section with no red line in it" must use exactly that: the config
    rule present, neither red line, `Forbidden` as the section's own title — the shape a
    carrier takes if the clause is deleted. Absence must then be visible to the term
    check, the count check and the wording check alike.
    """
    section = _forbidden_section(
        "### Forbidden\n\n- Do not modify `~/.emrg/config.toml`\n- Must push\n"
    )
    assert _missing_terms(section, SHARED_STOP_TERMS) == list(SHARED_STOP_TERMS)
    assert _missing_terms(section, SHARED_UPGRADE_TERMS) == list(SHARED_UPGRADE_TERMS)
    assert _clause_bullets(section, STOP_CLAUSE_MARK) == []
    assert _clause_bullets(section, UPGRADE_CLAUSE_MARK) == []

