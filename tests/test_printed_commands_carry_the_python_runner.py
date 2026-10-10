"""A printed command's runner must match its file type: the runner goes with `.py`.

The defect this pins, measured 2026-10-04
-----------------------------------------
`review-queue.py`'s re-trigger row printed its remedy by interpolating its `RUNNER`
constant in front of a **shell** script:

    command=f"{RUNNER} scripts/re-trigger-ci.sh <branch-of-{pr}>"

which renders as `uv run --no-sync python3 scripts/re-trigger-ci.sh`. That command cannot
run at all:

    uv run --no-sync python3 scripts/re-trigger-ci.sh
      File ".../scripts/re-trigger-ci.sh", line 11
        set -euo pipefail
                 ^^^^^^^^
      SyntaxError: invalid syntax                                  -> rc 1
    bash -n scripts/re-trigger-ci.sh                               -> parses
    scripts/re-trigger-ci.sh                                       -> dispatches (mode 755)

So the one row whose whole remedy is "re-trigger CI on the same head" - the state R4
describes when the push event was dropped - handed over a command that re-triggers
nothing. Why it happened is the point of putting a mechanism here: the rule lived in
`review-queue.py`'s `RUNNER` docstring as prose ("printed commands carry the runner"), and
prose does not say which file types the runner is *for*. The constant is right for the
`.py` tools (mode 644 here, so a bare `scripts/x.py` exits 126) and wrong for `.sh`, and
nothing read the difference.

The same class, measured again the next cycle (2026-10-05)
----------------------------------------------------------
Two clauses are not enough to catch the family, because both of the first version's clauses
scan for a runner that is **already there**: `RUNNER}` interpolations only exist in files
that declare the constant, and three of the tools did not. `cast-vote.py` printed every one
of its refusal and result remedies **bare**, and a bare path here is not a command:

    scripts/check-vote-count.py 1849                   -> rc 126
    scripts/review-queue.py --cycle cyc20261005-010923 -> rc 126
    scripts/check-merge-plan-suite.py 1849             -> rc 126
    uv run --no-sync python3 scripts/re-trigger-ci.sh  -> SyntaxError, rc 1

The first three are mode 644, so a bare path is not executable at all. The fourth is mode
755 and *does* run - which is the other half of the rule rather than an exception to it:
the runner has to match the file type, and behind the python runner a shell script exits 1
without re-triggering anything. Each row is the command typed as printed (measured
2026-10-05).

**On the host the row was typed on**, which the table has to say: those four rows were
taken on a POSIX host, and the fourth is true of one only. Measured 2026-10-05 by a
reviewer on a Windows host (cycle `cyc20261005-054639`): `Get-Command bash` ->
CommandNotFoundException and a git-bundled `bash.exe` present but not on PATH, so the bare
path does not run there either - *mode 755* is not a fact Windows carries. A `.sh` tool's
runnable spelling is therefore host-dependent too, and the printed remedies lead with the
one that is not: `gh workflow run test.yml --ref <branch>` (`scripts/review-queue.py`,
`scripts/cast-vote.py`), with `bash scripts/re-trigger-ci.sh <branch>` beside it.

A cycle that did what the message said - "re-read it with `scripts/check-vote-count.py`" -
got `command not found`/`rc 126` at the exact moment the tool had just refused to act. So
the third clause below reads the **mentions** rather than the interpolations, and therefore
sees files that never declared a runner at all.

What is scanned, and what deliberately is not
---------------------------------------------
Clause 1 (`_bearers`) reads the interpolation sites, by source text.

Clause 3 reads the **strings the code can print**, through the AST: string literals plus
f-strings reconstructed with each interpolation written out as its own source
(`{RUNNER}` -> `RUNNER`), which is what lets the clause see through the `RUNNER` constant
instead of calling the constant's own name the missing runner.

Within those strings, only a **command-shaped mention** counts: `scripts/<name>.py|sh`
followed by an **argument** - a `<PLACEHOLDER>`, a `--flag`, an interpolation, or a bare
number, the four spellings this family writes. The argument is the discriminator, and it is
what keeps the clause off prose: "update `VERSION_SOURCES` in `scripts/bump-version.py`"
ends at the file name and is not a thing to run, while `scripts/check-vote-count.py <PR>` is.

What the clause reads on this tree, measured 2026-10-11: **40** command-shaped mentions, in
nine tools - `bump-version.py` 2, `cast-vote.py` 8, `check-merge-freshness.py` 8,
`check-merge-sequence.py` 1, `check-release-published.py` 1, `check-stacked-prs.py` 1,
`check-vote-count.py` 1, `review-queue.py` 17, `run-mutation-arm.py` 1. The newest arrived the
way the three before it did - through a *remedy*: `check-vote-count.py`'s cure for a head with no
CI run names the re-trigger in `check-merge-freshness.py`'s own spelling
(`bash scripts/re-trigger-ci.sh <branch>`), added the same day as the per-cause cures above it
(cycle `cyc20261011-002826`). Before that the newest was `review-queue.py`'s row for a PR
whose CI half could not be read (cycle `cyc20261007-203559`), which names the re-ask that failed
(`uv run --no-sync python3 scripts/check-merge-freshness.py <PR>`), and before it
`check-stacked-prs.py`, which arrived the same way as the two before it: through a *remedy*. Its
row for a PR that would
land another open PR's work ends by naming the instrument that prices what that merge changes
(`uv run --no-sync python3 scripts/check-merge-landing-diff.py <PR>`), and naming a reading is
printing a command. The sentence also moved the cycle before, when `run-mutation-arm.py`'s
refusal for a gate it cannot load gained a runnable remedy
(`uv run --no-sync python3 scripts/check-merge-plan-suite.py --help`) - a printed command like
any other, carrying the runner like any other. Two of the others are new in one
afternoon and both came from a *remedy*: a printed remedy is a printed
command, so adding one to a tool's repair path moves this number whether or not the author
was thinking about citations (`review-queue.py`'s no-verdict row, and the reason
`check-merge-freshness.py` now gives for it). The lesson is not to stop printing remedies -
a remedy a reader cannot run is worth less than the count - but that this sentence is read
from the **tree**, so a branch that adds a mention has to re-measure it, and two such
branches merging together leave the second one to re-measure again. The same
measurement applied to `cast-vote.py` one commit earlier (`afaeae0f`) finds the same 8
mentions, **all 8 bare** - that is the second carrier this clause exists for, after the
previous cycle's `review-queue.py` / `check-merge-freshness.py` pair (`60d77678`).
`review-queue.py` moved 13 -> 16 across the two branches this file was merged from: 13 -> 15
when the `unblock` row stopped handing over `gh pr view --json mergeable,mergeStateStatus` (a
command that reprints the fact the row has just stated) and named the two readings that answer
instead (cycle `cyc20261006-091811`), and one more from the no-verdict row that arrived with
`check-merge-freshness.py`'s new kind (#1867). That merge is the shape this paragraph warns
about, one step further on: both branches measured 34 against their own base, and the tree they
land on together holds 36 - a count two branches can each be right about and still leave the
other one stale.

**Docstrings are excluded**, and the reason is measured rather than assumed. The docstrings
of `scripts/*.py` hold **65** mentions of that shape, far more than the strings the code
prints hold, and the 9 bare ones among them are the family *naming* its readings rather
than offering commands: four comparisons to a sibling's rule ("in the same shape as
`scripts/bump-version.py --check`"), three usage lines in `llm-cost-report.py` (a file that
never hands its docstring to the parser), one transcript line in `cast-vote.py`
(`scripts/check-vote-count.py 1255`, the reading a reviewer is shown getting), and one
argument value that merely looks like a command (`--file scripts/check-pr-base.py` in
`run-mutation-arm.py`'s usage block). A rule keyed on command *shape* cannot tell a name from
a command, and one that fired on those would be firing on the explanations it stands beside.
So this clause reads the strings a tool hands a reader, and leaves the prose alone.

The first version of this paragraph justified the same exclusion with a mechanism the matcher
does not have - that in a usage block "the runner stands on the *previous line*" and "a
mention-level rule cannot see a line above it". Measured 2026-10-05: the matcher does see it
(the runner pattern is matched against the text before the mention, and its whitespace class
spans newlines, so `uv run --no-sync python3` on one line with
`    scripts/check-vote-count.py <PR>` on the next reads as carrying the runner), and of the
10 command-shaped mentions in the family's own `--help` output, **9 carry the runner on the
same line** - the tenth being the comparison above rather than a wrapped usage line. The
exclusion is right; the reason given for it was not, and a rule whose stated justification is
not what it does is a rule a reader cannot reason from.

The exclusion is a *scope*, not a licence: `cast-vote.py`'s prose pointer to
`review-queue.py --cycle <id>` was given its runner in the same commit, by hand.

Both directions, because a check is only evidence if it can fail
----------------------------------------------------------------
Every matcher here is exercised on synthetic sources before it is trusted on the tree: the
bearer scan must find a `.sh` and clear a `.py`; the mention matcher must fire on
`scripts/x.py --flag` and on `scripts/x.py <PR>`, and must *not* fire on a file name in
prose or on a word that merely follows the path; and the interpreter test must call a bare
path missing and a `RUNNER`-prefixed one present. Each corpus clause also requires a
non-empty subject count: a scan that finds no site has measured nothing, and "nothing
found" is not the verdict "nothing wrong" (`emrg/server/evolution_prompt.md`, the
`never a pass` rule).

Scope, stated because a rule's boundary is part of it: this file scans `scripts/*.py`, the
tools that print commands. The docs (`Agent.md`, `DEVELOPMENT.md`) spell commands too, and
`DEVELOPMENT.md` is where a host-side reading is documented - a command there is checked by
`tests/test_documented_scripts_exist.py` for existence, not for its runner.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: `{RUNNER} scripts/<name>` — the runner interpolated in front of a script. The name is
#: read to the first character that cannot be part of a path, so the flags after it
#: (`--json`, `<branch>`) are not swallowed.
_BEARER = re.compile(r"RUNNER\}\s+scripts/([A-Za-z0-9_./-]+)")

#: The runner constant's definition, so a file that prints commands is known by its own
#: declaration rather than by this test's idea of where the constant lives.
_DEFINES_RUNNER = re.compile(r"^RUNNER\s*=\s*", re.MULTILINE)

#: A command-shaped mention: the path followed by one or more arguments. An argument is a
#: `<PLACEHOLDER>`, a `--flag`, an interpolation, or a bare number. The first three are how
#: the family writes the arguments it cannot know (`scripts/check-vote-count.py {PR}`); a
#: number is a literal one, as a hand-written message spells a run id. A **word** is not an
#: argument, which is what keeps "check-doc-count.py exits 1" out of the scan, and the
#: argument is consumed so the finding prints the command a reader would paste.
_ARGUMENT = r"(?:<[^>\n]+>|--[A-Za-z][A-Za-z-]*|\{[^}\n]*\}|\d+)"
_MENTION = re.compile(rf"scripts/([A-Za-z0-9_.-]+)\.(py|sh)(\s+{_ARGUMENT})+")

#: What has to stand in front of a `.py` mention. `{RUNNER}` is the constant as
#: `_printed_text` writes an interpolation out, so a mention behind the constant counts as
#: carrying it; the literal spellings are for the files that write the runner out by hand
#: (`bump-version.py` prints `python3 scripts/check-release-tag.py v<x.y.z>`, which runs -
#: python3 is on PATH - and is not this rule's subject). The braced form alone, without the
#: words around it, is what a hand-written runner can look like too (`{RUNNER}` cannot be
#: confused with a literal because a literal is written out in full).
_PY_RUNNER = re.compile(
    r"(?:\{RUNNER\}|RUNNER|uv run --no-sync python3|uv run python3|python3|python)\s+$"
)

#: A shell script's interpreter, and only that: the python spellings above are the defect.
_SH_RUNNER = re.compile(r"bash\s+$")


def _bearers(source: str) -> list[str]:
    """Every script name this source hands the runner to, in order."""
    return _BEARER.findall(source)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes, by position rather than by value.

    `body[0]` is the docstring's home, and taking it that way keeps a literal that happens
    to repeat a docstring's text distinguishable from the docstring itself.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _interpolation_parts(tree: ast.AST) -> set[int]:
    """The nodes already accounted for by their f-string, so they are not read twice."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                ids.add(id(value))
    return ids


