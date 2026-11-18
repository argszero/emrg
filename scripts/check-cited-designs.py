#!/usr/bin/env python3
"""Does every design document the shipped prompts cite exist on this machine?

Measured on this host 2026-11-18 (issue #2984): `emrg/server/evolution_prompt.md` cites
five paths under the host's home, and four of them are there. The fifth --
`~/.emrg/designs/memory-index-compaction-design.md` -- is the document the index-hygiene
rule calls the authoritative statement of what compaction is, what counts as done and what
must never be done. `~/.emrg/designs/` is not a directory on this machine, so the rule a
cycle is told to follow cannot be read, and nothing said so.

Why this cannot be a test of the design's existence
---------------------------------------------------
The design is the host's document. It is not in the checkout, so no CI step can read it and
no test may depend on it being present -- which is why the citation's *spelling* is what
`tests/test_evolution_prompt_index_rule.py` pins (a bare `.emrg/designs/<name>` would
resolve inside the repository and be unopenable to the reader), and why that pin passes
whether or not the file exists. This is the reading that has to be asked at home.

Exit codes, because "could not measure" is never a pass:
  0  every design the templates cite is readable on this machine
  1  this machine has an EMRG home and a cited design is absent -- the rule that cites it
     cannot be read here
  2  not measurable: no template directory, no citation found, or **no EMRG home on this
     machine**, which is what a CI runner answers because it has never run EMRG. Never 0,
     because a guard that passes by not looking is the defect this family exists to catch.

The scope is designs, deliberately. The same templates cite host paths whose absence is
normal on a working machine -- a pause file, a notarization env file, a task's own output
-- so a sweep over every `~/` path would report expected absences as faults, and a guard
that cries on a healthy machine is read as noise and then ignored. `~/.emrg/designs/*.md`
is a class whose members exist to be read.

Usage: check-cited-designs.py [--root <checkout>] [--home <the home to ask>]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the shipped prompts live, relative to the tree this script reads.
TEMPLATES = Path("emrg") / "server"

#: A design under the host's home, as the prompt writes it. Backticked in the prose and
#: bare inside a sentence, so the pattern stops at a quote, a backtick or whitespace
#: rather than trying to parse Markdown.
CITATION = re.compile(r"~/\.emrg/designs/[A-Za-z0-9._-]+\.md")

#: What a citation resolves to under the home this run was given. The `~` is never handed
#: to `expanduser` here: this script must be able to ask about a home it was pointed at,
#: which is what lets a test pass one it created.
HOME_PREFIX = "~/"


def citations(templates: Path) -> dict[str, list[str]]:
    """Every cited design, mapped to the `file:line` places that cite it."""
    places: dict[str, list[str]] = {}
    for path in sorted(templates.glob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for citation in CITATION.findall(line):
                places.setdefault(citation, []).append(f"{path.name}:{number}")
    return places


def check(root: Path, home: Path) -> int:
    """Answer for one tree and one home; the caller prints nothing before this line."""
    print(f"tree: {root}")
    templates = root / TEMPLATES
    if not templates.is_dir():
        print(f"could not measure: {templates} is not a directory, so no template was read")
        return 2

    found = citations(templates)
    if not found:
        print(
            f"could not measure: no file under {templates} cites a design under "
            f"`{HOME_PREFIX}.emrg/designs/`, so nothing was asked"
        )
        return 2

    emrg_home = home / ".emrg"
    if not emrg_home.is_dir():
        print(
            f"could not measure: {emrg_home} is not a directory, so this machine has no EMRG "
            f"home and a host-side document being absent here says nothing either way "
            f"({len(found)} citation(s) went unasked)"
        )
        return 2

    absent: list[str] = []
    present: list[str] = []
    for citation, where in sorted(found.items()):
        target = home / citation[len(HOME_PREFIX) :]
        if target.is_file():
            present.append(citation)
        else:
            absent.append(citation)
            print(f"ABSENT: {citation}, cited by {', '.join(where)}")

    print(
        f"designs cited: {len(found)} distinct citation(s) on "
        f"{sum(len(where) for where in found.values())} line(s) across {len(sorted(templates.glob('*.md')))} "
        f"template(s)"
    )
    if absent:
        print(
            f"A CITED DESIGN IS ABSENT: {len(absent)} of {len(found)} design(s) the shipped "
            f"prompts send a cycle to cannot be read on this machine, which has an EMRG home "
            f"at {emrg_home}. The rule that cites one cannot be followed from its stated "
            f"source: restore the document, or change the text that cites it so the "
            f"authority it names is where the reader can reach it."
        )
        return 1

    print(
        f"OK: every cited design is readable - {len(present)} of {len(found)}, "
        f"under {emrg_home}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every design document the shipped prompts cite exists on this machine.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the tree holding the prompt templates (default: this checkout)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="the home to ask about (default: this process's home)",
    )
    args = parser.parse_args(argv)
    return check(args.root.resolve(), args.home.resolve())


if __name__ == "__main__":
    sys.exit(main())
