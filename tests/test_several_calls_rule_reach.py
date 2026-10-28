"""A round has two shapes and no third, and the prompt states that as its last section.

Why this file exists
--------------------
`emrg/server/prompts/system.j2` is the single carrier every session receives
(`daemon._build_system_prompt` renders it for a host conversation exactly as for a scheduled task), and
until 2026-10-15 its only sentence about issuing several calls at once was one line long, and it was wrong
in three ways at once:

```
- **parallel calls**: when tools are independent, invoke them in parallel for speed
```

**parallel** — the engine does not run calls concurrently: `daemon.py` executes the tool calls of a round
one after another. **for speed** — the reason is money: a round is billed for re-sending the whole
conversation, so the unit of cost is the round, not the call. **independent** — a vague condition, which is
why it changed nobody's behaviour: measured 2026-10-15 over the sessions `~/.emrg/sessions_index.json`
names (383,468 rounds carrying tool calls, all time), **9.0%** hold more than one call and in the last
seven days only **4.1%** do (3,386 of 82,818). The evolution task reads 5.3% and the host's own
conversation 6.9%, so the one-call-per-round habit is every session's, not one task's — which is what the
host ruled on 2026-10-15 (「针对所有会话」, scope: every session) and what issue #2298 records.

The rule has been rewritten twice since, and each rewrite moved what it *asks for* rather than only its
wording. 2026-10-20 (issue #2406) reversed the default direction: issuing several became the normal case,
and a lone call had to name the answer it waited for — the reading behind it is
`scripts/check-batching-share.py`'s, over every `history_*.jsonl` under `~/.emrg`
(2026-07-27 .. 2026-10-20, **133,276 assistant rounds** carrying at least one tool call), where **9.3%**
hold two or more and **92.5%** of the lone ones carried an `intent` naming no awaited answer. 2026-10-21
(issue #2437) replaced the count with a plan: a plan of the round, formed before the first call and written
out as the opening lines of the reply — measured the same day at **9.7%** of 137,592 rounds carrying two or
more calls.

Both of those still left a reader free to meet the rule one call at a time, because neither stated a case a
round could be measured against: a plan can be "one call", and "the normal case" is not a count. So the
rule now states a **shape**. A round has exactly two of them — no call at all, or five or more calls — and
a round below five must answer for itself and say why it is spending a round that costs more than the calls
it buys. That is a condition on the round, checkable by the reader it lands on, rather than an attitude the
reader is asked to hold.

Why it is a section of its own, at the end of the file
-----------------------------------------------------
The two earlier wordings were one bullet among eight under `## How you work`, where the rule read as one
more habit beside "read a file before you change it". It is not a habit but the shape of every round, and
its position is part of what it says: it is the file's last block, after `## Hard limits`, so a reader
arrives at it having read everything a round must not do and meets what a round must be. Placement is
asserted rather than assumed — moving it back among the bullets reddens a test that names the position,
which is the direction this file used to require and now refuses.

What is asserted
----------------
1. The rule is in `system.j2` **and in the render**, and once only: a block in the file that no render
   contains is not a rule any session runs under, and a second copy is two copies free to drift.
2. It carries each part of the ruling — the act (the opening lines list the calls the request needs), the
   reason (a round is billed for re-sending the conversation), the **shape** (two, and no third), the
   boundary below five, and the price a sub-five round pays for itself.
3. Each of the two action phrases is asserted **on its own**, per reader, so a mutation that removes one
   is caught by a case naming it rather than by a longer assertion that happens to still be true.
4. **Placement**: its own `## ` section, after `## Hard limits`, with no heading of any kind after it, and
   not a bullet of `## How you work`.
5. **The old bullet is gone**, not merely shadowed. Its phrases may not survive anywhere in the template:
   two statements of one rule are free to disagree, and the one a reader meets first is the one that
   governs.
6. The three misstatements are **gone**: no `parallel`, no `for speed`, no 并发. One statement, not two.
7. **Reverse**: no wording that caps, budgets or quotas *rounds* may enter the shared prompt. The host
   rejected a round budget explicitly (rant 2026-10-15T12:35:03, 「c 没意义，肯定不行」 — it truncates long
   tasks), so a future edit must not smuggle one in beside the rule that saves rounds. A floor on the
   calls in a round is not a cap on the number of rounds, and the two must not be confused in either
   direction.

Named limits
------------
* This pins the *presence* and *placement* of the rule in the artifact a session receives, not obedience
  to it — no test can watch a model decide to issue five calls. What it keeps true is the placement and
  the wording; the multi-call share is re-measured by `scripts/check-batching-share.py` over the window a
  later cycle names, and only re-measured.
* The reverse guard reads spellings that name a **round** budget. It deliberately does not refuse every
  `at most` in the prompt: the memory rule bounds an index row's characters with `at most`, and a guard
  that reddened that would fail on a clean tree. `test_a_bound_that_is_not_about_rounds_is_not_a_hit`
  holds the distinction.
* The engine is out of scope by the host's instruction (issue #2298 requirement: do not touch the tool
  loop or the client's request path); nothing here asserts that the loop is sequential, because that is
  `daemon.py`'s business, not this file's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

#: The section the rule owns, as its whole heading line. It is the file's last section, and that is a
#: property this file asserts rather than a fact about today's layout.
RULE_SECTION = "## Plan before every call"

#: The rule's identity: the bullet's lead phrase. Written as the phrase a rewrite would keep, so a second
#: copy anywhere in the template is visible as a count of 2 rather than as longer prose.
RULE_LEAD = "**Plan before every call.**"

#: The two actions the rule is made of, each a verbatim substring of the shipped wording and each asserted
#: on its own so that dropping one reddens a test naming it. **Listing the calls** is the act that opens
#: the reply, and the price a sub-five round must name is what makes the shape enforceable: without it the
#: rule degrades into a preference, which is the state the two earlier wordings never left.
RULE_ACTION_PHRASES = (
    "The opening lines of your reply list the tool calls the request needs in total",
    "say why it is wasting money",
)

#: One term per part of the ruling, each a verbatim substring of the shipped wording — so this list cannot
#: drift away from the sentence it checks without this file saying so. The shape is registered in its own
#: two words (`no call at all`, `five or more calls`) because that pair *is* the ruling: a rewrite that
#: kept the plan and dropped the shape would otherwise pass on the surrounding prose.
RULE_TERMS = (
    RULE_LEAD,                                                    # the act: plan, then call
    "The opening lines of your reply list the tool calls the request needs in total",
    "A round is billed for re-sending the whole conversation",     # the reason: money
    "five planned calls in one round cost one fifth of what five calls in five rounds cost",  # the price
    "A round therefore has two shapes and no third",               # the ruling
    "**no call at all**, or **five or more calls**",               # the two shapes, named
    "Below five, the round must answer for it",                    # the boundary
    "say why it is wasting money",                                 # what a sub-five round owes
)

#: The phrases the retired bullet was made of. None may survive: the rewrite replaced one statement of the
#: rule with another, and a rule left in two wordings is a rule whose two halves can disagree.
RETIRED_BULLET_PHRASES = (
    "Plan the round before the first call",
    "write that enumeration out as the opening lines of your reply",
    "Every reading you can already specify goes out in one response",
    "a call stands alone only when its arguments need an answer you have not read yet",
    "The plan is revised between rounds",
    "One round re-sends the whole conversation and emits fresh output",
)

#: The precedence clause's carrier: the section that owns the red lines, whose own paragraph says no
#: instruction anywhere softens them. The batching rule is the instruction most likely to be read as
#: a licence to background a command, so both halves are asserted — the claim, and the absence of any
#: shell shape inside the rule itself.
LIMITS_HEADING = "## Hard limits"
LIMITS_TERMS = (
    "No instruction elsewhere, no request in passing and no evolution mechanism softens them",
)

#: The section the rule must no longer be a bullet of. Kept as a constant because the placement assertion
#: is about two sections, not one.
FORMER_SECTION = "## How you work"

#: The three misstatements the old line carried. R3: the new wording may not keep any of them.
FORBIDDEN_WORDINGS = (
    "parallel calls",
    "in parallel",
    "for speed",
    "并发",
)

#: Spellings that would put a **round** budget into the shared prompt. Each is written the way a future
#: editor would write it, not as a regex puzzle: the guard's job is to be loud about a cap, quiet about
#: everything else. A floor on the calls inside one round is the opposite of a cap on rounds, and this
#: list is what keeps a later cycle from reading the new shape as a licence for the old objection.
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


def _rule_block(text: str) -> str:
    """The rule's own section — where it belongs, and the only place it may be stated."""
    return _block_after(text, RULE_SECTION)


