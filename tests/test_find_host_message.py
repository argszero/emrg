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


class TestTheInventory:
    def test_measure_lists_host_messages_and_not_prompts(self, sources):
        done = run(["--measure", *both(sources)])
        assert done.returncode == 0
        assert "host message(s)" in done.stdout
        assert "the first thing the host said" in done.stdout
        assert "## Evolution Cycle" not in done.stdout


class TestAMultiLineRecordIsAReadableRecord:
    """Measured 2026-10-03 (`cyc20261003-085457`): 122 of this host's 156 records.

    The daemon writes the prompt verbatim, so a multi-line message is one record over
    many physical lines and a line-at-a-time reader never matched it. Each leg is
    paired with the one-line form of the same record, so a red is attributable to the
    newline and not to the fixture.
    """

    def _log_only(self, tmp_path: Path, record: str) -> dict:
        """A log with `record` as its only record, plus one covering session row.

        The second source carries no claim of its own - it is there so the span is
        covered and the verdict turns on the **log** record, which is what these legs
        are about.
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "emrgd.log").write_text(
            "2026-09-01 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: starting up\n"
            + record
            + "2026-10-31 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: still running\n",
            encoding="utf-8",
        )
        sess = tmp_path / "sessions" / "sess-a"
        sess.mkdir(parents=True)
        (sess / "history.jsonl").write_text(
            session_row("2026-09-20T10:00:00.000000+08:00", "an unrelated host row") + "\n",
            encoding="utf-8",
        )
        return {"log_dir": log_dir, "sessions": tmp_path / "sessions"}

    @staticmethod
    def _record(ts: str, session: str, lines: list[str]) -> str:
        """A record the way the daemon writes it: prompt verbatim, note on the last line."""
        head = (f'{ts} [INFO] [-] [{session}] emrg.server.daemon: '
                f'task received: session={session} prompt="')
        return head + lines[0] + "\n" + "\n".join(lines[1:]) + '" → routing via LLM\n'

    def test_a_multi_line_host_message_is_found(self, tmp_path):
        """The defect: this answered NOT FOUND about a message on disk."""
        tree = self._log_only(
            tmp_path,
            self._record("2026-09-15 10:00:00", "sess-a",
                         ["the car sinks", "through the track", "look at maplesyrup"]),
        )
        done = run(["--pattern", "maplesyrup", *both(tree)])
        assert done.returncode == 0, done.stdout + done.stderr
        assert "maplesyrup" in done.stdout

    def test_the_one_line_form_of_the_same_record_is_found(self, tmp_path):
        """The control: the newline is what made the difference, not the fixture."""
        tree = self._log_only(
            tmp_path,
            host_line("2026-09-15 10:00:00", "sess-a", "the car sinks and then maplesyrup"),
        )
        done = run(["--pattern", "maplesyrup", *both(tree)])
        assert done.returncode == 0, done.stdout + done.stderr

    def test_a_multi_line_scheduled_prompt_is_set_aside_not_reported(self, tmp_path):
        """The scheduler talking is not the host talking, newline or not.

        Before the fix this record was not read at all, so it was neither reported nor
        counted - it simply did not exist. Now it is read and the host-only default
        must refuse it, which is what the `skipped` count is for.
        """
        tree = self._log_only(
            tmp_path,
            self._record("2026-09-16 10:00:00", "sess-a",
                         ["## Evolution Cycle", "You are EMRG's self-evolution module."]),
        )
        done = run(["--pattern", "Evolution Cycle", *both(tree)])
        assert done.returncode == 1, done.stdout + done.stderr
        assert "1 scheduled prompt(s) set aside" in done.stdout
        # And `--all` does show it, so it was read rather than lost.
        every = run(["--pattern", "Evolution Cycle", "--all", *both(tree)])
        assert every.returncode == 0, every.stdout + every.stderr
        assert "## Evolution Cycle" in every.stdout

    def test_a_record_that_never_closes_is_unmeasurable(self, tmp_path):
        """A record cut off mid-write is a hole: absence is not claimed over it."""
        tree = self._log_only(
            tmp_path,
            '2026-09-17 10:00:00 [INFO] [-] [sess-a] emrg.server.daemon: '
            'task received: session=sess-a prompt="a message that never ends\n',
        )
        done = run(["--pattern", "never-said-this", *both(tree)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not be read" in done.stderr


class TestAHostRowThatCannotBeReadIsAHole:
    """Measured 2026-10-03 (`cyc20261003-083317`): one truncated row, one false absence."""

    def _tree(self, tmp_path: Path, rows: list[str]) -> dict:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "emrgd.log").write_text(
            "2026-09-01 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: starting up\n"
            "2026-10-31 00:00:00 [DEBUG] [-] [-] emrg.server.daemon: still running\n",
            encoding="utf-8",
        )
        sessions = tmp_path / "sessions" / "sess-a"
        sessions.mkdir(parents=True)
        (sessions / "history.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        return {"log_dir": log_dir, "sessions": tmp_path / "sessions"}

    def test_a_truncated_row_refuses_absence(self, tmp_path):
        """The row announces itself as a host row and cannot be read: exit 2, not 1."""
        tree = self._tree(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "hi"),
            session_row("2026-09-16T10:00:00.000000+08:00", "the phrase is maplesyrup")[:-1],
            session_row("2026-09-17T10:00:00.000000+08:00", "bye"),
        ])
        done = run(["--pattern", "maplesyrup", *both(tree)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not be read" in done.stderr

    def test_the_same_tree_readable_answers_one(self, tmp_path):
        """The control for the leg above: same rows, the truncated one made whole."""
        tree = self._tree(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "hi"),
            session_row("2026-09-16T10:00:00.000000+08:00", "the phrase is maplesyrup"),
            session_row("2026-09-17T10:00:00.000000+08:00", "bye"),
        ])
        found = run(["--pattern", "maplesyrup", *both(tree)])
        assert found.returncode == 0, found.stdout + found.stderr
        absent = run(["--pattern", "nobody-said-this", *both(tree)])
        assert absent.returncode == 1, absent.stdout + absent.stderr

    def test_a_match_wins_over_a_hole(self, tmp_path):
        """A hole cannot un-find a message this reader did read."""
        tree = self._tree(tmp_path, [
            session_row("2026-09-16T10:00:00.000000+08:00", "the phrase is maplesyrup"),
            session_row("2026-09-17T10:00:00.000000+08:00", "bye")[:-1],
        ])
        done = run(["--pattern", "maplesyrup", *both(tree)])
        assert done.returncode == 0, done.stdout + done.stderr


class TestASearchSetThatCouldNotBeRead:
    """The session source's *size* is part of its coverage (cyc20261003-112023).

    The index names every session directory this tool reads, and a failure to read it
    used to yield `[]` silently: the search then covered only `./.emrg/sessions` and
    reported absence over that smaller set. Measured: a covering log plus an index
    holding `{}` turned a host message that lives in another project into `NOT FOUND`
    (rc 1), with `1 dir(s)` where an honest tree prints `2` as the only trace. `{}` is
    not hypothetical - it is what the index rebuild wrote while its liveness check could
    not answer (`cyc20261003-110524`).

    Every other test in this file passes `--sessions`, so this path had no coverage at
    all until now; the refusal it pins is why the escape hatch matters.
    """

    def _two_projects(self, tmp_path: Path) -> dict:
        """A cwd session, another project's session, and a log covering the window."""
        cwd = tmp_path / "cwd"
        other = tmp_path / "other"
        for root, sid, text in ((cwd, "s_cwd", "an unrelated note"),
                                (other, "s_other", "THE-HOST-MESSAGE-I-WANT")):
            session = root / ".emrg" / "sessions" / sid
            session.mkdir(parents=True)
            (session / "history.jsonl").write_text(
                session_row("2026-09-15T10:00:00.000000+08:00", text) + "\n",
                encoding="utf-8")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # A log that covers the window, so the *log* is never the reason a verdict is
        # refused - that would make every case below pass for the wrong cause.
        (log_dir / "emrgd.log").write_text(
            "2026-09-15 10:00:00 [DEBUG] [-] [-] emrg.server.daemon: a span line\n",
            encoding="utf-8")
        return {"cwd": cwd, "other": other, "log_dir": log_dir,
                "index": tmp_path / "sessions_index.json"}

    def _run(self, tree: dict, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--pattern", "THE-HOST-MESSAGE-I-WANT",
             "--index", str(tree["index"]), "--log-dir", str(tree["log_dir"]), *extra],
            cwd=str(tree["cwd"]), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def test_the_index_readable_finds_the_message_in_the_other_project(self, tmp_path):
        """The control: with an index that names the directory, it is found."""
        tree = self._two_projects(tmp_path)
        tree["index"].write_text(json.dumps(
            {"s_other": str(tree["other"] / ".emrg" / "sessions" / "s_other")}),
            encoding="utf-8")
        done = self._run(tree)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "THE-HOST-MESSAGE-I-WANT" in done.stdout

    @pytest.mark.parametrize("label,body", [
        ("an empty mapping", "{}"),
        ("a list", "[]"),
        ("a string", '"s_cwd"'),
        ("a truncated file", '{"s_other": "/tmp/x"'),
    ])
    def test_an_index_that_cannot_supply_roots_refuses_absence(self, tmp_path, label, body):
        """The measured defect: rc 1 (`the host never said it`) over a partial search.

        The four bodies are four ways of failing to supply roots, and one verdict
        covers them: the tool cannot tell a complete small set from a truncated large
        one, so it must not claim absence over either.
        """
        tree = self._two_projects(tmp_path)
        tree["index"].write_text(body, encoding="utf-8")
        done = self._run(tree)
        assert done.returncode == 2, f"{label}: {done.stdout}{done.stderr}"
        assert "NOT FOUND" not in done.stdout, f"{label}: absence was claimed"
        assert "incomplete" in done.stdout + done.stderr, done.stdout + done.stderr

    def test_a_missing_index_is_the_same_answer(self, tmp_path):
        """A host that never wrote an index is indistinguishable from one whose index
        was lost, and both leave the search unbounded. `--sessions` is the escape."""
        tree = self._two_projects(tmp_path)
        assert not tree["index"].exists()
        done = self._run(tree)
        assert done.returncode == 2, done.stdout + done.stderr
        assert "does not exist" in done.stdout + done.stderr

    def test_the_escape_hatch_is_naming_the_directories(self, tmp_path):
        """With `--sessions` the caller owns the set, so nothing is refused and the
        message is found even though the index is garbage."""
        tree = self._two_projects(tmp_path)
        tree["index"].write_text("{}", encoding="utf-8")
        done = self._run(tree, "--sessions",
                         str(tree["other"] / ".emrg" / "sessions" / "s_other"))
        assert done.returncode == 0, done.stdout + done.stderr

    def test_the_inventory_is_a_lower_bound_over_a_partial_set(self, tmp_path):
        """`--measure` follows the same rule: rows printed, count labelled, rc 2."""
        tree = self._two_projects(tmp_path)
        tree["index"].write_text("{}", encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--measure", "--index", str(tree["index"]),
             "--log-dir", str(tree["log_dir"])],
            cwd=str(tree["cwd"]), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert done.returncode == 2, done.stdout + done.stderr
        assert "at least" in done.stdout, done.stdout
        assert "lower bound" in done.stderr, done.stderr

    def test_a_match_still_wins_over_a_partial_set(self, tmp_path):
        """Refusing must not hide a message the tool did find."""
        tree = self._two_projects(tmp_path)
        tree["index"].write_text("{}", encoding="utf-8")
        done = self._run(tree, "--sessions",
                         str(tree["cwd"] / ".emrg" / "sessions" / "s_cwd"))
        # the cwd session does not contain it, and --sessions names a complete set,
        # so this is a real absence rather than a refusal
        assert done.returncode == 1, done.stdout + done.stderr
        # `--sessions` is `action="append"`, so two directories are two flags - a bare
        # second path is a usage error, which the test above found rather than assumed.
        found = self._run(tree, "--sessions",
                          str(tree["cwd"] / ".emrg" / "sessions" / "s_cwd"),
                          "--sessions",
                          str(tree["other"] / ".emrg" / "sessions" / "s_other"))
        assert found.returncode == 0, found.stdout + found.stderr


