#!/usr/bin/env python3
"""Does every retry loop's message read the bound the loop actually iterates?

The defect class
----------------
`Agent.md` states it - *a derived number is never written where a guard can measure it* -
and the release workflow's retry loops are the place where being wrong costs the most:
each loop counted with one number and printed another. Measured at `255f6f9b`, four retry
loops and three spellings of the same shape:

    step                     loop                          message
    Notarization preflight   `attempts=3` + `while`         `${attempt}/${attempts}`   (the model)
    Notarize pkg             `for attempt in 1 2 3`         `(attempt ${attempt}/3)`
    Staple pkg               `for attempt in 1 2 3 4 5`     `(attempt ${attempt}/5)`, `all 5 attempts`
    Verify release           `for attempt in 1 2 3 4 5 6`   `(attempt ${attempt}/6)`

Changing a loop bound *is* caught today: `tests/test_release_staple_survives_a_lost_ticket_
service.py` carries its own `ATTEMPTS = 5` and the suite reddens when the observed submit
count disagrees. The person then updates that constant, the suite is green, and **the
printed sentence still states the old number**, because no test reads a message - the
operator triaging a lost release is told "on all 5 attempts" by a loop that tried six
times. The failure is silent exactly after the guard was satisfied, which is what a
mechanised rule is for (issue #2210, where two carriers of this shape were written in one
day by two instances).

What this measures
------------------
Not "is the printed number a literal". A literal scan would have to be red on the notarize
step until another open PR replaces its copy, and a guard that is red for a reason outside
itself teaches the next reader to ignore it. It measures the **derivation**: for every
retry loop, every value the loop presents as its bound - after the counter's slash, before
the word "attempt(s)", or as the right operand of the counter's own `-lt`/`-le` - must
equal the bound the loop actually iterates.

  * a bare number is checked as itself;
  * `$VAR`/`${VAR}` is resolved from the nearest assignment above the loop's header;
  * anything else in a bound position is prose and is not a claim (`submit attempt(s)`,
    `every attempt was retried`).

So a number that agrees passes, and a *variable* that disagrees is caught too - strictly
stronger than "no literals", and green on both spellings while the tree is mid-migration.

The reader classifies a loop from its **header line**, and only three spellings are among
them: `for attempt in 1 2 3 4 5; do`, `for (( attempt=1; attempt<=VAR; attempt++ ))` and
`while [ "$attempt" -le "$VAR" ]`. The scope of that classification is what makes the guard
honest rather than merely quiet, and it is the one clause cycle cyc20261013-194608 vetoed:

  * a header that **names the counter** in none of those three shapes - `for attempt in 1 2 3
    4` with `do` on the next line, `until [ "$attempt" -gt 4 ]; do`, `for attempt in $(seq 1
    4); do` - is this guard's subject with a bound it cannot read, so it is **exit 2**, with
    the header and its line printed. It used to be skipped in silence, which made the run
    answer `OK` over a stale message and answer the identical line once that message was
    corrected: a guard that tells a clean loop from a stale one by nothing is worse than no
    guard, because a reader relies on its `OK`;
  * a header that never names the counter - `for f in dist/...`, `while IFS= read -r f` - is
    a loop this guard is not about, and is skipped. That is the control that keeps the rule
    above from becoming "every other loop in the file is exit 2".

The subject is never assumed: a tree with no retry loop at all is exit 2, never 0, and the
census printed is what a reader can check against the workflow.

Exit codes (the family's contract - "clean" and "could not measure" are never the same)
--------------------------------------------------------------------------------------
    0  every bound claim in every retry loop of every workflow equals its loop's bound
    1  at least one claim disagrees - the file, the line, the claim and the bound print
    2  could not measure, and never a pass: no workflow file, no retry loop, a bound
       variable with no assignment above it, or a loop whose bound cannot be read

Usage
-----
    uv run --no-sync python3 scripts/check-retry-bounds.py
    uv run --no-sync python3 scripts/check-retry-bounds.py --root <checkout>
    uv run --no-sync python3 scripts/check-retry-bounds.py --json

`--root` defaults to the checkout this guard **lives in**, derived from the file rather
than from the caller's working directory: a guard that answers about whatever directory it
happens to be launched from prints the same report about two different trees, which is the
failure `check-doc-count.py` and `check_nonlocal.py` carry the incident for.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WORKFLOW_DIR = ".github/workflows"

#: The checkout this guard belongs to: its default subject, derived from the file. The
#: caller's cwd is not the answer - see the usage note in the docstring.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent

EXIT_CLEAN = 0
EXIT_FAULT = 1
EXIT_UNMEASURABLE = 2

#: The counter this repository's retry loops use, and the only ones this guard is about:
#: a loop counting something else is not a retry loop and has no bound message.
COUNTER = "attempt"

_LOOP_OPEN = re.compile(r"^\s*(?:for|while|until)\b")
_DONE = re.compile(r"^\s*done\b")

#: `for attempt in 1 2 3 4 5; do` - group 2 is the list the loop iterates.
_LIST_LOOP = re.compile(rf"\bfor\s+({COUNTER})\s+in\s+([0-9 ]+?)\s*;?\s*do\b")
#: `for (( attempt=1; attempt<=ATTEMPTS; attempt++ )); do` - group 2 is the bound variable.
_C_LOOP = re.compile(
    rf"\bfor\s*\(\(\s*({COUNTER})\s*=\s*1\s*;\s*\1\s*<=\s*(\w+)\s*;\s*\1\+\+\s*\)\)"
)
#: `while [ "$attempt" -le "$attempts" ]; do` - group 1 is the bound variable.
_WHILE_LOOP = re.compile(rf"\bwhile\s*\[\s*\"\${COUNTER}\"\s*-l[et]\s*\"?\$(\w+)\"?\s*\]")

#: A loop header that names the counter at all. Its purpose is the *negative* of the three
#: patterns above: a header this reader cannot classify but that names `attempt` is this
#: guard's subject with an unreadable bound (exit 2), while one that never names it - `for f
#: in ...`, `while IFS= read -r f` - is a loop this guard is not about, and is skipped.
#: `\b` keeps `attempts/` and `attempt_count` out of it; `$attempt` and `${attempt}` both
#: match, since `$` and `{` are not word characters.
_NAMES_COUNTER = re.compile(rf"\b{COUNTER}\b")

_ASSIGN = re.compile(r"\b(\w+)=(\d+)\b")

#: The three bound positions. Each captures the token the message presents as the bound.
_SLASH_CLAIM = re.compile(rf"\$\{{{COUNTER}\}}/(\S+)")
_WORD_CLAIM = re.compile(r"(\S+)\s+attempts?\b")
_COND_CLAIM = re.compile(rf"\[\s*\"\${COUNTER}\"\s*-l[et]\s+(\S+)")

_INT = re.compile(r"^\d+$")
_VAR = re.compile(r"^\$\{?(\w+)\}?$")

#: Trailing/leading punctuation a token picks up from the sentence around it.
_STRIP = " \t)]}\"';,"

# --- reading the loops --------------------------------------------------------


class Loop:
    """One retry loop: where it is, what it iterates, and whether that could be read."""

    def __init__(self, header: int, scope: list[int], closed: bool):
        self.header = header
        self.scope = scope
        self.closed = closed
        self.bound: int | None = None
        self.form = "other"
        self.reason: str | None = None


def _indent(line: str) -> int | None:
    """The line's indentation, or None for a blank line (which bounds nothing)."""
    if not line.strip():
        return None
    return len(line) - len(line.lstrip())