def _printed_text(node: ast.AST) -> str | None:
    """A string node's printed text, with each interpolation written out as its source."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    return None


def _printed_strings(source: str) -> list[tuple[int, str]]:
    """Every string the code can print - literals and reconstructed f-strings.

    Docstrings are left out (see the module docstring for why the exclusion is a scope and
    not a licence), and an f-string's own pieces are read once, through the f-string.
    """
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    parts = _interpolation_parts(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and (id(node) in docs or id(node) in parts):
            continue
        text = _printed_text(node)
        if text is not None:
            out.append((node.lineno, text))
    return out


def _mentions(text: str) -> list[tuple[str, bool]]:
    """Every command-shaped mention in one printed string, with its interpreter's verdict."""
    out: list[tuple[str, bool]] = []
    for match in _MENTION.finditer(text):
        extension = match.group(2)
        before = text[: match.start()]
        runner = _SH_RUNNER if extension == "sh" else _PY_RUNNER
        out.append((match.group(0), bool(runner.search(before))))
    return out


def _sources() -> list[tuple[Path, str]]:
    """Every `scripts/*.py`, with its text.

    A file that cannot be read is a failure, not a skip: this guard's whole subject is what
    those files print, and an unread file has answered nothing.
    """
    return [
        (path, path.read_text(encoding="utf-8"))  # OSError is the failure it is
        for path in sorted(SCRIPTS.glob("*.py"))
    ]


