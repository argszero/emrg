#!/usr/bin/env python3
"""How many calls does one round carry, and does a lone one say what it is waiting for?

The several-calls rule (`emrg/server/prompts/system.j2`, rulebook R10 of the evolution
prompt) changed its default on 2026-10-20: it used to permit several calls "when no call's
arguments depend on another's answer" and now asks for every call that can already be
specified, leaving a call alone only when its arguments need an answer not yet read. A
wording change is cheap to claim and cheap to get wrong -- the same rule was rewritten once
before and the share did not move -- so the claim is checked against what the rounds
actually did, and the check has to be the *same* one before and after. That is this tool:
the numbers it prints now are the "before" reading, and the same command run against a
session that grew up under the new wording is the "after" reading.

Two readings, and they are not the same kind of thing:

  * **the share** -- of every assistant record carrying at least one tool call, the share
    carrying two or more. Exact: it counts `tool_calls` and judges nothing.
  * **the lone intent** -- of the records carrying exactly one call, the share whose
    `intent` names no answer it waits for. This one is a **lexical proxy** and is reported
    as one: an intent counts as naming an answer only when it carries one of the markers
    printed under `--markers`, so the reading is auditable rather than trusted. The
    distinction it exists for is why the rule needed a second number at all -- a total that
    rises cannot tell "applied the new rule and judged wrongly" from "did not apply it",
    while an intent that never mentions an answer is evidence of the second.

What it reads, and it says so before any number: `history_*.jsonl` under the roots given,
one line per record, records on the `records_to_messages` shape -- `type: message`,
`role: assistant`, and `tool_calls[]` either at the top level or under a `content` object.

Exit codes: 0 a reading was taken; 2 nothing measurable -- no history file under the roots
(or every one of them unreadable), which is never a pass.

    check-batching-share.py                            # the host's whole corpus under ~/.emrg
    check-batching-share.py ~/.emrg/evolution/emrg/.emrg --since 2026-10-21
    check-batching-share.py --json                     # the same reading for another program

A subject is a **corpus root** -- a directory to walk or one `history_*.jsonl` file -- and is a
positional, like `check-icon-transparency.py`'s subjects: this tool's subject is a set of session
histories, not a working tree, so it is classified among the guards that answer about something
other than a tree and prints the roots it read instead of a `tree:` line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: Phrases that mark an `intent` as naming an answer it waits for. English is the language every
#: tool call's `intent` is written in (the prompt requires it), so the list is English-only and short
#: on purpose: a longer list would make the proxy agree with whatever the reader hoped for. Printed
#: by `--markers` so the reading can be argued with instead of taken on faith.
DEPENDENCY_MARKERS: tuple[str, ...] = (
    "the result",
    "its result",
    "the output",
    "its output",
    "from the",
    "based on",
    "depending on",
    "depends on",
    "whether",
    "the answer",
    "if the",
    "if it",
    "whatever",
    "after seeing",
    "so that i can",
    "then the",
    "what the",
)

#: A `history_YYMMDD.jsonl` name is the only thing that makes a file part of the corpus; the
#: contents decide nothing about membership, so a file that cannot be parsed is counted and named.
HISTORY_NAME = re.compile(r"^history_\d{6}\.jsonl$")


def history_files(roots: list[Path]) -> list[Path]:
    """Every `history_*.jsonl` under `roots`, deduplicated, in a stable order."""
    seen: set[Path] = set()
    for root in roots:
        if root.is_file() and HISTORY_NAME.match(root.name):
            seen.add(root)
            continue
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if HISTORY_NAME.match(name):
                    seen.add(Path(dirpath) / name)
    return sorted(seen)


def tool_calls_of(record: dict) -> list:
    """The record's tool calls, wherever this shape keeps them -- never a guess beyond the two."""
    calls = record.get("tool_calls")
    if not isinstance(calls, list):
        content = record.get("content")
        calls = content.get("tool_calls") if isinstance(content, dict) else None
    return calls if isinstance(calls, list) else []


def intent_of(call: dict) -> str:
    """The `intent` inside a call's `arguments`, or `""` when it says nothing readable."""
    function = call.get("function") if isinstance(call, dict) else None
    raw = function.get("arguments") if isinstance(function, dict) else None
    if isinstance(raw, dict):
        args = raw
    elif isinstance(raw, str):
        try:
            args = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    else:
        return ""
    intent = args.get("intent") if isinstance(args, dict) else None
    return intent if isinstance(intent, str) else ""


