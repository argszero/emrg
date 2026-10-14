#!/usr/bin/env python3
"""Does a shipped icon still carry its transparency? -- measured, on the artifact.

Rant 2026-10-14T09:18:05 (the v0.3.11 release) asks for the icon to be verified "from the
published artifact itself, not by reading the source or by trusting a green CI", and that
is the one acceptance item a reader is most likely to skip: the ring is invisible in every
gate this repo runs. This tool is that step, made repeatable.

What it reads -- anything that holds an icon, and nothing that does not:

  * a `.png`  -- the four corner blocks' alpha;
  * a `.icns` -- every PNG-typed representation inside the container;
  * a directory (an `.app` bundle, an expanded `.pkg`, a `dist/` tree) -- walked for an
    `.icns`, preferring the one `Contents/Info.plist`'s `CFBundleIconFile` names, since
    that is the file the platform renders;
  * anything else (a `.pkg`, a `.tar.gz`) -- **not measured**, rc 2, with the command that
    extracts it. A container this tool cannot open is never a pass: the Linux `tar.gz`
    really does carry no icon at all, so "no icon found" must not read as "fine".

Exit contract, the one this repository's guards share: 0 every icon is clear; 1 a fault --
an icon whose corner block is not fully transparent, named with the value read; 2 the
subject could not be measured at all. A fault outranks an unmeasured subject, because it
is a fact about a subject that *was* read.

Why the corners: the design insets its rounded rect (56px on a 1024 canvas), so the canvas
corners are outside the art on every representation. An opaque corner block therefore means
the render baked a background in -- the white ring of rant 2026-10-13T17:45:42. A whole-image
ratio would not answer the question, because an opaque ring is a small share of the pixels.

Usage:
    python3 scripts/check-icon-transparency.py <path> [<path> ...]
"""
from __future__ import annotations

import argparse
import plistlib
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging"))

try:
    import pngutil
except ImportError as exc:  # pragma: no cover - a checkout missing its own helper
    print(f"not measurable: packaging/pngutil.py is not importable ({exc})")
    raise SystemExit(2) from exc

# icns chunk type -> the pixel size it must hold. Only the PNG-typed ones are decoded;
# the old-style entries (ic04/ic05/it32/...) are raw ARGB and are reported as unmeasured
# rather than skipped silently.
ICNS_PNG_TYPES = {
    b"ic11": 32, b"ic12": 64, b"ic07": 128, b"ic13": 256, b"ic08": 256,
    b"ic14": 512, b"ic09": 512, b"ic10": 1024,
}
# The design insets its rounded rect by 56px on a 1024 canvas (5.5% per side), so a corner
# block has to stay well inside that band: a fixed 8px block covers the art itself at 32px
# and reads a perfectly good icon as opaque. 2% of the width, capped, is inside the band at
# every size the icns carries (32px -> 1px, 1024px -> 16px).
CORNER_BLOCK_FRACTION = 0.02
CORNER_BLOCK_MAX = 16


def corner_block_size(width):
    return max(1, min(CORNER_BLOCK_MAX, round(width * CORNER_BLOCK_FRACTION)))


def corner_max_alpha(width, height, bpp, pixels):
    """The highest alpha in each of the four corner blocks -- (name, max_alpha) pairs.

    `pixels` is pngutil.read_png's row-major buffer without filter bytes; bpp 3 means the
    PNG has no alpha channel at all, so every pixel is opaque (alpha 255) and the whole
    image is a ring candidate.
    """
    if bpp not in (3, 4):
        raise ValueError(f"bpp {bpp} is not RGB or RGBA")
    if bpp == 3:
        return [("no alpha channel (RGB)", 255)]
    block = corner_block_size(width)
    corners = {
        "top-left": (0, 0),
        "top-right": (max(0, width - block), 0),
        "bottom-left": (0, max(0, height - block)),
        "bottom-right": (max(0, width - block), max(0, height - block)),
    }
    out = []
    for name, (x0, y0) in corners.items():
        best = 0
        for y in range(y0, min(y0 + block, height)):
            row = pixels[y * width * bpp:(y + 1) * width * bpp]
            for x in range(x0, min(x0 + block, width)):
                best = max(best, row[x * bpp + 3])
        out.append((name, best))
    return out