def test_the_matcher_finds_a_shell_bearer_and_clears_a_python_one():
    """The matcher's own two directions, before it is trusted on the tree.

    Without this the clause below could pass by finding nothing at all: a regex that
    silently matched no bearer would report a clean family forever.
    """
    with_shell = 'command=f"{RUNNER} scripts/re-trigger-ci.sh <branch-of-{pr}>"\n'
    with_python = 'command=f"{RUNNER} scripts/check-vote-count.py {pr}"\n'

    assert _bearers(with_shell) == ["re-trigger-ci.sh"]
    assert _bearers(with_python) == ["check-vote-count.py"]


def test_the_mention_matcher_needs_an_argument_and_reads_the_runner_through():
    """What counts as a command, and what the verdict says about it.

    The not-a-command rows are the ones that keep this clause off prose - and they are real
    texts from this tree, not invented ones.
    """
    assert [m for m, _ in _mentions("run `scripts/check-vote-count.py {PR}` please")] == [
        "scripts/check-vote-count.py {PR}"
    ]
    assert [m for m, _ in _mentions("run `scripts/check-vote-count.py 1849` please")] == [
        "scripts/check-vote-count.py 1849"
    ]
    assert [m for m, _ in _mentions("(`scripts/check-merge-plan-suite.py <PR>`)")] == [
        "scripts/check-merge-plan-suite.py <PR>"
    ]
    # Not commands: a file name in prose, and a word that merely follows the path.
    assert _mentions("update VERSION_SOURCES in scripts/bump-version.py") == []
    assert _mentions("scripts/check-doc-count.py exits 1 when a doc states the count") == []

    # The verdict, both ways, including through an f-string's interpolation.
    assert _mentions("(`scripts/check-vote-count.py 1849`)")[0][1] is False
    assert _mentions("(`{RUNNER} scripts/check-vote-count.py 1849`)")[0][1] is True
    assert _mentions("(`bash scripts/re-trigger-ci.sh <branch>`)")[0][1] is True
    assert _mentions("(`{RUNNER} scripts/re-trigger-ci.sh <branch>`)")[0][1] is False


