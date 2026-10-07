"""Content guards for `emrg/server/prompts/vibe_check.j2` (host directive 2026-10-06T10:40:46).

The measurement this file exists for
------------------------------------
The host asked, twice, that a scheduled task be allowed to say **never slow this
task down**, and on 2026-10-06T10:40:46 named the file the rule belongs in
(pointable: `scripts/find-host-message.py --pattern '禁止降频'`). The directive was
written into **two** carriers on that day:

* `emrg/server/competition_prompt.md` — the task's own statement of the rule; and
* `emrg/server/prompts/vibe_check.j2` — the exception that tells the *judge* to obey
  an explicit prohibition in the task requirement.

Both were uncommitted work; the worktree recovery pinned them into
`refs/emrg/rescue/20261006T035349Z` (commit `1b8ad5d5`, "On master:
emrg-recovery-20261006T035349Z", whose diff is exactly
`competition_prompt.md | 10 +` and `prompts/vibe_check.j2 | 3 +`), and the same 13
lines were also written into the tree the daemon renders from
(`~/.emrg/install/source/`), which is not under version control and is overwritten
whole by the next install/upgrade.

Only **one** of the two reached source: PR #1890/#1891 landed the competition-prompt
half (cycle `cyc20261007-130240`). Measured 2026-10-07 (`cyc20261007-230326`), by the
convention the drift tool uses (`git hash-object --path=<rel>` against `v0.3.7`):
exactly **2 of the 372 files** the install tree shares with that tag differ from it —
`competition_prompt.md` and `prompts/vibe_check.j2`. The shipped template was never
changed: `git rev-parse v0.3.7:… v0.3.8:… master:…vibe_check.j2` are all
`d46dbe84`, so the release the tag chain is part-way through carries no exception
either, and completing it deletes the live copy.

Why the pair is the invariant, not either file alone
----------------------------------------------------
The vibe check is a **separate LLM call with its own system prompt**
(`daemon._task_vibe_check` renders this template). The task requirement reaches it
only as *auxiliary context* (`（辅助上下文）任务要求：{{ prompt }}`), so a rule stated in
`competition_prompt.md` binds the round but not the judge: the judge's own field
description says "由你根据本任务长期是否值得高频运行判断" — by your own judgement —
and that is the reading the host complained about ("我看现在又降频了", 2026-10-06T10:39:45,
repeated at 10:40:46). The task prompt states the rule; this template is what makes
the judge follow it.

What is asserted
----------------
* the exception exists **inside the `recommend_slowdown` bullet** — block-scoped, the
  same way `tests/test_competition_prompt.py` scopes its `### 0.0` assertions, because
  this template also discusses slow runs elsewhere and a whole-file substring check
  would stay green after the block was deleted;
* it is placed after the field's own description and before the next field, so the
  reader meets the field and its exception together;
* it names the phrasings it applies to and states the precedence, because
  "an instruction overrides your judgement" is otherwise a sentence a reader (or a
  model) can read as advice;
* the host's own words and the timestamp are in the template header, so the rule rests
  on a message that can be looked up rather than on this instance's inference (R7);
* the two carriers are checked **together**: deleting either half fails here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "prompts" / "vibe_check.j2"
TASK_PROMPT = REPO_ROOT / "emrg" / "server" / "competition_prompt.md"

#: The field the exception constrains, and the field that follows it — the two edges
#: of the block every assertion below is taken against.
FIELD = "- recommend_slowdown："
NEXT_FIELD = "- slowdown_reason："


def test_the_template_exists() -> None:
    assert TEMPLATE.is_file(), (
        "vibe_check.j2 is missing — `daemon._task_vibe_check` renders it, so the vibe "
        "check has no system prompt at all"
    )


def _recommend_block() -> tuple[str, str]:
    """The `recommend_slowdown` bullet — the block, and the whole template it came from."""
    text = TEMPLATE.read_text(encoding="utf-8")
    parts = text.split(FIELD, 1)
    assert len(parts) == 2, (
        f"the `recommend_slowdown` field is gone from vibe_check.j2 — the host's "
        f"no-slowdown exception (2026-10-06T10:40:46) may not live only in the installed "
        f"copy, which the next upgrade overwrites"
    )
    parts = parts[1].split(NEXT_FIELD, 1)
    assert len(parts) == 2, (
        f"`{NEXT_FIELD}` no longer follows `{FIELD}`, so the block this file reads cannot "
        f"be bounded — the exception has to sit inside the field it constrains"
    )
    return parts[0], text


def test_an_explicit_task_prohibition_overrides_the_judgement() -> None:
    """The exception itself: the field's own instruction is overridden, not weighed."""
    block, _text = _recommend_block()
    assert "例外" in block, (
        "the `recommend_slowdown` field carries no exception — the judge is back to "
        "judging by itself, which is the reading the host complained about twice"
    )
    assert "硬规则" in block, (
        "the exception is not marked as a hard rule, so it reads as one consideration "
        "among others"
    )
    assert "必须返回 `false`" in block, (
        "the exception does not state the required value (`false`): without it the judge "
        "is told to obey an instruction without being told what obeying means here"
    )
    assert "任务要求" in block, (
        "the exception does not name where the prohibition it defers to is written — "
        "the TASK REQUIREMENT is the half its reader has to look for"
    )


