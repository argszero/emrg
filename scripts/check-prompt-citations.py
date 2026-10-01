#!/usr/bin/env python3
"""A citation of a document's section must name a heading that file actually has.

The class this exists for
------------------------
`emrg/server/evolution_prompt.md` was restructured (#1790): the seven-section
waterfall became a four-step loop with a rulebook, so **section 6 is no longer a section
of that file** - the index rule the old one carried is now `R9`. Five citations kept
pointing at the old number, and nothing noticed, because a section reference is prose
in a comment and no reader resolves it:

    scripts/check-memory-index.py:17   section 6 of emrg/server/evolution_prompt.md, "in this checkout"
    emrg/server/scheduler.py:2663      written by the agent per evolution_prompt, section 6
    tests/test_check_memory_index.py   the block the rule names, section 6, in its opening lines

The sites are quoted here with the number spelled out rather than in citation form, and
deliberately: a counter-example written in the shape this tool looks for is
indistinguishable from the defect, which is `check-citation-resolves.py`'s one measured
false positive - and this file's own first draft reproduced it (the run reported this
docstring, twice, before the sentences were rewritten).

What is read, and what is deliberately not
------------------------------------------
A **citation** here is one line that names a file *and* a section of it - the form
`<path> §<token>`, which is how this tree writes a reference whose target is not
obvious from context. The path must resolve to a file **in this tree**, and the token
must name one of that file's Markdown ATX headings. Anything else is out of scope, and
each exclusion is a shape that would otherwise be a false positive:

* **A bare `§4`**, with the document named a paragraph earlier or nowhere on the line
  (`design §5 item 5`, `rant #12 §11`, a `.ts` header comment). Measured 2026-10-01 over
  this tree: `§4`, `§6`, `§3.2.1`, `§14.5` and `§Forbidden` are all in use, and the
  document each belongs to is often not on the line - `build-release.yml` cites
  `rant #12 §11`, `config.py` cites an out-of-tree design document. A rule that guessed
  the document would invent the mismatch it reports.
* **A cited file this tree does not carry.** `~/.emrg/designs/*-design.md` is cited this
  way by several modules and is deliberately not in the checkout; a citation that cannot
  be resolved is not a citation that is wrong. Reported by neither this tool nor its
  exit code - the limit `check-memory-index.py` states for a target it does not guess.
* **A cited file that is not a document** (`.py`, `.ts`, `.json`): a section of a source
  file is not a heading, and the heading rule would report every one of them. Only
  `.md` and `.j2` are read as documents, which is where this tree's headings live.
* **Where the section is right and the number moved on purpose** - a citation that names
  a heading is a pass, and this tool has no opinion about whether the *text* under that
  heading still says what the citing sentence claims. Following that is a reader's job.

What this covers, measured rather than asserted
-----------------------------------------------
Run over this tree on 2026-10-01, before the five `§6` sites were repointed: **2**
same-line citations named a section the file does not have (both `evolution_prompt.md
§6`) and **5** named one it does (`§Forbidden` three times, `§2.2` twice), plus
`Agent.md §Releasing` in `build-release.yml`. Both directions therefore occur in the
tree itself - the reading is not vacuous, and its first run is the fix's own proof.

Which tree answered
-------------------
The first line is `tree: <resolved root>` before any verdict, the convention every
guard in this family carries. Its subject is the tree it is run against (defaulting to
the checkout this file lives in), so the same report is not silently true of another.

Exit codes
----------
``0``  every citation that names a file of this tree also names one of its headings.
``1``  at least one such citation names a section the file does not have; each is
       printed with its file, its line, the token and the headings that would fit.
``2``  nothing could be measured: the root is not a directory, or no text file could be
       read at all - which would make a green verdict a reading over an empty set.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

#: Directories never descended into: not source, and large enough to matter. `.emrg` is
#: in the list because it is this host's runtime state (gitignored), not the tree's
#: source - a cycle's own memory files cite section numbers of their own.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".emrg",
    }
)

#: What a citation can be written in.
TEXT_SUFFIXES = (".py", ".md", ".j2", ".yml", ".yaml", ".sh", ".txt", ".ts", ".tsx")

#: Suffixes whose sections are **headings**. A source file has no headings, so a
#: `*.py §4` would be reported for being the shape it is, not for being wrong.
DOCUMENT_SUFFIXES = (".md", ".j2")

#: `<path-like> §<token>`, allowing the backticks, quotes or one space a writer puts
#: between the name and the mark. The path must carry a suffix this tree uses, so that
#: prose like `issue #1793 §2` is not read as a file called `1793`.
CITATION = re.compile(
    r"(?P<file>[\w./@-]+\.(?:md|j2|py|ya?ml|sh|txt|ts|tsx|json))"
    r"[\s`'\"]{0,3}§(?P<token>[A-Za-z0-9.]+)"
)

#: A Markdown ATX heading, the shape a section number has to land on.
HEADING = re.compile(r"^#{1,6}\s+(?P<text>.*\S)\s*$")


class Finding(NamedTuple):
    """One citation whose section is not a heading of the file it names.

    A `NamedTuple` rather than a `@dataclass`: this module is loaded by path in its
    tests (`spec_from_file_location`, the shape `test_rant_citations.py` uses), and a
    dataclass built outside `sys.modules` raises in `dataclasses` itself when a string
    annotation is resolved.

    :param site: the file the citation is written in, relative to the root.
    :param line: its 1-based line number there.
    :param cited: the file the citation names, as written.
    :param token: the section it names.
    :param headings: every heading in the cited file, for the remedy line.
    """

    site: str
    line: int
    cited: str
    token: str
    headings: tuple[str, ...]


def text_files(root: Path) -> list[Path]:
    """Every readable text file under `root`, minus the skipped directories.

    :param root: the tree to walk.
    :returns: the files, in a stable order.
    """
    out: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix in TEXT_SUFFIXES:
                out.append(entry)
    return sorted(out)


def headings_of(path: Path) -> tuple[str, ...]:
    """`path`'s Markdown ATX headings, in file order.

    :param path: the document to read.
    :returns: the heading texts, with their leading `#`s and spacing removed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    return tuple(match.group("text") for match in (HEADING.match(line) for line in text.splitlines()) if match)


