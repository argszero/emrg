#!/usr/bin/env python3
"""Check that a rebuilt patch did not silently drop a file the previous one had.

Why this exists
---------------
A patch that drops a test file still satisfies every check a maintainer would
naturally run: it *applies* cleanly, and the suite is *green* - because the
missing file is simply not there to fail. This repo hit exactly that: a fix was
published whose only test file had been dropped while the patch was rebuilt from
a working tree that contained just the two files being edited that cycle. The
code survived (it lives in the file that descended from the previous version),
nothing failed, and a reader comparing the two patches' file lists could see the
loss immediately.

So the free check is a set comparison: list the files each patch touches, and
report any file present in a previous patch and absent from a later one. A
disappearance is either deliberate (and then it should be stated in the publish
text) or it is the defect.

Usage
-----
    python3 scripts/check-patch-files.py old.patch new.patch
    python3 scripts/check-patch-files.py a.patch b.patch c.patch    # each vs the prior
    python3 scripts/check-patch-files.py --list a.patch b.patch

Exit codes
----------
0  every file a previous patch carried is still carried by the next one
1  a file disappeared from the patch (named, with its hunk count)
2  the question could not be answered - no patches given, a patch is unreadable,
   a single patch without --list (there is nothing to compare it against), a
   patch that parsed to zero files, or a section whose file cannot be named. The
   last two matter for the same reason: a file the parser cannot see is a file
   that is missing from *both* patches, so it would make "nothing disappeared"
   a statement about the parser. An unnameable section is therefore never
   skipped - it makes the whole comparison unmeasurable.

The tool reads the patch files it is given, not the working tree; its first
output line names every patch it opened, so a reader can tell what was measured.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DIFF_HEADER = "diff --git "
HUNK_MARKER = "@@ "
PLUS_NAME = "+++ "
MINUS_NAME = "--- "
RENAME_TO = "rename to "
NEW_FILE_MODE = "new file mode "
DELETED_FILE_MODE = "deleted file mode "
NULL_PATH = "/dev/null"

# The escapes git's C-style quoting uses, besides octal byte escapes. Measured
# on a real repository: a path is quoted exactly when it holds one of these, a
# non-ASCII byte, or a control character - a space alone does NOT quote it.
_C_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    '"': '"', "\\": "\\",
}

DESCRIPTION = (
    "Compare the file lists of two or more patches and fail if a file carried by "
    "an earlier patch is missing from a later one."
)


@dataclass(frozen=True)
class Entry:
    """One file a patch touches."""

    path: str
    hunks: int
    is_new: bool
    is_deleted: bool

    def label(self) -> str:
        kind = "new file" if self.is_new else ("deletes" if self.is_deleted else "edits")
        return f"{kind} {self.path}" + (f" ({self.hunks} hunks)" if self.hunks else "")


class UnparseableSection(Exception):
    """A `diff --git` section whose file cannot be named.

    Raised rather than skipped: an unnamed file is absent from the parsed set of
    every patch, so it would agree with itself and turn "nothing disappeared"
    into a verdict about this parser (see the exit codes).
    """


def _closing_quote(text: str) -> int | None:
    """Index of the quote closing the C-quoted name `text` starts with."""
    i = 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return None


def _unquote(token: str) -> str:
    """git's C-style quoting undone: ``"b/w\\303\\255th.txt"`` -> ``b/wíth.txt``.

    git spells a non-ASCII byte as octal, so the escapes decode to bytes and the
    bytes to UTF-8. Anything that does not round-trip comes back untouched
    rather than guessed at.
    """
    if not token.startswith('"'):
        return token
    end = _closing_quote(token)
    if end is None:
        return token
    body = token[1:end]
    out = bytearray()
    i = 0
    try:
        while i < len(body):
            char = body[i]
            if char != "\\":
                out += char.encode("utf-8")
                i += 1
                continue
            i += 1
            if i >= len(body):
                return token
            escape = body[i]
            if escape in "01234567":
                j = i
                while j < len(body) and j - i < 3 and body[j] in "01234567":
                    j += 1
                out.append(int(body[i:j], 8))
                i = j
            elif escape in _C_ESCAPES:
                out += _C_ESCAPES[escape].encode("utf-8")
                i += 1
            else:
                return token
        return out.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return token


def _section_token(rest: str) -> str | None:
    """The one path on a `---`/`+++`/`rename to` line, or None if malformed.

    git terminates an *unquoted* name containing a space with a tab - measured:
    ``+++ b/a b.txt<TAB>`` - and quotes a name it cannot print plainly, so the
    first tab after an unquoted name is always git's terminator and never part of
    the name.
    """
    rest = rest.rstrip("\r\n")
    if rest.startswith('"'):
        end = _closing_quote(rest)
        return None if end is None else _unquote(rest[: end + 1])
    return rest.split("\t", 1)[0] or None


def _strip_side(path: str, side: str) -> str:
    """`b/<path>` -> `<path>`; git writes its side prefix on every one of these lines."""
    return path[len(side):] if path.startswith(side) else path


def _head(section: list[str]) -> list[str]:
    """A section's lines up to its first hunk - where git writes the names."""
    for i, line in enumerate(section):
        if line.startswith(HUNK_MARKER):
            return section[:i]
    return section


