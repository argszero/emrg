#!/usr/bin/env python3
"""Does the shipped product carry the prompt this tree has?

The release chain asks the version question three times and, until this tool, the
*content* question nowhere. `check-release-artifact-version.py` reads each product's own
version declaration, and its docstring records why that mattered: a product built from an
older tree passes every source-side check and re-triggers the upgrade session forever.
This tool asks the same question about the other thing a product carries -- the text its
agent runs on.

That gap was named by a release request verbatim: *assert against the artifact, not the
source*. A release cycle can satisfy every source-side guard while the installer it hands
a host still ships the previous prompt, and the reading that would catch it has been
hand-derived, release after release, as an ad-hoc `tarfile` snippet. A reading re-derived
from scratch every release is a coin flip, not an acceptance test -- the argument that
produced `check-release-artifact-version.py` in the first place.

Why the payload and not `app.asar`. The initial ask named the asar, and the asar holds
none of it: `emrg/gui/package.json`'s `build.files` packs `main.js`, `preload.js`,
`renderer/**`, `vendor/**` and the sibling modules -- the GUI only. The server ships
separately, copied by `packaging/build-runtime.sh` (`cp -R emrg ... source/`), so the
prompt travels inside the **runtime payload** at
`source/emrg/server/prompts/system.j2`. Measured on the published
`EMRG-0.3.21-linux-x86_64.tar.gz`: one member,
`runtime/source/emrg/server/prompts/system.j2`, 24,558 characters. That product predates
the rewrite, and it is this tool's "before" reading: the same member of a later artifact must
carry `## Hard limits` where the old one has `## Tool Usage`. Its size is not restated here —
the prompt is edited, so any figure written beside it is stale the next time a rule moves; the
tool compares against this tree's copy at run time, which is the only number that stays true.

Subjects it reads:

  * a `.tar.gz` / `.tgz` -- the Linux runtime archive and the macOS component payload,
    read with `tarfile` alone (no extraction tool of the host's involved);
  * a **directory** -- an expanded `.pkg` payload, an `AppImage`'s `squashfs-root`, or a
    locally built `dist/runtime`, walked for the same member.

Anything else -- a `.pkg`, an `.exe`, a `.zip`, a `.run` -- answers **rc 2 with the
command that extracts it**, never a pass, exactly as its sibling does.

Exit codes, the family's contract:

    0  every member read is byte-identical to this tree's copy
    1  a fault -- a member differs, or a subject carries the file more than once and they
       disagree. Both character counts are printed.
    2  not measurable: nothing could be read (no such path, a container this tool does not
       open, an archive that will not parse, a member that is not UTF-8 text). Never 0: a
       guard that passes because it could not read its subject is the defect this exists
       to catch.

A fault outranks an unmeasured subject, because a fault is a fact about a subject that
*was* read. An unexpected failure inside the tool answers 2 through `_entry()`: 1 is this
tool's fault code, so a traceback left to Python's default would report "this product is
the stale one" about a file that was never opened.

Usage:
    uv run --no-sync python3 scripts/check-release-prompt.py EMRG-0.3.22-linux-x86_64.tar.gz
    uv run --no-sync python3 scripts/check-release-prompt.py dist/runtime <expanded-payload>
    uv run --no-sync python3 scripts/check-release-prompt.py --file emrg/server/prompts/system.j2 <subject>
"""
from __future__ import annotations

import argparse
import sys
import tarfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The file whose presence in a product decides the question this tool asks, relative to
#: the repository root. Overridable, because the next shipped text will want the same
#: question asked of it and a second copy of this tool is a second thing to keep in step.
DEFAULT_FILE = "emrg/server/prompts/system.j2"

