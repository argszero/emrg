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
  never `1`. Absence is a claim about a span, and an unmeasured span supports none;
* the **row that will not parse**: dropped with a `continue` until 2026-10-03, which made
  the search smaller than the span it printed - a truncated row carrying the phrase read
  as "no message in the searched span contains" it. It is a channel now: `2`, naming the
  file and line, unless the row's own timestamp survived and predates the window.
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


#: A row cut short mid-record, the shape a partial write leaves. Its `timestamp` field
#: is intact, which is the case `main` can place; `UNDATABLE` damages that field too.
DAMAGED_TS = "2026-09-16T10:00:00.000000+08:00"


def damaged_row(text: str, ts: str = DAMAGED_TS) -> str:
    """A user row with its closing brace (and nothing else) missing."""
    return json.dumps({"type": "message", "role": "user", "content": text,
                       "timestamp": ts}, ensure_ascii=False)[:-1]


#: The same damage, but inside the timestamp - so the row cannot be dated at all.
UNDATABLE_ROW = ('{"type": "message", "role": "user", "content": "a phrase lost here", '
                 '"timestamp": "2026-09-16T10:00:0')


def covering_log(tmp_path: Path) -> Path:
    """A log directory whose span covers the fixture rows, so nothing else is uncovered."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "emrgd.log").write_text(
        "2026-09-01 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: starting up\n"
        "2026-09-30 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: still running\n",
        encoding="utf-8",
    )
    return log_dir


def sessions_with(tmp_path: Path, rows: list[str]) -> Path:
    """One session directory whose daily history is exactly `rows`."""
    session = tmp_path / "sessions" / "sess-a"
    session.mkdir(parents=True)
    (session / "history_260915.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return session


def ask(tmp_path: Path, rows: list[str], *args: str) -> subprocess.CompletedProcess:
    return run([*args, "--log-dir", str(covering_log(tmp_path)),
                "--sessions", str(sessions_with(tmp_path, rows))])


class TestARowThatWillNotParse:
    """A row that could be the host's and cannot be read is not a row that was searched.

    Measured 2026-10-03 (`cyc20261003-083317`, reproduced `cyc20261003-164808`): the row
    carrying the phrase was truncated, the printed span contained it, and the answer was
    "no message in the searched span contains" it - an absence read out of a search that
    never happened, which is the one reading this tool exists to refuse.
    """

    def test_the_phrase_in_a_damaged_row_is_not_reported_absent(self, tmp_path):
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            damaged_row("the host really said maplesyrup here"),
            session_row("2026-09-17T10:00:00.000000+08:00", "another host message"),
        ], "--pattern", "maplesyrup")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "NOT FOUND" not in done.stdout, (
            "the phrase is on disk in a row the tool never read, so absence is not the reading"
        )
        assert "could not be read" in done.stderr
        assert "history_260915.jsonl:2" in done.stderr, (
            "the hole must name the file and line, or a reader cannot go and look"
        )

    def test_the_same_phrase_is_found_when_the_row_is_whole(self, tmp_path):
        """The control: it is the damage that changes the answer, not the phrase."""
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            session_row(DAMAGED_TS, "the host really said maplesyrup here"),
        ], "--pattern", "maplesyrup")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "maplesyrup" in done.stdout

    def test_a_clean_tree_still_answers_absent(self, tmp_path):
        """The other control: "report every hole" must not become "never answer absent"."""
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            session_row("2026-09-17T10:00:00.000000+08:00", "another host message"),
        ], "--pattern", "never-said-this")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout
        assert "could not be read" not in done.stderr

    def test_a_hole_dated_before_the_window_is_excluded(self, tmp_path):
        """The hole is a hole in *the window*, and a row that predates it cannot be in it.

        Without this leg, "any hole blocks every window" would pass the tests above while
        making a tree with one damaged row answer 2 to every absence query forever - the
        over-wide direction, which is its own way of losing the instrument.
        """
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            damaged_row("a phrase nobody asked about"),          # dated 2026-09-16
            session_row("2026-09-17T10:00:00.000000+08:00", "another host message"),
        ], "--pattern", "never-said-this", "--since", "2026-09-17")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "NOT FOUND" in done.stdout

    def test_a_hole_that_cannot_be_dated_blocks_every_window(self, tmp_path):
        """A row whose own timestamp is damaged cannot be placed outside anything."""
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            UNDATABLE_ROW,
            session_row("2026-09-17T10:00:00.000000+08:00", "another host message"),
        ], "--pattern", "never-said-this", "--since", "2026-09-18")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not be read" in done.stderr

    def test_a_match_elsewhere_is_still_reported(self, tmp_path):
        """A hole can only hide a match, never manufacture one, so 0 is unaffected."""
        done = ask(tmp_path, [
            session_row("2026-09-17T10:00:00.000000+08:00", "the host said maplesyrup"),
            damaged_row("an unreadable row"),
        ], "--pattern", "maplesyrup")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "FOUND" in done.stdout

    def test_the_inventory_counts_the_rows_it_could_not_read(self, tmp_path):
        """`--measure` is a count of what was read, so a hole belongs in the count."""
        done = ask(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "an ordinary host message"),
            damaged_row("an unreadable row"),
        ], "--measure")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "could not be read" in done.stdout
        assert "history_260915.jsonl:2" in done.stdout


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
