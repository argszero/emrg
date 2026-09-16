#!/usr/bin/env python3
"""Every rant citation in the instruction class must name a public record.

Reads the tree it is standing in, and says so in its first line: the files it
scans are found relative to this file's own repository root, not to the cwd.

Usage
-----
    uv run --no-sync python3 scripts/check-rant-citations.py             # the rule
    uv run --no-sync python3 scripts/check-rant-citations.py --measure   # the inventory

The class this applies to
-------------------------
The **instruction class**: prose a reader is expected to *act on* - the built-in
task-prompt templates, the evolution template, the upgrade/vibe-check prompts and
the GUI redesign spec. Nine files, measured 2026-09-16: 47 citation sites over 29
distinct timestamps. Code comments are deliberately out of scope (the same
spelling occurs in 1300+ lines there): a comment's citation is a historical note
about why the line exists, and rewriting those burns the `git log -S` trail that
makes the note checkable.

Why a citation has to be resolvable
-----------------------------------
A rant timestamp is a **host-local** reference: it indexes `~/.emrg/rants.jsonl`
on the machine that wrote it (issue #1252). Measured by the issue's reporter on a
second host: 0 of 24 resolve there, and the store that *does* hold them keeps only
the ten most recent completed rants, so a timestamp also ages out of its own
host's reach - measured here 2026-09-16: the oldest `emrg` entry is
`2026-08-24T09:50:13`, which is *after* every citation in this class. A PR or
issue number, by contrast, stays resolvable forever.

So the rule is not "do not cite a rant" - the citation is the provenance of the
instruction. It is: **cite the public record beside it**, which is the PR that
first landed the citation on master (`gh pr view <N>`). Where the sweep has put
one, the spelling is `(PR #N; rant <ts>)`: the resolvable half first, so a reader
who cannot see the local store still has an anchor. The record is spelled
`PR #N` / `issue #N`; a bare `#N` is not accepted, and that is a measured
decision rather than a preference (see `PUBLIC_RECORD`).

A second spelling is reported too: a time with no date (`+ 11:00:31`). It is
unresolvable even on the host that wrote it, since nothing links it to a day, and
the block it sits in already supplies the date - so the fix is to spell it out.

What this guard cannot measure
------------------------------
It checks that a record is **spelled**, not that it **resolves**: replacing a
real record with `PR #999999` leaves `rc=0` (measured on this tree). That is the
deliberate edge of the rule rather than a gap -
resolvability needs the network, and a guard that could not measure would have to
answer ``2`` on every offline run. Whether the named PR is *the* record that
landed the citation is therefore the sweep's claim, not this guard's; it was
verified against GitHub when the sweep was made (a reviewer sampled 7 of the 26
inserted pairs, each credited PR merged with the timestamp present in its own
diff).

The frozen debt
---------------
`evolution_prompt.md` is the one template routine evolution must not edit (host
rant 2026-08-17T14:22:21; asserted by
`tests/test_rants_single_writer.py::_UNEDITABLE_TEMPLATES`), so its citations are
the caller's to sweep, not this guard's. They are listed in `DEBT` with a reason
and are the *only* permitted host-local-only sites. An entry that no longer
occurs is itself a failure: a debt list that cannot shrink grows until it means
"everything", which is the same as no rule. That is also why the list holds
`(file, timestamp)` pairs rather than a count - a count can stay true while the
citations behind it change.

Exit codes
----------
``0`` the rule holds. ``1`` a site names no public record, a debt entry is stale,
or the debt list names a file that is not host-owned. ``2`` the question could not
be answered - a file in the class is missing, or the tree could not be read.
``2`` is not a pass: a guard that cannot measure must never report the healthy
answer.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The instruction class, as measured 2026-09-16: nine files, 47 sites, 29
#: timestamps. Names, not a glob: the class is a decision (prose a reader acts on),
#: so a new template has to be added here deliberately rather than swept in by a
#: pattern that also catches its code comments.
INSTRUCTION_FILES = (
    "emrg/server/evolution_prompt.md",     # host-owned: its sites are DEBT
    "emrg/server/journal_prompt.md",
    "emrg/server/open_source_prompt.md",
    "emrg/server/promote_prompt.md",
    "emrg/server/paper_prompt.md",
    "emrg/server/prompts/system.j2",
    "emrg/server/prompts/upgrade_prompt.j2",
    "emrg/server/prompts/vibe_check.j2",
    "docs/gui-redesign.md",
)

#: The one file routine evolution must not edit, so its citations wait for the
#: caller. Kept as a name here rather than imported from the test module: a guard
#: that imported its own scope from a test would report a failure when the test
#: moves, not when the tree changes.
HOST_OWNED = "emrg/server/evolution_prompt.md"

#: `(file, timestamp)` -> why this site may stay host-local-only. Every entry is
#: in the host-owned file for the same reason; the reason is spelled per entry so
#: the list cannot quietly become "things nobody got to". The set is the measured
#: one (`--measure`), not a hand-copied list: it is eight sites, and the two
#: spellings that are not sites of their own (`+ 11:00:31` is date-less, so it has
#: no id; `+ 2026-08-28T22:12:16` is a continuation) are covered by DATEDLESS
#: and by their block rather than by an entry each.
DEBT: dict[tuple[str, str], str] = {
    (HOST_OWNED, ts): "host-owned template (host rant 2026-08-17T14:22:21)"
    for ts in (
        "2026-08-07T10:17:27",
        "2026-08-10T08:59:57",
        "2026-08-12T18:03:26",
        "2026-08-17T12:09:57",
        "2026-08-17T14:22:21",
        "2026-08-18T16:42:52",
        "2026-08-23T08:04:26",
        "2026-09-14T20:14:56",
    )
}

#: A citation: the word "rant"/"rants" followed within three non-digits by a
#: timestamp. Three characters, not a line: `(rant 2026-…` and `(rants\n  2026-…`
#: are the same citation, while a timestamp that is *not* introduced by the word
#: cannot be a rant citation - which is what keeps `system.j2`'s memory-format
#: example (`event_at: 2026-01-15T14:30:00`) out of scope.
#:
#: The word is a **named** group because a caller has to be able to repeat it
#: verbatim: a timestamp run (`rant ts1 + ts2`) whose two times resolve to
#: different records needs the word beside the second time, and `group(0)` cannot
#: supply it - that is the whole match, timestamp included, so repeating it
#: duplicates the first timestamp (measured, then fixed, 2026-09-16).
CITATION = re.compile(
    r"\b(?P<word>[Rr]ants?)\b[^0-9\n]{0,3}(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)"
)

#: Any full timestamp inside a citation block - the citation itself, or one that
#: continues it (`+ 2026-08-28T22:12:16`). The optional seconds matter: a site whose
#: only citation is the minute-truncated spelling (`2026-08-24T17:50`) had *no*
#: timestamps under a seconds-only pattern, which made `problems` index an empty
#: list and crash. A guard that cannot survive its own input is not a guard.
ALL_TIMESTAMPS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")

#: A public record, and it has to be *spelled*. The obvious wider form - any bare
#: `#\d+`, which is how this repo refers to a PR in prose - was measured on this
#: class and is unsound: `journal_prompt.md`'s numbered list matches `#12`, `#13`,
#: `#2`, `#1`, `#3` on five lines, so a bare-hash rule calls five sites resolved
#: that name no record at all. A guard whose instrument reports the healthy answer
#: without measuring is the failure this file exists to prevent, so the rule takes
#: the explicit spelling that `gh pr view` accepts verbatim.
PUBLIC_RECORD = re.compile(
    r"\b(?:PRs?|pull requests?|issues?)\s*#\d+", re.IGNORECASE
)

#: A time with no date, inside a citation block: `(rants 2026-08-23T08:04:26 +
#: 11:00:31 ...)`. It cannot be resolved even by the host that wrote it, so it is
#: reported with its fix rather than left to look swept. The date is in the block
#: already, which is why "expand it" is the whole instruction.
DATEDLESS = re.compile(r"\+\s*(?P<time>\d{2}:\d{2}:\d{2})(?![\d-])")


class Site:
    """One citation site: the citation line and any wrapped parenthetical."""

    def __init__(self, path: str, first_line: int, text: str,
                 timestamps: list[str], records: list[str], words: int,
                 shorthand: list[str]) -> None:
        self.path = path
        self.first_line = first_line
        self.text = text
        self.timestamps = timestamps
        self.records = records
        self.words = words
        self.shorthand = shorthand

    @property
    def has_record(self) -> bool:
        """Enough records for each citation word on the site."""
        return len(self.records) >= self.words > 0

    @property
    def key(self) -> str:
        return f"{self.path}:{self.first_line}"

    def exempt(self) -> bool:
        """A site is exempt only if *every* timestamp on it is frozen debt."""
        return bool(self.timestamps) and all(
            (self.path, ts) in DEBT for ts in self.timestamps
        )


def _paren_balance(line: str) -> int:
    """Unclosed opening minus closing parentheses, ignoring escaped ones."""
    depth = 0
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def scan(text: str, path: str) -> list[Site]:
    """Every citation site in `text`, in file order.

    A site starts at a line carrying a citation and continues while that block's
    parentheses are unbalanced - a wrapped parenthetical is one site, so a record
    added to its first line covers the timestamp on its last one. That matters
    because two sites in the class are written exactly that way and a sweep that
    edits only the first line would leave them looking resolved.
    """
    lines = text.splitlines()
    sites: list[Site] = []
    i = 0
    while i < len(lines):
        found = list(CITATION.finditer(lines[i]))
        if not found:
            i += 1
            continue
        block = [lines[i]]
        depth = _paren_balance(lines[i])
        j = i
        while depth > 0 and j + 1 < len(lines):
            j += 1
            block.append(lines[j])
            depth += _paren_balance(lines[j])
        joined = "\n".join(block)
        sites.append(
            Site(
                path=path,
                first_line=i + 1,
                text=joined,
                # Every full timestamp in the block, not only the ones the
                # citation regex reached: `(rants 2026-08-23T08:04:26 + 2026-08-28T22:12:16)`
                # is two rants and one record *word*, and a block whose second
                # timestamp no rule mentioned would be exactly the "looks swept"
                # site this guard is meant to catch.
                timestamps=ALL_TIMESTAMPS.findall(joined),
                # Counted, not a bool: a line can carry two citations of two
                # different rants (measured: `journal_prompt.md:461`), and one
                # record would anchor only the first. Records must be at least as
                # many as citation words, so each word has something resolvable
                # beside it.
                records=[m.group(0) for m in PUBLIC_RECORD.finditer(joined)],
                # Words are counted by CITATION, not by a bare `rant[s]?`: the
                # string `rants.jsonl` appears in two of these templates and a
                # bare-word count made those lines demand a second record for a
                # word that cites nothing (measured - and the same confusion put
                # a record inside the code span when the sweep anchored on it).
                words=len(list(CITATION.finditer(joined))),
                shorthand=[m.group("time") for m in DATEDLESS.finditer(joined)],
            )
        )
        i = j + 1
    return sites


def scan_tree(root: Path, files: tuple[str, ...] = INSTRUCTION_FILES) -> tuple[list[Site], list[str]]:
    """Sites in `root`, plus the names of files that could not be read."""
    sites: list[Site] = []
    missing: list[str] = []
    for rel in files:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            missing.append(rel)
            continue
        sites.extend(scan(text, rel))
    return sites, missing


def problems(sites: list[Site]) -> list[str]:
    """Every rule violation, and never a verdict over a subset of the sites."""
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for site in sites:
        for ts in site.timestamps:
            seen.add((site.path, ts))
        if site.exempt():
            continue
        if not site.has_record:
            cited = ", ".join(site.timestamps) or "no readable timestamp"
            example = site.timestamps[0] if site.timestamps else "<ts>"
            need = ("a public record" if site.words <= 1
                    else f"{site.words} public records (one per citation word)")
            found.append(
                f"{site.key}: cites {cited} with {len(site.records)} public record(s) "
                f"- add {need}, e.g. `(PR #123, rant {example})`"
            )
        for time in site.shorthand:
            day = site.timestamps[0][:10] if site.timestamps else "<date>"
            found.append(
                f"{site.key}: cites the date-less time `{time}` - expand it to a full "
                f"timestamp (`{day}T{time}`), or a reader cannot resolve it even on "
                f"the host that wrote it"
            )
    for key, reason in sorted(DEBT.items()):
        if key[0] != HOST_OWNED:
            found.append(
                f"debt entry {key[0]}:{key[1]} is not in the host-owned file "
                f"({HOST_OWNED}) - the debt list is only for the file routine "
                f"evolution must not edit ({reason})"
            )
        elif key not in seen:
            found.append(
                f"stale debt entry {key[0]}:{key[1]} ({reason}) - the site no "
                f"longer exists, so prune the entry or the list stops meaning anything"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true",
                        help="print the inventory instead of enforcing the rule")
    args = parser.parse_args(argv)

    sites, missing = scan_tree(REPO_ROOT)
    if missing:
        print(f"unmeasurable: {len(missing)} file(s) in the instruction class are "
              f"missing from {REPO_ROOT}: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"tree: {REPO_ROOT}")
    if args.measure:
        print(f"{len(sites)} citation site(s) in {len(INSTRUCTION_FILES)} "
              f"instruction file(s)")
        for site in sites:
            state = ("record" if site.has_record
                     else "DEBT" if site.exempt() else "NO RECORD")
            # A date-less time on an exempt site is not an enforcement failure (the
            # file is host-owned, so routine evolution may not fix it) but it must
            # still be *measurable*: this inventory is where the frozen debt is
            # supposed to be readable, and a spelling the guard never prints is one
            # nobody can act on - measured 2026-09-16, the host-owned template's
            # `+ 11:00:31` was invisible in both modes before this line.
            shorthand = "".join(f" [date-less {t}]" for t in site.shorthand)
            print(f"  {state:<9} {site.key} {', '.join(site.timestamps)}{shorthand}")
        stale = [k for k in DEBT if k not in {(s.path, t) for s in sites for t in s.timestamps}]
        print(f"debt entries: {len(DEBT)}, stale: {len(stale)}")
        for key in sorted(stale):
            print(f"  stale {key[0]}:{key[1]}")
        return 0

    found = problems(sites)
    if found:
        for line in found:
            print(line)
        print(f"FAIL: {len(found)} problem(s)")
        return 1
    print(f"OK: every citation site in the instruction class names a public record "
          f"({len(sites)} site(s), {len(DEBT)} frozen debt entr(y/ies) in the "
          f"host-owned template)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
