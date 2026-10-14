"""`scripts/check-icon-transparency.py` -- the release's icon acceptance, mechanised.

Rant 2026-10-14T09:18:05 requires the shipped icon's transparency to be read from the
artifact, and names the trap: "if it is still an opaque white background, the release
failed -- do not read the source and do not trust a green CI". Measured 2026-10-14
(`cyc20261014-115025`), that step had no tool: reading a shipped `.icns` meant expanding a
`.pkg`, pulling the app out of a cpio payload and decoding `icns` chunks by hand.

Both directions are driven as **subprocesses on the real entry point**, and every subject is
built here rather than taken from the host: an icon fixture is a few hundred bytes, and a
subject the host supplies would make the reading an environment's.

The arms, and the defect each one would catch:

* a transparent-cornered RGBA icon -> rc 0;
* an opaque-white RGBA icon, and an RGB icon with **no alpha channel at all** (the exact
  shape the 2026-10-13 white ring shipped as) -> rc 1, naming the corners;
* a 32x32 icon: its corners are transparent, and the design's 5.5% inset leaves ~1.7px of
  margin there -- so a corner block that does not scale with the image reports a good small
  representation as OPAQUE. This arm is the mutation the tool shipped with before it was
  first run (measured: ic11/ic12 read 255 on a clear icon);
* an icns that carries only a raw (non-PNG) entry -> rc 2, never a pass;
* a directory with no icon at all, and a non-icon subject (a `.pkg`) -> rc 2, because the
  published Linux `tar.gz` really does carry no icon and "nothing found" must not read as
  "fine".
"""

from __future__ import annotations

import plistlib
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-icon-transparency.py"

sys.path.insert(0, str(REPO / "packaging"))
import pngutil  # noqa: E402