class TestARowThePrefilterWouldHaveHidden:
    """`read_sessions` reads every line (cyc20261003-112023).

    A `'"user"' in line` prefilter stood between the file and the parser, so a row
    truncated *before* its `"user"` marker was neither read nor counted - the one bound
    this reader could not report, documented instead of removed. `Session.append_message`
    writes one `json.dumps` record per line, so a line that does not parse is a hole and
    nothing else; the cost of reading them all is measured in the function.
    """

    def _tree(self, tmp_path: Path, raw_lines: list[str]) -> dict:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "emrgd.log").write_text(
            "2026-09-15 10:00:00 [DEBUG] [-] [-] emrg.server.daemon: a span line\n",
            encoding="utf-8")
        session = tmp_path / "sessions" / "sess-a"
        session.mkdir(parents=True)
        (session / "history.jsonl").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        return {"log_dir": log_dir, "sessions": tmp_path / "sessions"}

    def test_a_row_truncated_before_its_user_marker_is_counted(self, tmp_path):
        """The row is cut inside the leading `{"type": "message", "role": ` so the
        prefilter could never see `"user"` - and the phrase it contains must not be
        reported absent."""
        full = session_row("2026-09-16T10:00:00.000000+08:00", "the phrase is maplesyrup")
        cut = full.split('"user"')[0]
        tree = self._tree(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "hi"),
            cut,
        ])
        done = run(["--pattern", "maplesyrup", *both(tree)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not be read" in done.stderr, done.stderr

    def test_the_same_tree_with_the_row_whole_is_a_real_absence(self, tmp_path):
        """The control, so the leg above cannot pass by refusing everything."""
        tree = self._tree(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "hi"),
            session_row("2026-09-16T10:00:00.000000+08:00", "the phrase is maplesyrup"),
        ])
        absent = run(["--pattern", "nobody-said-this", *both(tree)])
        assert absent.returncode == 1, absent.stdout + absent.stderr
        found = run(["--pattern", "maplesyrup", *both(tree)])
        assert found.returncode == 0, found.stdout + found.stderr

    def test_a_line_that_parses_to_a_non_record_is_a_hole_too(self, tmp_path):
        """`[]` is on disk, so a record the writer meant is not readable as one."""
        tree = self._tree(tmp_path, [
            session_row("2026-09-15T10:00:00.000000+08:00", "hi"),
            "[]",
        ])
        done = run(["--pattern", "nobody-said-this", *both(tree)])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not be read" in done.stderr, done.stderr


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
