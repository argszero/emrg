#!/usr/bin/env python3
"""Does the version inside a built artifact name the version the release claims?

The release chain asserts the version three times and never once inside a product.
`tests/test_version_sync.py` and `scripts/bump-version.py --check` compare the eight
*source* declarations with each other; `scripts/check-release-tag.py` compares the tag
with the tagged tree's declaration; `scripts/check-release-published.py` reads the
release's *metadata* -- draft, prerelease, and the asset **names**, never an asset's
bytes. So every one of those reads stops at the source, and an artifact carrying an old
version passes all of them.

That is not hypothetical. `build-release.yml:57-62` records the mechanism: the artifacts'
`version.txt` comes from the **built tree** (`packaging/build-runtime.sh:141`), not from
the tag, and `emrg/server/upgrade.py::UpgradeManager.tick` compares that installed
`version.txt` with `target.lstrip("v")`. A release whose name and whose `version.txt`
disagree re-triggers the upgrade session on every tick, forever, and no later release
repairs it. `check-release-tag.py` closes the *tag vs tree* half; this closes *tree vs
product*.

Why hand-running it is the defect rather than a chore. The v0.3.12 and v0.3.13 releases
each required this reading (release rants: "assert against the artifact itself, not the
source"; "confirm the `app.asar` is freshly rebuilt, not a stale product passed off as
new"), and each cycle re-derived it from scratch. Measured this cycle on the published
`EMRG-0.3.13-macos-arm64.pkg` (sha256 1ecafe2ab8114cbd7db0bf96abb9b86a22edb0e187119ba3b149c8466bf7029d):

    Contents/Info.plist  CFBundleShortVersionString / CFBundleVersion   0.3.13 / 0.3.13
    payload version.txt                                                 0.3.13
    Contents/Resources/app.asar  /package.json  (emrg-gui)              0.3.13
    old strings anywhere in the asar's 17086090 bytes  0.3.12/0.3.11/0.3.10   0/0/0

The last row is what "the GUI dist was really rebuilt" looks like in the artifact. Two
traps sat in the asar reader and this cycle fell into both before landing it: the header
is four little-endian uint32s (4, pickle size, inner size, JSON string length), the JSON
runs from byte 16 for the **string length** -- not the pickle size, which answers
`Extra data: line 1 column 462002` -- and the file offsets are relative to
`8 + pickle size` aligned up to a multiple of 4 (measured: base 462020 for a pickle size
of 462012; `16 + pickle size` is also 4-aligned but parses nothing). A reading with two
traps in it, re-derived every release, is a coin flip, not an acceptance test.

What this answers, and what it does not. Whether a product's own version declaration
names the claimed version. It does not ask whether the artifact is *signed*, notarized or
complete -- `check-release-published.py` and the workflow's own steps hold those. Nor does
it read every byte of an old version out of an archive: one declaration per subject is the
question, and a second reader of the same files would be a second thing to keep in step.

Subjects it reads:

  * a `version.txt` -- the field `UpgradeManager.tick` compares against the tag;
  * an `.asar` -- the app's own `package.json`, selected by the package **name**
    (`emrg/gui/package.json`'s `name`, read rather than hardcoded) because an asar holds
    eight of them and the others are dependencies' versions;
  * a `Contents/Info.plist`, or a directory (an `.app`, an expanded payload, a
    `dist/runtime`) walked for all of the above plus `version.txt`;
  * a `.tar.gz` -- the Linux runtime archive, read with `tarfile` alone.

Anything it does not open -- a `.pkg`, an `.exe`, an `.AppImage`, a `.zip` -- answers
**rc 2 with the command that extracts it**, never a pass. The `.pkg` route is
`pkgutil --expand` plus `gunzip -dc Payload | cpio -i -d`; pointing this tool at the
expanded directory runs the identical reading, so the host's command and CI's are one
measurement.

Exit codes, the family's contract:

    0  every subject read names the claimed version
    1  a fault -- a subject names a different version, or the claimed version appears
       nowhere in it. Both values are printed.
    2  not measurable: nothing could be read (no such path, a container this tool does
       not open, a header that will not parse). Never 0: a guard that passes because it
       could not read its subject is the defect this one exists to catch.

A fault outranks an unmeasured subject, because a fault is a fact about a subject that
*was* read. An unexpected failure inside the tool answers 2 through `_entry()`: 1 is this
tool's fault code, so a traceback left to Python's default would report "this artifact is
the stale one" about a file that was never opened.

Usage:
    uv run --no-sync python3 scripts/check-release-artifact-version.py <version> <subject> [<subject> ...]
    uv run --no-sync python3 scripts/check-release-artifact-version.py --from-tag v0.3.13 dist/runtime/version.txt
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import struct
import sys
import tarfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The version-shaped token a reader accepts. Three dotted numbers, optional pre-release
#: tail -- deliberately the same shape `scripts/bump-version.py` rewrites, so this reader
#: and the bump cannot disagree about what a version looks like while agreeing on the value.
_VERSION_RE = re.compile(r"^\s*v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)\s*$")

#: Archive members and bundle files that carry the product's own version declaration.
#: Matched by name so a subject can hold any of them and the reading stays one rule.
_VERSION_FILE_NAMES = ("version.txt",)
_BUNDLE_PLIST = ("Contents", "Info.plist")

#: A `.pkg` is a distribution wrapping component packages; a `.zip`/`.exe` is an installer
#: container. Keyed by suffix, and the value is the command that reaches the readable part.
#: The keys are lowercase because the lookup lowercases: a real Linux product is named
#: `EMRG-0.3.13-x86_64.AppImage`, and a `.AppImage` key would miss it and fall through to
#: "not a declaration this tool reads" -- a wrong cause for the right refusal.
_EXTRACT_HELP = {
    ".pkg": (
        "pkgutil --expand <pkg> <dir>, then read the component's Payload: "
        "(cd <dir> && gunzip -dc */Payload | cpio -i -d) -- point this tool at <dir>"
    ),
    ".zip": "unzip -q <zip> -d <dir> -- point this tool at <dir>",
    ".exe": (
        "Windows: the installer is built by Inno Setup; read the runtime's version.txt from "
        "dist/runtime (the tree this installer was built from) rather than the .exe"
    ),
    ".appimage": (
        "an AppImage is a mounted squashfs: run it with --appimage-extract, then point this "
        "tool at the squashfs-root directory"
    ),
    ".run": "a bash self-extracting header over a tarball; read the runtime tree it installs",
    ".msi": "msiexec /a <msi> /qb TARGETDIR=<dir>, then point this tool at <dir>",
}


class Unmeasurable(Exception):
    """This subject could not be read at all -- rc 2's cause, never a pass."""