#: Containers this tool does not open, keyed by suffix, value = the command that reaches a
#: subject it can. Lowercased on lookup for the reason its sibling gives: a real product is
#: named `EMRG-0.3.22-x86_64.AppImage`, and a `.AppImage` key would miss it.
_EXTRACT_HELP = {
    ".pkg": (
        "pkgutil --expand <pkg> <dir>, then (cd <dir> && gunzip -dc */Payload | cpio -i -d) "
        "-- point this tool at <dir>"
    ),
    ".zip": "unzip -q <zip> -d <dir> -- point this tool at <dir>",
    ".exe": (
        "the Windows installer is an Inno Setup container; extract it with innoextract, or "
        "point this tool at the runtime payload the same tree built"
    ),
    ".appimage": (
        "an AppImage is a squashfs: run it with --appimage-extract, then point this tool at "
        "the squashfs-root directory"
    ),
    ".run": "a self-extracting header over a tarball: read the payload tree it installs",
    ".msi": "msiexec /a <msi> /qb TARGETDIR=<dir>, then point this tool at <dir>",
}

#: The walk's ceiling, matching its sibling's: a product tree is a few thousand entries, so
#: a subject this big has stopped being a product. Failing loudly beats a walk that quietly
#: finds nothing and reports "this product does not carry it", which is a verdict about the
#: artifact rather than a statement about the tool's budget.
MAX_WALK_ENTRIES = 200_000


