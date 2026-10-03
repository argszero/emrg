"""A line that means to continue onto the next one continues - in every carrier a command is written in.

Measured 2026-10-03 (`cyc20261003-133828`). The defect this reads for was found twice on the
same day, in two carriers, and the second copy came from the first:

* `DEVELOPMENT.md` shipped the preflight command with **two** backslash bytes at the end of
  each continued line. Inside a ```bash fence that is an *escaped backslash*, not a line
  continuation, so the documented command ran the backslash as a command twice and reached the
  preflight with **none** of its three variables set - producing the exit-`2` "no verdict was
  reached" reading that the paragraph directly beneath the block exists to prevent. (Reported
  by `pm25coder`'s review of `cyc20261003-115810` on PR #1828, and fixed there.)
* `scripts/check-notary-credentials.py` carried the **same two bytes in its own `Usage:`
  block** - which is where the document's copy came from. That one was still live on master
  when this guard was written.

Both were blessed by pins that read the *words* of the command. Naming a command is not
shipping one: every name was present, in a block nobody could run.

**The reader is the shell's own rule, and it is one line.** A line continues when it ends in an
*odd* number of backslashes; an even number is an escaped backslash followed by an end of line.
Measured 2026-10-03 in bash, on one two-line script with 1, 2, 3 and 4 backslashes at the end of
its first line: 1 and 3 join the next line into the same command, 2 and 4 run it as a second one.
So the rule reports an **even** run of two or more - not "two or more", which would name a line
that continues and say it splits.

**Two characters the shell does not trim, this reader must not either.** A backslash followed by
whitespace escapes that whitespace, so the line ends there and the next line is a separate command
(measured: the same script with a stray space splits, without it joins). A CR from a CRLF file is
escaped the same way and never reaches the newline, so a continued line in such a file does not
continue. Both split exactly like a doubled backslash, and both were invisible to this file's
first reader: it stripped the whitespace before counting the run, and `str.splitlines()` drops the
CR on the way in. A reading that could not tell those two states apart was reading them as one -
so the readers below count the run on the line as it sits in the file, and name the second shape.

**The class is every tracked file except vendored third-party bundles**, discovered rather than
listed. Measured when this was written: 622 tracked files, 141 of them vendored, so a corpus of
**481 files** - and the sweep finds **no offender** once the two fixes are in. A new carrier (a
script's docstring, a workflow `run:` block, a README fence) is covered the moment it lands, and
reading tracked files keeps this off host-local state: `.emrg/` is untracked, so the cycle
records this instance writes are not files this rule reads.

The vendored boundary is the one this repository already draws - `tests/test_check_doc_count.py`
keeps `dist/` and `node_modules/` out of its walk "because a rule that read them would report
claims nobody in this repo wrote" - and the *measured* reason it is needed here is that
`emrg/gui/vendor/monaco/vs/editor-KLE6jdfb.js` carries a doubled backslash inside minified
JavaScript, where it is an escaped backslash in a regular expression and not a command at all.

**One thing this file's own measurement got wrong, recorded because it is the failure mode the
rule is about.** The first sweep applied a size cap and skipped anything over 2 MB - which
silently dropped exactly that Monaco bundle, and the sweep therefore reported "0 offenders" for
a corpus it had not finished reading. The guard's own first run surfaced it. A reader that
quietly skips an input is indistinguishable from one that read it and found nothing, which is
why the sweep below has **no cap** and reports a file it cannot open rather than passing over
it.
"""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Written as `chr(92)` so this file carries **no backslash literal at all**. The defect is a
#: doubled one, and a pin on a doubled literal is a pin a future edit can get wrong the same
#: way it did; `BACKSLASH * 2` cannot.
BACKSLASH = chr(92)

#: Directories holding third-party sources, by the boundary the repository already uses. Matched
#: as a path *component* of the parent directories, so a file named `vendor.js` is still read.
VENDORED_DIRS = ("vendor", "node_modules", "dist")

#: How many tracked files the sweep must see before its verdict means anything. Far below the
#: 481 measured on 2026-10-03, because the number should not need updating as files are added -
#: but far above zero, so a moved checkout or an empty `git` answer cannot pass.
CORPUS_FLOOR = 300