class NoDeclaration(Exception):
    """The file was read successfully and simply holds no version claim.

    Distinct from `Unmeasurable`, and the difference is what decides a release: the
    published v0.3.13 payload contains `emrg-gui/卸载 EMRG.app/Contents/Info.plist`, which
    parses cleanly and declares neither `CFBundleShortVersionString` nor
    `CFBundleVersion` -- an uninstaller stub, a true fact about a file that *was* read.
    Answering rc 2 for it would refuse every real macOS artifact, while answering rc 0
    for a corrupt header would pass an unreadable one. So: read-and-says-nothing is a
    note; unreadable is rc 2.
    """


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table, while `2` is the code for "the question could not be answered". A caller
    that checks the code -- which is how this family composes -- would otherwise read a
    crash as a verdict. Byte-identical in every tool of the family, and
    `tests/test_a_crash_is_a_measurement_error.py` pins that.
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


def parse_version(text: str) -> str | None:
    """The bare version a declaration holds, or None when it does not look like one.

    A leading `v` is accepted and dropped: the tag spells `v0.3.13`, `version.txt` spells
    `0.3.13`, and `UpgradeManager.tick` reconciles them with `target.lstrip("v")`. This
    reader uses the same reconciliation rather than inventing its own.
    """
    m = _VERSION_RE.match(text)
    return m.group(1) if m else None


