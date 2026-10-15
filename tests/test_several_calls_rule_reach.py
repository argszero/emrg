"""Issuing several tool calls in one round must be stated in the prompt every session renders.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the single carrier every session receives
(`daemon._build_system_prompt` renders it for a host conversation exactly as for a scheduled task), and
until 2026-10-15 its only sentence about issuing several calls at once was one line long, and it was wrong
in three ways at once:

```
- **parallel calls**: when tools are independent, invoke them in parallel for speed
```

**parallel** — the engine does not run calls concurrently: `daemon.py:4640` is `for tc in openai_tool_calls:`
and executes them one after another, and the file's only two `asyncio.gather` sites (`:6117`, `:6123`) are
unrelated to tool calls. **for speed** — the reason is money: a round is billed for re-sending the whole
conversation, so the unit of cost is the round, not the call. **independent** — a vague condition, which is
why it changed nobody's behaviour: measured 2026-10-15 over the sessions `~/.emrg/sessions_index.json` names
(383,468 rounds carrying tool calls, all time), **9.0%** hold more than one call and in the last seven days
only **4.1%** do (3,386 of 82,818). The evolution task reads 5.3% and the host's own conversation 6.9%, so
the one-call-per-round habit is every session's, not one task's — which is what the host ruled on
2026-10-15 (「针对所有会话」, scope: every session) and what issue #2298 records.

So this is the placement defect `tests/test_language_policy_reach.py` and
`tests/test_no_background_process_reach.py` already pin, at its third instance: a rule that governs every
actor, stated in a carrier some actors never open — here, stated *incorrectly* in the one carrier all of
them open.

What is asserted
----------------
1. The rule is in `system.j2` **and in the render**, and once only: a block in the file that no render
   contains is not a rule any session runs under, and a second copy is two copies free to drift.
2. It carries each part of the ruling — the unit (one round, several calls), the boundary (a call whose
   arguments depend on another's answer waits for its own round), the reason (cost, not speed), and the
   precedence of the no-background-process red line.
3. The three misstatements are **gone**: no `parallel`, no `for speed`, no 并发. One statement, not two.
4. **Reverse**: no wording that caps, budgets or quotas *rounds* may enter the shared prompt. The host
   rejected a round budget explicitly (rant 2026-10-15T12:35:03, 「c 没意义，肯定不行」 — it truncates long
   tasks), so a future edit must not smuggle one in beside the rule that saves rounds.

Named limits
------------
* This pins the *presence* of the rule in the artifact a session receives, not obedience to it — no test
  can watch a model decide to issue two calls. What it keeps true is the placement and the wording; the
  multi-call share is re-measured in the PR, and only re-measured.
* The reverse guard reads spellings that name a **round** budget. It deliberately does not refuse every
  `at most` in the prompt: line 77 bounds an index row's characters, and a guard that reddened that would
  fail on a clean tree. `test_a_bound_that_is_not_about_rounds_is_not_a_hit` holds the distinction.
* The engine is out of scope by the host's instruction (issue #2298 requirement: do not touch
  `daemon.py:4640` or `llm.py:837-861`); nothing here asserts the loop is sequential, because that is
  `daemon.py`'s business, not this file's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The rule's identity: the bullet's lead phrase. Written as the phrase a rewrite would keep, so a second
#: copy anywhere in the template is visible as a count of 2 rather than as longer prose.
RULE_LEAD = "**several tool calls in one round**"

#: One term per part of the ruling, each a verbatim substring of the shipped wording — so this list cannot
#: drift away from the sentence it checks without this file saying so.
RULE_TERMS = (
    RULE_LEAD,                                                   # the unit: a round, not a command
    "no call's arguments depend on what another call returns",   # the boundary, R2
    "a round is billed for re-sending the whole conversation",   # the reason: money, R1
    "a judgement gets its own round",                            # what must stay split
    "the host's red line at the end of this prompt outranks it", # precedence, R5
    "`&`, `nohup`, `disown`, `setsid`",                          # the shape it never licenses
)

#: The three misstatements the old line carried. R3: the new wording may not keep any of them.
FORBIDDEN_WORDINGS = (
    "parallel calls",
    "in parallel",
    "for speed",
    "并发",
)

#: Spellings that would put a **round** budget into the shared prompt. Each is written the way a future
#: editor would write it, not as a regex puzzle: the guard's job is to be loud about a cap, quiet about
#: everything else.
ROUND_BUDGET_SPELLINGS = (
    "round budget",
    "budget of rounds",
    "round quota",
    "quota of rounds",
    "maximum number of rounds",
    "cap on rounds",
    "rounds are capped",
    "at most %d rounds",
    "limit the number of rounds",
    "no more than %d rounds",
)


def _block_after(text: str, heading: str) -> str:
    """The block starting at ``heading``, up to the next `## ` heading or EOF.

    Returns ``""`` when the heading is absent — the caller asserts, so a missing block reads as a failure
    to measure rather than as a pass.
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


def _budget_hits(text: str) -> list[str]:
    """Every round-budget spelling this text carries. Empty means the prompt caps nothing."""
    hits = []
    for spelled in ROUND_BUDGET_SPELLINGS:
        needle = spelled % 10 if "%d" in spelled else spelled
        if needle in text:
            hits.append(spelled)
    return hits