def is_vendored(name: str) -> bool:
    """Is this path inside a vendored third-party directory?

    The **parent** components are what count: a file at `emrg/tools/vendor.py` is ours and is
    read, while `emrg/gui/vendor/...` is not.
    """
    return any(part in VENDORED_DIRS for part in Path(name).parts[:-1])


def tracked_files() -> list[str]:
    """Every tracked path, relative to the repository root, minus the vendored ones.

    Fails loudly when `git` cannot answer: an empty list would satisfy the rule while having
    read nothing, and "could not list the files" is not "no file has this defect".
    """
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"`git ls-files` failed (rc={proc.returncode}), so the corpus was never read: "
        f"{proc.stderr.strip()!r}"
    )
    return [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and not is_vendored(line)
    ]


def _lines(text: str) -> list[str]:
    """The file's lines **as the shell reads them**: split on the newline, keep every other byte.

    `str.splitlines()` is the wrong reader here, and measurably so: it also splits on a bare CR
    and drops it, so a CRLF file's continued line would come back *ending in a backslash* - a
    continuation - while the shell escapes the CR and never reaches the newline.
    """
    return text.split("\n")


def trailing_backslashes(line: str) -> int:
    """How many backslashes the line ends in, read on the line exactly as it sits in the file."""
    return len(line) - len(line.rstrip(BACKSLASH))


def doubled_continuations(text: str) -> list[tuple[int, int]]:
    """`(line number, count)` for each line ending in an **even** run of two or more backslashes.

    An even run is an escaped backslash followed by an end of line, so the command splits there
    and whatever follows is a separate command. An odd run continues the line, so it is not
    reported however long it is - that is the shell's rule, measured (see this module's docstring).

    Only the very end of the line counts: a doubled backslash *inside* a line is ordinary. A line
    whose backslash is followed by whitespace is a different shape, read by `void_continuations` -
    and the run is counted here on the *untrimmed* line so that the two readers cannot both claim
    the same line.
    """
    found: list[tuple[int, int]] = []
    for number, line in enumerate(_lines(text), start=1):
        count = trailing_backslashes(line)
        if count >= 2 and count % 2 == 0:
            found.append((number, count))
    return found


def void_continuations(text: str) -> list[tuple[int, int]]:
    """`(line number, count)` for each line that means to continue onto the next one and cannot.

    Measured 2026-10-03 in bash, on one two-line script: with a backslash and a stray space at the
    end of its first line, the two lines run as two commands - the backslash escapes the space, so
    the line ends there - while the same script with no space joins them into one. A CR from a CRLF
    file is escaped the same way, so a continued line in such a file does not continue either.

    The test is the line's **last non-whitespace character**: when it is a backslash and the line
    does not itself end in one, the whitespace sits between the backslash and the newline, and the
    shell will not reach past it.
    """
    found: list[tuple[int, int]] = []
    for number, line in enumerate(_lines(text), start=1):
        if line.endswith(BACKSLASH):
            continue
        count = trailing_backslashes(line.rstrip())
        if count == 0:
            continue
        found.append((number, count))
    return found


def sweep() -> list[str]:
    """Every offender in the corpus, as a printable line - with no input skipped silently."""
    offenders: list[str] = []
    for name in tracked_files():
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # Reported rather than passed over: a file the reader could not open is a file
            # this rule did not read, and a rule that quietly drops its inputs is the same
            # failure as a rule that quietly passes (see this module's docstring).
            offenders.append(f"{name}: could not be read as UTF-8 text ({exc})")
            continue
        for number, count in doubled_continuations(text):
            offenders.append(
                f"{name}:{number} ends in {count} backslashes - an even number of them is an "
                f"escaped backslash, not a continuation, so whatever follows is a separate command"
            )
        for number, count in void_continuations(text):
            offenders.append(
                f"{name}:{number} ends in {count} backslash(es) and then whitespace - the shell "
                f"escapes that whitespace and ends the line there, so whatever follows is a "
                f"separate command"
            )
    return offenders