def test_the_string_reader_reconstructs_an_fstring_and_skips_docstrings():
    """The reader's two claims: f-strings come out as their text, docstrings do not come out."""
    source = (
        '"""A docstring naming scripts/check-vote-count.py 1849."""\n'
        "def f():\n"
        '    """Another one: scripts/review-queue.py --cycle <id>."""\n'
        '    x = f"{RUNNER} scripts/check-vote-count.py {pr}"\n'
        '    y = "plain scripts/check-merge-plan-suite.py <PR>"\n'
    )
    printed = [text for _, text in _printed_strings(source)]

    assert printed == [
        "{RUNNER} scripts/check-vote-count.py {pr}",
        "plain scripts/check-merge-plan-suite.py <PR>",
    ], "an f-string's interpolation is written out, and no docstring reaches the reader"  # noqa: RUF001
    # Which is what makes the verdicts above decidable: the constant's name is visible.
    assert [ok for _, ok in _mentions(printed[0])] == [True]
    assert [ok for _, ok in _mentions(printed[1])] == [False]


def test_every_runner_interpolation_names_a_python_tool():
    """Where a runner is printed, the script after it is one an interpreter runs."""
    sources = _sources()
    assert sources, (
        "no scripts/*.py could be read, so this scan measured nothing - which is not the "
        "verdict that no command is spelled wrong"
    )

    sites = 0
    offenders: list[str] = []
    for path, text in sources:
        for name in _bearers(text):
            sites += 1
            if not name.endswith(".py"):
                offenders.append(f"{path.name}: {name}")
    assert sites, (
        "no file interpolates a runner in front of a script, so the rule below had no "
        "subject - an unreadable queue is not a clean one"
    )
    assert not offenders, (
        "a printed command hands the python runner a script that is not a python tool: "
        + ", ".join(offenders)
        + " - a `.sh` takes `bash` (measured: the python runner gives it a SyntaxError "
        "and exit 1)"
    )


