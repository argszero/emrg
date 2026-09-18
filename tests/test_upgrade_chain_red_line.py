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

#: Terms of the 附则三 clause in the shipped evolution template's `### Forbidden` list.
TEMPLATE_TERMS = (
    "triggers the real auto-upgrade chain",   # the rule itself
    "UpgradeManager.tick()",                  # the in-process route
    "install/version.txt",                    # the file the chain must not touch
    "emrg-upgrade",                           # the session it must not write
    "_guard_upgrade_hermeticity",             # the mechanical half, named
    "第四条附则三",                            # provenance
    PERMANENCE,
)


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
    """The template's `### Forbidden` section, up to the next `###` heading."""
    mark = "### Forbidden"
    start = text.find(mark)
    assert start != -1, "the template has no `### Forbidden` section"
    rest = text[start + len(mark):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _bullet_lines(section: str) -> list[str]:
    """The section's list items — the lines a reader scans for a rule of this class."""
    return [line for line in section.splitlines() if line.startswith("- ")]


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
    missing = _missing_terms(section, TEMPLATE_TERMS)
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
        ("evolution_prompt.md", template_text, TEMPLATE_TERMS),
    ):
        for route in ("UpgradeManager.tick()", "install/version.txt", "emrg-upgrade"):
            assert route in text, (
                f"{name} must name the route {route!r} itself, not only point at "
                "MANIFESTO.md"
            )
        assert not _missing_terms(text, terms), (
            f"{name} must state the 附则三 red line in full"
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
                          TEMPLATE_TERMS) == list(TEMPLATE_TERMS)


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