def app_package_name() -> str:
    """The GUI package's own name, read from this checkout -- never hardcoded here.

    Resolved **lazily**, by whoever is about to read an asar: a caller pointing this tool
    at a `version.txt` or a Linux tarball is asking a question that has nothing to do with
    the GUI manifest, and resolving it eagerly made that reading fail whenever the script
    ran outside a checkout (the release job downloads artifacts without one).

    `emrg/gui/package.json` is the declaration `scripts/bump-version.py` rewrites, so its
    `name` is what identifies the app's `package.json` inside an asar. An asar carries
    eight of them and seven belong to dependencies: selecting "the first one" would make a
    dependency's version the verdict, which is the mistake `bump-version.py` documents for
    `package-lock.json`'s 334 version fields.
    """
    path = REPO / "emrg" / "gui" / "package.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["name"]
    except Exception as exc:  # noqa: BLE001 - no name means no way to select the app
        raise Unmeasurable(f"cannot read the app's package name from {path} ({exc})")


def read_version_txt(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    ver = parse_version(text)
    if ver is None:
        raise Unmeasurable(f"{path} does not hold a version ({text[:40]!r})")
    return ver


def read_info_plist(path: Path) -> list[tuple[str, str]]:
    """The bundle's version declarations, both of them.

    `CFBundleShortVersionString` is the marketing version and `CFBundleVersion` the build
    version; a release that moved one and not the other is exactly the half-built dist this
    reading exists to catch, so both are reported and both are compared.
    """
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - a plist that will not parse says nothing
        raise Unmeasurable(f"{path} is not a readable plist ({exc})")
    out = []
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        raw = data.get(key)
        if isinstance(raw, str) and parse_version(raw):
            out.append((key, parse_version(raw)))
    if not out:
        raise NoDeclaration(f"{path.name} declares neither CFBundleShortVersionString nor CFBundleVersion")
    return out


def asar_header(archive: bytes) -> tuple[dict, int]:
    """The asar's file list and the offset its file contents are relative to.

    The layout, measured on the shipped v0.3.13 asar rather than read off a blog post: a
    uint32 `4`, then the pickle size, then an inner size, then the JSON string length; the
    JSON itself starts at byte 16 and runs for the **string length**. Contents begin at
    `8 + pickle size` rounded up to a multiple of 4 -- the pickle size is padded, so using
    the JSON length as the data base lands on a non-aligned offset and reads garbage.
    """
    if len(archive) < 16:
        raise Unmeasurable("an asar shorter than its own header block")
    tag, pickle_size, _inner, json_len = struct.unpack("<4I", archive[:16])
    if tag != 4:
        raise Unmeasurable(f"asar tag is {tag}, not the pickle type id 4")
    try:
        header = json.loads(archive[16 : 16 + json_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Unmeasurable(f"asar header JSON did not parse ({exc})")
    base = 8 + pickle_size
    base += (-base) % 4
    return header, base


def read_asar(path: Path, name: str) -> str:
    """The app's own version, out of the packaged `package.json`.

    Selected by the package's `name`, not by position: the archive holds dependencies'
    `package.json` files whose versions are theirs.
    """
    archive = path.read_bytes()
    header, base = asar_header(archive)

    def walk(node):
        for entry, meta in (node.get("files") or {}).items():
            if entry == "package.json" and "offset" in meta:
                off, size = int(meta["offset"]), int(meta["size"])
                try:
                    pkg = json.loads(archive[base + off : base + off + size].decode("utf-8"))
                except Exception:  # noqa: BLE001 - one unreadable entry is not the answer
                    continue
                if pkg.get("name") == name:
                    return str(pkg.get("version") or "")
            if "files" in meta:
                found = walk(meta)
                if found:
                    return found
        return ""

    version = walk(header)
    if not version:
        raise Unmeasurable(
            f"{path.name} carries no package.json whose name is {name!r} -- a dependency's "
            f"version is not this product's version, so nothing here answers the question"
        )
    if parse_version(version) is None:
        raise Unmeasurable(f"{path.name} declares {name} version {version!r}, not a version")
    return parse_version(version)


def read_tar_gz(path: Path) -> list[tuple[str, str]]:
    """The version declarations inside a Linux runtime archive (stdlib only)."""
    out: list[tuple[str, str]] = []
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                base = member.name.rsplit("/", 1)[-1]
                if base in _VERSION_FILE_NAMES:
                    fh = tf.extractfile(member)
                    if fh is None:
                        continue
                    text = fh.read().decode("utf-8", errors="replace")
                    ver = parse_version(text)
                    if ver:
                        out.append((member.name, ver))
    except (tarfile.TarError, OSError) as exc:
        raise Unmeasurable(f"{path} is not a readable tar archive ({exc})")
    if not out:
        raise Unmeasurable(f"{path} holds no version.txt -- a runtime archive without one")
    return out


#: The walk's hard ceiling. A product tree is a few thousand entries (measured: the
#: expanded v0.3.13 payload holds 4979 paths, and rglob over it costs 64 ms), so a tree
#: this big stops being a product and starts being a filesystem. Failing loudly is the
#: family's answer -- a walk that quietly finds nothing reads as "no declaration here",
#: which is a verdict about the artifact rather than about the tool's budget.
MAX_WALK_ENTRIES = 200_000


def _bundles_and_files(subject: Path):
    """Every `.app` bundle, `*.asar` and version file under a directory subject.

    Measured on the expanded v0.3.13 payload, which is what a `.pkg` becomes after
    `pkgutil --expand` and one `cpio`:

        emrg-gui/EMRG.app/Contents/Info.plist                    short=0.3.13 build=0.3.13
        emrg-gui/EMRG.app/Contents/Resources/app.asar            (emrg-gui 0.3.13)
        emrg-gui/EMRG.app/Contents/Frameworks/EMRG Helper*.app/…  build=0.3.13, no short
        emrg-gui/卸载 EMRG.app/Contents/Info.plist                 declares neither
        version.txt                                               0.3.13

    Two shapes that walk decides about. A bundle's declaration is its **own**
    `Contents/Info.plist` -- the helpers inside `Frameworks` are bundles too and electron-
    builder gives them the app's build version, so reading them is a real check rather
    than noise. And the uninstaller bundle declares no version at all: it is reported as
    unreadable-by-design, and a directory whose *other* declarations were read still
    counts as measured. That is why the caller ORs the per-file answers instead of
    trusting "found some files" as "read some files".
    """
    bundles, asars, version_files, seen = [], [], [], 0
    for path in sorted(subject.rglob("*")):
        seen += 1
        if seen > MAX_WALK_ENTRIES:
            raise Unmeasurable(
                f"walk stopped at {MAX_WALK_ENTRIES} entries under {subject} -- this looks "
                f"like a filesystem, not a product tree, and nothing was concluded about it"
            )
        if path.is_dir() and path.name.endswith(".app"):
            plist = path / "Contents" / "Info.plist"
            if plist.is_file():
                bundles.append(plist)
        elif path.suffix == ".asar":
            asars.append(path)
        elif path.name in _VERSION_FILE_NAMES:
            version_files.append(path)
    return bundles + asars + version_files


def find_declarations(subject: Path) -> list[Path]:
    """Every readable declaration under a directory subject.

    A subject that *is* a bundle is read through its own `Contents` rather than walked:
    pointing the tool at `EMRG.app` is the common case, and its helpers are inside it.
    """
    if subject.name.endswith(".app"):
        out = []
        plist = subject / "Contents" / "Info.plist"
        if plist.is_file():
            out.append(plist)
        out.extend(sorted(subject.glob("Contents/Resources/*.asar")))
        return out
    return _bundles_and_files(subject)


def _needs_app_name(subject: Path) -> bool:
    """Whether reading this subject can reach an asar, and so needs the app's own name.

    A single `.asar`, or a directory that holds one. Everything else -- a `version.txt`, a
    tarball, a plist -- answers without it, which is what lets the release job read an
    artifact from a directory that has no GUI manifest in it.
    """
    if subject.suffix == ".asar":
        return True
    if subject.is_dir():
        return bool(next(subject.rglob("*.asar"), None))
    return False


def check_subject(subject: Path, expected: str, name: str | None) -> tuple[bool, bool]:
    """Measure one subject. Returns (fault, measured) -- measured False is rc 2 territory.

    A subject that holds several declarations must name the claimed version in **all** of
    them: an `.app` whose `Info.plist` says 0.3.13 while its packaged `package.json` still
    says 0.3.12 is a GUI dist that was not rebuilt, which is the failure the release rant
    asks about by name.

    Returns `(fault, unmeasurable)`. A directory is unmeasurable when **any** declaration
    under it could not be read, even if its neighbours read fine -- a run that got an
    answer from `version.txt` has not read the bundle beside it, and reporting OK would
    let a corrupt asar hide behind a good text file (measured: with the sibling rule
    written the other way, a payload whose asar header was overwritten answered `0`). A
    declaration that was read and holds no claim is the other case, and counts as neither.
    """
    if not subject.exists():
        print(f"{subject}: not measurable -- no such path")
        return False, True

    if subject.is_dir():
        try:
            declarations = find_declarations(subject)
        except Unmeasurable as exc:
            print(f"{subject}: not measurable -- {exc}")
            return False, True
        if not declarations:
            print(
                f"{subject}: not measurable -- no .app bundle, .asar or version.txt under it "
                f"(a product tree puts its version in one of those; an empty directory answers "
                f"nothing about a release)"
            )
            return False, True
        print(f"{subject}: {len(declarations)} declaration file(s) found")
        fault = read_any = unreadable = False
        for decl in declarations:
            f, value, bad = check_file(decl, expected, name, indent="  ")
            fault = fault or f
            read_any = read_any or value
            unreadable = unreadable or bad
        if unreadable:
            print("  at least one declaration under this subject could not be read -- see "
                  "the lines above; a sibling that did read is not an answer about this one")
        elif not read_any:
            print(f"  nothing under {subject} declares a version at all")
            return fault, True
        return fault, unreadable

    if subject.suffix.lower() in _EXTRACT_HELP:
        print(
            f"{subject}: not measurable -- this tool does not open a {subject.suffix}. "
            f"Extract it first: {_EXTRACT_HELP[subject.suffix.lower()]}"
        )
        return False, True
    fault, value, bad = check_file(subject, expected, name)
    return fault, (bad or not value)


def check_file(path: Path, expected: str, name: str | None, indent="") -> tuple[bool, bool, bool]:
    """Read one declaration file and compare it. Returns (fault, read_a_value, unreadable).

    The third answer is why this returns three values rather than two: an unreadable file
    makes the run unmeasurable, while a file that was read and declares nothing is a fact
    worth printing and moving on from.
    """
    try:
        if path.suffix == ".plist":
            readings = read_info_plist(path)
        elif path.suffix == ".asar":
            if name is None:
                raise Unmeasurable("no app package name to select the asar's own manifest by")
            readings = [("package.json", read_asar(path, name))]
        elif path.suffix in (".gz", ".tgz"):
            readings = read_tar_gz(path)
        elif path.name in _VERSION_FILE_NAMES:
            readings = [(path.name, read_version_txt(path))]
        else:
            print(f"{indent}{path}: not measurable -- not a declaration this tool reads")
            return False, False, True
    except NoDeclaration as exc:
        print(f"{indent}{path.name}: holds no version claim ({exc}) -- noted, not a fault")
        return False, False, False
    except Unmeasurable as exc:
        print(f"{indent}{path}: not measurable -- {exc}")
        return False, False, True
    except OSError as exc:
        print(f"{indent}{path}: not measurable -- {exc}")
        return False, False, True

    fault = False
    for label, value in readings:
        ok = value == expected
        print(f"{indent}{label}: {value} -> {'matches' if ok else 'DOES NOT MATCH'} {expected}")
        fault = fault or not ok
    return fault, True, False


def claimed_version(raw: str, from_tag: bool) -> str:
    """The version the release claims, in the form the products declare it.

    With `--from-tag` the argument is a tag (`v0.3.13`), and the stripping is
    `target.lstrip("v")` -- the upgrade chain's own reconciliation, so this guard and the
    code it protects cannot disagree about what a tag means.
    """
    ver = parse_version(raw)
    if ver is None:
        hint = " (with --from-tag the argument is a tag name like v0.3.13)" if not from_tag else ""
        raise SystemExit(f"{raw!r} is not a version{hint}")
    return ver


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 every subject read names the claimed version; 1 a subject names a\n"
            "different one (both values printed); 2 nothing could be read -- never a pass.\n"
            "A container this tool does not open (.pkg, .zip, .exe, .AppImage) answers 2 with\n"
            "the command that extracts it, so the host's reading and CI's are the same one."
        ),
    )
    ap.add_argument("claimed", help="the version the release says it is, e.g. 0.3.13")
    ap.add_argument("subjects", nargs="+", help="an artifact, a bundle directory, or a declaration file")
    ap.add_argument(
        "--from-tag",
        action="store_true",
        help="the first argument is a tag name (v0.3.13); strip the v the way UpgradeManager.tick does",
    )
    args = ap.parse_args(argv)

    try:
        expected = claimed_version(args.claimed, args.from_tag)
    except SystemExit as exc:
        # A claim that is not a version is "nothing was measured", not a fault: rc 1 would
        # report "an artifact disagrees with the release" about an argument that never
        # reached an artifact. SystemExit from argparse (a missing operand) is left alone.
        print(f"not measurable: {exc}")
        return 2

    lazy_name: str | None = None
    any_fault = any_unmeasured = False
    for raw in args.subjects:
        if lazy_name is None and _needs_app_name(Path(raw)):
            try:
                lazy_name = app_package_name()
            except Unmeasurable as exc:
                print(f"{raw}: not measurable -- {exc}")
                any_unmeasured = True
                continue
        fault, unmeasurable = check_subject(Path(raw), expected, lazy_name)
        any_fault = any_fault or fault
        any_unmeasured = any_unmeasured or unmeasurable

    print()
    if any_fault:
        print(
            f"FAULT: an artifact declares a version other than {expected} -- the release name and "
            f"the product disagree, which is the pair UpgradeManager.tick compares every tick. "
            f"Rebuild the artifact from the tree that declares {expected}."
        )
        return 1
    if any_unmeasured:
        # The remedy is printed per cause, not once for every unmeasured subject: a path
        # that does not exist is not a container that needs extracting, and telling a reader
        # to `pkgutil --expand` a missing file sends them down a road that cannot work
        # (this repository's own finding about a gate that printed one remedy for every
        # cause). The per-subject lines above already name each cause; this closes the run.
        print(
            "not measurable: at least one subject yielded no version to read -- this is not "
            "a pass. Each line above names its own cause: a container to extract, a path to "
            "fix, or a product that declares its version nowhere."
        )
        return 2
    print(f"OK: every version read names {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_entry())
