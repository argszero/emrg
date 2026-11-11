#!/usr/bin/env python3
"""Can a macOS package put its bundles somewhere other than the location it declares?

A component package states `install-location`, and it may also carry a
`<relocate>` element naming bundle identifiers. `<relocate>` is what PackageKit
consults: it asks LaunchServices for each named bundle's current path and
installs *there* instead, so `install-location` stops being the destination.
`relocatable="false"` on the same `pkg-info` element does not prevent it.

Measured 2026-11-11 (cycle `cyc20261111-164717`) on this macOS host, with a
payload carrying two `.app` bundles:

* `pkgbuild --root <payload> --install-location '/.emrg/install'` printed
  `Inferring bundle components from contents of <payload>` and emitted
  `<relocate><bundle id="..."/></relocate>` naming both bundles -- the block is
  the tool's own inference, and `pkgbuild` has no flag to suppress it (`--help`
  is unrecognized; `man pkgbuild` lists no such option).
* `pkgbuild --analyze --root <payload>` writes the component property list that
  inference came from, and each entry carries `BundleIsRelocatable` = true.
  Setting it to false and passing that list as `--component-plist` produced
  `<relocate/>` -- the element stays, **empty**.

That last line is the whole reason this tool measures bundle identifiers rather
than the element: a guard phrased as "the package must not carry `<relocate>`"
would fail the one output that is correct, and could never pass. What makes a
package relocate is a bundle identifier *inside* the element.

The question this answers, and the one it does not. With no identifier inside
`<relocate>`, the package hands PackageKit nothing to look up, so the metadata
itself asks for no relocation. Whether the Installer behaves differently in the
field is read from `/var/log/install.log` on the next real install; what this
tool establishes is that the artifact no longer asks.

Usage, host side (the same question CI asks of the artifact it just built):

    uv run --no-sync python3 scripts/check-pkg-relocation.py <subject> [<subject> ...]
    uv run --no-sync python3 scripts/check-pkg-relocation.py --disarm-component-plist <plist>

A subject is a `PackageInfo` file, a directory to search for them (an expanded
package, or a package build root), or a `.pkg` file -- which is expanded with
`pkgutil` first, so it needs macOS. `--disarm-component-plist` is the write mode
the packaging step uses on the property list `pkgbuild --analyze` wrote: it sets
`BundleIsRelocatable` to false on every entry and reports what it changed.

Exit codes, the family's contract:

    0  no bundle identifier sits inside a <relocate>, so the package asks for no relocation
    1  a fault: at least one component would let PackageKit relocate a bundle -- the
       identifiers are printed, because that is what tells the package where they go
    2  nothing could be read: no PackageInfo under the subject, an unreadable or
       unparsable one, or a `.pkg` that cannot be expanded here. A subject that
       could not be read is not a pass.
"""

from __future__ import annotations

import argparse
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

EXIT_CLEAN = 0
EXIT_FAULT = 1
EXIT_UNMEASURABLE = 2


class Unmeasurable(Exception):
    """Nothing was measured about the subject, which is never a pass."""


#: A `pkg-info` element's own attributes, reported as context rather than judged.
_INSTALL_LOCATION = re.compile(r'install-location="([^"]*)"')
_RELOCATABLE_ATTR = re.compile(r'\brelocatable="([^"]*)"')


def _read_text(info: Path) -> str:
    try:
        return info.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Unmeasurable(f"{info} could not be read: {exc}") from exc


def _readings(subject: Path) -> list[tuple[str, str]]:
    """Every (label, PackageInfo text) pair this subject holds.

    The text is returned rather than a path because a `.pkg` is read from a
    scratch directory that is removed before the caller sees the result; a path
    handed back from inside that window is a path that no longer exists.
    """
    if subject.is_file() and subject.name == "PackageInfo":
        return [(str(subject), _read_text(subject))]
    if subject.is_file():
        return _from_package_file(subject)
    if subject.is_dir():
        found = sorted(p for p in subject.rglob("PackageInfo") if p.is_file())
        if not found:
            raise Unmeasurable(
                f"{subject} holds no PackageInfo - an expanded package or a package "
                "build root carries one per component, and this one carries none"
            )
        return [(str(path), _read_text(path)) for path in found]
    raise Unmeasurable(f"{subject} does not exist")