def test_the_exception_names_the_phrasings_it_applies_to() -> None:
    """The forms in the task requirement that trigger it, one per carrier it must catch.

    An exception whose trigger is only "禁止降频" is defeated by the same rule written
    as "`recommend_slowdown` must always be false" or as the idle-capacity instruction
    the host stated in the same message — and those are not hypothetical: they are the
    two phrasings the rule in `competition_prompt.md` §0.0 is written in.
    """
    block, _text = _recommend_block()
    for phrasing in ("永不降频", "`recommend_slowdown` 必须恒为 false", "有闲置资源就参加更多比赛"):
        assert phrasing in block, (
            f"the exception no longer names the phrasing {phrasing!r} — a task that "
            f"writes the prohibition that way would not trigger it"
        )


def test_the_precedence_is_stated_not_implied() -> None:
    """The sentence that makes it an override, and the empty reason it leaves behind."""
    block, _text = _recommend_block()
    assert "优先于你的判断" in block, (
        "the exception no longer states which half wins — 'the task requirement's "
        "explicit prohibition overrides your judgement' is the whole rule"
    )
    assert "`slowdown_reason` 留空" in block, (
        "the exception does not say what to return for `slowdown_reason`, so a judge "
        "obeying it can still emit a reason beside `recommend_slowdown: false`"
    )


def test_the_exception_is_placed_before_the_field_it_ends() -> None:
    """Placement, not only presence: the exception is read with the field it constrains.

    Parked elsewhere in the template it would be a rule the judge reads after it has
    already answered this field — or not at all.
    """
    _block, text = _recommend_block()
    field_at = text.index(FIELD)
    next_at = text.index(NEXT_FIELD)
    exception_at = text.index("例外")
    assert field_at < exception_at < next_at, (
        "the exception drifted out of the `recommend_slowdown` bullet — it must sit "
        "between that field and the next one"
    )
    # And the field's own description still comes first: the exception amends the
    # judgement, it does not replace the field's statement of what `false` means.
    assert "由你根据本任务长期是否值得高频运行判断" in text[field_at:exception_at], (
        "the field's own description was replaced by the exception instead of amended "
        "by it — the two answer different questions"
    )


def test_the_host_message_the_rule_rests_on_is_quoted() -> None:
    """A rule recorded as the host's is a message that can be pointed at (R7).

    Quoted verbatim with its timestamp, so the next cycle re-runs
    `find-host-message.py --pattern '禁止降频'` instead of trusting this file. The
    quote lives in the Jinja header comment (not rendered to the model), which is the
    template's own record of where its rules come from.
    """
    _block, text = _recommend_block()
    header = text.split("#}", 1)[0]
    assert "2026-10-06T10:40:46" in header, (
        "the header no longer carries the timestamp of the host message the exception "
        "rests on, so the rule cannot be traced to the directive that produced it"
    )
    assert "禁止降频" in header, (
        "the host's own words are gone from the header — a paraphrase cannot be looked "
        "up, and the rule would rest on this instance's inference"
    )
    assert "find-host-message.py" in header, (
        "the header does not say how to re-find the message"
    )


def test_the_task_prompt_and_the_judge_carry_the_rule_together() -> None:
    """Both carriers, because either one alone leaves the mandate unenforceable.

    Measured 2026-10-07 (`cyc20261007-230326`): the host's directive was written into
    two files on 2026-10-06 and only one of them reached source, so the shipped judge
    could still recommend a slowdown for a task whose requirements forbid it. This is
    the assertion that fails when that happens again in either direction — the task
    prompt losing §0.0 (its own guard, `tests/test_competition_prompt.py`, also holds
    it) or this template losing the exception.
    """
    prompt = TASK_PROMPT.read_text(encoding="utf-8")
    block, _text = _recommend_block()
    assert "### 0.0 " in prompt, (
        "competition_prompt.md no longer states the cadence rule, so the exception this "
        "template carries defers to a requirement that is not there"
    )
    assert "`recommend_slowdown` must be `false`, always" in prompt
    assert "禁止降频" in block or "禁令" in block, (
        "one of the two carriers is missing: the task prompt states the rule and this "
        "template is what makes the judge follow it — the judge never sees the task "
        "prompt as a system prompt, only as auxiliary context"
    )
