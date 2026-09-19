"""The language policy must reach every session, not only the templates that restate it.

Why this file exists
--------------------
The project has a rule about the language of outward-facing output: GitHub-facing text is
written in **English** (PR titles and bodies, review comments, issue replies, commit
messages, community participation), quotes stay verbatim, and internal artifacts — memory
entries, session notes, cycle records — are exempt and may stay in the author's language.

Measured 2026-09-19 (`cyc20260919-114441`) on master `dd2a0e64`, that rule was carried by
task templates and by nothing else:

| carrier | statements |
|---|---|
| `evolution_prompt.md` | 2 (`### 🌐 Language Policy (global, applies to every cycle)`) |
| `journal_prompt.md` | 2 |
| `open_source_prompt.md` | 2 |
| `promote_prompt.md` | 1, a citation — `--title "<English title>"` (language policy) |
| `competition_prompt.md` | 0 |
| `paper_prompt.md` | 0 |

and `emrg/server/prompts/system.j2` — the one prompt **every** session is rendered under,
host conversation and scheduled task alike — carried none. So a `competition` or `paper`
session was never told the rule at all, and a host conversation that opens a PR on the
host's behalf was told it only if the model happened to infer it.

That is the same defect shape as the one `tests/test_upgrade_chain_red_line.py` pins one
class over: a rule that governs every actor, stated in carriers that some actors never
open. The fix is placement, not more copies — the block now sits in `system.j2`, where one
statement serves every render.

Named limit
-----------
This pins the *presence* of the rule in the artifact a session receives, not the language
of any output produced under it — no test can read a pull-request body and judge its
language. What it does keep true is the placement the rule depends on: one render site, one
block, present in the render rather than only in the file. A future session that reads a
readable prompt and writes Chinese anyway is a model-behaviour failure, not one this file
can see.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The block's identity in `system.j2`. Written as the heading line, so a second copy
#: appended anywhere in the template is visible as a count of 2.
POLICY_HEADING = "## Language Policy — outward-facing output"

#: Load-bearing terms of the statement. Each is a verbatim substring of the shipped
#: wording, so the list cannot drift away from the sentence it checks without this file
#: saying so. They cover both halves: what must be English, and what is exempt.
POLICY_TERMS = (
    "Outward-facing GitHub output is written in English",  # the rule itself
    "PR titles and PR bodies",                            # the outward artifacts
    "review comments",                                    # the case a review cycle writes
    "Quotes stay verbatim",                               # quoting a Chinese rant
    "Internal artifacts are exempt",                      # the half that keeps memory Chinese
    "`.emrg/memory/`",                                    # where the exempt artifacts live
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
    could differ in `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt
    under test would then be a prompt nobody receives. The context is deliberately
    minimal: the block sits outside every conditional, so no context key can remove it,
    and a render that loses it because of a *context* difference is what this checks for.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_language_policy() -> None:
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), POLICY_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the language-policy block — it is the "
        "only prompt every session is rendered under, and the task templates restate the "
        "rule unevenly (competition and paper state it nowhere)"
    )
    missing = _missing_terms(block, POLICY_TERMS)
    assert not missing, f"the language-policy block must state both halves; missing: {missing}"


def test_the_rendered_prompt_carries_the_policy_not_merely_the_template() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Reading the file answers "is the rule in `system.j2`?" — one level short of this
    file's question, "is it in the prompt a session runs under?". A block can be in the
    file and in no render: wrap it in `{% if false %}` and the file still carries every
    term while the prompt carries none. Measuring the render is also the only way the
    "every session" claim is checked rather than asserted, since there is no session here.
    """
    rendered = _render_system_prompt()
    block = _block_after(rendered, POLICY_HEADING)
    assert block, (
        "the rendered session prompt must carry the language-policy block: it is present "
        "in system.j2 but no render a session receives contains it"
    )
    missing = _missing_terms(block, POLICY_TERMS)
    assert not missing, f"the rendered language-policy block is missing terms: {missing}"
    found = rendered.count(POLICY_HEADING)
    assert found == 1, (
        f"the rendered prompt must state the policy once; found {found} copies — a "
        "duplication costs prompt and leaves two copies free to drift apart"
    )


def test_this_template_has_one_render_site_so_the_policy_reaches_every_session() -> None:
    """The claim "every session receives it" rests on there being one builder to reach.

    `_build_system_prompt` builds the system message for the tool loop, and the tool loop
    is the single entry point both a host request and a headless scheduled task take. If a
    second render site of `system.j2` appeared, the block could be present in the prompt
    this file measures and absent from the prompt half the sessions receive — so the count
    is pinned rather than assumed.

    Named limit: this counts render sites of *this* template. A second builder that
    assembles a system message without `system.j2` is outside what a scan for the template
    name can see.
    """
    pattern = re.compile(r'get_template\(\s*["\']system\.j2["\']\s*\)')
    hits: list[Path] = []
    for path in sorted((REPO_ROOT / "emrg").rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            hits.append(path.relative_to(REPO_ROOT))
    assert hits == [Path("emrg/server/daemon.py")], (
        "system.j2 must have exactly one render site (emrg/server/daemon.py, inside "
        f"`_build_system_prompt`), so the language policy reaches every session; found: {hits}"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the block must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This
    feeds the extractors a prompt that has no such block and requires the absence to be
    visible, and a prompt carrying the heading twice and requires the duplication to be
    visible.
    """
    sample = "# A prompt with no language policy\n\n- Must push\n"
    assert _block_after(sample, POLICY_HEADING) == ""
    assert _missing_terms(sample, POLICY_TERMS) == list(POLICY_TERMS)
    doubled = f"{POLICY_HEADING}\n\n{POLICY_HEADING}\n\n- Must push\n"
    assert doubled.count(POLICY_HEADING) == 2