def names_a_heading(token: str, headings: tuple[str, ...]) -> bool:
    """Whether `token` names one of `headings`.

    The match is a **prefix that stops before an alphanumeric**, which is what a
    section number needs: `§2` names the heading `2. Promotion Channels` (the `.` after
    the `2` ends the number, not the token), `§2.2` names `2.2 Every open issue …` and
    not `2. Either`, and `§Forbidden`/`§R9` name their headings exactly. The token is
    read whole - its own dots are part of it - so `2.2` is never read as `2`.

    Measured 2026-10-01, when this rule was written the other way round (a `.` treated
    as part of the token): `tests/test_scheduler.py`'s `promote_prompt.md §2` was
    reported against a file whose heading *is* `2. Promotion Channels` - a guard firing
    on a correct citation is the defect this family exists to avoid, so the boundary is
    "alphanumeric", and the `.` belongs to the heading's punctuation.

    :param token: the section as written, without the `§`.
    :param headings: the cited file's headings.
    :returns: whether one of them is this section.
    """
    for heading in headings:
        if heading == token:
            return True
        if heading.startswith(token) and not heading[len(token)].isalnum():
            return True
    return False


#: How far down a bare name is looked up when the path as written is not there: a
#: citation may write the short form (the file's own name, section 6, on one line) where
#: the full path is `emrg/server/evolution_prompt.md`. A basename matching several files
#: is left unresolved rather than guessed.
def resolve(name: str, root: Path) -> Optional[Path]:
    """The file `name` refers to, or None when this tree does not carry it.

    :param name: the path as the citation wrote it.
    :param root: the tree to resolve it in.
    :returns: the file, when exactly one candidate answers.
    """
    direct = root / name
    if direct.is_file():
        return direct
    matches = [path for path in text_files(root) if path.name == Path(name).name]
    return matches[0] if len(matches) == 1 else None


def scan(root: Path) -> list[Finding]:
    """Every citation in `root` whose section is not a heading of the file it names.

    :param root: the tree to read.
    :returns: the findings, in file then line order.
    """
    findings: list[Finding] = []
    for path in text_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in CITATION.finditer(line):
                cited = match.group("file")
                if not cited.endswith(DOCUMENT_SUFFIXES):
                    continue
                target = resolve(cited, root)
                if target is None:
                    continue
                token = match.group("token").rstrip(".")
                if not token:
                    continue
                headings = headings_of(target)
                if names_a_heading(token, headings):
                    continue
                findings.append(
                    Finding(
                        str(path.relative_to(root)),
                        number,
                        cited,
                        token,
                        headings,
                    )
                )
    return findings


def main(argv: Optional[list[str]] = None) -> int:
    """Read every citation and report against the headings of the file it names.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: the exit code the docstring states.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Every citation that names a file of this tree and a section of it must "
            "name a heading that file actually has. A bare section number (the document "
            "named elsewhere, or not at all), a cited file this tree does not carry, and "
            "a section of a source file are out of scope, each for a reason the module "
            "docstring states."
        ),
        epilog=(
            "Exit 0: every citation that names a file of this tree names one of its "
            "headings. Exit 1: at least one names a section that is not there. "
            "Exit 2: nothing could be measured. Example: uv run --no-sync python3 "
            "scripts/check-prompt-citations.py"
        ),
    )
    parser.add_argument("root", nargs="?", default=None, metavar="ROOT",
                        help="the tree to read (default: this file's own checkout)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    print(f"tree: {root}")
    if not root.is_dir():
        print(f"could not measure: {root} is not a directory")
        return 2
    if not text_files(root):
        print(f"could not measure: no text file under {root}, so a green verdict "
              f"would be a reading over an empty set")
        return 2

    findings = scan(root)
    if not findings:
        print("OK: every citation that names a file of this tree names a heading it has")
        return 0
    for finding in findings:
        print(
            f"{finding.site}:{finding.line} cites {finding.cited}, section "
            f"{finding.token}, which is not a heading of it"
        )
        if finding.headings:
            print("  headings: " + " | ".join(finding.headings))
        else:
            print("  headings: (the file has none)")
    print(
        f"{len(findings)} citation(s) name a section the cited file does not have; "
        "the remedy is to point the sentence at the heading it means (the number moved "
        "when the document was restructured)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