def test_every_command_shaped_mention_states_its_interpreter():
    """Clause 3: a mention a reader can paste must say how to run it.

    This is the clause that sees a file which never declared a runner - the shape
    `cast-vote.py` had - and it is the one that would have caught the previous cycle's
    defect in a file the first two clauses could not read at all.
    """
    sources = _sources()
    assert sources, (
        "no scripts/*.py could be read, so this scan measured nothing - which is not the "
        "verdict that no command is spelled wrong"
    )

    sites = 0
    offenders: list[str] = []
    for path, text in sources:
        for lineno, printed in _printed_strings(text):
            for mention, ok in _mentions(printed):
                sites += 1
                if not ok:
                    offenders.append(f"{path.name}:{lineno}: {mention}")
    assert sites, (
        "no script in this family hands a reader a command, which cannot be true of the "
        "tools whose whole output is remedies - so this scan has measured nothing, and "
        "'nothing found' is not 'nothing wrong'"
    )
    assert not offenders, (
        "a printed command cannot run as printed - a bare `scripts/x.py` is mode 644 here "
        "(rc 126), a `.sh` is not a python file, and the reader is being told to run it at "
        "the moment the tool refused to act: " + "; ".join(offenders)
    )


def test_a_source_that_prints_a_bare_command_is_caught():
    """The clause's own positive control, on a synthetic file rather than the live tree.

    Without this, a scan whose matcher silently stopped matching would report the family
    clean - the failure mode the live-tree assertion above cannot distinguish from health.
    """
    caught = 'print(f"re-read it with scripts/check-vote-count.py {pr}")\n'
    cleared = 'print(f"re-read it with {RUNNER} scripts/check-vote-count.py {pr}")\n'

    assert [
        m for _, text in _printed_strings(caught) for m, ok in _mentions(text) if not ok
    ], "a bare command in a printed string must be a finding"
    assert not [
        m for _, text in _printed_strings(cleared) for m, ok in _mentions(text) if not ok
    ], "the same line with the runner in front must clear"


