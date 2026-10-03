#!/usr/bin/env python3
"""Did the host actually send the message a cycle is about to attribute to it?

Usage
-----
    uv run --no-sync python3 scripts/find-host-message.py --pattern '每个issue应该'
    uv run --no-sync python3 scripts/find-host-message.py --pattern '拒绝' --since 2026-09-26
    uv run --no-sync python3 scripts/find-host-message.py --measure   # the inventory

Why this exists
---------------
A cycle records rules *as the host's* — in a rant, a memory, an issue, or an edit to
`emrg/server/evolution_prompt.md` — and nothing ever asked the host's own record
whether the message existed. Measured 2026-09-28 (`cyc20260928-075201`): a cycle
recorded a rejection-path rule as `host 2026-09-28T07:47`, extended a guard and its
remedy text around it, and wrote two mutation arms to pin it. No host message contains
any such directive, and **none was sent in that window at all**; the host's ruling the
day before says the opposite ("发生pr被拒绝或者要求更正时，应该还是在这个pr里更新" —
a rejected PR is updated in place, `2026-09-26 18:52:30`). The cost was a whole
cycle's work, and the near-miss was shipping a false attribution into the template
every instance reads.

The question is answerable because the daemon logs what it receives, and a host
message appears there as one line:

    <local time> [INFO] ... task received: session=<sid> prompt="<text>" → routing via LLM

Two sources, because neither is sufficient
------------------------------------------
**The log** covers every session without the caller knowing where any session lives,
and it is the daemon's own record rather than a transcript. But it keeps only the
first ``LOG_HEAD_CHARS`` characters of each message — measured on this host
2026-09-28, a 60-character row ended mid-URL with no ellipsis, so a reader cannot tell
a truncated message from a short one by looking. A pattern occurring only past that
point matches nothing here, which is why a log-only search would answer "the host
never said it" about a message the host did send.

**Session histories** carry the full text, but only for the directories the caller
knows about, so they cannot bound the search by themselves.

So a match in either is a match, and "not found" is only reported when **both** sources
reached back past the window being claimed. Otherwise the answer is ``2``, unmeasurable
— the state that matters most, because it is the one a hand-rolled `grep` gets wrong in
silence: this tool's own author's first search for that directive was cut off by a
timeout and read as "no such message".

What this cannot measure
------------------------
A host message older than the oldest readable log line *and* older than every session
history on the machine. Both spans are printed, so the answer is always bounded by a
span a reader can see; `--since` before either span is reported as ``2`` rather than as
absence. Messages the host typed into a client that never reached the daemon left no
record anywhere and are outside both sources — as is any channel this repo does not
know about.

Exit codes
----------
``0`` found (prints the message). ``1`` no host message in the searched span contains
the pattern, and both sources covered the span. ``2`` the question could not be
answered: no source, a `--since` older than what either source reaches back to, or a
session row that **could have been a host message and could not be read**. ``2`` is not
a ``1``: an unmeasured window is never evidence of absence.

A row that will not parse is not skipped
----------------------------------------
A history line is a JSON record, and one that fails to parse used to be dropped with a
`continue` - which made the search quietly smaller than the span it printed. Measured
2026-10-03 (`cyc20261003-083317`): a truncated row carried the phrase, the reported span
(``2026-09-15 -> 2026-09-17``) contained that row, and the answer was
"no message in the searched span contains" it. The row is now a channel: `main` answers
``2`` and names the file and line, unless the damage left the row's own timestamp
readable **and** that timestamp is before the window - the one case in which the hole
cannot hold an in-window message. A hole whose timestamp is itself damaged blocks every
window, deliberately: a line that cannot be dated cannot be placed outside one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: Where the daemon writes its received-message record, and where it keeps the index
#: of session directories that the second source is read from.
DEFAULT_LOG_DIR = Path.home() / ".emrg"
DEFAULT_INDEX = Path.home() / ".emrg" / "sessions_index.json"

#: The daemon's line for one received message. The prompt is quoted and may be
#: followed by the routing note; both the quote and the note are optional in the
#: sense that older lines predate either.
LOG_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*?task received: '
    r'session=(?P<session>\S+) prompt="(?P<prompt>.*?)"(?: → routing.*)?$'
)

#: A line's leading local timestamp, read without parsing the rest of the line: it is
#: what bounds the window this source can speak about.
LINE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")

#: Measured 2026-09-28 on this host: the daemon keeps the first 60 characters of the
#: message. That number is the whole reason the second source exists, so it is named
#: here rather than inferred per line — an exactly-60-character message and a truncated
#: one are indistinguishable in the log, and this is the only way to say which.
LOG_HEAD_CHARS = 60

#: A record's own timestamp, read straight out of a line that will **not** parse. A damaged
#: line is a hole in the span; this is the one fact that can place it, and only when the
#: damage left the field intact (a line truncated after it, which is what a partial write
#: produces). A line this cannot date is treated as in-window, deliberately - see
#: `read_sessions`.
RAW_TS = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')

#: A scheduled task's own prompt, as the daemon logged it. The scheduler talking is not
#: the host talking, and conflating them is a measured error mode rather than a worry:
#: every such prompt is a rendered template, so a pattern drawn from one of them (or
#: from a rule this repo wrote into one) would be "found" while the host never sent it.
TASK_PROMPT_PREFIX = "## "


@dataclass
class Message:
    """One message one source recorded, with the source that showed it."""

    ts: str          # normalised to `YYYY-MM-DD HH:MM:SS`, so spans compare lexically
    session: str
    text: str
    source: str      # "log" | "sessions"
    truncated: bool  # the log's head cap, reached exactly


def normalise_ts(ts: str) -> str:
    """`2026-09-28T07:47:09.424270+08:00` -> `2026-09-28 07:47:09`.

    One spelling for both sources, because the alternative is two parsers that can
    disagree about which messages a window contains. Fixed-width, so the comparison
    is lexical and needs no timezone handling: both sources write host-local time.

    This is for timestamps the sources produced, which are always canonical. A
    value a caller typed goes through `parse_since` instead: truncating an
    arbitrary string here is what made `--since 2026-9-28` answer "NOT FOUND".
    """
    return ts.replace("T", " ")[:19]


#: The one spelling `--since` accepts: a zero-padded ISO date, optionally with a
#: time (space or `T`, seconds optional). Everything else is refused rather than
#: coerced — see `parse_since`.
SINCE_FORMS = (
    ("%Y-%m-%d %H:%M:%S", 19),
    ("%Y-%m-%d %H:%M", 16),
    ("%Y-%m-%d", 10),
)


def parse_since(value: str) -> str | None:
    """The window start a caller typed, in the sources' spelling, or ``None``.

    The comparison this feeds is **lexical** (`message.ts < since`), which is only
    sound while both sides are the fixed-width canonical spelling. A caller's
    string was passed through `normalise_ts` untested until 2026-09-28, and the
    non-padded `--since 2026-9-28` sorts *after* every canonical timestamp of that
    day ('9' > '0' at the month position), so every message was skipped and the run
    answered "NOT FOUND: no message in the searched span contains ..." about a
    message the host had sent that morning — measured: the same phrase with
    `--since 2026-09-28` finds 4 matches, with `--since 2026-9-28` finds none.

    That is the failure this script exists to prevent (its own author's first
    search was cut off by a timeout and read as "no such message"), so an
    unparsable or non-canonical value is refused by the caller of this function
    with exit code 2 — unmeasurable — rather than quietly redefining the window.
    Padding `2026-9-28` for the caller was the alternative and is worse: it accepts
    one near-miss and still cannot say what `28/09/2026` or `yesterday` meant.

    :param value: whatever the caller passed to `--since`.
    :returns: `YYYY-MM-DD HH:MM:SS`, or None when the value is not that form.
    """
    text = value.strip().replace("T", " ")
    for form, width in SINCE_FORMS:
        if len(text) == width:
            try:
                return datetime.strptime(text, form).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    return None


def parse_log_line(line: str) -> Message | None:
    """The message a daemon log line records, or None if it records none."""
    match = LOG_LINE.match(line.rstrip("\n"))
    if match is None:
        return None
    text = match.group("prompt")
    return Message(
        ts=match.group("ts"),
        session=match.group("session"),
        text=text,
        source="log",
        truncated=len(text) == LOG_HEAD_CHARS,
    )


def read_log(log_dir: Path) -> tuple[list[Message], list[Path], str | None, str | None]:
    """Messages in `log_dir`'s `emrgd.log*`, plus the span those files cover.

    The first and last line of each file give the span without parsing 10 MB of DEBUG
    lines twice over: the files are append-only and chronological, so the span's ends
    are their ends. `oldest`/`newest` are the extremes across every file read, because
    rotation is what makes a window claimable or not.
    """
    messages: list[Message] = []
    files: list[Path] = []
    ends: list[tuple[str, str]] = []
    for path in sorted(log_dir.glob("emrgd.log*")):
        files.append(path)
        first: str | None = None
        last: str | None = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stamp = LINE_TS.match(line)
                if stamp is not None:
                    if first is None:
                        first = stamp.group("ts")
                    last = stamp.group("ts")
                if "task received: " in line:
                    message = parse_log_line(line)
                    if message is not None:
                        messages.append(message)
        if first is not None and last is not None:
            ends.append((first, last))
    if not ends:
        return messages, files, None, None
    return messages, files, min(e[0] for e in ends), max(e[1] for e in ends)


def read_sessions(
    roots: list[Path],
) -> tuple[list[Message], list[Path], str | None, str | None, list[tuple[Path, int, str]]]:
    """Host messages in `roots`' session histories, plus the span they cover.

    `history_*.jsonl` is the complete daily record and `history.jsonl` the current
    context; both are read because a session created today has a daily file while a
    compacted one may hold a host message only in the current file. Rows are deduped
    on `(timestamp, session, text)`, so reading both is not a double count.

    The fifth value is the **damaged** rows: `(path, line number, recovered timestamp or
    "")` for every line that passed the prefilter and then would not parse. Those lines
    are the reason this is a channel and not a `continue` - a row that could have been a
    host message and could not be read is a hole in the search, so `main` refuses to
    report absence over one (measured 2026-10-03, `cyc20261003-083317`: the phrase was on
    disk in a truncated line, the printed span contained that line, and the tool answered
    "no message in the searched span contains" it). The recovered timestamp is what lets
    a hole **outside** the window be excluded rather than blocking a claim it cannot
    affect; a line whose timestamp is itself damaged is reported with `""` and blocks
    every window, because a line that cannot be dated cannot be excluded from one.
    """
    messages: list[Message] = []
    scanned: list[Path] = []
    stamps: list[str] = []
    damaged: list[tuple[Path, int, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in roots:
        files = sorted(root.glob("history_*.jsonl")) + sorted(root.glob("history.jsonl"))
        present = [f for f in files if f.exists()]
        if present:
            scanned.append(root)
        for path in present:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    # Cheap prefilter: the expensive part is json.loads on 11 MB of
                    # one-line records, and a non-user row can never be a host message.
                    # Damage is only collected for lines the prefilter lets through, and
                    # that is exact rather than a shortcut: a line without the `"user"`
                    # token cannot be a user row, so it cannot be the host speaking.
                    if '"user"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        found = RAW_TS.search(line)
                        damaged.append(
                            (path, number, normalise_ts(found.group(1)) if found else "")
                        )
                        continue
                    if row.get("role") != "user":
                        continue
                    ts = normalise_ts(str(row.get("timestamp") or ""))
                    if not ts:
                        continue
                    content = row.get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            part.get("text", "")
                            for part in content
                            if isinstance(part, dict)
                        )
                    if not isinstance(content, str) or not content:
                        continue
                    stamps.append(ts)
                    key = (ts, root.name, content[:200])
                    if key in seen:
                        continue
                    seen.add(key)
                    messages.append(
                        Message(
                            ts=ts,
                            session=root.name,
                            text=content,
                            source="sessions",
                            truncated=False,
                        )
                    )
    if not stamps:
        return messages, scanned, None, None, damaged
    return messages, scanned, min(stamps), max(stamps), damaged


def index_roots(index: Path) -> list[Path]:
    """Session directories the daemon's index names. Unreadable index -> none."""
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    seen: list[Path] = []
    for value in payload.values():
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path not in seen:
            seen.append(path)
    return seen