def _path_from_diff_header(line: str) -> str | None:
    """The post-image path in a `diff --git` header, or None when it is ambiguous.

    git writes `a/<path> b/<path>`, quoting a whole token only when that token
    needs C-escaping. It does *not* quote a path merely for containing a space
    (measured: ``diff --git a/a b.txt b/a b.txt``), so an unquoted header cannot
    be split on whitespace. The split is instead chosen by plausibility: the two
    sides agreeing is the reading of every section that is not a rename, which
    resolves the ambiguous-looking case uniquely. A header with more than one
    plausible reading and no agreeing one is reported as unknown rather than
    guessed - a wrong name is invisible in both patches, which is the failure
    this tool exists to prevent.
    """
    rest = line[len(DIFF_HEADER):].rstrip("\r\n")
    if rest.startswith('"'):
        end = _closing_quote(rest)
        if end is None:
            return None
        remainder = rest[end + 1:].strip()
        token = _section_token(remainder) if remainder else None
        return None if token is None else _strip_side(token, "b/")

    candidates: list[tuple[str, str]] = []
    for match in re.finditer(" b/", rest):
        left, right = rest[: match.start()], rest[match.start() + 1:]
        if left.startswith("a/") and right.startswith("b/"):
            candidates.append((left[2:], right[2:]))
    for left, right in candidates:
        if left == right:
            return right
    if len(candidates) == 1:
        return candidates[0][1]
    return None


def _path_from_section(section: list[str]) -> str | None:
    """The one file a section touches, or None if it cannot be named.

    Each source is a line git writes the name on alone, in the order that makes
    the post-image path the answer for every shape measured: `+++ b/<path>` (an
    edit or an addition), `--- a/<path>` (a deletion), `rename to <path>` (a
    rename). The header is the fallback for the two shapes that carry no name
    line of their own - a mode-only change and a binary section.
    """
    head = _head(section)
    for line in head:
        if line.startswith(PLUS_NAME):
            token = _section_token(line[len(PLUS_NAME):])
            if token and token != NULL_PATH:
                return _strip_side(token, "b/")
    for line in head:
        if line.startswith(MINUS_NAME):
            token = _section_token(line[len(MINUS_NAME):])
            if token and token != NULL_PATH:
                return _strip_side(token, "a/")
    for line in head:
        if line.startswith(RENAME_TO):
            token = _section_token(line[len(RENAME_TO):])
            if token:
                return _strip_side(token, "b/")
    return _path_from_diff_header(section[0])


def _split_sections(text: str) -> list[list[str]]:
    """The patch cut at every `diff --git` header; text before the first is ignored."""
    sections: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith(DIFF_HEADER):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return sections


def parse_patch(text: str) -> dict[str, Entry]:
    """{path: Entry} for every file the patch touches, in the order git wrote them.

    Raises UnparseableSection when a section's file cannot be named - see the
    exit codes: an unreadable section must make the comparison unmeasurable
    instead of quietly shrinking both file sets.
    """
    entries: dict[str, Entry] = {}
    for section in _split_sections(text):
        path = _path_from_section(section)
        if path is None:
            raise UnparseableSection(section[0])
        head = _head(section)
        entries[path] = Entry(
            path,
            sum(1 for line in section if line.startswith(HUNK_MARKER)),
            any(line.startswith(NEW_FILE_MODE) for line in head),
            any(line.startswith(DELETED_FILE_MODE) for line in head),
        )
    return entries


def _describe(label: str, entries: dict[str, Entry]) -> None:
    print(f"  {label}: {len(entries)} file(s)")
    for entry in entries.values():
        print(f"      {entry.label()}")


def _load(path: str) -> tuple[dict[str, Entry], str] | None:
    target = Path(path)
    if not target.is_file():
        print(f"unmeasurable: no such patch file: {path}")
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # unreadable file (permissions, a directory, ...)
        print(f"unmeasurable: cannot read {path}: {exc}")
        return None
    try:
        entries = parse_patch(text)
    except UnparseableSection as exc:
        print(f"unmeasurable: cannot name the file of this section: {exc}")
        return None
    return entries, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("patches", nargs="*", help="patch files, oldest first")
    parser.add_argument(
        "--list",
        action="store_true",
        help="print each patch's file set and stop (no comparison)",
    )
    args = parser.parse_args(argv)

    if not args.patches:
        parser.print_help()
        return 2
    if len(args.patches) == 1 and not args.list:
        print(
            "unmeasurable: one patch given and nothing to compare it against; "
            f"pass a second patch or use --list ({args.patches[0]})"
        )
        return 2

    print("patches read:")
    for path in args.patches:
        print(f"    {path}")

    loaded: list[tuple[str, dict[str, Entry]]] = []
    for path in args.patches:
        result = _load(path)
        if result is None:
            return 2
        entries = result[0]
        if not entries:
            # The failure this guard must not have: a parser that matched
            # nothing agrees with every input, so "no file disappeared" would
            # be a verdict about the parser, not about the patches.
            print(
                f"unmeasurable: {path} parsed to 0 files - it is empty, not a diff, "
                "or in a format this tool does not read; reporting 'nothing wrong' "
                "here would be a verdict about the parser"
            )
            return 2
        loaded.append((path, entries))

    if args.list:
        for path, entries in loaded:
            print()
            _describe(Path(path).name, entries)
        return 0

    dropped_any = False
    for (prev_path, prev), (cur_path, cur) in zip(loaded, loaded[1:]):
        lost = [prev[f] for f in prev if f not in cur]
        gained = [cur[f] for f in cur if f not in prev]
        print()
        print(f"{Path(prev_path).name}  ->  {Path(cur_path).name}")
        _describe("previous", prev)
        _describe("current ", cur)
        if gained:
            print(f"  gained: {[e.path for e in gained]}")
        if lost:
            dropped_any = True
            print(f"  FAIL: {len(lost)} file(s) dropped")
            for entry in lost:
                print(f"      {entry.label()}")
            print(
                "      A dropped file is either deliberate (say so where the patch "
                "is published) or a rebuild defect: the patch still applies and the "
                "suite is still green, because the file is absent."
            )
        else:
            print("  OK: no file the previous patch carried is missing from this one")
    return 1 if dropped_any else 0


if __name__ == "__main__":
    sys.exit(main())