class Unmeasurable(Exception):
    """This subject could not be read at all -- rc 2's cause, never a pass."""


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table, while `2` is the code for "the question could not be answered". A caller
    that checks the code - which is how this family composes, one gate running another or
    reading its `rc` - would otherwise read a crash as a verdict. Byte-identical in every
    tool of the family, and `tests/test_a_crash_is_a_measurement_error.py` pins that.
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - reported as unmeasurable, never swallowed
        traceback.print_exc()
        print(
            f"{Path(__file__).name}: could not measure - {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2  # cause: tool-failed


def tree_text(relpath: str) -> str:
    """This tree's copy of the file, which every member is compared against."""
    path = REPO / relpath
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise Unmeasurable(
            f"{relpath} does not exist in this tree, so there is nothing to compare a "
            f"product against -- the subject is this checkout, not the artifact"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise Unmeasurable(f"{path} could not be read as UTF-8 text ({exc})") from exc


def _tail(relpath: str) -> str:
    """The path suffix a payload member must end with.

    Matched as a suffix rather than by an exact path because the prefix is the platform's
    business: the Linux archive roots at `runtime/`, a macOS payload at its own product
    directory, a local build at `dist/runtime/`. All of them end at the same place.
    """
    return relpath.replace("\\", "/").strip("/")


def members_of_tarball(path: Path, relpath: str) -> list[tuple[str, str]]:
    """Every member of a runtime archive whose path ends with `relpath`."""
    found: list[tuple[str, str]] = []
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(_tail(relpath)):
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                try:
                    found.append((member.name, fh.read().decode("utf-8")))
                except UnicodeDecodeError as exc:
                    raise Unmeasurable(
                        f"{path}: member {member.name} is not UTF-8 text ({exc}) -- the "
                        f"subject was located and could not be read"
                    ) from exc
    except (tarfile.TarError, OSError) as exc:
        raise Unmeasurable(f"{path} is not a readable tar archive ({exc})") from exc
    return found


def members_of_directory(root: Path, relpath: str) -> list[tuple[str, str]]:
    """Every file under a payload directory whose path ends with `relpath`."""
    found: list[tuple[str, str]] = []
    seen = 0
    for candidate in root.rglob("*"):
        seen += 1
        if seen > MAX_WALK_ENTRIES:
            raise Unmeasurable(
                f"{root} holds more than {MAX_WALK_ENTRIES} entries -- this stops being a "
                f"product tree and becomes a filesystem, so the walk is refused rather "
                f"than answered from its first {MAX_WALK_ENTRIES} entries"
            )
        if not candidate.is_file():
            continue
        if not candidate.as_posix().endswith(_tail(relpath)):
            continue
        try:
            found.append((str(candidate), candidate.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            raise Unmeasurable(f"{candidate} could not be read as UTF-8 text ({exc})") from exc
    return found


def read_subject(subject: Path, relpath: str) -> list[tuple[str, str]]:
    """Locate and read the shipped file in one subject, or say why it could not be."""
    if not subject.exists():
        raise Unmeasurable(f"{subject}: no such path")
    if subject.is_dir():
        return members_of_directory(subject, relpath)
    suffixes = [s.lower() for s in subject.suffixes]
    joined = "".join(suffixes[-2:]) if len(suffixes) >= 2 else ""
    if joined in (".tar.gz", ".tar.bz2", ".tar.xz") or suffixes and suffixes[-1] in (
        ".tgz",
        ".tar",
    ):
        return members_of_tarball(subject, relpath)
    for suffix in reversed(suffixes):
        if suffix in _EXTRACT_HELP:
            raise Unmeasurable(f"{subject} is a {suffix} this tool does not open: {_EXTRACT_HELP[suffix]}")
    raise Unmeasurable(
        f"{subject} is neither a directory this tool walks nor an archive it reads "
        f"(a .tar.gz is the Linux runtime archive; expand a .pkg or an AppImage first)"
    )


def check(relpath: str, subjects: list[Path]) -> int:
    """One row per subject: what was located, and whether it is this tree's text."""
    expected = tree_text(relpath)
    print(f"tree: {REPO}")
    print(f"file: {relpath} ({len(expected)} chars)")
    faulted = False
    unmeasured: list[str] = []
    for subject in subjects:
        try:
            members = read_subject(subject, relpath)
        except Unmeasurable as exc:
            print(f"UNMEASURABLE {exc}")
            unmeasured.append(str(subject))
            continue
        if not members:
            print(
                f"UNMEASURABLE {subject}: carries no member ending in {_tail(relpath)} -- a "
                f"product that does not carry the file cannot be compared with it, and "
                f"'absent' is not 'identical'"
            )
            unmeasured.append(str(subject))
            continue
        for name, text in members:
            if text == expected:
                print(f"{subject}: {name}: {len(text)} chars -> identical to this tree")
                continue
            faulted = True
            print(
                f"{subject}: {name}: {len(text)} chars -> DIFFERS from this tree's "
                f"{len(expected)} chars"
            )
            for lineno, (a, b) in enumerate(zip(text.splitlines(), expected.splitlines()), 1):
                if a != b:
                    print(f"  first difference at line {lineno}:")
                    print(f"    artifact: {a[:120]!r}")
                    print(f"    this tree: {b[:120]!r}")
                    break
            else:
                print(
                    f"  the first {min(len(text.splitlines()), len(expected.splitlines()))} "
                    f"line(s) agree; the difference is in the remaining content"
                )
    if faulted:
        print(
            f"FAULT: a product does not carry this tree's {relpath} -- the installer would "
            f"hand a host the previous text while the release names the new one. Rebuild "
            f"the product from the tree that has it."
        )
        return 1
    if unmeasured:
        print(
            f"UNMEASURED: {len(unmeasured)} subject(s) could not be read - 'could not "
            f"measure' is not a pass; expand the container the message named and point "
            f"this tool at the result"
        )
        return 2
    print(f"OK: every product carries this tree's {relpath}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # This report prints a `tree:` line and `_entry` writes the failure to stderr, so a
    # piped reader could see the failure before the identity line it is explained against.
    # The family's remedy, and `tests/test_guard_report.py` sweeps for it.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        description="Does the shipped product carry the prompt this tree has?",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file",
        default=DEFAULT_FILE,
        help=f"the file to locate inside the product, relative to the repository root (default: {DEFAULT_FILE})",
    )
    parser.add_argument("subjects", nargs="+", help="an artifact, or an expanded payload directory")
    args = parser.parse_args(argv)
    return check(args.file, [Path(s) for s in args.subjects])


if __name__ == "__main__":
    sys.exit(_entry())
