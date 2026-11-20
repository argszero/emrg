"""The judge's round: who is on the roster, how much each one presents, and the gate.

Three questions, and each has a test that reddens on its own defect:

* **Membership** — activity inside the window decides it, and nothing else does.
* **Reach** — the whole absence, measured from the last judgement rather than from
  the window, so a returning agent is not presented as a busy one.
* **The gate** — an empty roster spends no request, and a skipped round advances no
  window, so the gap carries rather than being lost.

The prompt's own rule is held here too: the judge decides in prose, so its template
carries no digit and no branch, and a threshold added to it is a change this file
refuses.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from emrg.server import life_review as lr
from emrg.server.agents import AgentRegistry
from emrg.server.life_review import (
    describe_span,
    read_watermark,
    render_roster,
    roster,
    skip_reason,
    watermark_path,
    write_watermark,
)
from tests.task_handler_factory import make_handler

TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime(2026, 11, 20, 17, 0, tzinfo=TZ)


def _row(name: str, active: datetime.datetime | None, **extra) -> dict:
    """One register row, shaped as `AgentRegistry.entries()` hands them over."""
    row: dict = {"path": f"/tmp/{name}", "name": name}
    if active is not None:
        row["last_active"] = active.isoformat()
    row.update(extra)
    return row


# ── 1. membership ─────────────────────────────────────────────────────


def test_a_row_active_inside_the_window_is_on_the_roster() -> None:
    since = NOW - datetime.timedelta(minutes=30)
    rows = [_row("busy", NOW - datetime.timedelta(minutes=5))]
    assert [e["name"] for e in roster(rows, since, NOW)] == ["busy"]


def test_a_row_silent_through_the_window_is_not_on_it() -> None:
    """The absence of a row from a round is not a judgement, and must not read as one."""
    since = NOW - datetime.timedelta(minutes=30)
    rows = [_row("quiet", NOW - datetime.timedelta(hours=5))]
    assert roster(rows, since, NOW) == []


def test_a_row_with_no_readable_activity_is_not_on_the_roster() -> None:
    since = NOW - datetime.timedelta(minutes=30)
    rows = [_row("unknown", None), _row("garbled", None, last_active="not a time")]
    assert roster(rows, since, NOW) == []


def test_the_judge_is_not_on_its_own_roster() -> None:
    """A round triggered by the judge's activity would offer its presence as evidence.

    Scoring itself on the activity that caused the round is a closed loop rather than
    a judgement, so the judge is the one row this module leaves out — and it stays in
    the register, reading as unjudged, which is the honest state for it.
    """
    since = NOW - datetime.timedelta(minutes=30)
    rows = [
        _row(lr.JUDGE_AGENT_NAME, NOW - datetime.timedelta(minutes=1)),
        _row("other", NOW - datetime.timedelta(minutes=1)),
    ]
    assert [e["name"] for e in roster(rows, since, NOW)] == ["other"]


# ── 2. reach ─────────────────────────────────────────────────────────


def test_a_returning_agent_presents_its_whole_absence() -> None:
    """The window decides membership; the last judgement decides reach.

    An agent away a fortnight presents those two weeks, not the half hour whose end
    it happened to come back in — the defect this test exists for is a roster that
    hands over the window and calls a returning agent busy.
    """
    since = NOW - datetime.timedelta(minutes=30)
    judged = NOW - datetime.timedelta(days=14)
    rows = [_row("back", NOW - datetime.timedelta(minutes=2), life=40, life_at=judged.isoformat())]
    entry = roster(rows, since, NOW)[0]
    assert entry["since"] == judged
    assert entry["span"] == datetime.timedelta(days=14)
    assert "weeks" in describe_span(entry["span"])


def test_a_never_judged_agent_presents_the_window_and_reads_unjudged() -> None:
    since = NOW - datetime.timedelta(minutes=30)
    rows = [_row("fresh", NOW - datetime.timedelta(minutes=3))]
    entry = roster(rows, since, NOW)[0]
    assert entry["life"] is None
    assert entry["since"] == since
    assert "never judged" in render_roster([entry])
    assert "0 (" not in render_roster([entry]), (
        "an unjudged row must not render as a zero — a zero is a judgement"
    )


def test_span_words_stay_coarse() -> None:
    """No unit smaller than a minute, and nothing that has not happened reads as a span."""
    assert describe_span(datetime.timedelta(seconds=-5)) == "just now"
    assert describe_span(datetime.timedelta(seconds=30)) == "just now"
    assert describe_span(datetime.timedelta(minutes=35)) == "35 minutes"
    assert describe_span(datetime.timedelta(hours=5)) == "5 hours"
    assert describe_span(datetime.timedelta(days=3)) == "3 days"
    assert describe_span(datetime.timedelta(days=15)) == "2 weeks"


# ── 3. the gate ──────────────────────────────────────────────────────


def test_an_empty_roster_refuses_the_round() -> None:
    since = NOW - datetime.timedelta(minutes=30)
    reason = skip_reason([_row("quiet", NOW - datetime.timedelta(hours=4))], since, NOW)
    assert reason is not None
    assert "since" in reason


def test_a_roster_with_one_agent_admits_the_round() -> None:
    since = NOW - datetime.timedelta(minutes=30)
    assert skip_reason([_row("busy", NOW - datetime.timedelta(minutes=1))], since, NOW) is None


def test_the_empty_roster_renders_a_line_rather_than_nothing() -> None:
    """A placeholder, so an empty render is visible as empty rather than as a gap."""
    assert render_roster([]) == "(nobody was active in this window)"


# ── 4. the watermark ─────────────────────────────────────────────────


def test_the_watermark_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "life-review" / "judge.json"
    assert read_watermark(path) is None
    write_watermark(path, NOW)
    assert read_watermark(path) == NOW


def test_a_damaged_watermark_reads_as_absent(tmp_path: Path) -> None:
    """Unreadable is "no window settled yet", never a refusal of every round.

    The distinction matters: refusing over a damaged file would stop the review for
    ever and say nothing, while treating it as absent re-presents a window — twice
    judged is a fault, and a review that has stopped is a bigger one.
    """
    path = tmp_path / "judge.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_watermark(path) is None
    path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert read_watermark(path) is None


#: The cap a spawned watermark probe is given. The defect it catches is an *open that
#: never returns*, so the failure mode is a hang rather than an exception: a cap is the
#: only reading that separates "answered" from "still waiting", and 8 s is far above the
#: 0.00 s a watermarked probe needs.
_PROBE_CAP_SECONDS = 8


def _probe(source: str) -> None:
    """Run ``source`` in its own process under the cap, so a blocked open reddens.

    Spawned rather than called in-process on purpose: a FIFO read still waiting at the
    cap would hang the suite, and this file is not worth a wedged run. ``errors="replace"``
    keeps a child's own traceback readable when the platform encodes it unexpectedly.
    """
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PROBE_CAP_SECONDS,
        cwd=str(Path(lr.__file__).parent.parent.parent),
    )
    assert result.returncode == 0, (
        f"the probe answered rc={result.returncode}: {result.stdout}{result.stderr}"
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_a_pipe_at_the_watermark_reads_as_absent_instead_of_blocking(tmp_path: Path) -> None:
    """A FIFO is answered ``None``, like a damaged file — and answered at all.

    The watermark lives under the config root, and the scheduler reads it on a tick: an
    open that waits for a peer there waits on the daemon's own thread, which is the whole
    daemon rather than one round. Nothing in the file can say "not a regular file", so the
    property has to be exercised. Without the kind check this probe never returns and the
    cap reddens it; with it, the answer is the same absence a damaged file gives.
    """
    fifo = tmp_path / "life-review" / "judge.json"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    _probe(
        "import sys;"
        "from pathlib import Path;"
        "from emrg.server.life_review import read_watermark;"
        f"sys.exit(0 if read_watermark(Path({str(fifo)!r})) is None else 1)"
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_the_watermark_writer_never_opens_a_path_it_did_not_create(tmp_path: Path) -> None:
    """A pipe planted where the old temporary file went does not block the write.

    The writer used to name its scratch file ``<name>.tmp`` and open it, so anything able
    to create that path could plant a FIFO the next round would wait on. It now creates a
    file under a name nobody can predict — measured by planting the old name, which this
    writer must not touch. The first half pins the ordinary case, because a writer that
    stopped writing would pass the second half trivially.
    """
    path = tmp_path / "life-review" / "judge.json"
    write_watermark(path, NOW)
    assert read_watermark(path) == NOW, "the ordinary write stopped round-tripping"
    legacy = path.with_name(path.name + ".tmp")
    os.mkfifo(legacy)
    _probe(
        "import sys, datetime;"
        "from pathlib import Path;"
        "from emrg.server.life_review import write_watermark, read_watermark;"
        f"p = Path({str(path)!r});"
        f"write_watermark(p, datetime.datetime(2026, 11, 20, 18, 0, tzinfo=datetime.timezone.utc));"
        "print(read_watermark(p).hour)"
    )
    assert read_watermark(path) is not None, "the probe wrote and the value did not land"
    assert legacy.exists(), "the planted pipe was replaced — the writer opened it"


# ── 5. the prompt ────────────────────────────────────────────────────


def _prompt_text() -> str:
    return (
        Path(lr.__file__).parent / "life_review_prompt.md"
    ).read_text(encoding="utf-8")


def test_the_judges_prompt_carries_no_digit_and_no_judging_branch() -> None:
    """Prose is the subject, and this is what keeps it prose.

    A digit is a threshold waiting to be written. A Jinja tag is a condition followed
    instead of a judgement — and exactly one tag pair is allowed, because **every**
    built-in template carries the installation's `extra_prompt` channel and the guard
    over them requires it. So the assertion is the set of tags, not the presence of
    one: any second tag is a branch nobody declared, and a threshold written as
    `{% if value < … %}` reddens here before it can be read as guidance.
    """
    text = _prompt_text()
    digits = [line for line in text.splitlines() if any(ch.isdigit() for ch in line)]
    assert digits == [], f"the judge's prompt carries a digit: {digits[:3]}"
    tags = set(re.findall(r"\{%-?\s*(.*?)\s*-?%\}", text))
    assert tags == {"if task.extra_prompt", "endif"}, (
        "the judge's prompt carries a Jinja tag beyond the installation channel, so it "
        f"carries a branch to follow instead of a judgement: {sorted(tags)}"
    )
    assert "{{ life_roster }}" in text, "the roster placeholder is what the gate fills"


def test_the_review_type_is_registered_and_resolves_to_its_template() -> None:
    from emrg.server.scheduler import TASK_TEMPLATES, _resolve_task_template

    assert TASK_TEMPLATES[lr.LIFE_REVIEW_TASK_TYPE] == "life_review_prompt.md"
    resolved = _resolve_task_template(lr.LIFE_REVIEW_TASK_TYPE)
    assert resolved.name == "life_review_prompt.md"
    assert resolved.exists()


def test_every_name_the_template_reads_is_in_the_render_context() -> None:
    """A name the context omits renders as the empty string under `Undefined`.

    So "the template renders" proves nothing on its own, and the question worth
    asking is the one this asserts: every `{{ name }}` the judge's prompt reads is a
    key `evolution_context` actually supplies.
    """
    import re as _re

    from emrg.server.scheduler import evolution_context

    context = evolution_context(
        instance_id="i", host_name="h", repo_url="u", local_source="l",
        owner="o", repo="r", source_dir="s", session_id="sid",
        task={}, agent={}, life_roster="(roster)", now=NOW,
    )
    names = set(_re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", _prompt_text()))
    assert names, "the prompt reads no name at all, so this asserts nothing"
    missing = sorted(names - set(context))
    assert missing == [], f"the template reads names the context omits: {missing}"


# ── 6. the round, end to end ─────────────────────────────────────────


def _drive(handler, seen, *, caplog_text=None, timeout: float = 4.0) -> None:
    """Wake the handler once and let the gate answer.

    A review task's first wait is its own interval (sixty seconds at least), so the
    round is reached with `trigger()` rather than by waiting: the gate sits on the
    same path either way, which is the property under test.
    """

    async def _run() -> None:
        task = asyncio.create_task(handler.run())
        try:
            await asyncio.sleep(0.1)
            handler.trigger()
            end = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < end:
                if seen():
                    return
                if caplog_text is not None and caplog_text():
                    return
                await asyncio.sleep(0.02)
        finally:
            handler.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def _review_handler(tmp_path: Path, monkeypatch, *agents: tuple[str, datetime.datetime | None]):
    """A review handler whose register, tasks table and watermark all live in `tmp_path`.

    The register is written directly rather than through `upsert`, because `upsert`
    stamps every row with the present instant — which would make every agent "active
    in this window" and leave the empty-roster case untestable. The instants are the
    subject here, so the fixture states them.
    """
    import yaml

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "agents_file", lambda: tmp_path / "agents.yml")
    monkeypatch.setattr(mod, "watermark_path", lambda name: tmp_path / "life-review" / f"{name}.json")
    mod.write_table(
        [{"name": "life-judge-task", "type": lr.LIFE_REVIEW_TASK_TYPE,
          "config": {}, "interval": 60, "enabled": True}],
        tmp_path / "tasks.yml",
    )
    rows = []
    for name, active in agents:
        (tmp_path / name).mkdir(exist_ok=True)
        row = {"path": str(tmp_path / name), "name": name}
        if active is not None:
            row["last_active"] = active.isoformat()
        rows.append(row)
    (tmp_path / "agents.yml").write_text(yaml.safe_dump(rows), encoding="utf-8")
    handler = make_handler(name="life-judge-task", config={}, interval=60,
                           type=lr.LIFE_REVIEW_TASK_TYPE)
    handler._build_evolution_prompt = lambda: "test prompt"
    return handler


def test_an_empty_roster_starts_no_round_and_leaves_the_window_open(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """The whole point of the gate: nobody to judge costs no request.

    The window is already settled — the first round is exempt on purpose, presenting
    every agent that has ever worked — and the only row has been silent for four
    hours, so this half hour has nobody in it. Both halves are measured: no cycle ran,
    **and** the watermark still reads the instant it held, so the idle window is
    carried into the next wake instead of being consumed here.
    """
    settled = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=30)
    written = tmp_path / "life-review" / "life-judge-task.json"
    write_watermark(written, settled)
    handler = _review_handler(
        tmp_path, monkeypatch,
        ("idle-agent", settled - datetime.timedelta(hours=4)),
    )
    ran: list[str] = []
    handler._run_cycle_bounded = lambda: _record(ran)

    with caplog.at_level(logging.INFO):
        _drive(handler, lambda: False, caplog_text=lambda: _said(caplog, "no round"))

    assert ran == [], "a round with an empty roster spent a request"
    assert _said(caplog, "no round"), "the skip was silent, so nothing records why"
    assert read_watermark(written) == settled, (
        "a skipped round moved the window, so the gap is lost rather than carried"
    )


def test_an_admitted_round_hands_over_the_roster_and_advances_the_window(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    handler = _review_handler(
        tmp_path, monkeypatch,
        ("busy-agent", datetime.datetime.now().astimezone() - datetime.timedelta(minutes=2)),
    )
    ran: list[str] = []
    handler._run_cycle_bounded = lambda: _record(ran)

    with caplog.at_level(logging.INFO):
        _drive(handler, lambda: bool(ran))

    assert ran, "a round with an agent on its roster never started"
    assert "busy-agent" in handler._life_roster_text, (
        f"the judge was handed no roster: {handler._life_roster_text!r}"
    )
    written = tmp_path / "life-review" / "life-judge-task.json"
    assert written.exists(), (
        "the window was not advanced, so the next round would present it again"
    )
    assert read_watermark(written) is not None


async def _record(sink: list[str]) -> str:
    sink.append("ran")
    return "done"


def _said(caplog, needle: str) -> bool:
    return any(needle in record.getMessage() for record in caplog.records)