def _block_scope(lines: list[str], header: int) -> list[int]:
    """The lines after the header that belong to the block the loop lives in.

    Not just the body: the sentence an operator reads is as often the summary **after**
    the loop as it is the per-attempt line inside it ("failed on all 5 attempts" sits
    after `done`, as does the notarization preflight's "on every one of N attempts"), and
    a scope of the body alone would report those two as absent rather than as read. The
    block ends at the first non-blank line indented less than the header, and at a sibling
    loop's header, so one loop never answers for another's sentence.
    """
    base = _indent(lines[header])
    if base is None:
        return []
    scope: list[int] = []
    index = header + 1
    while index < len(lines):
        indent = _indent(lines[index])
        if indent is None:
            index += 1
            continue
        if indent < base or (indent == base and _LOOP_OPEN.match(lines[index])):
            break
        scope.append(index)
        index += 1
    return scope


def _loop_spans(lines: list[str]) -> list[Loop]:
    """Every loop in the file, by nesting depth of `for`/`while`/`until` ... `done`.

    A loop whose `done` is missing is returned unclosed rather than dropped: that is a
    reading the guard cannot make, which is exit 2 and never a silent skip.
    """
    spans: list[Loop] = []
    index = 0
    while index < len(lines):
        if _LOOP_OPEN.match(lines[index]):
            depth = 1
            scan = index + 1
            while scan < len(lines) and depth > 0:
                if _LOOP_OPEN.match(lines[scan]):
                    depth += 1
                elif _DONE.match(lines[scan]):
                    depth -= 1
                scan += 1
            closed = depth == 0
            spans.append(Loop(index, _block_scope(lines, index) if closed else [], closed))
        index += 1
    return spans


