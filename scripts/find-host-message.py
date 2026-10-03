#!/usr/bin/env python3
"""Did the host actually send the message a cycle is about to attribute to it?

Usage
-----
    uv run --no-sync python3 scripts/find-host-message.py --pattern '每个issue应该'
    uv run --no-sync python3 scripts/find-host-message.py --pattern '拒绝' --since 2026-09-26
    uv run --no-sync python3 scripts/find-host-message.py --measure   # the inventory
                                                                     # (rc 2 if it is
                                                                     # a lower bound)

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

A record that cannot be read is a hole, not a line that was not there
--------------------------------------------------------------------
Measured 2026-10-03 (`cyc20261003-085457`), and the reason this file reads records
rather than lines: the daemon writes the prompt **verbatim**, so a multi-line message
is one record across many physical lines. A line-at-a-time reader never matched one,
and the loss was silent and large - **122 of this host's 156** `task received` records
(78%), including two real host messages (a 2026-09-27 message about a car model and a
2026-09-30 one about the car sinking through the track, both with images) that the tool
answered "the host never said it" about, at exit `1`.

The same shape recurs in the session source: a row that announces itself as a host row
and then cannot be parsed was skipped, so one truncated `history.jsonl` row made a
phrase it contains answer `NOT FOUND` while the identical row, well formed, answered
`FOUND` (measured `cyc20261003-083317`).

So both readers **count** what they could not read, and a count above zero makes the
answer `2` instead of `1` - absence is never reported over a hole. A match still wins:
a record this reader could not read cannot un-find a message it did read. The count is
printed with the file it came from, so the hole is locatable and fixable rather than
merely refused.

What this cannot measure
------------------------
A host message older than the oldest readable log line *and* older than every session
history on the machine. Both spans are printed, so the answer is always bounded by a
span a reader can see; `--since` before either span is reported as ``2`` rather than as
absence. Messages the host typed into a client that never reached the daemon left no
record anywhere and are outside both sources — as is any channel this repo does not
know about. A session row that cannot be read is counted like any other hole: reading
every line is what makes that possible, so there is no second, unreported bound on what
reaches the parser.

Exit codes
----------
``0`` found (prints the message). ``1`` no host message in the searched span contains
the pattern, both sources covered the span, and **no record went unread**. ``2`` the
question could not be answered: no source, a `--since` older than what either source
reaches back to, or a record either source wrote that this reader could not read.
``2`` is not a ``1``: an unmeasured window is never evidence of absence, and neither is
a window with a hole in it. **``--measure`` follows the same rule** rather than being an
exception to it: it prints the rows either way, labels its count a lower bound and exits
``2`` when there is a hole, because a count is exactly the answer a hole changes in
silence.
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
#:
#: The prompt group is `[\s\S]*?` rather than `.*?` on purpose, and it is the fix
#: for a measured defect (2026-10-03, cycle cyc20261003-085457): the daemon writes
#: the prompt **verbatim**, so a multi-line message is one record spread over many
#: physical lines. With `.*?` a record never matched, because the closing quote sits
#: on a later line - and on this host that silently dropped **122 of 156** records
#: (78%), including two real host messages (`特斯拉 model 3 …` 2026-09-27,
#: `车在跑道外…` 2026-09-30), every one of which the tool then answered "the host
#: never said it" about. The trailing `$` plus the non-greedy group is what makes it
#: land on the record's own closing quote: the earliest quote only wins when what
#: follows it is the end or the routing note.
LOG_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*?task received: '
    r'session=(?P<session>\S+) prompt="(?P<prompt>[\s\S]*?)"(?: → routing[\s\S]*)?$'
)

#: Where a received-message record *begins*. What follows may run across physical
#: lines, so this is the line reader's start condition rather than a whole record.
RECORD_BEGIN = re.compile(r'task received: session=\S+ prompt="')

#: A line's leading local timestamp, read without parsing the rest of the line: it is
#: what bounds the window this source can speak about.
LINE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")

#: Measured 2026-09-28 on this host: the daemon keeps the first 60 characters of the
#: message. That number is the whole reason the second source exists, so it is named
#: here rather than inferred per line — an exactly-60-character message and a truncated
#: one are indistinguishable in the log, and this is the only way to say which.
LOG_HEAD_CHARS = 60

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


def read_log(
    log_dir: Path,
) -> tuple[list[Message], list[Path], str | None, str | None, list[str]]:
    """Messages in `log_dir`'s `emrgd.log*`, plus the span those files cover.

    A record is read from its `task received: session=… prompt="` line to the physical
    line that closes the quote (and carries the routing note), so a prompt the daemon
    wrote verbatim across many lines is read whole instead of dropped. Any record that
    began and never closed - or that the next record cut into - is **named** in the
    fifth value, because a record this reader could not read is a hole in the answer
    rather than a line that was not there.

    The span is read from each physical line's leading timestamp and is unaffected by a
    record spanning lines: only a record's first line carries one.
    """
    messages: list[Message] = []
    files: list[Path] = []
    ends: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for path in sorted(log_dir.glob("emrgd.log*")):
        files.append(path)
        first: str | None = None
        last: str | None = None
        pending: list[str] = []
        unclosed = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stamp = LINE_TS.match(line)
                if stamp is not None:
                    if first is None:
                        first = stamp.group("ts")
                    last = stamp.group("ts")
                if pending:
                    pending.append(line)
                    joined = "".join(pending).rstrip("\n")
                    if LOG_LINE.match(joined):
                        message = parse_log_line(joined)
                        if message is not None:
                            messages.append(message)
                        pending = []
                        continue
                    if not RECORD_BEGIN.search(line):
                        continue
                    # A new record began before this one closed: the first is a hole.
                    unclosed += 1
                    pending = [line]
                elif RECORD_BEGIN.search(line):
                    pending = [line]
                else:
                    continue
                # A record that fits on one physical line closes right here.
                text = line.rstrip("\n")
                if LOG_LINE.match(text):
                    message = parse_log_line(text)
                    if message is not None:
                        messages.append(message)
                    pending = []
        if pending:
            unclosed += 1
        if unclosed:
            unreadable.append(f"{path.name}: {unclosed} received-message record(s)")
        if first is not None and last is not None:
            ends.append((first, last))
    if not ends:
        return messages, files, None, None, unreadable
    return (
        messages,
        files,
        min(e[0] for e in ends),
        max(e[1] for e in ends),
        unreadable,
    )


def read_sessions(
    roots: list[Path],
) -> tuple[list[Message], list[Path], str | None, str | None, list[str]]:
    """Host messages in `roots`' session histories, plus the span they cover.

    `history_*.jsonl` is the complete daily record and `history.jsonl` the current
    context; both are read because a session created today has a daily file while a
    compacted one may hold a host message only in the current file. Rows are deduped
    on `(timestamp, session, text)`, so reading both is not a double count.

    A line that cannot be read is **counted** (the fifth value), whether it failed to
    parse or parsed to something that is not a record: it is a record this reader could
    not read, so an absence reported over it is an absence reported over a hole.
    Measured 2026-10-03 (`cyc20261003-083317`): a single truncated row made a phrase it
    contains answer `NOT FOUND` (rc=1) on a tree where the same row, well formed,
    answers `FOUND` (rc=0).

    Every line is parsed, with no prefilter. There was one - `'"user"' in line` - and it
    was the one bound this reader could not report: a row truncated *before* its
    `"user"` marker was neither read nor counted, so it was invisible rather than
    named. That bound is now removed rather than documented (the cost is measured in the
    loop below; `Session.append_message` writes one `json.dumps` record per line, so a
    line that does not parse is a hole and nothing else).
    """
    messages: list[Message] = []
    scanned: list[Path] = []
    stamps: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    unreadable: list[str] = []
    for root in roots:
        files = sorted(root.glob("history_*.jsonl")) + sorted(root.glob("history.jsonl"))
        present = [f for f in files if f.exists()]
        if present:
            scanned.append(root)
        for path in present:
            bad = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    # Every line is parsed. There used to be a `'"user"' in line`
                    # prefilter here, justified by the cost of parsing the whole file -
                    # and it hid exactly the rows that matter: a row truncated *before*
                    # its `"user"` marker never reached the parser, so it was neither
                    # read nor counted, and absence was reported over it.
                    #
                    # Measured on this host 2026-10-03 (`cyc20261003-112023`), 17 files /
                    # 41.3 MB / 24,982 lines: `json.loads` on every line costs **152 ms**
                    # against **26 ms** for the substring pass - and `read_sessions` is
                    # not the call that dominates this tool's runtime (a full search is
                    # ~0.38 s wall). The history writer is
                    # `json.dumps(entry) + "\n"` per record (`Session.append_message`),
                    # so one line is one record by construction: a line that does not
                    # parse is a hole, not a different kind of line, and the honest read
                    # is the only one that can say so.
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        bad += 1
                        continue
                    if not isinstance(row, dict):
                        # Parses, but is not a record: `[]`/`null`/`"x"`. Same answer as
                        # unparseable - it announced no role this reader can read.
                        bad += 1
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
            if bad:
                unreadable.append(f"{root.name}/{path.name}: {bad} host row(s)")
    if not stamps:
        return messages, scanned, None, None, unreadable
    return messages, scanned, min(stamps), max(stamps), unreadable


def index_roots(index: Path) -> tuple[list[Path], str | None]:
    """Session directories the daemon's index names, **and why it named none**.

    The second value is the point of this function. It used to return `[]` for every
    way of failing to read the index, and the caller then searched whatever it could
    reach (`./.emrg/sessions`) and reported absence over that - a smaller set of
    directories, silently. Measured 2026-10-03 (`cyc20261003-112023`), a covering log
    plus an index holding `{}`: a host message that lives in another project's session
    answered **`NOT FOUND`, rc 1** ("the host never said it") instead of `FOUND`, and the
    only trace was `1 dir(s)` where an honest tree prints `2`.

    That state is not hypothetical: `{}` is exactly what the index rebuild wrote while
    its liveness check could not answer (`cyc20261003-110524`), and a missing index is
    the normal state of a host that has never written one. A caller who knows the set of
    session directories says so with `--sessions`, and then this reading is not used at
    all - which is what makes refusing here safe rather than obstructive.
    """
    if not index.exists():
        return [], f"the session index {index} does not exist"
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"the session index {index} could not be read ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return [], f"the session index {index} is not a mapping of session to directory"
    seen: list[Path] = []
    for value in payload.values():
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path not in seen:
            seen.append(path)
    if not seen:
        return [], f"the session index {index} names no session directory"
    return seen, None


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
    log_messages, log_files, log_oldest, log_newest, log_holes = read_log(log_dir)

    if args.sessions:
        roots = [Path(p) for p in args.sessions]
        # The caller named the set, so the index is not consulted and its state cannot
        # narrow the search - which is the escape hatch that makes refusing below safe.
        roots_problem: str | None = None
    else:
        roots, roots_problem = index_roots(Path(args.index))
        here = Path.cwd() / ".emrg" / "sessions"
        if here.is_dir():
            roots.extend(p for p in here.iterdir() if p.is_dir() and p not in roots)
    session_messages, session_dirs, ses_oldest, ses_newest, session_holes = read_sessions(roots)

    print(f"log: {log_dir} ({len(log_files)} file(s), "
          f"{log_oldest or 'none'} -> {log_newest or 'none'})")
    print(f"sessions: {len(session_dirs)} dir(s) with a history, "
          f"{ses_oldest or 'none'} -> {ses_newest or 'none'}")
    if roots_problem:
        # Printed on every run, not only when a verdict is refused: the count on the
        # line above is the visible symptom, and a reader who is about to trust it
        # should see the cause next to it.
        print(f"sessions: search set incomplete - {roots_problem}")
    if since:
        print(f"window: messages at or after {since}")

    messages = log_messages + session_messages
    if args.measure:
        found, skipped = matches(messages, re.compile(".*"), since, not args.all)
        host = [m for m in found if not m.text.startswith(TASK_PROMPT_PREFIX)]
        # The inventory is a *count*, and a count is the one answer a hole changes
        # silently: "74 host messages" over three unreadable records is a lower bound
        # printed as an exact number. Measured 2026-10-03 (`cyc20261003-112023`): before
        # this, `--measure` on a tree with holes printed the count and exited 0 with
        # nothing on stderr - while the search path in the same run refused (rc 2). The
        # rows are still printed (the inventory is useful either way); the *verdict* is
        # what a hole must move, so the count is labelled and the exit code says
        # unmeasurable.
        holes = log_holes + session_holes
        short: list[str] = list(holes)
        if roots_problem:
            # A partial search set is a second way this count is a lower bound, and it
            # is not a hole *inside* a source - it is a source that was never opened.
            short.append(roots_problem)
        if short:
            print(f"{len(host)} host message(s) at least (lower bound - see below), "
                  f"{skipped} scheduled prompt(s) set aside")
        else:
            print(f"{len(host)} host message(s), {skipped} scheduled prompt(s) set aside")
        for message in sorted(host, key=lambda m: m.ts):
            print(describe(message))
        if short:
            print("unmeasurable: " + "; ".join(short)
                  + " - this inventory is a lower bound, not a count of what the host "
                  "sent", file=sys.stderr)
            return 2
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
    if roots_problem:
        # The session source's *size* is part of its coverage: a search over a set that
        # could not be read answers a question about a different set of directories, and
        # the two are indistinguishable in the answer it produces. Measured 2026-10-03
        # (`cyc20261003-112023`): a covering log + an index holding `{}` turned a real
        # host message into `NOT FOUND`, with only `1 dir(s)` vs `2` to show for it.
        uncovered.append(f"{roots_problem}, so the session source searched an "
                         f"incomplete set of directories")
    for label, oldest, count in (("the log", log_oldest, len(log_files)),
                                 ("session histories", ses_oldest, len(session_dirs))):
        if oldest is None or count == 0:
            uncovered.append(f"{label} had nothing to read")
        elif since is not None and oldest > since:
            uncovered.append(f"{label} reaches back only to {oldest}, after {since}")
    if uncovered:
        print("unmeasurable: " + "; ".join(uncovered)
              + " - an uncovered window is not evidence of absence", file=sys.stderr)
        return 2

    # A record either source wrote and this reader could not read is a hole of unknown
    # position, and absence cannot be claimed over a hole. Measured 2026-10-03: with a
    # multi-line prompt unreadable (78% of this host's log records) and with one
    # truncated session row, `NOT FOUND` was printed about messages that are on disk.
    holes = log_holes + session_holes
    if holes:
        print("unmeasurable: " + "; ".join(holes)
              + " could not be read, and a record that cannot be read is not evidence "
              "of absence", file=sys.stderr)
        return 2

    print(f"NOT FOUND: no message in the searched span contains {args.pattern!r} "
          f"({skipped} scheduled prompt(s) set aside; --all searches them too)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