#: The sentence in the module docstring that states what the clause reads, and its two
#: numbers. Parsed rather than restated: the docstring is the carrier a reader of this
#: file meets first, and a number in it is a claim about the tree - which is what
#: `Agent.md` means by *a derived number is never written where a guard can measure it*.
#: This guard measures it on every run, so the claim is checked here instead of trusted.
#: The anchor is the claim's **shape**, not its wording: any sentence that states this
#: coverage has to carry the bolded total ({total} below is read from it), so a rewording
#: of the prose around the numbers is not a finding while a rewrite that drops them is.
#:
#: **Both spellings, and one home for the pair.** This quantity is written two ways in the
#: prose - `<N> command-shaped mentions`, and the shorter `<N> in the strings the code
#: [actually] prints` a comparison reaches for - and a guard that read either one alone
#: would be green over a docstring stating the quantity twice with one of the two stale.
#: That is not hypothetical: measured 2026-10-05, the first spelling was corrected to
#: **32** while the second went on saying **31** one paragraph down, and the guard of the
#: day - anchored on the first spelling only, and reading its first match - reported the
#: file clean (the veto on PR #1859, cycle `cyc20261005-162709`). So every occurrence of
#: either spelling is read, and the two of them together must be exactly one claim: one
#: quantity with two homes is one number free to drift apart, and a writer who has to
#: correct it in two places is a writer who will correct it in one.
_COVERAGE_CLAIM_SHAPES = (
    re.compile(r"\*\*(\d+)\*\* command-shaped mentions"),
    re.compile(r"\*\*(\d+)\*\* in the strings the code"),
)
_COVERAGE_TOOL = re.compile(r"`([A-Za-z0-9_-]+\.py)` (\d+)")


def _counts_by_tool() -> dict[str, int]:
    """What the clause below reads on this tree, per file, as a name -> count mapping.

    The same three readers the clauses use, in the same order, so the documentation and
    the verdict cannot be about two different measurements.
    """
    counts: dict[str, int] = {}
    for path, text in _sources():
        found = sum(
            len(_mentions(printed)) for _, printed in _printed_strings(text)
        )
        if found:
            counts[path.name] = found
    return counts


#: How the number of tools reads in the claim. Prose, not a parsed field - but the remedy
#: below writes the whole sentence, so it has to write this part the way the docstring does.
_TOOL_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _coverage_remedy(counts: dict[str, int]) -> str:
    """The sentence this tree's numbers call for, in the shape the claim is written in.

    A guard that names a mismatch and not its replacement leaves the arithmetic to the
    reader, and the arithmetic is the part that has to be redone at every merge: measured
    2026-10-06 (`cyc20261006-115348`), two branches each measured **34** command-shaped
    mentions against their own base - both numbers right about the tree each one stood on -
    while the tree one of them landed on after merging the other held **36**. The number a
    branch has to write is the one its *merge result* has, which no branch can measure from
    where it stands, and that reconciliation was done by hand twice in one cycle. The
    numbers here come from the reader the verdict comes from - `_counts_by_tool` - so the
    sentence a failure prints cannot be about a different measurement than the failure
    itself.
    """
    tools = ", ".join(f"`{name}` {count}" for name, count in counts.items())
    word = _TOOL_WORDS.get(len(counts), str(len(counts)))
    noun = "tool" if len(counts) == 1 else "tools"
    return (
        f"What the clause reads on this tree, measured <date>: "
        f"**{sum(counts.values())}** command-shaped mentions, in {word} {noun} - {tools}"
    )