def matches(
    messages: list[Message], pattern: re.Pattern[str], since: str | None, host_only: bool
) -> tuple[list[Message], int]:
    """Messages matching `pattern`, and how many scheduled prompts were set aside."""
    found: list[Message] = []
    skipped = 0
    for message in messages:
        if since is not None and message.ts < since:
            continue
        if host_only and message.text.startswith(TASK_PROMPT_PREFIX):
            skipped += 1
            continue
        if pattern.search(message.text):
            found.append(message)
    return found, skipped


def describe(message: Message) -> str:
    """One line per message: where it is, when it arrived, and what it says."""
    where = "log" if message.source == "log" else "sessions"
    note = ", head only" if message.truncated else ""
    return f"  {message.ts} | {message.session} | [{where}{note}] {message.text}"


def main(argv: list[str] | None = None) -> int:
    # A merged reader must see the sources before any verdict, and this family's
    # docstrings promise that order. stdout is block-buffered when it is a pipe (how a
    # cycle reads this report: `2>&1 | tail`) while stderr is not, so without this
    # every stderr line overtakes the header.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # A literal description rather than the module docstring: this file's docstring
    # quotes the host's ruling verbatim, and what args prints is output on the
    # `--help` path, where a non-ASCII character crashes a legacy codec
    # (tests/test_script_output_ascii.py; its rule scopes the docstring by whether
    # the source reaches for it, so this comment must not name that attribute).
    # The sibling `check-issue-links.py` keeps its own Chinese quote the same way.
    parser = argparse.ArgumentParser(
        description=(
            "Did the host actually send the message a cycle is about to attribute to "
            "it? Searches the daemon's received-message record and the session "
            "histories: 0 found, 1 absent over a covered span, 2 unmeasurable."
        )
    )
    parser.add_argument("--pattern", default=None,
                        help="a regex searched in the recorded text (a plain phrase works)")
    parser.add_argument("--since", default=None,
                        help="ISO date/time; the window the claim is about")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                        help=f"where emrgd.log* lives (default: {DEFAULT_LOG_DIR})")
    parser.add_argument("--index", default=str(DEFAULT_INDEX),
                        help=f"the session index to read directories from (default: {DEFAULT_INDEX})")
    parser.add_argument("--sessions", action="append", default=None,
                        help="a session directory to read full texts from (repeatable; "
                             "default: the index's directories, plus ./.emrg/sessions)")
    parser.add_argument("--all", action="store_true",
                        help="include scheduled task prompts (default: the host only)")
    parser.add_argument("--measure", action="store_true",
                        help="print every host message in the window instead of a verdict")
    args = parser.parse_args(argv)

    since = None
    if args.since:
        since = parse_since(args.since)
        if since is None:
            # Never a verdict: a window this tool cannot read is not evidence that
            # the host said nothing in it (measured 2026-09-28 — `--since 2026-9-28`
            # used to answer NOT FOUND about a message sent that morning).
            print(f"unmeasurable: --since {args.since!r} is not a date this reads; "
                  "use YYYY-MM-DD (or YYYY-MM-DD HH:MM[:SS]), zero-padded",
                  file=sys.stderr)
            return 2
    log_dir = Path(args.log_dir)
    log_messages, log_files, log_oldest, log_newest = read_log(log_dir)

    if args.sessions:
        roots = [Path(p) for p in args.sessions]
    else:
        roots = index_roots(Path(args.index))
        here = Path.cwd() / ".emrg" / "sessions"
        if here.is_dir():
            roots.extend(p for p in here.iterdir() if p.is_dir() and p not in roots)
    session_messages, session_dirs, ses_oldest, ses_newest, damaged = read_sessions(roots)

    print(f"log: {log_dir} ({len(log_files)} file(s), "
          f"{log_oldest or 'none'} -> {log_newest or 'none'})")
    print(f"sessions: {len(session_dirs)} dir(s) with a history, "
          f"{ses_oldest or 'none'} -> {ses_newest or 'none'}")
    if since:
        print(f"window: messages at or after {since}")

    messages = log_messages + session_messages
    if args.measure:
        found, skipped = matches(messages, re.compile(".*"), since, not args.all)
        host = [m for m in found if not m.text.startswith(TASK_PROMPT_PREFIX)]
        print(f"{len(host)} host message(s), {skipped} scheduled prompt(s) set aside")
        if damaged:
            # The inventory is a count of what was read, so a line that could not be read
            # belongs in it: without this line the count reads as the whole file.
            print(f"and {len(damaged)} row(s) that could not be read: "
                  f"{', '.join(f'{p.name}:{n}' for p, n, _ in damaged)}")
        for message in sorted(host, key=lambda m: m.ts):
            print(describe(message))
        return 0

    if not args.pattern:
        print("unmeasurable: --pattern is required (or --measure for the inventory)",
              file=sys.stderr)
        return 2

    try:
        pattern = re.compile(args.pattern)
    except re.error as exc:
        print(f"unmeasurable: --pattern is not a regex: {exc}", file=sys.stderr)
        return 2

    found, skipped = matches(messages, pattern, since, not args.all)
    if found:
        for message in sorted(found, key=lambda m: m.ts):
            print(describe(message))
        print(f"FOUND: {len(found)} message(s) match")
        return 0

    # Absence is reported only over a span both sources reached back past. A window
    # that neither source covers is unmeasurable, never empty.
    uncovered: list[str] = []
    for label, oldest, count in (("the log", log_oldest, len(log_files)),
                                 ("session histories", ses_oldest, len(session_dirs))):
        if oldest is None or count == 0:
            uncovered.append(f"{label} had nothing to read")
        elif since is not None and oldest > since:
            uncovered.append(f"{label} reaches back only to {oldest}, after {since}")
    # A row that could have been a host message and could not be read is a hole in the
    # window, and absence over a hole is the reading this tool exists to refuse. A hole
    # whose own timestamp survived the damage and is before the window is excluded,
    # because it cannot hold an in-window message; one that cannot be dated is not
    # excluded, because a line that cannot be dated cannot be placed outside anything.
    holes = [d for d in damaged if d[2] == "" or since is None or d[2] >= since]
    if holes:
        named = ", ".join(f"{p.name}:{n}" for p, n, _ in holes)
        uncovered.append(
            f"{len(holes)} row(s) could have been a host message and could not be read "
            f"({named})"
        )
    if uncovered:
        print("unmeasurable: " + "; ".join(uncovered)
              + " - a span that was not fully read is not evidence of absence",
              file=sys.stderr)
        return 2

    print(f"NOT FOUND: no message in the searched span contains {args.pattern!r} "
          f"({skipped} scheduled prompt(s) set aside; --all searches them too)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