def _from_package_file(subject: Path) -> list[tuple[str, str]]:
    """Expand a flat `.pkg` and read its components while the copy still exists."""
    if subject.suffix.lower() != ".pkg":
        raise Unmeasurable(
            f"{subject} is a file this tool does not open - give it a PackageInfo, a "
            "directory, or a .pkg"
        )
    pkgutil = shutil.which("pkgutil")
    if pkgutil is None:
        raise Unmeasurable(
            f"expanding {subject} needs pkgutil, which this host does not have - the "
            "package itself was not read"
        )
    scratch = Path(tempfile.mkdtemp(prefix="emrg-pkg-relocation-"))
    try:
        expanded = scratch / "expanded"
        done = subprocess.run(
            [pkgutil, "--expand", str(subject), str(expanded)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if done.returncode != 0:
            raise Unmeasurable(
                f"pkgutil --expand refused {subject} (exit {done.returncode}): "
                f"{(done.stderr or done.stdout).strip()}"
            )
        found = sorted(p for p in expanded.rglob("PackageInfo") if p.is_file())
        if not found:
            raise Unmeasurable(f"{subject} expanded but carries no PackageInfo")
        return [
            (f"{subject}:{path.relative_to(expanded).as_posix()}", _read_text(path))
            for path in found
        ]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def relocated_bundles(text: str) -> list[str]:
    """The bundle identifiers a PackageInfo puts inside a `<relocate>`."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise Unmeasurable(f"the PackageInfo is not parseable XML: {exc}") from exc
    ids: list[str] = []
    for relocate in root.iter("relocate"):
        for bundle in relocate.iter("bundle"):
            identifier = bundle.get("id")
            if identifier:
                ids.append(identifier)
    return ids


def _context(text: str) -> str:
    location = _INSTALL_LOCATION.search(text)
    relocatable = _RELOCATABLE_ATTR.search(text)
    return (
        f"install-location={location.group(1) if location else '<absent>'}"
        f" relocatable={relocatable.group(1) if relocatable else '<absent>'}"
    )


def report(subjects: list[Path]) -> int:
    """Read every subject, print one line per component, and return the verdict."""
    scanned = 0
    faults: list[tuple[str, list[str]]] = []
    unreadable: list[str] = []
    for subject in subjects:
        try:
            readings = _readings(subject)
        except Unmeasurable as exc:
            print(f"unmeasurable: {exc}")
            unreadable.append(str(subject))
            continue
        for label, text in readings:
            scanned += 1
            try:
                ids = relocated_bundles(text)
            except Unmeasurable as exc:
                print(f"{label}: {_context(text)} -> unmeasurable ({exc})")
                unreadable.append(label)
                continue
            verdict = "RELOCATES" if ids else "no relocation"
            named = ", ".join(ids) if ids else "none"
            print(f"{label}: {_context(text)} -> {verdict} (bundle ids in <relocate>: {named})")
            if ids:
                faults.append((label, ids))

    if unreadable:
        print(
            f"unmeasurable: {len(unreadable)} subject(s) could not be read, so nothing was "
            "established about them - a reading that could not be taken is not a pass"
        )
        return EXIT_UNMEASURABLE
    if not scanned:
        print("unmeasurable: no component was read at all")
        return EXIT_UNMEASURABLE
    if faults:
        total = sum(len(ids) for _, ids in faults)
        print(
            f"RELOCATES: {total} bundle id(s) across {len(faults)} component(s) - PackageKit "
            "installs a named bundle wherever LaunchServices says it is, overriding "
            "install-location. Mark every bundle non-relocatable in the component plist "
            "(`BundleIsRelocatable` = false) before pkgbuild, and read the artifact again."
        )
        return EXIT_FAULT
    print(
        f"clean: {scanned} component(s) read, and no bundle id sits inside a <relocate> - "
        "install-location is the destination the package asks for"
    )
    return EXIT_CLEAN


def disarm(plist_path: Path) -> int:
    """Set `BundleIsRelocatable` false on every entry of a pkgbuild component plist."""
    try:
        raw = plist_path.read_bytes()
    except OSError as exc:
        print(f"unmeasurable: {plist_path} could not be read: {exc}")
        return EXIT_UNMEASURABLE
    try:
        entries = plistlib.loads(raw)
    except Exception as exc:  # plistlib raises several unrelated types
        print(f"unmeasurable: {plist_path} is not a plist this tool reads: {exc}")
        return EXIT_UNMEASURABLE
    if not isinstance(entries, list) or not entries:
        print(
            f"unmeasurable: {plist_path} is not a pkgbuild component property list - that "
            "file holds a non-empty array, one dictionary per bundle"
        )
        return EXIT_UNMEASURABLE
    changed: list[str] = []
    already: list[str] = []
    unnamed: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            print(f"unmeasurable: {plist_path} holds an entry that is not a dictionary")
            return EXIT_UNMEASURABLE
        path = entry.get("RootRelativeBundlePath")
        if not isinstance(path, str) or not path:
            unnamed.append(repr(entry.get("RootRelativeBundlePath")))
            continue
        if entry.get("BundleIsRelocatable") is False:
            already.append(path)
        else:
            entry["BundleIsRelocatable"] = False
            changed.append(path)
    if unnamed:
        print(
            "unmeasurable: an entry has no RootRelativeBundlePath, so this tool cannot say "
            f"which bundle it disarmed: {', '.join(unnamed)}"
        )
        return EXIT_UNMEASURABLE
    try:
        plist_path.write_bytes(plistlib.dumps(entries))
    except OSError as exc:
        print(f"unmeasurable: {plist_path} could not be written: {exc}")
        return EXIT_UNMEASURABLE
    for path in changed:
        print(f"disarmed: {path} -> BundleIsRelocatable=false")
    for path in already:
        print(f"already disarmed: {path}")
    print(
        f"{len(entries)} bundle(s) in {plist_path.name}: {len(changed)} changed, "
        f"{len(already)} already non-relocatable"
    )
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ask whether a macOS component package lets PackageKit relocate a bundle, "
            "which overrides the install-location the package declares."
        )
    )
    parser.add_argument(
        "subjects",
        nargs="*",
        help="a PackageInfo file, a directory to search, or a .pkg to expand",
    )
    parser.add_argument(
        "--disarm-component-plist",
        metavar="PLIST",
        default=None,
        help=(
            "write mode: take the property list `pkgbuild --analyze` wrote and set "
            "BundleIsRelocatable=false on every entry, then pass it back as --component-plist"
        ),
    )
    args = parser.parse_args(argv)

    if args.disarm_component_plist:
        if args.subjects:
            parser.error("--disarm-component-plist takes one plist and no reading subject")
        return disarm(Path(args.disarm_component_plist))
    if not args.subjects:
        parser.error("give at least one subject to read, or --disarm-component-plist")
    return report([Path(subject) for subject in args.subjects])


if __name__ == "__main__":
    sys.exit(main())