def _documented_counts(docstring: str) -> tuple[int, dict[str, int]]:
    """The docstring's stated total and per-tool counts.

    Raises rather than returning empty: a claim that cannot be found is a failure of this
    check, not a reason to skip it - the `never a pass` rule the clauses above cite. And it
    raises on a *second* claim rather than reading the first: the quantity this reads is
    stated in two spellings, and any second occurrence of either is another sentence making
    the same claim - the drift the shapes exist to catch, one site to the left of where
    this guard was first looking.
    """
    claims = [
        (match, match.group(1))
        for shape in _COVERAGE_CLAIM_SHAPES
        for match in shape.finditer(docstring)
    ]
    assert claims, (
        "the module docstring states no coverage in the `**N** command-shaped mentions` "
        "shape - that sentence is what a reader uses to see what this clause covers, and "
        "this check reads it, so a rewrite has to keep the numbers and their shape or the "
        "check would pass by finding nothing"
    )
    assert len(claims) == 1, (
        "the module docstring states this count "
        f"{len(claims)} times ({', '.join('**' + n + '**' for _, n in claims)}) across the "
        "`**N** command-shaped mentions` and `**N** in the strings the code` shapes - one "
        "quantity with two homes is one number free to drift apart, and a guard that reads "
        "a single site goes green over the other disagreeing with it. State the clause's "
        "reading once; a second count of the same kind needs its own shape and its own "
        "measurement, not a repeat of this one."
    )
    total_match, total_text = claims[0]
    # From the total to the first blank line: the paragraph that makes the claim, so a
    # count written elsewhere (a transcript line, an example) is not read as one.
    paragraph = docstring[total_match.start() :].split("\n\n", 1)[0]

    tools = {name: int(count) for name, count in _COVERAGE_TOOL.findall(paragraph)}
    assert tools, (
        "the coverage claim states no per-tool counts, so a reader cannot tell which files "
        f"this clause reads: {paragraph!r}"
    )
    return int(total_text), tools


def test_the_stated_coverage_is_the_measured_one():
    """The docstring's count is a claim about the tree, so the tree checks it.

    Measured 2026-10-05, on the commit that landed this file: the sentence said **31**
    mentions in six tools with `review-queue.py` **12**, and the clause read **32** with
    `review-queue.py` **13** - one more, because the last commit of the same PR added the
    portable re-trigger spelling as a second line of one remedy. The number went stale
    inside the change that wrote it, which is the whole reason it is read here instead of
    asserted there.
    """
    docstring = ast.get_docstring(ast.parse(Path(__file__).read_text(encoding="utf-8")))
    assert docstring, "this file has no module docstring, so its coverage claim is gone"

    stated_total, stated_tools = _documented_counts(docstring)
    measured = _counts_by_tool()

    assert measured, (
        "the clause read nothing on this tree, so there is no measurement to compare the "
        "docstring against - 'nothing found' is not 'nothing wrong'"
    )
    assert sum(measured.values()) == stated_total, (
        f"the docstring says {stated_total} command-shaped mentions, and this clause reads "
        f"{sum(measured.values())} - write this tree's numbers in its place:\n\n"
        f"    {_coverage_remedy(measured)}\n"
    )
    assert stated_tools == measured, (
        "the docstring's per-tool breakdown is not what the clause reads - a reader uses "
        f"it to find the files this rule covers: stated {stated_tools}, measured {measured}"
        f"\n\n    {_coverage_remedy(measured)}\n"
    )


