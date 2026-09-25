"""The shipped template's vote example must produce a vote the counter counts.

Why this file exists
--------------------
The template told a cycle to post its approval as

    gh pr review <N> -R <owner>/<repo> --comment --body "✅ LGTM — cycle"

— **no cycle id**. `scripts/check-vote-count.py` attributes a vote to a cycle *by the
id in the body* (`distinct_cycle_ids`), so a body carrying none is filed
`(no cycle id)` and does not count. `scripts/cast-vote.py` refuses to *post* such a
body for exactly this reason (its refusal says the vote "would be spent in silence",
because `gh pr review` prints nothing either way) — but nothing pinned the shipped
*example*, which is the only carrier the agent reads. A cycle that follows the
template literally believes it approved a PR while the count says it did not.

Why the pin renders instead of grepping the template
----------------------------------------------------
The template is a Jinja2 template rendered with ``undefined=jinja2.Undefined``
(``TaskHandler._build_evolution_prompt`` in ``emrg/server/scheduler.py``). A name that
is not in the builder's context renders as the **empty string**: an id written as
``{{ cycle_id }}`` would ship as ``✅ LGTM — cycle `` — the same defect, with a
file-level grep still passing. So these tests render through the *real builder* and
hand the *rendered* body to the counter's own reader. The id is written as
``{{ timestamp }}``, which is the variable §6 already uses for the cycle record's id,
so the example names the same id the record is written with.

Named limit
-----------
This pins that the example's body is readable as a countable vote for this cycle. It
cannot pin that an agent substitutes its own id when it improvises a body, and it says
nothing about whether the vote *should* be cast (that is the abstain rule, issue #1408).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import TaskHandler

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"
COUNTER = REPO_ROOT / "scripts" / "check-vote-count.py"

#: A postable vote command, as the rendered prompt prints it. Only the bodies matter.
VOTE_EXAMPLE = re.compile(r'gh pr review <N> -R \S+ --comment --body "(?P<body>[^"]*)"')

#: Where a vote is *instructed* as work. The Contributor's `⚠️ Forbidden commands` list
#: also prints `gh pr review ... --body "✅ LGTM..."` lines, but those illustrate what may
#: NOT be run, so they are deliberately not required to carry an id — hence the two
#: regions rather than a scan of the whole prompt.
REVIEW_BLOCK_START = "- Review every open PR"
REVIEW_BLOCK_END = "- **Reviewing PRs IS evolution work**"
FOLLOW_UP_ANCHOR = "If you are a Committer on this repo and there are currently <3"

#: The id the same render tells the cycle to write its record with (§6 Record).
RECORD_ID = re.compile(r"`id`: `(?P<id>cyc\d{8}-\d{6})`")


def _load_counter():
    """`scripts/check-vote-count.py` (hyphenated, so not importable by name)."""
    spec = importlib.util.spec_from_file_location("check_vote_count_vote_body", COUNTER)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module declares dataclasses, and dataclasses resolves
    # through sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def counter():
    return _load_counter()


@pytest.fixture
def rendered(tmp_path, monkeypatch) -> str:
    """The real template, rendered through the real builder with the real context."""
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    project_dir = tmp_path / "demoproj"
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "demoproj", "path": str(project_dir)}]), encoding="utf-8"
    )
    handler = TaskHandler(
        name="demo-task",
        config={"project": "demoproj"},
        interval=300,
        identity=InstanceIdentity(),
        template_path=PROMPTS_DIR / "evolution_prompt.md",
    )
    return handler._build_evolution_prompt()


def _instructed_examples(text: str) -> list[str]:
    """Every vote body the prompt instructs a cycle to post, in prompt order."""
    start = text.find(REVIEW_BLOCK_START)
    end = text.find(REVIEW_BLOCK_END)
    assert start != -1 and end > start, (
        "the review block anchors moved — this test cannot measure the vote examples, "
        "which is a failure to measure, not a pass"
    )
    regions = [text[start:end]]
    follow_up = [ln for ln in text.splitlines() if FOLLOW_UP_ANCHOR in ln]
    assert len(follow_up) == 1, (
        f"expected exactly one follow-up vote instruction, found {len(follow_up)} — the "
        "anchor is ambiguous"
    )
    regions.append(follow_up[0])
    return [m.group("body") for region in regions for m in VOTE_EXAMPLE.finditer(region)]


def _record_id(text: str) -> str:
    match = RECORD_ID.search(text)
    assert match is not None, (
        "the rendered prompt no longer states the cycle record's id (``id``: ``cyc…``) — "
        "without it the examples below have nothing to be checked against"
    )
    return match.group("id")


def test_the_instructed_vote_examples_name_exactly_one_cycle_id(rendered, counter) -> None:
    """Each example body attributes the vote to exactly one cycle — this cycle."""
    examples = _instructed_examples(rendered)
    assert len(examples) == 3, f"expected 3 instructed examples, found {len(examples)}"
    cycle_id = _record_id(rendered)
    for body in examples:
        ids = counter.distinct_cycle_ids(body)
        assert ids == [cycle_id], (
            f"the shipped vote example {body!r} names {ids} — the counter attributes a "
            f"vote by the id in the body and reads this one as "
            f"{counter._cycle_label(_as_vote(counter, body, ids))!r}, so the vote would "
            f"be lost in silence; it must name exactly this cycle ({cycle_id})"
        )


def test_the_instructed_vote_examples_read_as_the_verdict_they_advertise(
    rendered, counter
) -> None:
    """Countable is not enough: the body must still classify as the verdict it shows."""
    by_mark: dict[str, list[str]] = {"✅": [], "❌": []}
    for body in _instructed_examples(rendered):
        for mark in by_mark:
            if body.startswith(mark):
                by_mark[mark].append(body)
    assert len(by_mark["✅"]) == 2 and len(by_mark["❌"]) == 1, by_mark
    for body in by_mark["✅"]:
        assert counter.classify(body) == "approve", body
    for body in by_mark["❌"]:
        assert counter.classify(body) == "veto", body


def _as_vote(counter, body: str, ids: list[str]):
    """A `Vote` carrying what the reader loop derives, for labelling only."""
    return counter.Vote(
        at="2026-09-19T00:00:00Z",
        kind=counter.classify(body),
        cycle=ids[0] if len(ids) == 1 else None,
        valid=len(ids) == 1,
        why="" if len(ids) == 1 else "no cycle id in the vote body",
        ids=tuple(ids),
    )


def test_the_instrument_sees_the_body_that_was_shipped(counter) -> None:
    """The control: the *old* example is exactly the vote the counter drops.

    Without this, a reader cannot tell "the template is fixed" from "the check reports
    the healthy answer for anything". The old body still reads as an approval, which is
    what made the loss silent — and it carries no id, which is why it counted for
    nothing.
    """
    old = "✅ LGTM — cycle"
    assert counter.distinct_cycle_ids(old) == []
    assert counter.classify(old) == "approve", (
        "the dangerous direction: the body reads as an approval, so a cycle that posted "
        "it had every reason to believe it had voted"
    )
    assert counter._cycle_label(_as_vote(counter, old, [])) == "(no cycle id)"