def _assigned_above(lines: list[str], header: int, name: str) -> int | None:
    """The nearest `name=<digits>` above the loop's header, or None.

    Nearest-preceding rather than file-wide: two steps may both call their bound
    `attempts`, and a file-wide search would make one step's value answer for the other's.
    """
    for index in range(header - 1, -1, -1):
        for match in _ASSIGN.finditer(lines[index]):
            if match.group(1) == name:
                return int(match.group(2))
    return None


def _read_bound(lines: list[str], loop: Loop) -> None:
    """Fill in `loop.bound`/`form`/`reason` from the loop's own header line."""
    line = lines[loop.header]
    match = _LIST_LOOP.search(line)
    if match:
        loop.form = "list"
        tokens = match.group(2).split()
        if not tokens or any(not token.isdigit() for token in tokens):
            loop.reason = f"the attempts of the loop at line {loop.header + 1} are not a list of numbers"
            return
        wanted = [str(n) for n in range(1, len(tokens) + 1)]
        if tokens != wanted:
            loop.reason = (
                f"the loop at line {loop.header + 1} does not count 1..{len(tokens)}: {tokens}"
            )
            return
        loop.bound = len(tokens)
        return
    match = _C_LOOP.search(line)
    if match:
        loop.form = "c"
        name = match.group(2)
    else:
        match = _WHILE_LOOP.search(line)
        if not match:
            # The header names the counter but is spelled in none of the three shapes this
            # reader classifies - `for attempt in 1 2 3 4` with `do` on the next line,
            # `until [ "$attempt" -gt 4 ]; do`, `for attempt in $(seq 1 4); do`. That is
            # this guard's subject with a bound it cannot read, which its own contract makes
            # exit 2 ("a loop whose bound cannot be read"), never a pass. Skipping it was a
            # hole measured by cycle cyc20261013-194608: a file whose only retry loop was
            # spelled this way with a stale `/2` over four iterations reported
            # `OK: every bound message in 1 claim(s) reads its loop's bound`, rc 0 - and the
            # same file with the message corrected reported the identical line, so the two
            # readings told a clean loop from a stale one by nothing at all.
            if _NAMES_COUNTER.search(line):
                loop.reason = (
                    f"the loop at line {loop.header + 1} names `{COUNTER}` but this reader "
                    f"cannot classify its header, so its bound is unread: {line.strip()}"
                )
            return  # a loop that never names the counter is not this guard's subject
        loop.form = "while"
        name = match.group(1)
    bound = _assigned_above(lines, loop.header, name)
    if bound is None:
        loop.reason = (
            f"the loop at line {loop.header + 1} is bounded by `${name}`, which has no "
            f"assignment above it to read"
        )
        return
    loop.bound = bound


# --- reading the claims -------------------------------------------------------


def _claim_tokens(lines: list[str], loop: Loop) -> list[tuple[int, str, int]]:
    """Every token a line of the loop's block presents as the bound.

    Comment lines are skipped: the rule is about what an operator reads at triage time,
    and a comment quoting the old wording is history, not a claim. A token must look like
    a value (digits or `$VAR`) to be a claim at all - `submit attempt(s)` and `every
    attempt was retried` are prose.
    """
    claims: list[tuple[int, str, int]] = []
    for index in loop.scope:
        line = lines[index]
        if line.lstrip().startswith("#"):
            continue
        for pattern in (_SLASH_CLAIM, _WORD_CLAIM, _COND_CLAIM):
            for match in pattern.finditer(line):
                token = match.group(1).strip(_STRIP)
                if _INT.match(token) or _VAR.match(token):
                    claims.append((index, token, match.start(1)))
    return claims


def _value_of(token: str, lines: list[str], loop: Loop) -> tuple[int | None, str | None]:
    """The number a claim states, or why it could not be read."""
    if _INT.match(token):
        return int(token), None
    match = _VAR.match(token)
    assert match  # _claim_tokens only passes ints and vars
    name = match.group(1)
    value = _assigned_above(lines, loop.header, name)
    if value is None:
        return None, f"`{token}` has no assignment above line {loop.header + 1} to read it from"
    return value, None


# --- the run ------------------------------------------------------------------


class Reading:
    def __init__(self, root: Path):
        self.root = root
        self.files: list[Path] = []
        self.loops = 0
        self.rows: list[dict] = []
        self.faults: list[dict] = []
        self.unmeasured: list[str] = []


