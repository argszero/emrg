"""Content guards for `emrg/server/feishu_prompt.md` — the cadence rule (issue #2594).

The Feishu task is a watcher: its value is that it reads the host's conversations
often enough that a message which arrived is answered in the round after it arrived.
That value is decided entirely by the task record — `interval`, and any `schedule`
window that overrides it.

The vibe check sits across that decision. Every round an agent may answer
`recommend_slowdown: true`, and the scheduler then waits the heartbeat interval
instead of the record's own. For most task types that is a reasonable safeguard; for
a watcher it inverts the task, because a round that found nothing is exactly the
round that invites the recommendation — the quieter the inbox, the stronger the case
for slowing the thing that watches the inbox. Until now the template said nothing
about it, so a Feishu task was free to slide and the only defence was a block in one
installation's `tasks.yml`, which no other installation can read.

`competition_prompt.md` already carries a version of this rule for the same kind of
reason, and the two must agree rather than offer a round two answers.

Two levels, two questions
-------------------------
`feishu_prompt.md` is a Jinja2 template: `TaskHandler._build_evolution_prompt`
renders it and sends the **result**. So the file-level assertions below answer "does
the source that ships carry this?", and the render-level ones answer "is this what a
round is sent?" — neither implies the other. A Jinja construct (`{# … #}`,
`{% if false %}`) removes text from the prompt while the file keeps every character,
a failure measured on `competition_prompt.md` on 2026-10-08 (issue #1904) where all
five load-bearing phrases stayed in the file and none reached the render.

A citation this rule does not carry
----------------------------------
The rule is written as the template's own reasoning, with no host attribution,
because no message states it as a directive for this task. Two readings, both taken
with `scripts/find-host-message.py` on 2026-10-26: `--pattern '禁止降频'` answers
`NOT FOUND`, while `--pattern '降频'` finds 25 messages and none of them on
2026-10-06 — the date `competition_prompt.md` cites. So the competition file's own
citation cannot be resolved from here, and the reason is the corpus rather than the
host: the daemon log keeps only a message's first 60 characters.

Those counts are readings over the span that run reported, not a property of this
tree. The instrument states that span in its own header — how many log files it read,
and how many session directories hold a history, with the first and last instant of
each — and both numbers are the installation's, so a reader elsewhere reproduces the
reading's shape rather than its digits. A `NOT FOUND` is therefore a statement about
what the record holds, never about what the host said.

What the host did say — `2026-10-23 20:20:51`, 「支持特定任务不降频吗？」, whether a
specific task can be exempt from the slowdown — is the question this section answers.
A question is not a directive, so it is the occasion for the rule and not its
authority; the authority is the task's own purpose. The quotation is carried for what
it asks and is not offered as evidence another installation could open, because the
session id holding it resolves only in the record where it was spoken.
`scripts/check-rant-citations.py` scans `emrg/server/feishu_prompt.md` — the template
is in that guard's `INSTRUCTION_FILES` — so an attribution added to it without a
public record reddens there. The attribution rule is mechanised, and nothing here
restates it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from tests.task_handler_factory import make_handler

PROMPT = Path(__file__).resolve().parent.parent / "emrg" / "server" / "feishu_prompt.md"
COMPETITION_PROMPT = PROMPT.with_name("competition_prompt.md")

#: The heading of the section this file exists to keep. Its number is not decoration:
#: `0` states that it is read before the round's first step, which is §1.
HEADING = "### 0. Cadence"

#: The first step of the round — the thing the rule has to precede to bind it.
FIRST_STEP = "### 1. Read"

#: The agent name the render fixture asks for — a name in no template, so "the render
#: carries it" proves a placeholder resolved rather than a string being copied through.
PROMPT_PROJECT = "demoproj"

#: What the block must say, and why each term is load-bearing rather than decorative.
#: `recommend_slowdown` is the field the round answers; `false` is the answer; the two
#: record fields are what the block must name as the cadence's owner, so a round knows
#: both what to reply and what not to touch.
BOUND_FIELD = "recommend_slowdown"
CADENCE_OWNERS = ("interval", "schedule")

#: The misreading the block exists to name: a round that read nothing is a normal round.
EMPTY_ROUND_MARK = "a correct round"


def _cadence_block(text: str) -> str | None:
    """The `### 0. Cadence` block as a reader meets it, or `None` when it is absent.

    Deliberately answers rather than asserting, so the both-ways control below can show
    that a missing block is reported as missing instead of as nothing to check.
    """
    marker = f"\n{HEADING}"
    start = text.find(marker)
    if start < 0:
        return None
    rest = text[start + len(marker) :]
    end = re.search(r"\n#{2,3} ", rest)
    return rest[: end.start()] if end else rest


def _require_block(text: str, subject: str) -> str:
    block = _cadence_block(text)
    assert block is not None, (
        f"{subject} carries no `{HEADING}` block — the no-slowdown rule is gone, so this "
        "task is free to answer `recommend_slowdown: true` and wait up to eight times its "
        "record's interval (issue #2594)"
    )
    assert block.strip(), (
        f"the `{HEADING}` heading is in {subject} but its body is empty — a template "
        "construct kept the title and sent nothing underneath it"
    )
    return block


def _missing_terms(block: str) -> list[str]:
    return [term for term in (BOUND_FIELD, "false", "slowdown_reason", *CADENCE_OWNERS) if term not in block]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """The Feishu prompt as a round receives it, rendered by the real builder.

    `TaskHandler._build_evolution_prompt` is the only thing that turns this template into
    a prompt, so it is what these tests render through: a fresh `jinja2.Environment` here
    could differ in `trim_blocks`, `lstrip_blocks` or the loader, and the artifact under
    test would then be a prompt no round is ever sent.

    `config_dir` is redirected at a tmp tree — the shape
    `tests/test_evolution_prompt_index_rule.py` uses — so the `agents.yml` read lands on
    a file this test made, never on the host's `~/.emrg`.
    """
    tmp_path = tmp_path_factory.mktemp("feishu-prompt")
    agent_dir = tmp_path / PROMPT_PROJECT
    agent_dir.mkdir(exist_ok=True)
    (tmp_path / "agents.yml").write_text(
        yaml.safe_dump([{"name": PROMPT_PROJECT, "path": str(agent_dir)}]),
        encoding="utf-8",
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name="feishu-task",
            type="feishu",
            config={"agent": PROMPT_PROJECT},
            identity=InstanceIdentity(),
            template_path=PROMPT,
        )
        return handler._build_evolution_prompt()
    finally:
        mod.config_dir = original


def test_the_template_states_the_cadence_rule():
    """The rule exists in the source that ships, in its own section."""
    text = PROMPT.read_text(encoding="utf-8")
    block = _require_block(text, "feishu_prompt.md")
    missing = _missing_terms(block)
    assert not missing, (
        f"the `{HEADING}` block no longer names {missing}: an agent reading it cannot tell "
        "which field it answers, or which fields decide the cadence it must not lower"
    )


def test_the_cadence_block_is_read_before_the_first_step():
    """Placement, not just presence: the rule sits above the step it constrains.

    A rule parked below §1 is met by a round that has already started reading, and the
    round is what it binds — the same defect `competition_prompt.md` §0.0 guards against.
    """
    text = PROMPT.read_text(encoding="utf-8")
    _require_block(text, "feishu_prompt.md")
    assert text.index(HEADING) < text.index(FIRST_STEP), (
        f"the `{HEADING}` block drifted below `{FIRST_STEP}` — the rule that governs the "
        "round is read after the round has begun"
    )


def test_the_block_says_an_empty_round_is_a_correct_round():
    """The specific misreading, named: finding nothing is not a reason to wait longer.

    Without this sentence the block only forbids an answer, and a round that read nothing
    has no other way to justify itself than the recommendation it is being denied.
    """
    block = _require_block(PROMPT.read_text(encoding="utf-8"), "feishu_prompt.md")
    assert EMPTY_ROUND_MARK in block, (
        f"the `{HEADING}` block no longer says a round that read nothing is {EMPTY_ROUND_MARK} "
        "— that is the one reading which produces the recommendation"
    )
    assert "### 1. Read" not in block, (
        "the block has swallowed §1 — the answer to an empty round belongs to §1, and a "
        "second copy here would be two instructions where one is enough"
    )


def test_the_cadence_rule_reaches_the_round_and_not_merely_the_file(rendered):
    """The same requirement, asserted on the prompt the agent is actually sent.

    The file-level test above cannot see this failure: a Jinja comment around the block
    leaves every phrase in `feishu_prompt.md` and drops all of them from the render.
    """
    block = _require_block(rendered, "the rendered Feishu prompt")
    missing = _missing_terms(block)
    assert not missing, (
        f"the rendered `{HEADING}` block does not carry {missing}: the rule is in the file "
        "and in no render, which is the one failure a file-level guard cannot see"
    )
    assert EMPTY_ROUND_MARK in block, (
        "the rendered block lost the empty-round sentence — a round is told what to answer "
        "and not why the round it just had was fine"
    )


def test_the_rendered_cadence_block_still_precedes_the_round_it_binds(rendered):
    """Placement has to survive rendering, which is where the reader meets it."""
    assert HEADING in rendered, (
        "the rendered Feishu prompt has no `### 0. Cadence` block at all — whether or not "
        "the file still carries it"
    )
    assert rendered.index(HEADING) < rendered.index(FIRST_STEP), (
        "the rendered cadence block drifted below §1, so a round that is already reading "
        "has not been told the rule that binds it"
    )


def test_both_templates_that_forbid_a_slowdown_give_the_same_answer():
    """One rule, two carriers, compared — not one carrier trusted.

    `competition_prompt.md` forbids the same recommendation for the same reason. A round
    reading two templates that disagree would follow whichever it met first, so the two
    are read together and both must name the field and the answer.
    """
    competitors = COMPETITION_PROMPT.read_text(encoding="utf-8")
    assert "### 0.0 " in competitors, (
        "the competition template lost its own cadence block — this comparison would then "
        "have one side and prove nothing"
    )
    for name, path in (("feishu_prompt.md", PROMPT), ("competition_prompt.md", COMPETITION_PROMPT)):
        body = path.read_text(encoding="utf-8")
        assert "recommend_slowdown" in body, f"{name} no longer names the field the round answers"
        assert re.search(r"`recommend_slowdown`[^.\n]{0,40}`false`", body), (
            f"{name} no longer states that `recommend_slowdown` is `false` — the two task "
            "templates must give a round one answer, not two"
        )


#: The rule that makes a thread part of the conversation it hangs under: the key the
#: listing answers a thread in, so a round that reads it sees the replies.
THREAD_MARK = "`thread_replies`"


def test_the_template_says_a_thread_is_part_of_its_conversation():
    """Without this the round reads the parent and reports the thread as covered."""
    text = PROMPT.read_text(encoding="utf-8")
    section = text[text.index(FIRST_STEP) :]
    assert THREAD_MARK in section, (
        "§1 no longer tells a round to read a message's thread replies — an @ to the host "
        "written inside a thread arrives in that key and in no other list, so a round that "
        "stops at the parent reports a conversation it never saw"
    )
    assert "never treat a thread as read because its parent was" in section, (
        "§1 lost the sentence that stops a thread being closed by having read its parent — "
        "the replies are the part that can carry the @"
    )


def test_the_thread_rule_reaches_the_round_and_not_merely_the_file(rendered):
    """The same requirement on the prompt a round is sent, where a Jinja construct hides."""
    assert THREAD_MARK in rendered, (
        "the rendered Feishu prompt lost the thread-reply rule — a round is told to read the "
        "conversation and not that a thread is part of it"
    )


#: The rule that tells a round how to read an empty answer: one conversation known to hold
#: messages, read before "nothing arrived" is reported.
POSITIVE_CONTROL_MARK = "known to hold messages"


def test_the_template_says_an_empty_answer_needs_a_positive_control():
    """§1 says a round that reads nothing must say so — and said nothing about how to know.

    A zero from a listing is the same answer a genuinely quiet conversation, a token valid
    under another `HOME` and a lagging index all give. Without this sentence a round reports
    "nothing arrived" on an instrument it never checked, and this task's whole product is the
    difference between a quiet inbox and an unread one.
    """
    text = PROMPT.read_text(encoding="utf-8")
    section = text[text.index(FIRST_STEP) :]
    assert "reads nothing must say so" in section, (
        "§1 lost the sentence this rule qualifies — the positive control is owed precisely "
        "because an empty round has to be reported, and an unchecked empty report is not a "
        "reading"
    )
    assert POSITIVE_CONTROL_MARK in section, (
        "§1 no longer says how to tell an empty answer from an unreadable one — read one "
        "conversation known to hold messages before reporting that nothing arrived"
    )


def test_the_positive_control_reaches_the_round_and_not_merely_the_file(rendered):
    """The same requirement on the prompt a round is sent: a Jinja construct hides it in the file."""
    assert POSITIVE_CONTROL_MARK in rendered, (
        "the rendered Feishu prompt lost the positive-control sentence — a round is told an "
        "empty round is a correct round and given no way to know the round was empty rather "
        "than blind"

    )


def test_the_cadence_scanner_answers_both_ways():
    """The device earns its verdict: a block is found, and its absence is not silence.

    A scanner that returned the whole text for a missing marker would make every
    assertion above pass on a template that had lost the section.
    """
    present = "\n### 0. Cadence\n\nrule text\n\n### 1. Read\n\nbody\n"
    assert _cadence_block(present) is not None
    assert "rule text" in _cadence_block(present)
    assert "### 1. Read" not in _cadence_block(present), (
        "the scanner ran past the next heading, so it would keep reading a deleted block's "
        "neighbours as if they were part of it"
    )
    assert _cadence_block("\n### 1. Read\n") is None, (
        "a template without the block was reported as carrying one"
    )