def _render_system_prompt() -> str:
    """The prompt a session receives, rendered by the daemon's own environment.

    `_get_jinja_env` is what `_build_system_prompt` uses — a fresh `jinja2.Environment` could differ in
    `trim_blocks` / `lstrip_blocks`, in the loader path, or in `autoescape`, and the prompt under test
    would then be a prompt nobody receives. The context is minimal on purpose: the rule sits outside every
    conditional, so no context key can remove it, and a render that lost it to a *context* difference is
    what this checks for.
    """
    from emrg.server.daemon import _get_jinja_env  # noqa: PLC0415

    return _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )


def _tool_usage_block(text: str) -> str:
    """The `## Tool Usage` section — where the rule belongs, and the only place it may be stated."""
    return _block_after(text, "## Tool Usage")


@pytest.mark.parametrize("read", [
    pytest.param(lambda: SYSTEM_PROMPT.read_text(encoding="utf-8"), id="template"),
    pytest.param(_render_system_prompt, id="render"),
])
def test_the_shared_prompt_states_the_several_calls_rule(read) -> None:
    """The rule is in the file **and** in the render, with every part of the ruling present.

    Two readings, not one: a block can be in the file and in no render (wrap it in `{% if false %}`, and
    the file still carries every term while the prompt carries none), and `autoescape` would turn the
    shell shapes into `&amp;`, which no reader reads as shell syntax.
    """
    text = read()
    block = _tool_usage_block(text)
    assert block, "system.j2 has no `## Tool Usage` section for the rule to sit in"
    missing = _missing_terms(block, RULE_TERMS)
    assert not missing, (
        "the shared prompt must state every part of the rule — the unit, the boundary, the reason, "
        f"what stays split, and the red line's precedence; missing: {missing}"
    )
    found = text.count(RULE_LEAD)
    assert found == 1, (
        f"the rule must be stated once; found {found} copies — a second copy costs prompt and the two "
        "are then free to disagree"
    )


def test_the_old_misstatements_are_gone() -> None:
    """R3: one statement, not two — `parallel` / `for speed` / 并发 may not survive the rewrite.

    Read on both the template and the render, because the defect this file exists for was a sentence in
    the shared prompt that said the wrong thing while looking like guidance.
    """
    for name, text in (("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
                       ("the rendered prompt", _render_system_prompt())):
        left = [word for word in FORBIDDEN_WORDINGS if word in text]
        assert not left, (
            f"{name} still says {left}: the engine runs calls one after another, the saving is money and "
            "not latency, and a rule that reads as a concurrency hint is the sentence that changed "
            "nothing (issue #2298)"
        )


def test_the_rule_is_a_tool_usage_line_not_a_new_section() -> None:
    """Placement: the rule belongs to the list of how to call tools, where the model reads it first.

    A `## ` section of its own would be a second carrier for one rule — the shape this repository has
    already paid for (a rule restated in carriers some sessions never open).
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "\n## ⛔ Several tool calls" not in text
    assert RULE_LEAD in _tool_usage_block(text)


def test_no_round_budget_enters_the_shared_prompt() -> None:
    """R4, read backwards: the host refused a cap on rounds, so the prompt may not carry one.

    The rule that saves rounds is the easiest place to smuggle a limit in beside, and the limit truncates
    long tasks — which is why the host rejected it by name (rant 2026-10-15T12:35:03, item (c)).
    """
    for name, text in (("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
                       ("the rendered prompt", _render_system_prompt())):
        hits = _budget_hits(text)
        assert not hits, f"{name} states a round budget: {hits}"


def test_a_bound_that_is_not_about_rounds_is_not_a_hit() -> None:
    """The reverse guard's own control, and the false-red it must not produce.

    `system.j2` line 77 bounds an index row's characters with `at most` — a real rule, nothing to do with
    rounds. A guard that reddened it would fail on a clean tree, which is worse than a missing guard, so
    the subject is a *round* budget and this test holds the line.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "at most" in text, "the fixture moved: this control's premise is that sentence"
    assert _budget_hits(text) == []
    assert _budget_hits("Issue at most 10 rounds per task, then stop.") == ["at most %d rounds"]
    assert _budget_hits("There is no round budget here.") == ["round budget"]


def test_the_scan_reports_absence() -> None:
    """The instrument's control: text without the rule must read as missing, not as passing.

    A check that reports the healthy answer whatever it is given is not a check. This feeds the extractors
    a prompt with no such bullet and requires the absence to be visible, and one carrying the lead phrase
    twice and requires the duplication to be visible.
    """
    sample = "# A prompt with no rule\n\n## Tool Usage\n\n- **read before edit**: always read a file\n"
    assert _tool_usage_block(sample), "the block extractor must find an empty-but-present section"
    assert _missing_terms(_tool_usage_block(sample), RULE_TERMS) == list(RULE_TERMS)
    doubled = f"## Tool Usage\n\n- {RULE_LEAD}: x\n\n- {RULE_LEAD}: y\n"
    assert doubled.count(RULE_LEAD) == 2
    assert _tool_usage_block("# nothing here\n") == ""
