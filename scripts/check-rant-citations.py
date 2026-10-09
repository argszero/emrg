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
task-prompt templates, the evolution template, the upgrade/vibe-check prompts, the
GUI redesign spec, and the CI README the host reads to set the release Secrets.
Twelve files: the ten of the 2026-09-16 measurement - 49 citation sites over 35
distinct timestamps on that day's tree, counted
as `len({t for s in sites for t in s.timestamps})` over `scan_tree` - plus two the
class was missing until 2026-10-05, `competition_prompt.md` and
`prompts/memory_compaction.j2` (see `missing_templates` for the reading that
catches the omission). The same tree
read 47 sites and 33 timestamps without the CI README (47/32 on `f07368ba`, the
master commit it joined, before this template's own citations were rewritten into
records). Re-measured 2026-10-09 on the tree issue #1989 lands, the twelve-file
class reads **45 sites over 30 distinct timestamps in 9 of the 12 files**, and 43
sites over 28 timestamps in 8 files without the CI README. `memory_compaction.j2`
carries no site; `competition_prompt.md` carries four, and all four are the bare
`host, <ts>:` spelling this class could not see before, which is why admitting the
file and widening the word set are one change rather than two. Each figure is a
reading of the tree it names rather than an estimate.
(The figure printed here said 29 until issue #1289 measured it: neither it nor the
27 records-only timestamps.) The citation words are the class's own four spellings -
`rant`, `ruling`, `directive`, and the bare `host` (see `CITATION`): on `94504e5d` the original one-word set
scanned 39 sites and could see neither `vibe_check.j2`'s `Host directive
2026-10-06T10:40:46` nor the `Host ruling` line this widening was measured against, so
it answered rc 0 about an inventory that had not moved - measured 2026-10-09, the
widened set reads 40 sites on that same tree and 41 on the tree this change lands,
which adds one `ruling` site to `system.j2`. Each figure names its tree: `40` is
`94504e5d` and `41` is the landing tree, because widening the word set alone was not
enough - on `94504e5d` the widened scan also reddens, its new `vibe_check.j2` site
carrying no record, which is why this change adds one there.

The class is enumerated by hand, and **the enumeration is itself read**: a name
listed twice and a prompt template the list omits are the same defect pointing in
opposite directions (`duplicated_files`, `missing_templates`). Both are defects in
the class rather than in a citation - every citation rule still passes - which is
why neither can be left to the printed count being "known": the count is a claim
about the class, and a template outside it makes that claim a partial reading
printed as the whole. Code
comments are deliberately out of scope (the same
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

The frozen debt (empty as of 2026-09-16)
---------------------------------------
`DEBT` is the mechanism by which a site may stay host-local-only: it is a
`(file, timestamp)` set, and an entry that no longer occurs is itself a failure,
because a debt list that cannot shrink grows until it means "everything", which is
the same as no rule.

It is **empty**. The list existed for one file, `evolution_prompt.md`, on the reading
that routine evolution must not edit it - and the host has since ruled the boundary of
that red line (issue #1252): it forbids editing the copy that is **running**, i.e. the
one resolved as `Path(scheduler.__file__).parent / "evolution_prompt.md"`, and not the
repository copy. With the repository copy sweepable, its sites carry records like every
other site and the debt is gone; the running copy is replaced on the normal release
path, not by a cycle.

The mechanism stays, and stays tested: the synthetic-entry tests in
`tests/test_rant_citations.py` build a debt list, exercise the exemptions and assert the
stale report, so an empty real list is not an untested code path. What the real tree now
asserts is the opposite of what it asserted while the debt existed - that **no** site is
exempt - which is the claim that would fail if an entry were quietly added back.

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

#: The instruction class, as measured 2026-09-16: ten files, 49 sites, 35
#: timestamps (the guard's own `scan_tree` counts them; see the module docstring).
#: Names, not a glob: the class is a decision (prose a reader acts on),
#: so a new template has to be added here deliberately rather than swept in by a
#: pattern that also catches its code comments.
#:
#: `.github/workflows/README.md` is here because it is the one file outside the
#: templates that a reader acts on: it tells the host which Secrets to set for the
#: release pipeline, and two of its lines cited a rant with nothing else to go on
#: (issue #1289). Adding a *file* is the deliberate act the comment above asks for;
#: the alternative - a glob over every `*.md` - would sweep in the code comments the
#: docstring excludes, which is why this is a list and not a pattern.
#:
#: A name listed twice is a failure and not a harmless repetition: the guard scans it
#: twice, so every site in it is counted twice and the count this file prints - and
#: the docstring above quotes - inflates in silence. Measured 2026-09-16: resolving
#: #1290 x #1293 by keeping both sides of the hunk listed
#: `emrg/server/evolution_prompt.md` twice and the guard still returned `rc=0`, now
#: printing `58 site(s)` instead of 49. `duplicated_files` is where that is checked.
#: A name listed twice is a failure (`duplicated_files`); a prompt template that the
#: list omits is a failure too (`missing_templates`). The two are the same defect
#: pointing in opposite directions, and both are defects in the **class**: every
#: citation rule still passes, so nothing but a reading of the class itself can see
#: either one.
INSTRUCTION_FILES = (
    ".github/workflows/README.md",         # tells the host which Secrets to set
    "emrg/server/competition_prompt.md",   # a task template added 2026-10-03 (#1822)
    "emrg/server/evolution_prompt.md",     # swept: the red line covers the running copy
    "emrg/server/journal_prompt.md",
    "emrg/server/open_source_prompt.md",
    "emrg/server/promote_prompt.md",
    "emrg/server/paper_prompt.md",
    "emrg/server/prompts/memory_compaction.j2",  # rendered by daemon.COMPACTION_TEMPLATE
    "emrg/server/prompts/system.j2",
    "emrg/server/prompts/upgrade_prompt.j2",
    "emrg/server/prompts/vibe_check.j2",
    "docs/gui-redesign.md",
)

#: The template whose **running** copy is the one routine evolution must not edit (the
#: host's reading of the red line, issue #1252). Kept as a name here because the debt
#: list may only ever hold sites in this file, and because a name is not imported from
#: the test module: a guard that imported its own scope from a test would report a
#: failure when the test moves, not when the tree changes.
HOST_OWNED = "emrg/server/evolution_prompt.md"

#: `(file, timestamp)` -> why this site may stay host-local-only. **Empty**, and the
#: reason it is worth keeping rather than deleting: the set is what makes "this site
#: is allowed to cite no public record" a decision with a name attached, and the
#: stale check below is what stops it from growing silently. It held the eight
#: timestamps of `HOST_OWNED` until 2026-09-16, when the host ruled that the red line
#: covers the running copy rather than the repository copy (issue #1252) - so the
#: repository copy was swept and the list emptied in the same change, which the
#: docstring's "the two halves are one action" note already asked for. Every entry
#: used to be in the host-owned file for the same reason; the reason was spelled per
#: entry so the list could not quietly become "things nobody got to".
DEBT: dict[tuple[str, str], str] = {}

#: A citation: one of the class's citation words followed within three non-digits by a
#: timestamp. Three characters, not a line: `(rant 2026-…` and `(rants\n  2026-…`
#: are the same citation, while a timestamp that is *not* introduced by the word
#: cannot be a citation - which is what keeps `system.j2`'s memory-format
#: example (`event_at: 2026-01-15T14:30:00`) out of scope.
#:
#: The word set is not just `rant`. The rule is about a **host-local reference**, and
#: the class spells the same reference four ways: `rant` (`(rant 2026-08-23T08:04:26 …`),
#: `ruling` (`Host ruling, 2026-10-09T14:34:54 (PR #1986)`), `directive`
#: (`Host directive 2026-10-06T10:40:46`, `vibe_check.j2`), and the **bare** `host`
#: (`host, 2026-10-06T10:40:46:`, `competition_prompt.md` - the form that carries no
#: second word at all). Measured 2026-10-09 on `94504e5d`: the rant-only spelling
#: scanned 39 sites and could not see the two `directive`/`ruling` sites that sit in
#: this very class, so the guard answered rc 0 *about an inventory that did not move* -
#: the failure mode its own header warns about. `rulings?`/`directives?` were added
#: because of that reading, not by preference: the widened scan reads 40 sites on
#: `94504e5d` - the 39 plus `vibe_check.j2`'s newly visible `directive` - and exactly
#: that one carries **no** record, so the widening reddens the tree rather than leaving
#: it at rc 0. That redness is the finding, not a false positive: this change adds the
#: missing record beside that site, and the landing tree reads 41 sites with none
#: unbacked (its extra one is `system.j2`'s `ruling`).
#:
#: `hosts?` is the fourth, added 2026-10-09 for issue #1989, and it is the same lesson
#: one step further: `competition_prompt.md` carried **four** `host, <ts>:` citations
#: that no spelling reached, so admitting the file to the class (issue #1989's other
#: half) was not enough on its own - the file was invisible *and* its spelling was.
#: The bare form only fires where no other word precedes the timestamp (the alternation
#: is tried left to right and `Host ruling, <ts>` still matches through `ruling`, since
#: `host` cannot reach the timestamp past ` ruling, ` - nine characters, over the
#: three the window allows), so the count does not double.
#:
#: The word is a **named** group because a caller has to be able to repeat it
#: verbatim: a timestamp run (`rant ts1 + ts2`) whose two times resolve to
#: different records needs the word beside the second time, and `group(0)` cannot
#: supply it - that is the whole match, timestamp included, so repeating it
#: duplicates the first timestamp (measured, then fixed, 2026-09-16).
CITATION = re.compile(
    r"\b(?P<word>[Rr]ants?|[Rr]ulings?|[Dd]irectives?|[Hh]osts?)\b[^0-9\n]{0,3}"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)"
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


def duplicated_files(files: tuple[str, ...] | None = None) -> list[str]:
    """Class entries listed more than once, in the order they first repeat.

    A duplicated name is a defect in the **class**, not in a citation: the file is
    scanned twice, so it contributes its sites twice, and no other rule can see it -
    every site still resolves, so a tree with a double-counted class is green. That
    is why this is enforced here rather than left to the count being "known": the
    number is printed, and nothing asserts it (issue #1293's review, 2026-09-16).
    """
    listed = INSTRUCTION_FILES if files is None else files
    seen: set[str] = set()
    repeated: list[str] = []
    for name in listed:
        if name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    return repeated


#: The prompt templates a reader acts on, as globs over the checkout. Used only to
#: assert the hand-made enumeration above still **covers** them: membership stays the
#: deliberate decision `INSTRUCTION_FILES` states, and this is the reading that a
#: decision taken once does not silently stop covering a template added later. A glob
#: is the right instrument for this half precisely because it is not a decision -
#: measured 2026-10-05, `competition_prompt.md` was added on 2026-10-03 (#1822) and
#: nothing noticed it was outside the class.
PROMPT_TEMPLATE_GLOBS = ("emrg/server/*_prompt.md", "emrg/server/prompts/*.j2")


def template_files(root: Path | None = None) -> list[str]:
    """Every prompt template under `root`, as class-style relative names, in order.

    :param root: the tree to look in; defaults to this file's repository root.
    :returns: the template paths, POSIX-spelled so they compare to the class names.
    """
    base = REPO_ROOT if root is None else root
    out: list[str] = []
    for pattern in PROMPT_TEMPLATE_GLOBS:
        out.extend(
            sorted(
                p.relative_to(base).as_posix()
                for p in base.glob(pattern)
                if p.is_file()
            )
        )
    return out


def missing_templates(
    files: tuple[str, ...] | None = None, root: Path | None = None
) -> list[str]:
    """Prompt templates the class does not list, in `template_files` order.

    The mirror of `duplicated_files`, and it exists for the same reason: a template
    outside the class is invisible to every citation rule - nothing scans it - so the
    count this guard prints, and the module docstring quotes, silently covers less than
    it says. Measured 2026-10-05: `competition_prompt.md` (added 2026-10-03 by #1822)
    and `prompts/memory_compaction.j2` were both outside the class, and neither
    carries a citation site today - which is exactly why the omission could sit there:
    it changes no verdict until the day it does.

    :param files: the class to check; defaults to `INSTRUCTION_FILES`.
    :param root: the tree to look in; defaults to this file's repository root.
    :returns: one name per template the class does not list.
    """
    listed = INSTRUCTION_FILES if files is None else files
    known = set(listed)
    return [name for name in template_files(root) if name not in known]


def scan_tree(root: Path, files: tuple[str, ...] | None = None) -> tuple[list[Site], list[str]]:
    """Sites in `root`, plus the names of files that could not be read.

    `files` defaults to `INSTRUCTION_FILES` by lookup at call time rather than by
    binding at definition time: a test that sets the class list has to reach the
    scan, and a default bound at `def` time silently ignores it (measured
    2026-09-16: `mod.INSTRUCTION_FILES = ("not-here.md",)` left `main()` scanning
    the real ten files).
    """
    files = INSTRUCTION_FILES if files is None else files
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
    # A merged reader must see the `tree:` line before any verdict, and this
    # family's docstrings promise that order. stdout is block-buffered when it is
    # a pipe (how a cycle reads this report: `2>&1 | tail`) while stderr is not,
    # so without this every stderr line overtakes the tree line. Behaviour and
    # pin: tests/test_guard_report.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true",
                        help="print the inventory instead of enforcing the rule")
    args = parser.parse_args(argv)

    repeated = duplicated_files()
    if repeated:
        for name in repeated:
            print(f"duplicate class entry {name}: INSTRUCTION_FILES lists it twice, so "
                  f"its sites are counted twice and the count this guard prints - the "
                  f"one the module docstring quotes - inflates by that file's sites "
                  f"while every citation rule still passes")
        print(f"FAIL: {len(repeated)} duplicated instruction-class entr(y/ies)")
        return 1

    absent = missing_templates()
    if absent:
        for name in absent:
            print(f"missing class entry {name}: it is a prompt template a reader acts "
                  f"on, but INSTRUCTION_FILES does not list it, so no citation rule "
                  f"scans it and the count this guard prints covers less than it says")
        print(f"FAIL: {len(absent)} prompt template(s) outside the instruction class")
        return 1

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
    # The debt half of the sentence is conditional because it is now usually absent:
    # "0 frozen debt entries in the host-owned template" names a list and asserts
    # nothing, and a line that reports a number without a reader is how the count in
    # this file's docstring went stale before (issue #1289).
    debt = (f", {len(DEBT)} frozen debt entr(y/ies) in the host-owned template"
            if DEBT else ", no frozen debt")
    print(f"OK: every citation site in the instruction class names a public record "
          f"({len(sites)} site(s){debt})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