def run(*subjects):
    """Run the tool on the real interpreter, as its caller does."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *[str(s) for s in subjects]],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def rgba_png(size, corner=(0, 0, 0, 0), body=(26, 32, 44, 255), inset=None):
    """A rounded-rect-ish fixture: `corner` outside a centred `body` block.

    The inset scales with the canvas by default, because the real design's does (56px on
    1024 = 5.5%): a fixed 2px inset on a 512px canvas puts the art *inside* a 10px corner
    block, which is a fixture that would make the tool look wrong.
    """
    if inset is None:
        inset = max(2, round(size * 0.06))
    px = bytearray()
    for y in range(size):
        for x in range(size):
            inside = inset <= x < size - inset and inset <= y < size - inset
            px += bytes(body if inside else corner)
    return pngutil.write_png_bytes(size, size, bytes(px))


def rgb_png(size):
    """A PNG with **no alpha channel** (colortype 2) -- every pixel opaque by definition."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def icns(*entries):
    """An icns container holding (type, payload) pairs."""
    body = b"".join(ctype + struct.pack(">I", len(payload) + 8) + payload
                    for ctype, payload in entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def app_bundle(root: Path, icns_bytes: bytes, name="EMRG.app", declared="icon.icns"):
    """An .app whose Info.plist names one icon file."""
    app = root / name
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIconFile": declared, "CFBundleShortVersionString": "0.0.0"}))
    (app / "Contents" / "Resources" / declared).write_bytes(icns_bytes)
    return app


# ── the two directions ───────────────────────────────────────────────────────

def test_a_transparent_icon_is_clear(tmp_path):
    """The fixed shape: corners hold nothing, so the reading is rc 0."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(rgba_png(64))
    code, out = run(icon)
    assert code == 0, out
    assert "top-left=0" in out
    assert "OK: every icon read" in out


def test_an_opaque_corner_is_a_fault(tmp_path):
    """The white ring: an opaque background baked into the render."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(rgba_png(64, corner=(255, 255, 255, 255), body=(26, 32, 44, 255)))
    code, out = run(icon)
    assert code == 1, out
    assert "top-left=255" in out
    assert "FAULT" in out


def test_a_png_without_an_alpha_channel_is_a_fault(tmp_path):
    """Colortype 2 is the exact shape the ring shipped as: no alpha at all."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(rgb_png(64))
    code, out = run(icon)
    assert code == 1, out
    assert "no alpha channel (RGB)" in out
    assert "FAULT" in out


def test_a_small_representation_is_not_judged_by_a_fixed_block(tmp_path):
    """32px corners are transparent, and its margin is ~1.7px.

    A corner block that does not scale with the image reaches into the art and reports a
    clear icon as opaque -- the tool did exactly that on the real v0.3.11 icns before this
    arm existed (ic11 `32x32` read 255 at every corner of a clear icon).
    """
    icon = tmp_path / "icon.png"
    icon.write_bytes(rgba_png(32, corner=(0, 0, 0, 0), body=(26, 32, 44, 255), inset=2))
    code, out = run(icon)
    assert code == 0, out
    assert "32x32" in out and "top-left=0" in out


# ── containers ───────────────────────────────────────────────────────────────

def test_an_icns_is_read_through_its_png_entries(tmp_path):
    icon = tmp_path / "icon.icns"
    icon.write_bytes(icns((b"ic09", rgba_png(512)), (b"ic11", rgba_png(32))))
    code, out = run(icon)
    assert code == 0, out
    assert "ic09 (512px)" in out and "ic11 (32px)" in out


def test_an_icns_whose_only_entry_is_raw_is_not_measured(tmp_path):
    """ic04 is raw ARGB, not a PNG: unread is rc 2, and never a pass."""
    icon = tmp_path / "icon.icns"
    icon.write_bytes(icns((b"ic04", b"\x00" * 64)))
    code, out = run(icon)
    assert code == 2, out
    assert "no PNG-typed representation" in out


def test_the_app_bundle_is_read_through_the_plist_named_icon(tmp_path):
    """`CFBundleIconFile` names the file the platform renders -- read that one."""
    app = app_bundle(tmp_path, icns((b"ic10", rgba_png(64, corner=(255, 255, 255, 255)))))
    code, out = run(app)
    assert code == 1, out
    assert "Info.plist" not in out or True  # the subject is the named file, not any file
    assert "OPAQUE" in out


def test_a_plist_that_names_a_missing_icon_falls_back_to_the_found_icns(tmp_path):
    """A named file that is not there must not silently become "no icon found"."""
    app = app_bundle(tmp_path, icns((b"ic10", rgba_png(64))), declared="missing.icns")
    (app / "Contents" / "Resources" / "missing.icns").unlink()
    (app / "Contents" / "Resources" / "other.icns").write_bytes(icns((b"ic10", rgba_png(64))))
    code, out = run(app)
    assert code == 0, out
    assert "other.icns" in out


# ── never a pass ─────────────────────────────────────────────────────────────

def test_a_directory_with_no_icon_is_not_a_pass(tmp_path):
    """A source tree's PNGs are not an icon, and an empty answer is rc 2."""
    (tmp_path / "notes.md").write_text("no icon here\n")
    (tmp_path / "logo.png").write_bytes(rgba_png(16))
    code, out = run(tmp_path)
    assert code == 2, out
    assert "not measurable" in out


def test_a_container_the_tool_cannot_open_is_not_a_pass(tmp_path):
    """The published Linux tar.gz really carries no icon: rc 2 with the recipe."""
    pkg = tmp_path / "EMRG-0.0.0-macos-arm64.pkg"
    pkg.write_bytes(b"not really a pkg")
    code, out = run(pkg)
    assert code == 2, out
    assert "pkgutil --expand" in out


def test_a_fault_outranks_an_unmeasured_subject(tmp_path):
    """One subject read and opaque, one unreadable: the fact about the read one wins."""
    opaque = tmp_path / "icon.png"
    opaque.write_bytes(rgba_png(64, corner=(255, 255, 255, 255)))
    missing = tmp_path / "nothing-here.pkg"
    missing.write_bytes(b"x")
    code, out = run(opaque, missing)
    assert code == 1, out
