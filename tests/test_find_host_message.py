"""Tests for scripts/find-host-message.py - an attributed directive must be a message.

Background (cycle cyc20260928-075201, measured 2026-09-28)
----------------------------------------------------------
A cycle recorded a rule as the host's — `host 2026-09-28T07:47` — extended a guard's
remedy text around it, and wrote mutation arms to pin it. No host message contains any
such directive, none was sent in that window at all, and the host's recorded ruling the
day before says the opposite. The remedy is an instrument that answers the question
before anything is built on the claim, and this module pins it in the three states that
matter — including the one a hand-rolled search gets wrong in silence.

What is pinned, in both directions (#455)
-----------------------------------------
* the **60-character head cap**: the daemon keeps a prefix, so a pattern past that point
  is invisible to the log alone. The test asserts such a message is still *found*, via
  the session-history source — an instrument that reported "not found" for a message the
  host sent would be worse than no instrument;
* the **scheduled prompt**: every rendered task prompt is a `task received` line, and a
  pattern drawn from one would be "found" while the host never sent it — so the host-only
  default must refuse it, and say how many it set aside;
* the **uncovered window**: `--since` older than either source reaches back to is `2`,
  never `1`. Absence is a claim about a span, and an unmeasured span supports none.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "find-host-message.py"
PROMPT = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"
CANONICAL = "uv run --no-sync python3 scripts/find-host-message.py"


def _load():
    spec = importlib.util.spec_from_file_location("find_host_message", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["find_host_message"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def host_line(ts: str, session: str, text: str) -> str:
    """A daemon log line shaped exactly as the daemon writes one."""
    return (f'{ts} [INFO] [-] [{session}] emrg.server.daemon: task received: '
            f'session={session} prompt="{text}" → routing via LLM')


def session_row(ts: str, text: str, role: str = "user") -> str:
    return json.dumps({"type": "message", "role": role, "content": text,
                       "timestamp": ts}, ensure_ascii=False)


@pytest.fixture
def sources(tmp_path: Path) -> dict:
    """A log directory and a session directory, with a claimable span in both."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "emrgd.log").write_text(
        "\n".join([
            # The span's two ends, so a window inside it is coverable.
            "2026-09-01 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: starting up",
            host_line("2026-09-15 10:00:00", "sess-a", "the first thing the host said"),
            # A scheduled task prompt: the scheduler talking, not the host.
            host_line("2026-09-16 10:00:00", "sess-a",
                      "## Evolution Cycle - the scheduler rendered this template"),
            "2026-09-30 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: still running",
        ]) + "\n",
        encoding="utf-8",
    )
    sessions = tmp_path / "sessions"
    (sessions / "sess-a").mkdir(parents=True)
    (sessions / "sess-a" / "history_260915.jsonl").write_text(
        "\n".join([
            session_row("2026-09-15T10:00:00.000000+08:00", "the first thing the host said"),
            # The tail past the log's 60-character cap, and nothing else in this row.
            session_row("2026-09-17T10:00:00.000000+08:00",
                        "x" * MOD.LOG_HEAD_CHARS + " and then the tail says maplesyrup"),
            # An assistant row: never a host message.
            session_row("2026-09-18T10:00:00.000000+08:00", "a reply mentioning maplesyrup",
                        role="assistant"),
            # A phrase the host's own rows never contain, in an assistant row and in a
            # tool row: what a "NOT FOUND" has to be told apart from.
            session_row("2026-09-19T10:00:00.000000+08:00", "a reply about honeysuckle",
                        role="assistant"),
            json.dumps({"type": "tool_result", "tool_call_id": "call_1",
                        "content": "tool output about honeysuckle",
                        "timestamp": "2026-09-20T10:00:00.000000+08:00"},
                       ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    return {"log_dir": log_dir, "sessions": sessions}


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def both(sources: dict) -> list[str]:
    return ["--log-dir", str(sources["log_dir"]),
            "--sessions", str(sources["sessions"] / "sess-a")]


class TestTheHeadCap:
    def test_the_daemon_keeps_a_prefix_and_the_tool_says_so(self):
        line = host_line("2026-09-15 10:00:00", "sess-a", "x" * 200)
        message = MOD.parse_log_line(line)
        assert message is not None
        # The fixture line carries the full 200 characters; the cap is what the *daemon*
        # applies when it writes, measured at 60. The flag therefore fires exactly at
        # the cap, and `describe` says so rather than leaving the reader to guess.
        assert message.truncated is (len(message.text) == MOD.LOG_HEAD_CHARS)
        assert "head only" in MOD.describe(
            MOD.Message("2026-09-15 10:00:00", "sess-a", "x" * 60, "log", True)
        )

    def test_a_message_found_only_past_the_cap_is_still_found(self, sources):
        """The discrimination: a log-only search would answer "not found" here."""
        done = run(["--pattern", "maplesyrup", *both(sources)])
        assert done.returncode == 0, done.stdout + done.stderr
        assert "sessions" in done.stdout      # the source that found it is named
        assert "maplesyrup" in done.stdout

    def test_an_assistant_row_is_not_a_host_message(self, sources):
        """Both rows mention the word; only the host's may be reported."""
        done = run(["--pattern", "maplesyrup", *both(sources)])
        assert done.returncode == 0
        assert "a reply mentioning" not in done.stdout


class TestTheSchedulerIsNotTheHost:
    def test_a_rendered_task_prompt_is_set_aside(self, sources):
        done = run(["--pattern", "Evolution Cycle", *both(sources)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout
        assert "1 scheduled prompt(s) set aside" in done.stdout
        # ...and the verdict says where the phrase *is*: the scheduler's own row, which is
        # the same refusal read as a fact rather than as a suspicion about the tool.
        assert "scheduled prompt 1" in done.stdout

    def test_all_searches_them_too(self, sources):
        done = run(["--pattern", "Evolution Cycle", "--all", *both(sources)])
        assert done.returncode == 0
        assert "sess-a" in done.stdout


class TestTheThreeStates:
    def test_found_names_the_message(self, sources):
        done = run(["--pattern", "the first thing the host said", *both(sources)])
        assert done.returncode == 0
        assert "2026-09-15 10:00:00" in done.stdout
        assert "FOUND" in done.stdout

    def test_absent_with_both_sources_covering_the_window(self, sources):
        done = run(["--pattern", "never-said-this", *both(sources)])
        assert done.returncode == 1
        assert "NOT FOUND" in done.stdout
        # The span searched is printed before the verdict, so a reader can see what the
        # absence is about rather than trusting it.
        assert "2026-09-01 00:00:00" in done.stdout

    def test_an_uncovered_window_is_two_never_one(self, sources):
        done = run(["--pattern", "never-said-this", "--since", "2020-01-01", *both(sources)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "unmeasurable" in done.stderr
        assert "not evidence of absence" in done.stderr

    def test_a_window_inside_the_span_is_answerable(self, sources):
        done = run(["--pattern", "the first thing the host said",
                    "--since", "2026-09-14", *both(sources)])
        assert done.returncode == 0

    def test_no_source_is_unmeasurable(self, tmp_path):
        done = run(["--pattern", "anything", "--log-dir", str(tmp_path / "gone"),
                    "--sessions", str(tmp_path / "also-gone"), "--index",
                    str(tmp_path / "no-index.json")])
        assert done.returncode == 2
        assert "unmeasurable" in done.stderr

    def test_a_missing_pattern_is_unmeasurable(self, sources):
        done = run(both(sources))
        assert done.returncode == 2
        assert "--pattern is required" in done.stderr

    def test_a_non_padded_window_is_refused_not_answered(self, sources):
        """The window start is compared **lexically**, so a form that is not the
        fixed-width canonical spelling defuses the comparison rather than failing
        it loudly: `2026-9-28` sorts *after* every canonical timestamp of that day
        ('9' > '0' at the month position), so every message is skipped and the run
        answers "no message in the searched span contains ..." about a message the
        host really did send.

        Measured on this host 2026-09-28, on the live records rather than a fixture:
        the phrase 「禁止跑后台任务」 finds 4 matches with `--since 2026-09-28` and found
        none with `--since 2026-9-28`. That is this script's whole reason for
        existing — an absence read out of search that never happened — so the
        near-miss is refused (2) instead of being padded into the window the caller
        probably meant.
        """
        done = run(["--pattern", "the first thing the host said",
                    "--since", "2026-9-15", *both(sources)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "unmeasurable" in done.stderr
        assert "NOT FOUND" not in done.stdout, (
            "a window the tool cannot read must not be answered as absence"
        )
        # The padded spelling of the very same instant does find it — which is what
        # makes this a parser defect and not a claim about the fixture.
        ok = run(["--pattern", "the first thing the host said",
                  "--since", "2026-09-15", *both(sources)])
        assert ok.returncode == 0, ok.stdout + ok.stderr

    def test_a_window_that_is_not_a_date_is_refused(self, sources):
        """`yesterday` cannot be resolved against records, and guessing it would
        silently choose a window — the same failure as the non-padded spelling."""
        done = run(["--pattern", "anything", "--since", "yesterday", *both(sources)])
        assert done.returncode == 2
        assert "unmeasurable" in done.stderr
        assert "YYYY-MM-DD" in done.stderr

    def test_a_window_without_seconds_is_accepted(self, sources):
        """The accepted forms are enumerated, so the refusals above are about the
        spelling and not about being strict for its own sake: a date, a date with
        `T` and minutes, and a full instant all read."""
        for spelling in ("2026-09-15", "2026-09-15T10:00", "2026-09-15 10:00:00"):
            done = run(["--pattern", "the first thing the host said",
                        "--since", spelling, *both(sources)])
            assert done.returncode == 0, f"{spelling}: {done.stdout}{done.stderr}"

    def test_a_window_start_after_the_message_excludes_it(self, sources):
        """The control for the parser work: a readable window must still *narrow*,
        or "refuse what it cannot read" would have been bought by ignoring `--since`
        altogether and answering about the whole span."""
        done = run(["--pattern", "the first thing the host said",
                    "--since", "2026-09-16", *both(sources)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout


class TestAbsentFromTheHostIsNotAbsentFromTheRecord:
    """The discrimination the verdict needed (measured 2026-10-03, cycle cyc20261003-043835).

    A peer's PR body asserted that this instrument "finds both messages, verbatim" for a
    phrase that occurs 21 times on this host and **zero** times as a host row — so the
    refusal was right and its one line could not show why, and the instrument was read as
    broken instead. `1` now carries the count it is excluding, which is what separates
    *the claim quotes something other than the host* (actionable: go look at the claim)
    from *nothing here is about it* (actionable: this is not readable from this host).
    """

    def test_a_phrase_only_non_host_rows_carry_is_named_where_it_is(self, sources):
        done = run(["--pattern", "honeysuckle", *both(sources)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout
        # One distinct row per kind, so the breakdown is what it claims to be.
        assert "message/assistant 1" in done.stdout, done.stdout
        assert "tool_result 1" in done.stdout, done.stdout
        # ...and the reader is told what that means, not left to infer it.
        assert "not the host's" in done.stdout
        assert "not readable from this host" in done.stdout

    def test_a_phrase_nowhere_in_the_record_says_so(self, sources):
        """The other direction of the same line: absence *everywhere*, not just the host's
        part of it. Without this arm the line above would pass on a counter that counted
        every row and called it "not the host's"."""
        done = run(["--pattern", "never-said-this", *both(sources)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout
        assert "nothing else in the span contains the phrase" in done.stdout, done.stdout
        assert "not the host's" not in done.stdout

    def test_the_host_s_own_row_is_not_counted_as_elsewhere(self, sources):
        """The control for the counter: the host's row is the verdict's business, and
        counting it as an "elsewhere" mention would report the phrase as both."""
        done = run(["--pattern", "the first thing the host said", *both(sources)])
        assert done.returncode == 0, done.stdout + done.stderr
        assert "FOUND" in done.stdout
        assert "not the host's" not in done.stdout

    def test_the_count_is_bounded_by_the_window(self, sources):
        """The same window as the verdict, or the numbers would be about a different span
        than the one the absence claims."""
        done = run(["--pattern", "honeysuckle", "--since", "2026-09-20", *both(sources)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "tool_result 1" in done.stdout, done.stdout      # 2026-09-20, kept
        assert "message/assistant" not in done.stdout           # 2026-09-19, outside it


class TestTheInventory:
    def test_measure_lists_host_messages_and_not_prompts(self, sources):
        done = run(["--measure", *both(sources)])
        assert done.returncode == 0
        assert "host message(s)" in done.stdout
        assert "the first thing the host said" in done.stdout
        assert "## Evolution Cycle" not in done.stdout


class TestTheTemplateCarriesTheStep:
    """A tool nothing tells a cycle to run is dead code (measured, this cycle)."""

    def test_the_rant_block_requires_pointing_at_the_message(self):
        text = PROMPT.read_text(encoding="utf-8")
        assert "find-host-message.py" in text
        for term in ("must be a message you can point at",
                     "never as the host's words",
                     "which is not a"):
            assert term in text, f"the template no longer states: {term}"

    def test_the_canonical_command_in_the_template_is_the_real_one(self):
        text = PROMPT.read_text(encoding="utf-8")
        assert CANONICAL in text
        assert SCRIPT.exists()
