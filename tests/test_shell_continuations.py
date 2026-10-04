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
So the rule reports **two or more** trailing backslashes rather than "exactly two", because
three backslashes is the same defect one keystroke short of `\\\\`.

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

import subprocess
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


def doubled_continuations(text: str) -> list[tuple[int, int]]:
    """`(line number, count)` for each line ending in two or more backslashes.

    Trailing whitespace is stripped first, so a line ending in a backslash *and* a stray space
    is still read as a continuation by the shell and by this reader alike.
    """
    found: list[tuple[int, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.rstrip()
        count = len(stripped) - len(stripped.rstrip(BACKSLASH))
        if count >= 2:
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
                f"{name}:{number} ends in {count} backslashes - an even number is an escaped "
                f"backslash, not a continuation, so whatever follows is a separate command"
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
        for line in text.splitlines():
            stripped = line.rstrip()
            if stripped.endswith(BACKSLASH) and not stripped.endswith(BACKSLASH * 2):
                continued += 1
    assert continued >= 20, (
        f"only {continued} single-backslash continuation(s) exist in the whole corpus, so this "
        f"rule guards a shape the repository has stopped using - re-measure before trusting "
        f"its silence"
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


def test_no_tracked_file_ends_a_line_with_a_doubled_backslash() -> None:
    """The rule: a line that means to continue onto the next one continues.

    The failure names its file and line, because the remedy differs by carrier - the same two
    bytes break the command inside a workflow `run:` block or an `.sh` file, and break the
    command a script *documents* when they sit in its usage block, which is how this defect
    reached `DEVELOPMENT.md`.
    """
    offenders = sweep()
    assert not offenders, (
        "these lines end in two or more backslashes, which a shell reads as an escaped "
        "backslash followed by an end of line - the command splits, and the arguments after it "
        "never arrive:\n  " + "\n  ".join(offenders)
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