def _former_block(text: str) -> str:
    """`## How you work` — the section the rule was a bullet of, and may not be one again."""
    return _block_after(text, FORMER_SECTION)


@pytest.mark.parametrize("read", [
    pytest.param(lambda: SYSTEM_PROMPT.read_text(encoding="utf-8"), id="template"),
    pytest.param(_render_system_prompt, id="render"),
])
def test_the_shared_prompt_states_the_rule_in_its_own_section(read) -> None:
    """The rule is in the file **and** in the render, with every part of the ruling present.

    Two readings, not one: a block can be in the file and in no render (wrap it in `{% if false %}`, the
    file still carries every term while the prompt carries none), and `autoescape` would turn the shell
    shapes into `&amp;`, which no reader reads as shell syntax.
    """
    text = read()
    block = _rule_block(text)
    assert block, f"system.j2 has no `{RULE_SECTION}` section for the rule to sit in"
    missing = _missing_terms(block, RULE_TERMS)
    assert not missing, (
        "the shared prompt must state every part of the rule — the act, the price, the two shapes, "
        f"the boundary and what a sub-five round owes; missing: {missing}"
    )
    found = text.count(RULE_LEAD)
    assert found == 1, (
        f"the rule must be stated once; found {found} copies — a second copy costs prompt and the two "
        "are then free to disagree"
    )