def test_the_corpus_is_read_and_carries_real_continuations() -> None:
    """The premise, in both directions.

    Too few files means the sweep read nothing. No continued line anywhere means the reader is
    looking for a shape this repository has stopped using, so its silence would be about
    nothing. Measured 2026-10-03: 481 files in class, and continued lines in the workflows, the
    install and packaging scripts, the task templates and the scripts' own usage blocks.
    """
    names = tracked_files()
    assert len(names) >= CORPUS_FLOOR, (
        f"only {len(names)} tracked file(s) were listed as in class - the rule would pass by "
        f"reading almost nothing"
    )

    continued = 0
    for name in names:
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in _lines(text):
            count = trailing_backslashes(line)
            if count and count % 2 == 1:
                continued += 1
    assert continued >= 20, (
        f"only {continued} continued line(s) exist in the whole corpus, so this rule guards a "
        f"shape the repository has stopped using - re-measure before trusting its silence"
    )


def test_the_vendored_bundles_are_out_of_class() -> None:
    """The class boundary, pinned in both directions.

    Both halves matter: a boundary that let vendored files in would report the Monaco bundle's
    regular expressions, and one that swallowed an ordinary path would quietly shrink the
    corpus until the rule was about nothing.
    """
    assert is_vendored("emrg/gui/vendor/monaco/vs/editor-KLE6jdfb.js") is True
    assert is_vendored("dist/runtime/lib/vendor.py") is True
    assert is_vendored("node_modules/pkg/readme.md") is True
    assert is_vendored("emrg/tools/vendor.py") is False, (
        "a file *named* vendor.py is this repository's own code and must stay in class"
    )
    assert is_vendored("tests/test_shell_continuations.py") is False


def test_no_tracked_file_carries_a_continuation_the_shell_will_not_honour() -> None:
    """The rule: a line that means to continue onto the next one continues.

    Both shapes are read, because both split the command the same way - an even run of
    backslashes (an escaped backslash) and a backslash followed by whitespace (an escaped space,
    or the CR of a CRLF file). The failure names its file and line, because the remedy differs by
    carrier - the same bytes break the command inside a workflow `run:` block or an `.sh` file,
    and break the command a script *documents* when they sit in its usage block, which is how this
    defect reached `DEVELOPMENT.md`.
    """
    offenders = sweep()
    assert not offenders, (
        "these lines mean to continue onto the next one and the shell will not do it - the "
        "command splits there, and the arguments after it never arrive:\n  " + "\n  ".join(offenders)
    )


def test_a_doubled_backslash_is_reported_with_its_line() -> None:
    """The failing direction, on synthetic text.

    The corpus is clean once the fix lands, so this is the arm that shows the rule
    discriminates rather than merely being silent on today's tree - and it pins the *count* as
    well as the line, so a reader that reported every backslash-terminated line as a defect
    would fail here instead of burying the real one in noise.
    """
    text = (
        f"first line {BACKSLASH}{BACKSLASH}\n"
        f"second line {BACKSLASH}\n"
        f"third line {BACKSLASH}{BACKSLASH}{BACKSLASH}{BACKSLASH}\n"
    )
    assert doubled_continuations(text) == [(1, 2), (3, 4)], (
        f"expected the two doubled lines and their counts, got "
        f"{doubled_continuations(text)!r}"
    )


def test_a_single_backslash_is_a_continuation() -> None:
    """The other direction: one backslash is exactly what the carriers are full of.

    A reader that flagged these would report the workflows, the install and packaging scripts
    and the task templates as defects, and the exemptions that followed are how a rule widens
    until it is ignored.
    """
    text = f"git merge-base --is-ancestor HEAD FETCH_HEAD {BACKSLASH}\n    echo reachable\n"
    assert doubled_continuations(text) == [], (
        f"a single trailing backslash was read as a defect: {doubled_continuations(text)!r}"
    )


def test_a_backslash_inside_a_line_is_not_read_as_a_continuation() -> None:
    """The reader looks at line *ends*, not at every backslash in the file.

    Doubled backslashes inside string literals and regular expressions are ordinary and appear
    throughout this repository. Reading them as continuations would make the rule about spelling
    rather than about the shell, and the real defect - which is always at an end of line - would
    be lost among the noise.
    """
    text = f'escaped = "a{BACKSLASH}{BACKSLASH}b"  # a doubled backslash inside a line\n'
    assert doubled_continuations(text) == [], (
        f"a backslash that is not at the end of its line was read as a continuation: "
        f"{doubled_continuations(text)!r}"
    )