def test_the_remedy_is_written_in_the_shape_this_guard_reads():
    """The sentence a failure prints is read back through the same clause that reads the claim.

    The remedy is not a second implementation of the coverage sentence if the guard's own
    reader accepts it: parsed by `_documented_counts`, it must yield exactly the counts
    `_counts_by_tool` measured. That is what keeps a printed fix from drifting away from the
    claim it is a fix for - a paste of it has to be a claim this file can read, and reading
    it back has to agree with the tree.
    """
    measured = _counts_by_tool()
    assert measured, "the clause read nothing, so there is no remedy to check"

    remedy = _coverage_remedy(measured)
    stated_total, stated_tools = _documented_counts(remedy + "\n")

    assert (stated_total, stated_tools) == (sum(measured.values()), measured), (
        "the printed remedy is not the sentence this guard reads: a reader who pastes it "
        f"would still be off. printed {remedy!r}, parsed {(stated_total, stated_tools)!r}, "
        f"measured {(sum(measured.values()), measured)!r}"
    )


def test_the_remedy_counts_every_tool_and_labels_the_number_of_them():
    """Both halves of the sentence on a synthetic tree, so each can fail on its own.

    The real tree's counts cannot separate "the total is the sum" from "the total happens
    to equal the first tool's count", and nothing there exercises the word for a tool count
    other than the one this file has. Synthetic inputs are the only place the arithmetic can
    be asked directly.
    """
    two = _coverage_remedy({"a.py": 2, "b.py": 1})
    assert "**3** command-shaped mentions" in two, (
        f"the total is the sum of the per-tool counts, not one of them: {two!r}"
    )
    assert "in two tools" in two, f"the number of tools has to be stated: {two!r}"
    assert "`a.py` 2" in two and "`b.py` 1" in two, (
        f"every tool of the measurement has to be named with its own count: {two!r}"
    )

    one = _coverage_remedy({"only.py": 4})
    assert "**4** command-shaped mentions" in one and "in one tool -" in one, (
        f"a single tool is not 'one tools', and its count is still the total: {one!r}"
    )


def test_a_second_sentence_stating_the_same_count_is_caught():
    """Two homes for one number is the defect, so the reader refuses the second.

    Both directions on synthetic docstrings, because the assert above is only evidence if
    it can fail: one claim is read in either of its two spellings, and the same claim
    stated twice is a failure even when the two sites agree - the failure this pins is not
    an arithmetic one, it is that a number written twice can be corrected once. The third
    case is the text that actually occurred, verbatim: **32** corrected at one site while
    the other went on saying **31**, in the spelling the guard of the day did not read.
    """
    one = (
        "What the clause reads on this tree, measured 2026-10-05: **32** command-shaped "
        "mentions, in\nsix tools - `review-queue.py` 13. The same measurement applied to\n"
        "`cast-vote.py` finds 8.\n\nA later paragraph, without a claim.\n"
    )
    total, tools = _documented_counts(one)
    assert (total, tools) == (32, {"review-queue.py": 13}), (
        "a single claim must be read, not refused: this is the shape the guard exists to read"
    )

    other_spelling = one.replace(
        "**32** command-shaped mentions", "**32** in the strings the code prints"
    )
    total, tools = _documented_counts(other_spelling)
    assert (total, tools) == (32, {"review-queue.py": 13}), (
        "the shorter spelling states the same quantity, so it has to be read rather than "
        "left unrecognised - a shape the guard does not know is a home it cannot check"
    )

    #: Verbatim the measured defect: the first spelling corrected, the second left stale.
    as_it_happened = one + (
        "\n**Docstrings are excluded** ... hold **65** command-shaped mentions against "
        "**31** in the strings the code\nactually prints.\n"
    )
    for docstring, number in ((as_it_happened, "31"), (one + "\nand **31** in the strings "
                                                         "the code prints.\n", "31")):
        try:
            _documented_counts(docstring)
        except AssertionError as exc:
            message = str(exc)
            assert "states this count" in message and "**31**" in message, (
                f"the refusal has to name the sites it found, not just count them: {message!r}"
            )
            assert number in message, f"the stale site has to be named: {message!r}"
        else:
            raise AssertionError(
                "a docstring stating the same count twice was read as if it had one home - "
                "this is exactly the drift the shapes exist to catch, and a guard reading "
                "one site goes green over the other disagreeing with it"
            )