def check_png_bytes(payload, label, indent="    "):
    """Measure one PNG payload. Returns (fault, measured) -- measured is False if it would
    not decode, which is rc 2 territory, never a pass."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(payload)
        path = fh.name
    try:
        width, height, bpp, pixels = pngutil.read_png(path)
    except Exception as exc:  # noqa: BLE001 - any decode failure is "not measured"
        print(f"{indent}{label}: not measured ({exc})")
        return False, False
    finally:
        Path(path).unlink(missing_ok=True)

    readings = corner_max_alpha(width, height, bpp, pixels)
    worst = max(a for _n, a in readings)
    pretty = ", ".join(f"{name}={alpha}" for name, alpha in readings)
    kind = {3: "RGB (no alpha channel)", 4: "RGBA"}[bpp]
    verdict = "clear" if worst == 0 else "OPAQUE"
    print(f"{indent}{label}: {width}x{height} {kind} -- {pretty} -> {verdict}")
    return worst != 0, True


def icns_chunks(raw):
    """(type, payload) for every chunk in an icns container."""
    if raw[:4] != b"icns":
        raise ValueError(f"not an icns container (magic {raw[:4]!r})")
    pos = 8
    while pos + 8 <= len(raw):
        ctype = raw[pos:pos + 4]
        length = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        if length < 8:
            raise ValueError(f"chunk {ctype!r} declares an impossible length {length}")
        yield ctype, raw[pos + 8:pos + length]
        pos += length


def measure_icns(path: Path, indent="    "):
    raw = path.read_bytes()
    fault = measured = False
    raw_only = []
    for ctype, payload in icns_chunks(raw):
        if ctype in ICNS_PNG_TYPES and payload[:8] == b"\x89PNG\r\n\x1a\n":
            size = ICNS_PNG_TYPES[ctype]
            f, m = check_png_bytes(payload, f"{ctype.decode('ascii', 'replace')} ({size}px)",
                                   indent=indent)
            fault, measured = fault or f, measured or m
        elif ctype[:2] == b"ic" and ctype not in ICNS_PNG_TYPES:
            raw_only.append(ctype.decode("ascii", "replace"))
    if raw_only:
        print(f"{indent}not decoded (raw ARGB, not PNG): {', '.join(sorted(set(raw_only)))}")
    if not measured:
        print(f"{indent}not measured: no PNG-typed representation in {path.name}")
    return fault, measured


def find_icons(subject: Path):
    """The icon files inside a directory subject, the plist-named one first."""
    preferred = []
    for plist in sorted(subject.rglob("Contents/Info.plist")):
        try:
            named = plistlib.loads(plist.read_bytes()).get("CFBundleIconFile")
        except Exception:  # noqa: BLE001 - a plist that will not parse names nothing
            continue
        if not named:
            continue
        candidate = plist.parent / "Resources" / named
        if candidate.is_file():
            preferred.append(candidate)
    found = sorted(set(subject.rglob("*.icns")))
    ordered = preferred + [p for p in found if p not in preferred]
    if ordered:
        return ordered, False
    # A bare PNG is a subject only when it is named like an icon: a source tree holds
    # hundreds of unrelated PNGs, and reporting one of those would be a reading about
    # something nobody asked about.
    named_png = sorted(p for p in subject.rglob("*.png") if "icon" in p.name.lower())
    return named_png, True


def check_subject(subject: Path):
    if not subject.exists():
        print(f"{subject}: not measurable -- no such path")
        return False, False
    if subject.is_dir():
        icons, png_fallback = find_icons(subject)
        if not icons:
            print(f"{subject}: not measurable -- no .icns and no icon*.png under it "
                  f"(a source tree's PNGs are not an icon; an artifact with no icon is "
                  f"not a pass either)")
            return False, False
        print(f"{subject}: {len(icons)} icon file(s) found"
              f"{' (fell back to PNGs named *icon*)' if png_fallback else ''}")
        fault = measured = False
        for icon in icons:
            print(f"  {icon}")
            if icon.suffix == ".icns":
                f, m = measure_icns(icon, indent="    ")
            else:
                f, m = check_png_bytes(icon.read_bytes(), icon.name, indent="    ")
            fault, measured = fault or f, measured or m
        return fault, measured
    if subject.suffix == ".icns":
        print(f"{subject}")
        return measure_icns(subject)
    if subject.suffix == ".png":
        print(f"{subject}")
        return check_png_bytes(subject.read_bytes(), subject.name)
    print(f"{subject}: not measurable -- this is not an icon this tool reads. Give it a "
          f".icns, a .png, or a directory (an .app bundle, an expanded .pkg, a dist/ tree). "
          f"A .pkg expands on macOS with `pkgutil --expand <pkg> <dir>`, and its app comes "
          f"out of `<dir>/<name>.pkg/Payload` (`gunzip -dc Payload | cpio -i -d "
          f"./emrg-gui/EMRG.app`) -- remember the app itself, not only the artifact: CI "
          f"installs librsvg while a host may not, so the two renders can differ.")
    return False, False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("subjects", nargs="+", help="a .icns, a .png, or a directory holding an icon")
    args = ap.parse_args(argv)

    any_fault = any_measured = any_unmeasured = False
    for raw in args.subjects:
        print(f"=== {raw}")
        fault, measured = check_subject(Path(raw))
        any_fault = any_fault or fault
        any_measured = any_measured or measured
        any_unmeasured = any_unmeasured or not measured

    print()
    if any_fault:
        print("FAULT: an icon in this subject is opaque at the corners -- the render baked "
              "a background in (the white ring), so this artifact is not the fixed one.")
        return 1
    if any_unmeasured:
        print("not measurable: at least one subject yielded no icon to read -- this is not "
              "a pass. Point the tool at the icon itself (see the message above).")
        return 2
    print(f"OK: every icon read ({len(args.subjects)} subject(s)) is fully transparent at "
          f"all four corner blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