def names_an_answer(intent: str) -> bool:
    """Whether an `intent` carries one of `DEPENDENCY_MARKERS` -- the proxy, stated plainly."""
    lowered = intent.casefold()
    return any(marker in lowered for marker in DEPENDENCY_MARKERS)


def read_corpus(files: list[Path], since: str | None) -> dict:
    """Walk the corpus and count. `since` is an ISO date; records before it are not counted."""
    reading = {
        "files": len(files),
        "records": 0,
        "unreadable_lines": 0,
        "rounds": 0,
        "multi": 0,
        "lone": 0,
        "lone_without_answer": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "counts": {},  # exact call-count histogram, so the share can be re-derived
    }
    for path in files:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            reading["unreadable_lines"] += 1
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    reading["unreadable_lines"] += 1
                    continue
                if not isinstance(record, dict):
                    reading["unreadable_lines"] += 1
                    continue
                reading["records"] += 1
                stamp = record.get("timestamp")
                if isinstance(stamp, str):
                    if reading["first_timestamp"] is None or stamp < reading["first_timestamp"]:
                        reading["first_timestamp"] = stamp
                    if reading["last_timestamp"] is None or stamp > reading["last_timestamp"]:
                        reading["last_timestamp"] = stamp
                    if since and stamp[: len(since)] < since:
                        continue
                if record.get("type") != "message" or record.get("role") != "assistant":
                    continue
                calls = tool_calls_of(record)
                if not calls:
                    continue
                reading["rounds"] += 1
                reading["counts"][len(calls)] = reading["counts"].get(len(calls), 0) + 1
                if len(calls) >= 2:
                    reading["multi"] += 1
                    continue
                reading["lone"] += 1
                if not any(names_an_answer(intent_of(call)) for call in calls):
                    reading["lone_without_answer"] += 1
    return reading


def share(part: int, whole: int) -> float:
    """A share, and 0.0 rather than a division when the subject is empty."""
    return (part / whole) if whole else 0.0


def render(reading: dict, roots: list[Path], since: str | None) -> str:
    """The reading, naming its subject before any number."""
    window = f"{reading['first_timestamp'] or '-'} .. {reading['last_timestamp'] or '-'}"
    lines = [
        f"subject: {reading['files']} history file(s) under "
        + ", ".join(str(root) for root in roots),
        f"window:  {window}" + (f"  (records before {since} not counted)" if since else ""),
        f"records: {reading['records']} line(s) read"
        + (f", {reading['unreadable_lines']} unreadable" if reading["unreadable_lines"] else ""),
    ]
    if not reading["rounds"]:
        lines.append("rounds:  0 -- no assistant record carrying a tool call; nothing measurable")
        return "\n".join(lines)
    lines += [
        f"rounds:  {reading['rounds']} assistant record(s) carrying at least one tool call",
        f"  share with two or more: {reading['multi']} "
        f"({100 * share(reading['multi'], reading['rounds']):.1f}%)",
        f"  share with exactly one:  {reading['lone']} "
        f"({100 * share(reading['lone'], reading['rounds']):.1f}%)",
        f"  lone intents naming no awaited answer (lexical proxy): "
        f"{reading['lone_without_answer']} of {reading['lone']} "
        f"({100 * share(reading['lone_without_answer'], reading['lone']):.1f}%)",
        "  calls per round: "
        + ", ".join(f"{n} -> {c}" for n, c in sorted(reading["counts"].items())),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure how many tool calls one round carries, and whether a lone call "
        "names the answer it waits for.",
        epilog="Exit 0 a reading was taken; 2 nothing measurable under the roots. Never 2 for 0.",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        metavar="CORPUS",
        help="a directory to walk for history_*.jsonl, or one such file; zero or more "
        "(default: ~/.emrg)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="count only records whose timestamp starts on or after this date",
    )
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")
    parser.add_argument(
        "--markers", action="store_true", help="print the dependency markers the proxy uses"
    )
    args = parser.parse_args(argv)

    if args.markers:
        print("\n".join(DEPENDENCY_MARKERS))
        return 0

    roots = [Path(raw).expanduser() for raw in (args.roots or ["~/.emrg"])]
    files = history_files(roots)
    if not files:
        print(
            "not measurable: no history_*.jsonl under "
            + ", ".join(str(root) for root in roots),
            file=sys.stderr,
        )
        return 2

    reading = read_corpus(files, args.since)
    if args.json:
        print(json.dumps({**reading, "roots": [str(r) for r in roots], "since": args.since},
                         ensure_ascii=False, indent=2))
    else:
        print(render(reading, roots, args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