def test_an_odd_run_still_continues() -> None:
    """The stated rule decides, not the tempting complement of it.

    Measured 2026-10-03 in bash: with three backslashes at the end of a line the *next* line is
    joined into the same command, and with four it is a second command. A reader that reported
    "two or more" would name the three-backslash line - a line that continues - and report it as
    one that splits, which is what the failure message it prints says.
    """
    assert doubled_continuations(f"cmd {BACKSLASH * 3}\n") == [], (
        "an odd run continues the line, so it is not this rule's defect however long it is"
    )
    assert doubled_continuations(f"cmd {BACKSLASH * 4}\n") == [(1, 4)]


def test_a_backslash_before_trailing_whitespace_is_reported() -> None:
    """The whitespace the shell escapes, this reader must read as an end of line.

    Measured 2026-10-03 in bash, on one two-line script: with a stray space after the backslash
    the two lines run as two commands, and without it they join into one. The reader counts the
    run on the line as it sits in the file, so the space is between the backslash and the
    newline and the line is named.
    """
    assert void_continuations(f"cmd {BACKSLASH} \n") == [(1, 1)]
    assert void_continuations(f"cmd {BACKSLASH}{BACKSLASH} \n") == [(1, 2)]
    assert void_continuations(f"cmd {BACKSLASH}\n") == [], (
        "a line that does end in a backslash continues, and belongs to the other reader"
    )
    assert void_continuations("cmd   \n") == [], (
        "trailing whitespace alone is not a continuation that was meant"
    )


def test_a_crlf_continuation_is_reported() -> None:
    """A CR ends the line for the shell, so the same two bytes do not continue in a CRLF file.

    Measured 2026-10-03 in bash, on a CRLF script: the two lines ran as two commands, while the
    same text with LF alone joined them. `str.splitlines()` hides this by splitting on the CR and
    dropping it, which is why the readers here split on the newline instead - and the doubled
    shape is read on the same untrimmed line, so neither reader misses a CRLF file.
    """
    assert void_continuations(f"cmd {BACKSLASH}\r\n") == [(1, 1)]
    assert void_continuations(f"cmd {BACKSLASH}{BACKSLASH}\r\n") == [(1, 2)], (
        "in a CRLF file the CR *is* the whitespace that ends the line, so the doubled run is "
        "read by this reader rather than by `doubled_continuations`"
    )
    assert doubled_continuations(f"cmd {BACKSLASH}{BACKSLASH}\r\n") == []
    assert _lines(f"cmd {BACKSLASH}\r\n")[0].endswith("\r"), (
        "the reader must see the CR; splitlines() would have dropped it"
    )


def test_the_two_readers_do_not_claim_the_same_line() -> None:
    """Each line has one shape, and naming it twice would double every real offender.

    The count is read on the untrimmed line precisely so that a backslash run followed by
    whitespace lands in `void_continuations` alone.
    """
    for line in (
        f"cmd {BACKSLASH}",
        f"cmd {BACKSLASH}{BACKSLASH}",
        f"cmd {BACKSLASH} ",
        f"cmd {BACKSLASH}{BACKSLASH} ",
        f"cmd {BACKSLASH}{BACKSLASH}{BACKSLASH}",
    ):
        text = line + "\n"
        both = doubled_continuations(text) and void_continuations(text)
        assert not both, f"{line!r} was claimed by both readers: {both!r}"




# ── the same byte in the other carrier ───────────────────────────────────────
#
# Everything above models the **shell**, where a trailing backslash continues the line and the
# command grows. A `.py` file has a second carrier for the same byte with a different effect:
# inside a string literal, a `\` at the end of a line makes Python remove the backslash **and the
# newline**, so the value joins the two lines. The command a docstring *documents* is then not the
# text its reader meets.
#
# Measured 2026-10-03 (`cyc20261003-182057`) on the tree #1832 landed: that PR changed
# `scripts/check-notary-credentials.py`'s `Usage:` block from `\\` to a single `\` - correct for
# the *shell* carrier the block documents, wrong for the carrier it is *written in* - so
# `ast.get_docstring` returned the whole command as **one** line. The readers above cannot see it:
# they ask for an **even** run, so a single backslash at a line end is not a shape to them. A rule
# whose model is the shell, applied to a file that is not a shell, is silent where it is needed -
# and the same sweep over the tree then found the shape in `scripts/run-mutation-arm.py`, whose own
# `Usage` block had been a single merged line since it was written.
#
# This reader asks the question the byte-level sweep was missing - *which carrier is this line in?* -
# by tokenizing the file rather than scanning it.

