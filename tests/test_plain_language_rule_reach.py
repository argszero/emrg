"""The host's plain-language rule must reach every session, not only one project's memory.

Why this file exists
--------------------
The host has asked, four times in five days, that the text EMRG addresses to them be
understandable without having read the agent's own workspace. Each message below resolves on this
host through `scripts/find-host-message.py --pattern '<fragment>'` (rc 0):

| when | where | the host's words |
|---|---|---|
| 2026-10-07T19:01:43 | `emrg-evolution-competition-task` | 「哪条路线，你说清楚，不要试图用各种行话或者黑化来蒙混过关。…」 |
| 2026-10-09T11:38:48, repeated 11:54:07 | `emrg-evolution-competition-task` | 「另外你说的"-3"，"-2"是什么意思？不雅说让人不好理解的话」 |
| 2026-10-10T12:19:36 | `s_260914_1334_ee25ad51` (host session) | 「…回答：今天每个比赛进展如何，简单说，说人话」 |
| 2026-10-12T13:02:02 | `s_260808_0857_387c852f` (host session) | 「…你不要使用自己造的词来和我对话，说人话，说清楚，说重点，这是基本原则」 |

The last was triggered by the coinage 「私榜槽位」 — the host could not tell what it meant and had to
ask. Every earlier answer lived in **one project's own memory** (the `competition` project's
`2f0e9d1a-say-it-plain-and-refute-yourself.md`, and the M88 method its hub added after the `-2`
question), and the complaint came back in *host conversations* — three days later, and again on
10-12 — which had read neither. Measured 2026-10-13 on master `8011295d`:
`git grep -n -E "说人话|造词|自己造|plain language|plainly|jargon" HEAD -- 'emrg/**'` returned **one**
hit, and it is unrelated to the rule (a Chinese comment in `emrg/gui/test/daemon_client.test.js` about
an invented deadline); no prompt in the tree stated the rule for any session, of any task type.

That is the defect `tests/test_language_policy_reach.py` records one rule over — *a rule that governs
every actor, stated in carriers some actors never open* — and the fix is placement, not more copies.
The block now sits in `emrg/server/prompts/system.j2`, the one prompt **every** session is rendered
under, host conversation and scheduled task alike.

What is asserted
----------------
Two levels, two questions. The file leg answers "does the source that ships state it?"; the render leg
answers "is this what a session receives?" — and neither implies the other, because `system.j2` is a
Jinja2 template: `{# … #}` or `{% if false %}` removes a block from the prompt while the file keeps
every character of it. Measured twice in this repository already (issues #1902, #1904) for the task
templates, and the render leg here compares the block **verbatim** rather than term by term, so a
construct that edited the wording is visible too.

Both halves of the rule are pinned together, and the pairing is the point: the prohibition (*never
coin a term at the host*) without the exemption (internal records may keep private vocabulary) is an
over-correction that would strip the agent's own memory, notes and logs of the shorthand they exist
to carry. A rewrite that keeps one and drops the other must fail here.

Named limit
-----------
This pins the *presence* of the rule in the artifact a session receives, not the vocabulary of any
message produced under it — no test can read a message and decide whether a word was coined at the
host. What it keeps true is the placement the rule depends on: one render site, one block, present in
the render rather than only in the file. A future session that reads a plain prompt and coins a term
anyway is a model-behaviour failure, not one this file can see.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The block's identity in `system.j2`. The file spells the heading with the record appended
#: (`## Plain language — how you address the host (issue #2171)`), so this is its prefix; a second
#: copy appended anywhere in the template is still visible as a count of 2.
PLAIN_HEADING = "## Plain language — how you address the host"

#: Load-bearing terms, each a verbatim substring of the shipped wording, so this list cannot drift
#: away from the sentence it checks without this file saying so. They cover the rule, both halves of
#: its scope, the repair it asks for, the reason it is a requirement rather than a preference, and
#: the public record beside the host-local citations.
PLAIN_TERMS = (
    "Write to the host in words they can read without your notes",  # the rule itself
    "Never coin a term at the host",                                # the prohibition
    "A shorthand is spelled on first use",                          # the repair
    "Internal records are exempt",                                  # the half that prevents over-correction
    "A request the host has had to make four times is a requirement, not a preference",
    "issue #2171",                                                  # the record beside the citations
)


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` heading or EOF.

    Returns ``""`` when the heading is absent — the caller asserts, so a missing block reads as a
    failure to measure rather than as a pass.
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

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ
    in `trim_blocks` / `lstrip_blocks` or in the loader path, and the prompt under test would then be
    a prompt nobody receives. The context is deliberately minimal: the block sits outside every
    conditional, so no context key can remove it.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def test_the_shared_prompt_states_the_plain_language_rule() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = _block_after(text, PLAIN_HEADING)
    assert block, (
        "emrg/server/prompts/system.j2 must carry the plain-language block — it is the only prompt "
        "every session is rendered under, and the rule was carried by one project's memory while "
        "the host was asking for it in host conversations that never read that memory"
    )
    missing = _missing_terms(block, PLAIN_TERMS)
    assert not missing, (
        f"the plain-language block must state the rule, both halves of its scope and its record; "
        f"missing: {missing}"
    )
    assert text.count(PLAIN_HEADING) == 1, (
        f"the rule must be stated once; found {text.count(PLAIN_HEADING)} copies — a duplication "
        "costs prompt and leaves two copies free to drift apart"
    )


def test_the_rendered_prompt_carries_the_rule_verbatim() -> None:
    """The artifact the reader gets, measured where `system.j2` is a *template*.

    Reading the file answers "is the rule in `system.j2`?" — one level short of this file's
    question, "is it in the prompt a session runs under?". The comparison is the **block**, verbatim,
    rather than its terms one by one: a Jinja construct that wrapped or edited it (`{# … #}`) leaves
    every term in the file, and a construct that reflowed it would leave every term in the render
    while changing what the reader is told.
    """
    rendered = _render_system_prompt()
    header_block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), PLAIN_HEADING)
    render_block = _block_after(rendered, PLAIN_HEADING)
    assert render_block, (
        "the rendered session prompt must carry the plain-language block: it is present in "
        "system.j2 but no render a session receives contains it"
    )
    assert header_block in rendered, (
        "the rendered prompt carries a *different* plain-language block than the template does — "
        "something in the render path edited it, and the wording a session reads is then not the "
        "wording this repository reviewed"
    )
    missing = _missing_terms(render_block, PLAIN_TERMS)
    assert not missing, f"the rendered plain-language block is missing terms: {missing}"
    assert rendered.count(PLAIN_HEADING) == 1, (
        f"the rendered prompt must state the rule once; found {rendered.count(PLAIN_HEADING)} copies"
    )


def test_the_prohibition_and_the_exemption_are_pinned_together() -> None:
    """Both halves, because either one alone is a defect in the other direction.

    The prohibition without the exemption reads as "never use a shorthand anywhere", which would
    strip the agent's own memory, session notes and run logs of the vocabulary they exist to carry —
    and those records are what the next round reads back. The exemption without the prohibition is
    the state that produced the complaint. This asserts the pair explicitly, so a rewrite that keeps
    the pleasant half fails here with the reason rather than passing as a wording change.
    """
    block = _block_after(SYSTEM_PROMPT.read_text(encoding="utf-8"), PLAIN_HEADING)
    assert "Never coin a term at the host" in block, (
        "the block no longer forbids coining a term at the host — the complaint it exists for"
    )
    assert "Internal records are exempt" in block, (
        "the block no longer exempts internal records — read alone, the prohibition would strip the "
        "agent's own memory and notes of the vocabulary they are written in"
    )


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the block must read as missing.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the
    extractors a prompt that has no such block and requires the absence to be visible, and a prompt
    carrying the heading twice and requires the duplication to be visible.
    """
    sample = "# A prompt with no plain-language block\n\n- Must push\n"
    assert _block_after(sample, PLAIN_HEADING) == ""
    assert _missing_terms(sample, PLAIN_TERMS) == list(PLAIN_TERMS)
    doubled = f"{PLAIN_HEADING}\n\n{PLAIN_HEADING}\n\n- Must push\n"
    assert doubled.count(PLAIN_HEADING) == 2
    # ... and the other direction, so the extractors are not reading a constant: a block that
    # carries one term must report exactly the terms it lacks.
    partial = f"{PLAIN_HEADING}\n\nNever coin a term at the host\n"
    assert _block_after(partial, PLAIN_HEADING).strip() == "Never coin a term at the host"
    assert "Never coin a term at the host" not in _missing_terms(partial, PLAIN_TERMS)
    assert len(_missing_terms(partial, PLAIN_TERMS)) == len(PLAIN_TERMS) - 1
