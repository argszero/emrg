"""The no-background-process red line must reach every session, not only evolution cycles.

Why this file exists
--------------------
The host's rule — never start a background process — was measured on 2026-09-28 (cycle
`cyc20260928-070538`, master `e07aa3da`) and found to be carried by **one project's
memory and by no shipped prompt at all**:

| carrier | statements |
|---|---|
| `emrg/server/prompts/system.j2` | 0 |
| `emrg/server/evolution_prompt.md` | 0 |
| every other root under `~/.emrg/evolution/*/.emrg/` (memory + session memory) | 0 |
| this project's own memory | 2 project + 3 session hits |

Memory is per-project and per-session, so an instance running another project's task, on
another machine, is not reachable by a file in this one's `.emrg/memory/`. The rule has to
sit where every render passes, and `system.j2` is that place: `daemon._build_system_prompt`
is its single render site — for a host conversation exactly as for a scheduled task. The
host stated the rule twice within a minute, the second time as 「在演化过程中，也禁止跑后台任务」,
because the temptation lives precisely in a cycle that has gates to run and a window to fill.

This is the same defect shape `tests/test_language_policy_reach.py` pins for the language
policy: a rule that governs every actor, stated in carriers some actors never open. The fix
is placement. `evolution_prompt.md` §Forbidden restates the rule for a cycle's own reader
and is pinned by `tests/test_evolution_prompt_red_lines.py`; this file pins the carrier
every session shares.

Named limit
-----------
This pins the *presence* of the rule in the artifact a session receives, not obedience to
it — no test can watch an agent decide not to background a command. What it does keep true
is the placement the rule depends on: one render site, present in the render and not merely
in the file. A future session that reads the rule and backgrounds a command anyway is a
model-behaviour failure, not one this file can see.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section's identity in `system.j2`. Written as the heading line, so a second copy
#: appended anywhere in the template is visible as a count of 2.
RED_LINE_HEADING = "## ⛔ Host red line — no background processes (host, 2026-09-28)"

#: Load-bearing terms of the statement. Each is a verbatim substring of the shipped
#: wording, so this list cannot drift away from the sentence it checks without this file
#: saying so. One term per shape the prohibition names, so the clause cannot be softened
#: into a general warning about caution; then what to do instead, the failure it names,
#: and the permanence it claims.
RED_LINE_TERMS = (
    "Never start a background process",       # the rule itself
    "No `&`, no `nohup`, no `disown`, no `setsid`",  # every shell shape, in one run
    "detached child",                         # the launcher shape
    "one at a time, with a generous `timeout`",      # what to do instead
    "the turn dies with its task attached",   # the failure, so the rule is not folklore
    "no cycle may gate, skip or trade it away",
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


def _missing_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this text does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in text]


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment`
    could differ in `trim_blocks` / `lstrip_blocks`, in the loader path, or in
    `autoescape`, and the prompt under test would then be a prompt nobody receives. The
    context is deliberately minimal: the block sits outside every conditional, so no
    context key can remove it, and a render that loses it because of a *context*
    difference is what this checks for.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_no_background_red_line() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), RED_LINE_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the no-background-process red line — it "
        "is the only prompt every session is rendered under, and the rule lived in one "
        "project's memory only"
    )
    missing = _missing_terms(block, RED_LINE_TERMS)
    assert not missing, f"the red-line section must name every shape; missing: {missing}"


def test_the_rendered_prompt_carries_the_rule_not_merely_the_template() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Reading the file answers "is the rule in `system.j2`?" — one level short of this
    file's question, "is it in the prompt a session runs under?". A block can be in the
    file and in no render: wrap it in `{% if false %}` and the file still carries every
    term while the prompt carries none, and an autoescaping environment renders ``&`` as
    ``&amp;``, which no reader reads as shell syntax. Measuring the render is also the
    only way the "every session" claim is checked rather than asserted, since there is
    no session here.
    """
    rendered = _render_system_prompt()
    block = _block_after(rendered, RED_LINE_HEADING)
    assert block, (
        "the rendered session prompt must carry the no-background-process red line: it is "
        "present in system.j2 but no render a session receives contains it"
    )
    missing = _missing_terms(block, RED_LINE_TERMS)
    assert not missing, f"the rendered red-line section is missing terms: {missing}"
    found = rendered.count(RED_LINE_HEADING)
    assert found == 1, (
        f"the rendered prompt must state the rule once; found {found} copies — a "
        "duplication costs prompt and leaves two copies free to drift apart"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the section must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This
    feeds the extractors a prompt that has no such block and requires the absence to be
    visible, and a prompt carrying the heading twice and requires the duplication to be
    visible.
    """
    sample = "# A prompt with no red line\n\n- Must push\n"
    assert _block_after(sample, RED_LINE_HEADING) == ""
    assert _missing_terms(sample, RED_LINE_TERMS) == list(RED_LINE_TERMS)
    doubled = f"{RED_LINE_HEADING}\n\n{RED_LINE_HEADING}\n\n- Must push\n"
    assert doubled.count(RED_LINE_HEADING) == 2