#: A string literal's own prefix, e.g. the `rb` of `rb"""..."""`. Python's rule (a backslash at the
#: end of a line joins it to the next) holds for every string that is not raw.
_LITERAL_PREFIX = re.compile(r"[A-Za-z]*")


def _is_raw_literal(literal: str) -> bool:
    """Does this literal's own prefix make it raw? A raw string keeps its backslashes."""
    return "r" in _LITERAL_PREFIX.match(literal).group(0).lower()


def string_lines_that_lose_their_backslash(text: str) -> list[tuple[int, int]] | None:
    """`(line, count)` for each line ending in an odd backslash run inside a **non-raw string**.

    Each one is a line the *source* continues and the *value* does not: Python removes the backslash
    and the newline together. The defect is invisible in the source - it looks exactly like the shell
    continuation the readers above tell authors to write - and visible only in the carrier the line
    is actually in.

    **Tokens, not bytes.** Three shapes look like this defect to a byte reader and are not it, and all
    three are in this repository today:

    * a backslash directly after the three opening quotes suppresses the leading newline. It is
      the idiom every multi-line string here uses, and the join is what its author asked for. Read
      from the token, the backslash has nothing before it on the opening line, which is what
      separates the idiom from a continuation.
    * a `\\` joining two **adjacent literals** in *code* (`"a \\` + newline + `"b"`). Python joins
      those too, but there the backslash is between two tokens rather than inside one, so no token
      carries it.
    * a raw f-string. Its segments are not `STRING` tokens, and their source text does not carry the
      prefix that makes them raw - so a byte reader calls `rf"C:\\Users"` a non-raw literal with a
      trailing backslash. Read from tokens, f-strings are a different token kind and out of class.

    Returns `None` when the file cannot be tokenized: a file this reader could not read is reported
    by its caller rather than passing as one with nothing in it.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    found: list[tuple[int, int]] = []
    for token in tokens:
        if token.type != tokenize.STRING or _is_raw_literal(token.string):
            continue
        for offset, line in enumerate(token.string.split("\n")):
            if offset == 0:
                # What follows the opening quotes on the literal's own first line. A backslash with
                # nothing before it is the leading-newline idiom, not a continuation.
                after_quotes = line.lstrip("\"' ")
                if trailing_backslashes(line) and not after_quotes.rstrip(BACKSLASH):
                    continue
            count = trailing_backslashes(line)
            if count and count % 2 == 1:
                found.append((token.start[0] + offset, count))
    return found


def test_no_python_string_loses_a_backslash_to_its_own_carrier() -> None:
    """The rule for the carrier the shell readers cannot model.

    A `.py` file whose string literal ends a line in a single backslash is not continuing a command:
    Python is removing the newline, so the text every consumer meets (a docstring, a message, a
    template) is not the text the source shows. Fix a hit by making the literal raw - the spelling
    that satisfies both carriers - not by removing the continuation the shell block needs.
    """
    offenders: list[str] = []
    for name in tracked_files():
        if not name.endswith(".py"):
            continue
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            offenders.append(f"{name}: could not be read as UTF-8 text ({exc})")
            continue
        hits = string_lines_that_lose_their_backslash(text)
        if hits is None:
            offenders.append(f"{name}: could not be tokenized, so its strings were never read")
            continue
        for number, count in hits:
            offenders.append(
                f"{name}:{number} ends in {count} backslash(es) inside a non-raw string - Python "
                f"removes the backslash and the newline, so the value joins this line to the next "
                f"and the text a reader meets is not the text the source shows"
            )
    assert not offenders, (
        "these lines mean 'continue' to a shell and 'join the lines' to Python, so the string they "
        'are in is not what it looks like. Write the literal raw (r"""...""") if a single '
        "backslash must reach its reader:\n  " + "\n  ".join(sorted(offenders))
    )


class TestTheCarrierOfAPythonString:
    """Both directions, on the shape that landed, and the three boundaries that keep it precise."""

    def test_a_non_raw_docstring_loses_the_newline(self) -> None:
        text = 'x = """one ' + BACKSLASH + '\ntwo"""\n'
        assert string_lines_that_lose_their_backslash(text) == [(1, 1)]

    def test_the_same_text_in_a_raw_literal_is_not_reported(self) -> None:
        """The fix itself: raw is the spelling that satisfies both carriers, so it is not a fault."""
        text = 'x = r"""one ' + BACKSLASH + '\ntwo"""\n'
        assert string_lines_that_lose_their_backslash(text) == []

    def test_the_leading_newline_idiom_is_not_a_continuation(self) -> None:
        """`\"\"\"\\` suppresses the string's first newline - that join is the point of writing it.

        23 of the 28 corpus hits a byte reader reported were this shape, in six files, and every one
        of them was correct: a rule that flagged them would be a rule nobody could keep.
        """
        text = 'x = textwrap.dedent("""' + BACKSLASH + "\n    a = 1\n\"\"\")\n"
        assert string_lines_that_lose_their_backslash(text) == []

    def test_a_backslash_joining_two_literals_in_code_is_not_this_shape(self) -> None:
        """`\"a \\` + newline + `\"b\"` joins the *literals*, and the backslash is between tokens."""
        text = 'x = "one " ' + BACKSLASH + '\n    "two"\n'
        assert string_lines_that_lose_their_backslash(text) == []

    def test_a_raw_fstring_is_out_of_class(self) -> None:
        """A raw f-string keeps its backslashes; its segments do not carry the `r` prefix."""
        text = 'x = rf"C:' + BACKSLASH + 'Users' + BACKSLASH + 'me"\n'
        assert string_lines_that_lose_their_backslash(text) == []

    def test_a_doubled_backslash_inside_a_string_is_not_this_shape(self) -> None:
        """An even run is an escaped backslash, and the newline after it stays a newline."""
        text = 'x = """one ' + BACKSLASH * 2 + '\ntwo"""\n'
        assert string_lines_that_lose_their_backslash(text) == []

    def test_a_backslash_outside_a_string_is_not_this_shape(self) -> None:
        """Code, not a string: an ordinary line continuation the interpreter is meant to honour."""
        text = "x = 1 + " + BACKSLASH + "\n    2\n"
        assert string_lines_that_lose_their_backslash(text) == []

    def test_every_line_of_a_multi_line_literal_is_read(self) -> None:
        """The offset is the literal's own, so a fault names the line it is really on."""
        text = "x = 1\ndoc = \"\"\"first\nsecond " + BACKSLASH + '\nthird"""\n'
        assert string_lines_that_lose_their_backslash(text) == [(3, 1)]

    def test_a_file_that_cannot_be_tokenized_is_reported_not_passed_over(self) -> None:
        """A reader that skips its input is indistinguishable from one that found nothing in it."""
        assert string_lines_that_lose_their_backslash('x = """unterminated\n') is None

    def test_the_reader_finds_the_document_shape_that_landed(self) -> None:
        """The control the corpus sweep cannot provide once the tree is fixed.

        Same document shape as the fault #1832 landed, one carrier over. Without this arm the sweep
        over a clean tree is green whether the reader works or not.
        """
        text = (
            '"""Exit codes.\n\nUsage:\n\n'
            "    APPLE_ID=<id> PASSWORD=<pw> " + BACKSLASH + "\n"
            "        TEAM=<team> " + BACKSLASH + "\n"
            '        uv run --no-sync python3 scripts/check-notary-credentials.py\n"""\n'
        )
        assert string_lines_that_lose_their_backslash(text) == [(5, 1), (6, 1)]


def test_the_rule_the_shell_readers_cannot_see_has_a_premise() -> None:
    """The new rule's own corpus floor, so its silence is a reading rather than an empty sweep.

    Measured 2026-10-03: 301 tracked `.py` files, and 5 offender lines in `scripts/run-mutation-arm.py`
    before the fix in the same commit. The floor is on **files read**, not on offenders: after the fix
    the corpus should have none, and an arm that required offenders would have to be deleted instead.
    """
    python = [name for name in tracked_files() if name.endswith(".py")]
    assert len(python) >= 200, (
        f"only {len(python)} tracked Python file(s) were listed - a sweep of almost nothing would "
        f"pass this rule for reasons that have nothing to do with the strings in them"
    )
    unreadable = [
        name for name in python
        if string_lines_that_lose_their_backslash(
            (REPO_ROOT / name).read_text(encoding="utf-8")
        ) is None
    ]
    assert not unreadable, f"these files were never read: {unreadable}"
