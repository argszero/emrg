#!/usr/bin/env python3
"""Classify merge-conflict blocks and say what the correct resolution is.

Why this exists (measured, cycle `cyc20260911-112155`): while unblocking the PR
queue after a merge, two conflicts of the *same shape* — "both sides edited this
file" — needed **opposite** resolutions, and telling them apart took several
rounds of hand measurement (`comm`, `diff`, counting `def test_` by hand):

* **#1136**: the branch carried an unmerged *duplicate* of #1134's tests (it had
  been built on #1134, which was then squash-merged, so git could no longer see
  the common ancestry). Master's copy was a strict superset — measured, zero
  lines existed only in the branch's copy — so **taking master's side** was
  correct, and it removed a duplicate rather than losing coverage.
* **#1140**: the sides were **disjoint** additions (this branch's
  `_resolve_root()` probes vs master's argv/decode probes, for the same test
  files). Either side-pick would have silently dropped four of the branch's own
  probes; the only correct resolution is to **keep both**.

A blanket `--theirs` is right for the first and wrong for the second. The
distinction is measurable, so it should not have to be re-derived by hand in
every cycle that runs this cascade — which, while the queue is deep, is every
cycle.

Two further shapes were fixed on 2026-09-11 (`cyc20260911-190629`), both found by
differencing this tool against **every real conflict block in the open-PR queue**
rather than against fixtures I wrote myself — which is the point: the fixtures I
write encode the shapes I already believe in, and both of these sat outside that
set while the tests stayed green.

* **Several count lines in one block.** 3 of the last 51 commits touching
  `Agent.md` moved 2+ documented counts at once, and git then emits one block
  covering all of them. The one-line-only count rule let that block reach the
  content-line fallback, which answered `KEEP BOTH` (concatenate) at rc 0 and
  emitted two copies of every count line — the exact state this repo's
  `_duplicated_count_line_kinds` guard rejects. Aligned sides that differ only in
  their numbers are now `count-line`, at any length.
* **The same lines at two revisions.** Sharing no byte-equal line is *not*
  evidence of separate additions: an older and a newer revision of a paragraph are
  never equal. #1140's live `Agent.md` block had ours' two paragraph lines as
  strict prefixes of master's (890 vs 539 and 601 vs 471 characters), and the
  fallback's `KEEP BOTH` would have emitted the stale *and* the current copy of
  each paragraph, at rc 0. A strict prefix relation now escalates to a human.

The tool is a **decision aid, not an automatic resolver**. It never edits a
file: it classifies each conflict block and prints the resolution the evidence
supports, so the class is explicit and reviewable instead of inferred. The cases
it cannot decide (`overlapping`) are where a human must read both sides.

Usage:
    python3 scripts/classify-conflict.py <file> [<file> ...]
    python3 scripts/classify-conflict.py --all        # every unmerged path

Exit codes:
    0  every block classified, none needs a human decision
    1  at least one block is `overlapping` (human must decide)
    2  usage error / no conflict blocks found
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# A conflict block. Deliberately the same shape as `check-doc-count.py`'s
# CONFLICT_BLOCK: the opening marker, then ours, then the separator, then
# theirs, then the closing marker. `(?!<<<<<<<)`-style guards are unnecessary
# here because we scan one block at a time with a non-greedy body.
CONFLICT_BLOCK = re.compile(
    r"^<{7} (?P<ours_label>.*?)\n(?P<ours>.*?)^={7}\n(?P<theirs>.*?)^>{7} (?P<theirs_label>.*?)$",
    re.MULTILINE | re.DOTALL,
)

# `merge.conflictStyle = diff3` (and `zdiff3`) inserts a `||||||| <base>` section
# between ours and the separator, so it does **not** match `CONFLICT_BLOCK` - and
# the old scan did not notice, it simply read the base into `ours`. Measured
# 2026-09-11 with a real block:
#
#     <<<<<<< HEAD          ours    = "def test_alpha():\n    assert 1 == 1\n"
#     def test_alpha():              + "||||||| merged common ancestors\n"
#         assert 1 == 1              + "def test_beta():\n    assert 2 == 2\n"
#     ||||||| merged common ancestors
#     def test_beta():      theirs  = "def test_gamma():\n    assert 3 == 3\n"
#         assert 2 == 2
#     =======
#     def test_gamma():
#         assert 3 == 3
#     >>>>>>> master        -> "disjoint ... KEEP BOTH (concatenate)"   exit 0
#
# Concatenating that keeps the **base** copy - a third version of the same hunk
# that neither side wants - and the advice arrives with exit 0, i.e. as a
# verdict, when the honest answer is "I cannot read this layout". The sibling
# tool reached the same conclusion first and refuses the layout by name
# (`check-doc-count.py`); a tool whose whole value is its discrimination must
# not keep advising on a shape it mis-reads.
#
# The label for a block this tool refuses to read: it is an answer in its own
# right ("a human must look"), not a failure of the classification rules.
UNPARSED_LAYOUT = "unparsed-layout"

# A single line that differs from another only by an integer. The repo's
# Agent.md count lines are the recurring case ("... (1401) — import check: ...").
_NUMBER = re.compile(r"\d+")

# A *documented* count: a parenthesised number that is not a call argument, i.e.
# the `(` is at the start of the line or preceded by a non-word character. This
# is what separates the Agent.md count line ("`uv run pytest tests/ -v` (1401)")
# from code that happens to differ by a literal ("x = compute(1)").
#
# Without it the count-line rule fired on any one-line numeric difference, so a
# bare code change was labelled `count-line`, given "MEASURE ... never pick a
# side", and exited 0 - wrong advice on code and, worse, a verdict that closes
# the only case left for a human to look at.
_DOC_COUNT = re.compile(r"(?:^|[^\w])\(\s*\d")

# Symbols a hunk *declares*. This is the axis that decides duplicate-vs-disjoint,
# and it is not the same as "which text lines are shared": my first version
# compared content lines and classified the two real #1140 conflicts as
# `overlapping` because both sides happened to contain `    """` and `    )` -
# structural boilerplate, not work. Measured on the same blocks, the declared
# names are fully disjoint, which is the property that actually matters.
#
# The declaration may be **indented**: the first version anchored at column 0, so
# every method in a class body was invisible - and that is the shape this repo's
# conflicts actually take. Measured 2026-09-11 (cyc20260911-171843) over this
# repo's own `tests/` + `scripts/`: 1262 of 2155 declarations (58.6%) sit at
# column 0, i.e. **893 (41.4%) were invisible**, across 46 of 80 files. The
# consequence is not a missing label but an *inverted* one for the same collision,
# differing only in indentation:
#
#     def test_alpha(x=1):        |    def test_alpha(self, x=2):
#         assert compute(x)==1    |        assert compute(x)==2
#     -> overlapping, rc 1        |    -> disjoint "KEEP BOTH", rc 0
#
# Concatenating the right-hand block leaves two same-name definitions where the
# second wins, so one side's edit disappears - at rc 0, i.e. as actionable advice.
# Reported by how2how2how2-arch, reproduced here before accepting it.
#
# `^\s*` also matches a `def` inside a multi-line string literal. That trade is
# made knowingly: a fixture quoting a declaration is read as declaring it, which
# can only move a verdict *toward* `overlapping` (a human reads it), never toward
# a silent side-pick.
_SYMBOL = re.compile(r"^[ \t]*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)

# Classification labels.
IDENTICAL = "identical"
DUPLICATE = "duplicate"
DISJOINT = "disjoint"
COUNT_LINE = "count-line"
OVERLAPPING = "overlapping"


def _content_lines(text: str) -> list[str]:
    """The lines that carry content: trailing whitespace kept out, blanks out.

    Blank lines are omitted because indentation/blank-line churn between two
    sides is not evidence of either duplication or of a conflict; comparing
    them would classify identical code as `overlapping`.
    """
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _count_kind_prefix(line: str) -> str | None:
    """The text naming the count's *kind*, or None when the line supplies none.

    This is everything before the first documented count (`_DOC_COUNT`), located by
    the regex **match position** rather than by splitting the masked line: the mask
    character is `#`, which is also the comment marker, so splitting on it lands on
    a literal `#` in real prose and returns bare indentation. Two unrelated comments
    then compare equal (`cyc20260912-070619`).

    A line whose count comes first carries no kind text, so it supplies no evidence
    that two sides are the same fact - returning None keeps it from firing on the
    strength of an empty string.
    """
    match = _DOC_COUNT.search(line)
    if match is None:
        return None
    prefix = line[: match.start()].strip()
    return prefix or None


def _differ_only_by_number(ours: list[str], theirs: list[str]) -> bool:
    """True when the sides are aligned and differ only in integers (all counts).

    Every corresponding pair must carry a *documented* count (see `_DOC_COUNT`): a
    parenthesised number, as in the Agent.md count line. Without that condition
    `x = compute(1)` vs `x = compute(2)` matched, and the tool answered "measure,
    never pick a side, exit 0" about a code change - advice that is not merely
    unhelpful but actively closes the one case a human must read.

    The sides must also be the **same length**: a block that only differs by
    numbers is an aligned pair of revisions, and an unaligned hunk is a different
    shape (there is no pairing to compare).

    Several count lines at once, not just one. The first version required both
    sides to be exactly one line, which was measured 2026-09-11
    (`cyc20260911-190629`) to be too narrow in a *silent* way: 3 of the last 51
    Agent.md commits moved 2+ documented counts at once, and git then emits one
    block covering all of them. That block fell through to the content-line
    fallback, which saw "the two sides share no content line" - true, the numbers
    differ - and answered KEEP BOTH (concatenate) at rc 0, concatenating two
    copies of every count line: precisely the state this repo's own
    `_duplicated_count_line_kinds` guard rejects. Measured on a real `git merge`
    of an aligned two-count block, and reproduced for three counts.
    """
    if not ours or len(ours) != len(theirs):
        return False
    for a, b in zip(ours, theirs):
        if a == b:
            continue
        if not (_DOC_COUNT.search(a) and _DOC_COUNT.search(b)):
            return False
        # Masking every digit run is what makes this "differs only by numbers":
        # all non-digit characters must still line up, so a text difference - a
        # renamed test, a reworded sentence - cannot pass as a count change.
        if _NUMBER.sub("#", a) != _NUMBER.sub("#", b):
            return False
    return True


def _looks_like_a_revision(ours: list[str], theirs: list[str]) -> bool:
    """True when a line on one side is a strict prefix of a line on the other.

    Sharing no byte-identical line is **not** evidence that the sides are separate
    additions: two sides can be the same lines at different revisions, and those
    are never equal. A strict prefix relation is the cheap exact signal of that -
    the newer side merely continues where the older one stopped.

    Measured on #1140's live Agent.md block (2026-09-11, `cyc20260911-190629`):
    ours' two paragraph lines are strict prefixes of master's two (890 vs 539 and
    601 vs 471 characters, same order), so "the two sides share no content line -
    KEEP BOTH" would have emitted **both the stale and the current copy of each
    paragraph**. Advice that duplicates content is as wrong as advice that drops
    it, and this one arrived at rc 0, i.e. as a verdict.

    The conservative answer is taken deliberately. The prefix relation is real
    evidence that the shorter side is an older revision, but it is weaker than the
    symbol path's name-subset test (a prefix is not a claim about the rest of the
    line), so it escalates to a human instead of recommending a side-pick. The two
    errors are not symmetric: escalating costs one read, a wrong "take theirs"
    silently drops a line.
    """
    for a in ours:
        for b in theirs:
            if a != b and (b.startswith(a) or a.startswith(b)):
                return True
    return False


def _looks_like_a_count_revision(ours: list[str], theirs: list[str]) -> bool:
    """True when an aligned pair of differing lines is the same documented count.

    The content-line fallback answers `KEEP BOTH` when the sides share no line,
    on the reasoning that "no shared line" means "two separate additions". That
    reasoning fails whenever the sides are the same lines at two revisions, and
    the failure is silent in the worst direction: concatenation emits the stale
    copy *and* the current copy.

    `_looks_like_a_revision` catches the pure text case (a line one side merely
    continues). This function catches the case that check structurally cannot:
    the differing pair is a **count line against a longer revision of itself**,
    where a strict prefix does not hold because the numbers sit *inside* the line
    and everything after them was rewritten.

    Measured 2026-09-11 (`cyc20260911-194733`). Reproduced with a real
    `git merge-file` on adjacent lines: ours `Python: ... (1393)` beside a
    `Doc count sync:` line, theirs the same two lines edited divergently. Both
    master and the parent PR's more general alignment rule (which requires the
    sides to be the same length *and* every differing pair to be count-shaped)
    answer `disjoint - KEEP BOTH (concatenate)` at rc 0, and the concatenation
    contains two `Python: \\`uv run pytest\\`` lines - the state
    `tests/test_doc_counts.py::_duplicated_count_line_kinds` rejects, i.e. a doc
    claiming two different pytest counts.

    Over the last 400 commits touching `Agent.md`, **7** hunks reach the fallback
    with a count line in them and every one of them gets content-duplicating
    advice: `e46c160` and `0c8a212` (aligned, now `count-line`), `cb651a4` (2v2)
    and `5c039b4` (3v3) - also aligned, but mixing a count line with a text
    revision, hence invisible to the equal-length rule - and `3335877` (1v2),
    `444e1d5` (1v2), `18fd0af` (1v13), which are unaligned. This function
    escalates all of the remaining five.

    Only a pair whose *first* line is a documented count escalates - the evidence
    is a count left unchanged beside a revision of the same block, not any
    alignment of differing lines - which is what keeps this rule off code blocks.
    Index-aligned rather than length-equal: the count lines pair up at the front
    in every measured case, and requiring equal lengths is what made `cb651a4`
    and `5c039b4` invisible. The masking is the whole evidence - a count pair that
    is equal once digits are removed is one fact re-measured, and a fact stated
    twice with two values is what the repo's own guard rejects, so `KEEP BOTH`
    cannot be right for it whatever else the block holds. Escalating is the cheap
    error: a read costs a minute, a silent duplicate ships.

    **Equal once masked is too narrow a test for "the same count kind"**
    (`cyc20260912-002444`). It requires the *whole rest of the line* to match, so a
    count line that was also **re-breakdown** - the same measured kind, its
    parenthesised detail revised - does not qualify, and the block falls through to
    `disjoint - KEEP BOTH` at rc 0. Measured on an authentic block from merge
    `47af6bc2`: ours `GUI: `cd emrg/gui && npm test` (92: ... + 3 preload-api +
    3 boot-contract)` against master's `(89: ... + 3 preload-api)` (one component
    removed *and* the total moved 92 -> 89). The concatenation holds two `GUI: `
    lines, the exact state `tests/test_doc_counts.py::_duplicated_count_line_kinds`
    rejects - driven through that guard, not inferred - and the sides' line counts
    are 1 vs 2, so neither the equal-length rule nor the index-pairing mask
    comparison can see it. Over **185** conflict blocks rebuilt from this repo's
    real merge commits (legacy `git merge-tree` on each merge's three real blobs),
    this rule changes exactly **1** class: that block, `disjoint` -> `overlapping`.

    The gap is measured in the right unit: **the same documented-count kind stated
    twice**, which is what the repo's guard keys on - not "the lines are equal".
    Two lines naming the same kind and differing in the parenthesised breakdown are
    one count kind at two revisions, and keeping both is what duplicates it.

    The axis is the **kind text**: everything before the first documented count,
    found by the regex match position (`_count_kind_prefix`). That is deliberately
    wider than "the whole line is identical once masked" - it admits same-kind pairs
    whose tails differ - because the re-breakdown shape *must* differ in its tail to
    be the shape it is. The widening is accepted on the asymmetry the fallback
    already relies on: both sides state the same count kind, so `KEEP BOTH`
    concatenates a duplicate and escalating asks for a read. A read costs a minute; a
    silent duplicate ships.

    It is also narrower than "both lines carry a count", which is what keeps it off
    unrelated blocks: a line with no kind text before its count supplies no evidence
    (`None`), and two unrelated comments mentioning numbers have different text
    before theirs. Both were real defects of the earlier `split("#", 1)` form, which
    split on a literal `#` in prose - the mask character and the comment marker are
    the same character (`cyc20260912-070619`).
    """
    for a, b in zip(ours, theirs):
        if a == b:
            continue
        if _DOC_COUNT.search(a) and _DOC_COUNT.search(b):
            masked_a, masked_b = _NUMBER.sub("#", a), _NUMBER.sub("#", b)
            if masked_a == masked_b:
                return True
            # Same count kind, revised breakdown: the two sides name the same kind
            # and only the parenthesised detail moved. The axis is the *kind text* -
            # everything before the first documented count, which is the command and
            # the label - so `GUI: ... (92: ...)` beside `GUI: ... (89: ...)` fires
            # while `Python: ... (1500)` beside `GUI: ... (100)` does not.
            #
            # The widening is deliberate and is the reason this clause is safe to
            # add: the masked-equality test above demands the *whole rest of the
            # line* match, which the re-breakdown shape fails by construction (the
            # breakdown moved). This one demands only the kind text match, so it
            # also admits a same-kind pair whose tails differ
            # (`... (900) # 1 note` beside `... (900) # 2 notes`,
            # `cyc20260912-090216`). That is accepted rather than fixed: both sides
            # still state the same count kind, so keeping both concatenates a
            # duplicate - the state `_duplicated_count_line_kinds` rejects - and
            # escalating asks a human to read instead. Escalating is the cheap
            # error; a silent duplicate ships. Measured over 185 conflict blocks
            # rebuilt from this repo's real merge commits, the clause changes
            # exactly 1 class.
            #
            # Errors in the other direction are what the kind text prevents: it is
            # `None` when the line supplies no kind (a count first, or no count),
            # and two unrelated comment lines that merely mention numbers have
            # different text before their counts. Both were live defects of the
            # earlier `split("#", 1)` version (`cyc20260912-070619`).
            prefix_a = _count_kind_prefix(a)
            prefix_b = _count_kind_prefix(b)
            if prefix_a is not None and prefix_a == prefix_b:
                return True
    return False


def _symbols(text: str) -> set[str]:
    """Names of the functions/classes a hunk declares (empty for non-code)."""
    return set(_SYMBOL.findall(text))


def _symbol_bodies(text: str) -> dict[str, str]:
    """Map each declared symbol to its *normalised* body text.

    Bodies are compared alongside the name sets so that "theirs declares every
    name ours does" cannot be read as "theirs contains ours". The two are not the
    same claim: when both sides contain `test_alpha` but with different bodies,
    taking theirs silently discards ours' edit - the exact data loss this tool
    exists to prevent, hidden behind a subset test that only ever looked at names.
    """
    bodies: dict[str, str] = {}
    matches = list(_SYMBOL.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = "\n".join(_content_lines(text[m.end() : end]))
        bodies[m.group(1)] = body
    return bodies


def _shared_bodies_agree(ours_bodies: dict[str, str], theirs_bodies: dict[str, str]) -> bool:
    """True when every symbol declared on *both* sides has the same body."""
    for name in set(ours_bodies) & set(theirs_bodies):
        if ours_bodies[name] != theirs_bodies[name]:
            return False
    return True


def classify(ours_text: str, theirs_text: str) -> tuple[str, str]:
    """Return (label, recommendation) for one conflict block.

    The `duplicate` test is a *subset* test over declared symbols, in both
    directions, so the recommendation can name which side is the superset. That
    is the measurement that decided #1136: it is not "one side is shorter", it
    is "every name one side declares is present in the other".

    When a hunk declares no symbols (a doc, a config, a prose line) the axes fall
    back to content lines, which is all there is to compare.
    """
    ours = _content_lines(ours_text)
    theirs = _content_lines(theirs_text)

    if ours == theirs:
        return IDENTICAL, "both sides are byte-identical; take either"

    if _differ_only_by_number(ours, theirs):
        return (
            COUNT_LINE,
            "differs only by a number - MEASURE on the merged tree, never pick a "
            "side (both sides are stale by construction); for Agent.md's test "
            "count use `check-doc-count.py --resolve-conflict`",
        )

    ours_syms, theirs_syms = _symbols(ours_text), _symbols(theirs_text)

    if ours_syms or theirs_syms:
        # Declared symbols are the evidence; shared boilerplate is not.
        only_ours = ours_syms - theirs_syms
        only_theirs = theirs_syms - ours_syms
        if not only_ours and not only_theirs:
            return (
                OVERLAPPING,
                "both sides declare the SAME names - a human must read both sides "
                "and reconcile the bodies (this is a real edit collision)",
            )
        # A subset verdict may only stand if the *shared* symbols are also
        # identical. Otherwise the side-pick drops ours' edits to those symbols:
        # `test_alpha` present on both sides with different bodies would be taken
        # from theirs, and the change would vanish with no signal.
        ours_bodies = _symbol_bodies(ours_text)
        theirs_bodies = _symbol_bodies(theirs_text)
        if not _shared_bodies_agree(ours_bodies, theirs_bodies):
            return (
                OVERLAPPING,
                "one side's names are a superset, but a symbol declared on BOTH "
                "sides has a different body - a side-pick would silently discard "
                "that edit, so a human must reconcile the shared symbol(s)",
            )
        if not only_theirs:
            return (
                DUPLICATE,
                "every name theirs declares is already ours - take OURS; this is "
                "the unmerged-duplicate shape from a squash-merged base, not lost "
                "work",
            )
        if not only_ours:
            return (
                DUPLICATE,
                "every name ours declares is already theirs - take THEIRS; this is "
                "the unmerged-duplicate shape from a squash-merged base, not lost "
                "work",
            )
        return (
            DISJOINT,
            f"the sides declare different symbols (ours only: "
            f"{len(only_ours)}, theirs only: {len(only_theirs)}) - KEEP BOTH "
            f"(concatenate); either side-pick silently drops one side's work",
        )

    # No declarations to compare: fall back to content lines. Unlike the symbol
    # path, containment *is* sufficient here - `ours_set < theirs_set` means every
    # line ours has appears byte-identically in theirs, so a superset pick cannot
    # drop an edit. (Audited when the symbol path's body check was added above.)
    #
    # One line against one line is exempt: with no symbols and no documented
    # count, a single differing line is ambiguous - it is either one line edited
    # (a human must pick) or two adjacent additions (keep both) - and `KEEP BOTH`
    # is wrong for the first (`x = f(1)` vs `x = f(2)` would concatenate into
    # nonsense). No evidence means no verdict, so escalate rather than guess.
    if len(ours) == 1 and len(theirs) == 1:
        return (
            OVERLAPPING,
            "one differing line on each side with no declared symbol and no "
            "documented count - this is either an edit to that line or two "
            "adjacent additions, and the lines do not say which, so a human must "
            "read it",
        )

    ours_set, theirs_set = set(ours), set(theirs)
    if not ours_set & theirs_set:
        # No shared line is *not* automatically disjoint additions: the sides can
        # be the same lines at two revisions, which are never byte-equal. Treat the
        # prefix relation as the evidence that this is what happened, and escalate
        # - see `_looks_like_a_revision`.
        if _looks_like_a_revision(ours, theirs) or _looks_like_a_count_revision(ours, theirs):
            return (
                OVERLAPPING,
                "the sides share no line, but they are the same lines at two "
                "revisions (a line on one side continues a line on the other, or "
                "one side left the documented count where the other revised it) - "
                "so KEEP BOTH would emit both copies and a side-pick may drop a "
                "change; a human must read it",
            )
        return (
            DISJOINT,
            "the two sides share no content line - KEEP BOTH (concatenate)",
        )
    if ours_set < theirs_set:
        return (DUPLICATE, "theirs is a strict superset - take THEIRS")
    if theirs_set < ours_set:
        return (DUPLICATE, "ours is a strict superset - take OURS")

    both = ours_set & theirs_set
    return (
        OVERLAPPING,
        f"sides partially overlap ({len(both)} shared line(s)) - a human must read "
        "both sides; this is the one case no rule can decide",
    )


def conflicts_in(text: str) -> list[tuple[str, str, str]]:
    """All conflict blocks in `text` as (ours, theirs, theirs_label)."""
    return [
        (m.group("ours"), m.group("theirs"), m.group("theirs_label"))
        for m in CONFLICT_BLOCK.finditer(text)
    ]


def base_section(text: str) -> str | None:
    """The base marker of a diff3 conflict block, or None if there is none.

    Returns the marker *line* whole, label included: the caller prints it, and
    the label is what tells a human which commit the base came from.

    The marker is matched only *inside* an open `<<<<<<<` .. `>>>>>>>` region,
    not anywhere in the file. Matching it anywhere is the obvious first version
    and it is wrong: a file that merely *mentions* the marker - a test fixture, a
    doc, this tool's own comment - has no conflict at all, yet was refused with
    "the file uses the diff3 layout" and told to re-merge. Measured 2026-09-11
    (cyc20260911-165337) against a three-line prose file. Anchoring the marker
    inside a real block is what makes the refusal mean what it says, and it is
    the ordering the sibling tool already uses (`check-doc-count.py` matches the
    block first, then looks for the base section).

    The scan is line-by-line rather than one regex because the *sides* can
    contain the other markers; once a block is open, a `|||||||` line is the
    base section - which is exactly the state this tool cannot parse.
    """
    open_block = False
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            open_block = True
        elif open_block and line.startswith(">>>>>>>"):
            open_block = False
        elif open_block and line.startswith("|||||||"):
            return line
    return None


def _unmerged_paths() -> list[str]:
    """Paths git currently reports as unmerged, in index order."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in (proc.stdout or "").splitlines() if line.strip()]


def _report(path: str, text: str) -> list[str]:
    """Classify one file; return the labels found (for the exit code)."""
    # The layout check comes FIRST, not inside the "no block matched" branch:
    # `CONFLICT_BLOCK` *does* match a diff3 block (it swallows the base section
    # into OURS), so a check placed after it would never run. The parser cannot
    # be trusted on this layout, so it must not get a chance to answer.
    base = base_section(text)
    if base is not None:
        print(f"{path}: block 1/1 -> {UNPARSED_LAYOUT}")
        print(f"    the file uses the diff3 layout (`{base}`)")
        print(
            "    this tool does not parse that layout: `CONFLICT_BLOCK` reads the "
            "base section as part of OURS, so the sides it compares - and the "
            "advice it gives - would be wrong"
        )
        print(
            "    re-merge with git's default layout (git config "
            "merge.conflictStyle merge and recreate the conflict), or resolve "
            "by hand"
        )
        return [UNPARSED_LAYOUT]

    blocks = conflicts_in(text)
    if not blocks:
        print(f"{path}: no conflict blocks")
        return []

    labels: list[str] = []
    for i, (ours, theirs, label) in enumerate(blocks, 1):
        kind, advice = classify(ours, theirs)
        labels.append(kind)
        print(f"{path}: block {i}/{len(blocks)} -> {kind}")
        print(f"    ours   {len(_content_lines(ours))} content line(s)   (HEAD)")
        print(f"    theirs {len(_content_lines(theirs))} content line(s)   ({label})")
        print(f"    {advice}")
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Classify merge-conflict blocks and report the resolution the "
            "evidence supports (never edits files)."
        ),
    )
    parser.add_argument("paths", nargs="*", help="conflicted files to classify")
    parser.add_argument(
        "--all",
        action="store_true",
        help="classify every path git reports as unmerged",
    )
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.all:
        paths.extend(_unmerged_paths())
    # de-duplicate, preserving order
    seen: set[str] = set()
    paths = [p for p in paths if not (p in seen or seen.add(p))]

    if not paths:
        print(
            "error: no paths given (pass files, or --all for every unmerged path)",
            file=sys.stderr,
        )
        return 2

    all_labels: list[str] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            print(f"{path}: not a file", file=sys.stderr)
            return 2
        all_labels.extend(_report(path, p.read_text(encoding="utf-8", errors="replace")))

    if not all_labels:
        print("error: no conflict blocks found in any given path", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for label in all_labels:
        counts[label] = counts.get(label, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"\nsummary: {summary}")

    if OVERLAPPING in counts:
        print(
            "at least one block needs a human decision (overlapping) - this tool "
            "does not guess"
        )
        return 1
    if UNPARSED_LAYOUT in counts:
        # exit 1 too: the caller's loop treats rc 0 as "every block was classified,
        # safe to act on the advice". A refused layout is not classified.
        print(
            "at least one block could not be read (unparsed-layout) - the file "
            "must be re-merged in git's default layout, or resolved by hand"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