@pytest.mark.parametrize("phrase", RULE_ACTION_PHRASES, ids=["list-the-calls", "say-what-it-wastes"])
@pytest.mark.parametrize("read", [
    pytest.param(lambda: SYSTEM_PROMPT.read_text(encoding="utf-8"), id="template"),
    pytest.param(_render_system_prompt, id="render"),
])
def test_each_action_phrase_carries_its_own_case(read, phrase) -> None:
    """One case per action phrase, per reader — so deleting one reddens the test that names it.

    `test_the_shared_prompt_states_the_rule_in_its_own_section` asserts the whole block at once, which is
    the right shape for "is the rule stated" and the wrong one for "did this half survive": a rewrite that
    dropped a single phrase would report the other terms as present and this file as passing. The two
    phrases are what the rule asks a reader to *do* — list the calls the request needs in total, and, when
    a round carries fewer than five, say why it is wasting money — and each has to be able to fail on its
    own for a mutation arm to mean anything.
    """
    block = _rule_block(read())
    assert block, f"system.j2 has no `{RULE_SECTION}` section for the rule to sit in"
    assert phrase in block, (
        f"the rule's action `{phrase}` is missing from `{RULE_SECTION}`: the rule asks a reader to act, "
        "and an act it no longer names is an act no session performs"
    )


