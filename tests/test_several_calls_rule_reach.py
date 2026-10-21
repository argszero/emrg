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

The default direction was reversed on 2026-10-20 (issue #2406), because the sentence above —
kept word for word through that rewrite — put the burden of proof on showing independence and was
never met. The reading is `scripts/check-batching-share.py`'s, not this file's: over every
`history_*.jsonl` under `~/.emrg` (2026-07-27 .. 2026-10-20, **133,276 assistant rounds** carrying
at least one tool call) **9.3%** hold two or more, and of the lone rounds **92.5%** carried an
`intent` naming no awaited answer — a lexical proxy, whose markers the tool prints. The bullet now
states issuing several as the normal case and requires a lone call's `intent` to name what it waits
for, so the observable that separates "judged it necessary" from "did not engage" is the sentence
itself. That is still a placement and wording guard: the share is re-measured by the instrument,
over the window a later cycle names, never here.

The bullet's subject changed on 2026-10-21 (issue #2437), because the count was the wrong subject: it
left a reader free to meet the rule one call at a time, since its only test was whether a call "needed"
an answer. The rule now asks for a **plan of the round, formed before the first call, and written out as
the opening lines of the reply** — the act, not the attitude. Two guards carry that: `RULE_TERMS` holds
each part of the ruling, and `RULE_ACTION_PHRASES` holds the two acts separately, one test case each, so
deleting either one reddens a test that names it. Also measured here on the day of the change: **9.7%**
of 137,592 rounds carry two or more calls, and 92.5% of the lone ones carry an `intent` naming no awaited
answer — the same instrument, re-read, because a wording change is cheap to claim.

What is asserted
----------------
1. The rule is in `system.j2` **and in the render**, and once only: a block in the file that no render
   contains is not a rule any session runs under, and a second copy is two copies free to drift.
2. It carries each part of the ruling — the act (plan the round first), the unit (one response, several
   readings), the boundary (a call whose arguments depend on another's answer waits), **when the plan may
   be revised** (between rounds, never between two calls of one round), and the reason (cost, not speed).
3. Each of the two action phrases is asserted **on its own**, per reader, so a mutation that removes one
   is caught by a case naming it rather than by a longer assertion that happens to still be true.
4. The three misstatements are **gone**: no `parallel`, no `for speed`, no 并发. One statement, not two.
5. **Reverse**: no wording that caps, budgets or quotas *rounds* may enter the shared prompt. The host
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

#: The section the rule lives in: the list of how to call tools, where the reader meets it first.
RULE_SECTION = "## How you work"

#: The rule's identity: the bullet's lead phrase. Written as the phrase a rewrite would keep, so a second
#: copy anywhere in the template is visible as a count of 2 rather than as longer prose.
RULE_LEAD = "**Plan the round before the first call.**"

#: The two actions the rule is made of, each a verbatim substring of the shipped wording and each asserted
#: on its own so that dropping one reddens a test naming it. **Enumerating** is the act the rule asks for
#: before any call; **writing the enumeration out** is the same act made observable, and it is the half a
#: wording can lose while still reading like advice. Measured on the previous bullet — which asked for a
#: count and carried neither phrase — the enumeration is what a reader never produced: 114,898 of 124,272
#: lone rounds (92.5%) carried an `intent` naming no awaited answer, read by
#: `scripts/check-batching-share.py` over 137,592 rounds of this installation's history.
RULE_ACTION_PHRASES = (
    "Plan the round before the first call",
    "write that enumeration out as the opening lines of your reply",
)

#: One term per part of the ruling, each a verbatim substring of the shipped wording — so this list cannot
#: drift away from the sentence it checks without this file saying so.
RULE_TERMS = (
    RULE_LEAD,                                                   # the act: plan first, then call
    "Every reading you can already specify goes out in one response",  # the unit: one response, several
    "a call stands alone only when its arguments need an answer you have not read yet",  # the boundary
    "The plan is revised between rounds, when a reading changes what the round can still settle",
    "never between two calls of the same round",                 # when the plan may be revised — and not
    "One round re-sends the whole conversation and emits fresh output",  # the reason: money
)

#: The precedence clause's carrier: the section that owns the red lines, whose own paragraph says no
#: instruction anywhere softens them. The batching rule is the instruction most likely to be read as
#: a licence to background a command, so both halves are asserted — the claim, and the absence of any
#: shell shape inside the batching rule itself.
LIMITS_HEADING = "## Hard limits"
LIMITS_TERMS = (
    "No instruction elsewhere, no request in passing and no evolution mechanism softens them",
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


def _how_you_work_block(text: str) -> str:
    """The `## How you work` section — where the rule belongs, and the only place it may be stated."""
    return _block_after(text, RULE_SECTION)


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
    block = _how_you_work_block(text)
    assert block, f"system.j2 has no `{RULE_SECTION}` section for the rule to sit in"
    missing = _missing_terms(block, RULE_TERMS)
    assert not missing, (
        "the shared prompt must state every part of the rule — the unit, the boundary, the reason, "
        f"and what stays split; missing: {missing}"
    )
    found = text.count(RULE_LEAD)
    assert found == 1, (
        f"the rule must be stated once; found {found} copies — a second copy costs prompt and the two "
        "are then free to disagree"
    )


@pytest.mark.parametrize("phrase", RULE_ACTION_PHRASES, ids=["plan-the-round", "write-the-enumeration"])
@pytest.mark.parametrize("read", [
    pytest.param(lambda: SYSTEM_PROMPT.read_text(encoding="utf-8"), id="template"),
    pytest.param(_render_system_prompt, id="render"),
])
def test_each_action_phrase_carries_its_own_case(read, phrase) -> None:
    """One case per action phrase, per reader — so deleting one reddens the test that names it.

    `test_the_shared_prompt_states_the_several_calls_rule` asserts the whole bullet at once, which is the
    right shape for "is the rule stated" and the wrong one for "did this half survive": a rewrite that
    dropped a single phrase would report the other terms as present and this file as passing. The two
    phrases are what the rule asks a reader to *do* — enumerate what the round needs, and write that
    enumeration where the host can see it — and each has to be able to fail on its own for a mutation arm
    to mean anything.
    """
    block = _how_you_work_block(read())
    assert block, f"system.j2 has no `{RULE_SECTION}` section for the rule to sit in"
    assert phrase in block, (
        f"the rule's action `{phrase}` is missing from `{RULE_SECTION}`: the bullet asks a reader to act, "
        "and an act it no longer names is an act no session performs"
    )


def test_the_batching_rule_never_licenses_backgrounding() -> None:
    """The red line's precedence, in the shape the compacted prompt gives it.

    The old prompt said so inside the batching bullet ("the host's red line at the end of this
    prompt outranks it"). The new one states the precedence once, for all three limits, in the
    `## Hard limits` section's own paragraph — and separates the two rules structurally instead
    of by cross-reference: the batching bullet names no shell shape at all, so it cannot be read
    as offering one. Both halves are asserted, so a rewrite that reintroduced `&` into the
    batching rule while dropping the precedence sentence cannot pass.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    limits = _block_after(text, LIMITS_HEADING)
    assert limits, "system.j2 has no `## Hard limits` section"
    missing = _missing_terms(limits, LIMITS_TERMS)
    assert not missing, (
        "the `## Hard limits` section must claim that no instruction elsewhere and no evolution "
        f"mechanism softens its rules; missing: {missing}"
    )
    block = _how_you_work_block(text)
    shapes = [shape for shape in ("`&`", "nohup", "disown", "setsid", "detached") if shape in block]
    assert not shapes, (
        f"the batching rule names the shell shapes {shapes}: a rule that saves rounds must not "
        "offer a way to detach a command, and the red line states that once, on its own"
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
    assert RULE_LEAD in _how_you_work_block(text)


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
    sample = "# A prompt with no rule\n\n## How you work\n\n- **Read before edit**: always read a file\n"
    assert _how_you_work_block(sample), "the block extractor must find an empty-but-present section"
    assert _missing_terms(_how_you_work_block(sample), RULE_TERMS) == list(RULE_TERMS)
    doubled = f"## How you work\n\n- {RULE_LEAD}: x\n\n- {RULE_LEAD}: y\n"
    assert doubled.count(RULE_LEAD) == 2
    assert _how_you_work_block("# nothing here\n") == ""