def measure(root: Path) -> Reading:
    reading = Reading(root)
    directory = root / WORKFLOW_DIR
    if directory.is_dir():
        reading.files = sorted(
            path for pattern in ("*.yml", "*.yaml") for path in directory.glob(pattern)
        )
    for path in reading.files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            reading.unmeasured.append(f"{path}: could not be read ({exc})")
            continue
        relative = path.relative_to(root).as_posix()
        for loop in _loop_spans(lines):
            _read_bound(lines, loop)
            # The reason is read *before* the form: a header the reader could not classify
            # still sets one when it names the counter, and the order matters - a loop left
            # as `form == "other"` because its header was unreadable is precisely the case
            # this guard must not answer "OK" about.
            if loop.reason:
                reading.unmeasured.append(f"{relative}: {loop.reason}")
                continue
            if loop.form == "other":
                continue
            if not loop.closed:
                reading.unmeasured.append(
                    f"the loop at {relative}:{loop.header + 1} has no `done` this reading can find"
                )
                continue
            assert loop.bound is not None
            reading.loops += 1
            for index, token, column in _claim_tokens(lines, loop):
                value, why = _value_of(token, lines, loop)
                if why:
                    reading.unmeasured.append(f"{relative}:{index + 1}: {why}")
                    continue
                assert value is not None
                row = {
                    "file": relative,
                    "line": index + 1,
                    "loop_line": loop.header + 1,
                    "bound": loop.bound,
                    "claim": value,
                    "text": lines[index].strip(),
                }
                reading.rows.append(row)
                if value != loop.bound:
                    reading.faults.append({**row, "column": column + 1, "token": token})
    return reading


def _verdict(reading: Reading) -> int:
    if reading.faults:
        return EXIT_FAULT
    if reading.unmeasured:
        return EXIT_UNMEASURABLE
    if not reading.rows:
        return EXIT_UNMEASURABLE
    return EXIT_CLEAN


def _print(reading: Reading, code: int, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "tree": str(reading.root),
                    "measured": code != EXIT_UNMEASURABLE or bool(reading.rows),
                    "workflows": len(reading.files),
                    "loops": reading.loops,
                    "claims": len(reading.rows),
                    "faults": reading.faults,
                    "unmeasured": reading.unmeasured,
                },
                indent=2,
            )
        )
        return
    print(
        f"workflows: {len(reading.files)} file(s) under {WORKFLOW_DIR}; "
        f"{reading.loops} retry loop(s), {len(reading.rows)} bound claim(s) read"
    )
    for row in reading.rows:
        mark = "ok  " if row["claim"] == row["bound"] else "BAD "
        print(
            f"  {mark}{row['file']}:{row['line']}  loop@{row['loop_line']} "
            f"bound={row['bound']} claim={row['claim']}"
        )
    for fault in reading.faults:
        print(
            f"FAIL: {fault['file']}:{fault['line']} - the loop at line "
            f"{fault['loop_line']} iterates {fault['bound']} time(s), but the message "
            f"states {fault['claim']}: {fault['text']}",
            file=sys.stderr,
        )
    for reason in reading.unmeasured:
        print(f"not measurable: {reason}", file=sys.stderr)
    if code == EXIT_CLEAN:
        print(f"OK: every bound message in {len(reading.rows)} claim(s) reads its loop's bound")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # The family's `tree:` line goes to stdout and the faults to stderr; line-buffered
    # stdout is what keeps them in that order for a piped reader, where the verdict on
    # stderr would otherwise overtake the tree line. `reconfigure` is absent on a
    # replaced stdout (a test capture), which is why the failure is swallowed.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="the checkout to read (default: the one this script lives in)",
    )
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not args.json:
        print(f"tree: {root}")

    if not (root / WORKFLOW_DIR).is_dir():
        if args.json:
            print(json.dumps({"tree": str(root), "measured": False,
                              "reason": f"no {WORKFLOW_DIR}/ directory"}, indent=2))
        else:
            print(f"not measurable: no {WORKFLOW_DIR}/ directory under {root}", file=sys.stderr)
        return EXIT_UNMEASURABLE

    reading = measure(root)
    if not reading.files:
        if args.json:
            print(json.dumps({"tree": str(root), "measured": False,
                              "reason": f"no workflow file under {WORKFLOW_DIR}"}, indent=2))
        else:
            print(f"not measurable: no workflow file under {root}/{WORKFLOW_DIR}", file=sys.stderr)
        return EXIT_UNMEASURABLE

    code = _verdict(reading)
    _print(reading, code, args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