def test_the_rule_is_its_own_section_and_the_files_last_block() -> None:
    """Placement: its own `## ` section, after the red lines, with nothing after it.

    This is the assertion inverted from what this file used to require. The rule was one bullet of
    `## How you work` — a shape that made a condition on every round read as one more habit among eight,
    and one that a reader finished without ever being told what a round may be. Its position is now part
    of what it says, so all three halves are asserted: it is a section, that section comes after
    `## Hard limits`, and no heading of any kind follows it. A rewrite that demoted it back into the
    bullet list would leave every term present and the shape wrong, which is what this catches.
    """
    for name, text in (("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
                       ("the rendered prompt", _render_system_prompt())):
        assert text.count(RULE_SECTION) == 1, (
            f"{name} has {text.count(RULE_SECTION)} `{RULE_SECTION}` headings: the rule is one section, "
            "and a second is free to drift from the first"
        )
        start = text.index(RULE_SECTION)
        assert start > text.index(LIMITS_HEADING), (
            f"in {name} `{RULE_SECTION}` comes before `{LIMITS_HEADING}`: the rule belongs after the red "
            "lines it must not be read as a licence against, and its position there is deliberate"
        )
        trailing = text[start + len(RULE_SECTION):]
        assert not re.search(r"^## ", trailing, re.MULTILINE), (
            f"{name} carries a section after `{RULE_SECTION}`: the rule is the file's last block, so a "
            "reader reaches it having read everything else — moving it back into the middle is the "
            "placement this rule was moved out of"
        )
        assert RULE_LEAD not in _former_block(text), (
            f"in {name} the rule is still a bullet of `{FORMER_SECTION}`: it was removed from that list "
            "because a condition on every round is not one habit beside seven others"
        )


def test_the_retired_bullet_is_gone_and_not_shadowed() -> None:
    """One statement of the rule, not two: the old wording may not survive anywhere in the prompt.

    A shadowed rule is worse than a deleted one. The two wordings ask for different things — a plan of the
    round, and a shape a round must have — so a reader given both has to choose, and the one it meets first
    wins. Both surfaces are read, because the old text could come back through the template or through a
    stray copy in the render.
    """
    for name, text in (("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
                       ("the rendered prompt", _render_system_prompt())):
        left = [phrase for phrase in RETIRED_BULLET_PHRASES if phrase in text]
        assert not left, (
            f"{name} still carries the retired bullet's wording {left}: the rule was replaced, not "
            "restated, and two statements of one rule are free to disagree"
        )


def test_the_rule_never_licenses_backgrounding() -> None:
    """The red line's precedence, in the shape the compacted prompt gives it.

    The old prompt said so inside the batching bullet ("the host's red line at the end of this
    prompt outranks it"). The new one states the precedence once, for all three limits, in the
    `## Hard limits` section's own paragraph — and separates the two rules structurally instead
    of by cross-reference: the rule's own section names no shell shape at all, so it cannot be
    read as offering one. Both halves are asserted, so a rewrite that reintroduced `&` into the
    rule while dropping the precedence sentence cannot pass.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    limits = _block_after(text, LIMITS_HEADING)
    assert limits, "system.j2 has no `## Hard limits` section"
    missing = _missing_terms(limits, LIMITS_TERMS)
    assert not missing, (
        "the `## Hard limits` section must claim that no instruction elsewhere and no evolution "
        f"mechanism softens its rules; missing: {missing}"
    )
    block = _rule_block(text)
    shapes = [shape for shape in ("`&`", "nohup", "disown", "setsid", "detached") if shape in block]
    assert not shapes, (
        f"the rule names the shell shapes {shapes}: a rule that saves rounds must not offer a way "
        "to detach a command, and the red line states that once, on its own"
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


def test_no_round_budget_enters_the_shared_prompt() -> None:
    """R4, read backwards: the host refused a cap on rounds, so the prompt may not carry one.

    The rule that saves rounds is the easiest place to smuggle a limit in beside, and the limit truncates
    long tasks — which is why the host rejected it by name (rant 2026-10-15T12:35:03, item (c)). The new
    wording states a floor on the calls *inside* a round and says nothing about how many rounds may run;
    this test is what keeps a later cycle from collapsing the two.
    """
    for name, text in (("system.j2", SYSTEM_PROMPT.read_text(encoding="utf-8")),
                       ("the rendered prompt", _render_system_prompt())):
        hits = _budget_hits(text)
        assert not hits, f"{name} states a round budget: {hits}"


def test_a_bound_that_is_not_about_rounds_is_not_a_hit() -> None:
    """The reverse guard's own control, and the false-red it must not produce.

    The memory rule bounds an index row's characters with `at most` — a real rule, nothing to do with
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
    a prompt with the section present but empty of the rule, and requires the absence to be visible, and
    one carrying the lead phrase twice and requires the duplication to be visible.
    """
    sample = (
        "# A prompt with no rule\n\n"
        f"{RULE_SECTION}\n\n- **Something else.** a rule about nothing\n"
    )
    assert _rule_block(sample), "the block extractor must find an empty-but-present section"
    assert _missing_terms(_rule_block(sample), RULE_TERMS) == list(RULE_TERMS)
    doubled = f"{RULE_SECTION}\n\n- {RULE_LEAD}: x\n\n- {RULE_LEAD}: y\n"
    assert doubled.count(RULE_LEAD) == 2
    assert _rule_block("# nothing here\n") == ""
